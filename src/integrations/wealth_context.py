"""Wealth context snapshot builder — PRD §7.6 (P0-F, owner-ratified 2026-07-23).

Composes the aggregates-only :class:`WealthContextSnapshot` from the two
federation sources at observation time:

- **portfolio tracker** (live REST API): today's investment total and its
  tax-treatment allocation (``LivePortfolio.total_market_value`` /
  ``by_tax_treatment``) — this supersedes wealthplan's tracker-auto-filled
  investment buckets, which go stale between plan saves.
- **wealthplan** (sibling checkout, borrowed Pydantic models — the same
  ``sys.path`` pattern as :mod:`integrations.wealthplan_capacity`): the
  manual balances (cash, illiquid, home equity), the typed balance
  ``as_of``, and the label-only cash-need band.

Scope discipline (PRD §7.6, amending the 2026-07-19 ruling): aggregates
ONLY. No item-level holdings, no transaction detail, no compensation
figures. Rows built from this model are observations, not facts — never
ratified, never quoted by the LLM as owner statements, and never a
substitute for a live read when the source APIs are up. Telegram redaction
excludes everything here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from integrations.wealthplan_capacity import read_cash_need_summary

# Same sibling-checkout default as integrations.wealthplan_capacity (kept
# local rather than importing its private constant).
DEFAULT_WEALTHPLAN_ROOT = Path(
    os.environ.get("WEALTHPLAN_ROOT")
    or Path.home() / ".gemini" / "antigravity" / "scratch" / "wealthplan"
)

# wealthplan buckets the tracker owns live (auto-filled into the plan at save
# time, stale afterwards). CASH / ILLIQUID / home_equity stay wealthplan-manual.
_WP_INVESTMENT_BUCKETS = ("taxable", "pretax", "roth", "hsa")


class WealthContextSnapshot(BaseModel):
    """One aggregates-only observation of the household balance sheet."""

    as_of: str  # effective observation date (max of source as-ofs), ISO
    tracker_as_of: str | None = None
    wealthplan_as_of: str | None = None
    currency: str = "USD"

    net_worth_total: float | None = None
    liquid_total: float | None = None  # excludes illiquid and home equity
    investable_total: float | None = None  # cash + live investment buckets
    home_equity: float | None = None

    # Aggregate allocation by account class. Investment buckets use the
    # tracker's vocabulary (taxable / tax_deferred / tax_free / unknown) when
    # the tracker responded, wealthplan's (taxable/pretax/roth/hsa) otherwise;
    # cash and illiquid are always wealthplan-manual. Account-class level,
    # never account or holding level.
    allocation_by_bucket: dict[str, float] = Field(default_factory=dict)

    # Label-only cash-need posture (integrations.wealthplan_capacity) — safe
    # to log/persist verbatim; carries no amounts.
    cash_need_band: str | None = None
    cash_need_reasons: tuple[str, ...] = ()

    # Per-field provenance: field name -> "tracker" | "wealthplan".
    sources: dict[str, str] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def input_sha(self) -> str:
        """Deterministic idempotency hash over the observed values (not the
        warnings, which can vary run-to-run without the observation moving)."""
        payload = self.model_dump(mode="json")
        payload.pop("warnings", None)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def validate_plausible(self) -> list[str]:
        """PRD §7.6.3 checks beyond schema shape; empty list = fit to append."""
        reasons: list[str] = []
        if not self.as_of:
            reasons.append("as_of missing")
        core = (self.net_worth_total, self.liquid_total, self.investable_total)
        if all(v is None for v in core):
            reasons.append("all core totals null")
        for name in ("net_worth_total", "liquid_total", "investable_total", "home_equity"):
            v = getattr(self, name)
            if v is None:
                continue
            if v != v or v in (float("inf"), float("-inf")):
                reasons.append(f"{name} not finite")
            elif v < 0:
                reasons.append(f"{name} negative")
        if not self.sources:
            reasons.append("source provenance missing")
        return reasons


def _module_matches_root(wealthplan_root: Path) -> bool:
    """True when the in-process ``wealthplan`` package either came from
    ``wealthplan_root`` or is a file-less injected fake (test seam)."""
    mod = sys.modules.get("wealthplan")
    mod_file = getattr(mod, "__file__", None) if mod is not None else None
    if mod_file is None:
        return True
    try:
        return Path(mod_file).resolve().is_relative_to((wealthplan_root / "src").resolve())
    except OSError:
        return False


def load_wealthplan_starting(
    wealthplan_root: Path = DEFAULT_WEALTHPLAN_ROOT,
) -> tuple[dict[str, float], float, str] | None:
    """(balances_by_bucket_name, home_equity, as_of_iso) from the sibling
    wealthplan plan, or None on any failure — this federation boundary never
    raises. Amounts pass through wealthplan's own models transiently; only
    the bucket-level aggregates cross (PRD §7.6 permits exactly these)."""
    src = str(wealthplan_root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from wealthplan.persistence import (  # pyright: ignore[reportMissingImports]
            load_plan,  # pyright: ignore[reportUnknownVariableType]
        )
    except ImportError:
        return None
    if not _module_matches_root(wealthplan_root):
        # sys.modules caching can satisfy the import with a module loaded from
        # a DIFFERENT root (the real sibling, once anything imported it) — the
        # reader would then answer from the wrong root's plan. File-less
        # injected fakes (tests) are exempt. Found as an order-dependent
        # full-suite failure, 2026-07-24.
        return None
    try:
        plan = cast("tuple[object, object] | None", load_plan())
    except Exception:
        return None
    if plan is None:
        return None
    # Borrowed-model boundary: wealthplan's types are opaque here, so extract
    # with getattr + runtime checks and cast the one validated container.
    starting = getattr(plan[0], "starting", None)
    balances_raw = getattr(starting, "balances", None)
    home_equity = getattr(starting, "home_equity", None)
    as_of_raw = getattr(starting, "as_of", None)
    if not isinstance(balances_raw, dict) or not isinstance(home_equity, int | float):
        return None
    balances: dict[str, float] = {}
    for bucket, v in cast("dict[object, object]", balances_raw).items():
        if isinstance(v, int | float):
            balances[str(getattr(bucket, "value", bucket))] = float(v)
    as_of = as_of_raw.isoformat() if isinstance(as_of_raw, date) else str(as_of_raw or "")
    return balances, float(home_equity), as_of


def build_wealth_context_snapshot(
    *,
    tracker_total: float | None,
    tracker_by_tax_treatment: dict[str, float] | None,
    tracker_as_of: str | None,
    wealthplan: tuple[dict[str, float], float, str] | None,
    today: date | None = None,
    cash_need_root: Path = DEFAULT_WEALTHPLAN_ROOT,
) -> WealthContextSnapshot | None:
    """Compose the snapshot from whichever sources responded. The execution
    entry point fetches the sources (so this stays pure and unit-testable)
    and passes ``tracker_total=None`` / ``wealthplan=None`` for a source that
    is down. Returns ``None`` when NO source produced data — the caller logs
    a failed ingestion event and the last valid row stands."""
    if tracker_total is None and wealthplan is None:
        return None

    warnings: list[str] = []
    sources: dict[str, str] = {}
    allocation: dict[str, float] = {}

    wp_balances = dict(wealthplan[0]) if wealthplan else {}
    home_equity = wealthplan[1] if wealthplan else None
    wealthplan_as_of = wealthplan[2] if wealthplan else None

    cash = wp_balances.get("cash")
    illiquid = wp_balances.get("illiquid")

    if tracker_total is not None:
        investment_total = float(tracker_total)
        for bucket, value in (tracker_by_tax_treatment or {}).items():
            allocation[bucket] = round(float(value), 2)
        if not allocation:
            allocation["investment"] = round(investment_total, 2)
        sources["investable_total"] = "tracker"
        sources["allocation_by_bucket"] = "tracker"
        if wealthplan is None:
            warnings.append("wealthplan plan unavailable; investment-only snapshot")
    else:
        # Tracker down: fall back to wealthplan's (stale) investment buckets.
        investment_total = sum(wp_balances.get(b, 0.0) for b in _WP_INVESTMENT_BUCKETS)
        for b in _WP_INVESTMENT_BUCKETS:
            if b in wp_balances:
                allocation[b] = round(wp_balances[b], 2)
        sources["investable_total"] = "wealthplan"
        sources["allocation_by_bucket"] = "wealthplan"
        warnings.append("tracker unavailable; investment buckets are wealthplan-stale")

    if cash is not None:
        allocation["cash"] = round(cash, 2)
        sources["cash"] = "wealthplan"
    if illiquid is not None:
        allocation["illiquid"] = round(illiquid, 2)
        sources["illiquid"] = "wealthplan"
    if home_equity is not None:
        sources["home_equity"] = "wealthplan"

    liquid = investment_total + (cash or 0.0)
    net_worth = liquid + (illiquid or 0.0) + (home_equity or 0.0)
    if tracker_total is not None and wealthplan is not None:
        sources["net_worth_total"] = "tracker+wealthplan"
    elif tracker_total is not None:
        sources["net_worth_total"] = "tracker"
    else:
        sources["net_worth_total"] = "wealthplan"

    cash_need = read_cash_need_summary(wealthplan_root=cash_need_root, as_of=today)

    as_of_candidates = [s for s in (tracker_as_of, wealthplan_as_of) if s]
    effective_as_of = (
        max(as_of_candidates) if as_of_candidates else (today or datetime.now().date()).isoformat()
    )

    return WealthContextSnapshot(
        as_of=effective_as_of,
        tracker_as_of=tracker_as_of,
        wealthplan_as_of=wealthplan_as_of,
        net_worth_total=round(net_worth, 2),
        liquid_total=round(liquid, 2),
        investable_total=round(liquid, 2),
        home_equity=home_equity,
        allocation_by_bucket=allocation,
        cash_need_band=cash_need.band if cash_need.available else None,
        cash_need_reasons=cash_need.reasons if cash_need.available else (),
        sources=sources,
        warnings=tuple(warnings),
    )


__all__ = [
    "WealthContextSnapshot",
    "build_wealth_context_snapshot",
    "load_wealthplan_starting",
]
