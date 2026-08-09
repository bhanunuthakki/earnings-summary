"""Deterministic pre-analysis for the position-review service (Slice 1).

The position-review service answers "should I trim / hold / add <TICKER> at
these levels?" by composing five inputs the owner cares about — portfolio
weight, dollar value, company fundamentals + break-rules, valuation, and his
own convictions. This module is the *grounded, LLM-free* first stage: it fetches
those facts and packs them into a :class:`PreAnalysis` so the later reasoning
stage (Slice 2) argues over numbers, not vibes.

Design split (mirrors ``pipeline.allocation_decisions_panel``'s pure ``score_row``
vs I/O ``build_sizing_audit_rows``):

  * ``build_pre_analysis`` — the I/O orchestrator: reads the live tracker, the
    materialized weight cache, the thesis evaluator, the DCF runs, the sizing
    intents, the instrument registry, and the conviction stores.
  * ``assemble_pre_analysis`` + the ``classify_*`` / ``summarize_verdict``
    helpers — a PURE core over primitives, unit-testable with no DB or network.

Two determinations are pure Python and never touch an LLM:

  1. the **valuation verdict** — the ``dcf.valuation.over_under_pct`` trim/sell
     ladder, executed here against the holding's ``mos_bar`` (see
     ``classify_valuation``); and
  2. **weight-vs-band** — a target-weight tolerance band, falling back to a
     single-name concentration flag when no target is recorded (which is the
     prod reality today: ``position_sizing_intent`` is empty).

Heavy dependencies (the DCF cockpit, thesis evaluator, tracker client, stores)
are imported lazily inside ``build_pre_analysis`` so importing this module for
the pure helpers stays cheap and side-effect-free (``db_paths`` docstring: a
top-level ``db`` import triggers ``init_db``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from advisor.store import STANCES
from allocation.concentration import (
    ENTRY_INTENTIONAL,
    classify_entry_method,
    classify_zone,
    zone_at_least,
)
from calibration_guard import confidence_note, is_confident
from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

if TYPE_CHECKING:
    from advisor.position_tax import PositionTaxView
    from capture.matcher import RosterIndex
    from compute.thesis_evaluator import ThesisVerdict
    from integrations.portfolio_tracker_client import LiveTransaction
    from owner_profile.store import OwnerProfileFactRow

log = logging.getLogger(__name__)

__all__ = [
    "AGENT_SOURCE",
    "BAND_TOLERANCE",
    "CAPACITY_HORIZON_DAYS",
    "CONFIDENCES",
    "DEFAULT_MOS_BAR",
    "OWNER_ATTESTED_KEY",
    "POSITION_REVIEW_PURPOSE",
    "REVIEW_SOURCES",
    "REVIEW_SOURCE_KEY",
    "SELL_OVER_UNDER",
    "SELL_WINNERS_KEY",
    "SUGGESTED_EXPRESSIONS",
    "TRIM_OVER_UNDER",
    "CapacityContext",
    "PositionReview",
    "PreAnalysis",
    "RiskContext",
    "VerdictOutput",
    "apply_behavioral_guard",
    "assemble_pre_analysis",
    "attest_review_changed",
    "behavioral_rules_block",
    "build_capacity_context",
    "build_pre_analysis",
    "classify_valuation",
    "classify_weight_band",
    "encode_first_output",
    "graded_sell_record",
    "mechanical_read",
    "parse_review_command",
    "parse_verdict_output",
    "render_capacity_lines",
    "render_pre_analysis_chat",
    "render_pre_analysis_plain",
    "render_risk_lines",
    "render_tax_lines",
    "resolve_review_target",
    "review_position",
    "review_reply_text",
    "seed_behavioral_rules",
    "summarize_verdict",
]

# "at $70" / "above 70" / "over $12.50" -> the price level to review at. Shared
# by every chat-shaped surface that parses "/review <TICKER> [at $X]" (the Ask
# tab command and the Telegram poller) so the at-price behavior never drifts.
_AT_PRICE_RX = re.compile(r"(?:at|above|over)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Tunables — the valuation ladder mirrors the ``dcf.valuation.over_under_pct``
# docstring EXACTLY (>0.10 trim, >0.20 sell, < -mos_bar initiation). The guard
# test asserts these against that function so the two can never drift.
# --------------------------------------------------------------------------- #
TRIM_OVER_UNDER = 0.10  # over_under (decimal) above which the ladder says "trim"
SELL_OVER_UNDER = 0.20  # ... "sell"
DEFAULT_MOS_BAR = 0.30  # fallback initiation bar when a holding JSON omits mos_bar
BAND_TOLERANCE = 0.20  # +/- 20% tolerance band around a stated target weight
# NOTE: the old flat 8% single-name concentration bar is gone (PRD §7.2,
# P0.2) — single-name sizing is now read through allocation.concentration's soft
# zones (ordinary/meaningful/concentrated/highly_concentrated/exceptional).
# `concentration_flag` on PreAnalysis now means "zone >= concentrated"
# (weight >= allocation.concentration.TRIM_ASSESSMENT_THRESHOLD_PCT).

# tenet-2 Phase 2 capacity block (§4 delivery seam 1 of
# docs/design/tenet2_advisory_program.md): a dated life event only shows in
# the capacity block when it falls within this many days of today — "the
# position's horizon window (default 24mo lookahead)". A baby due in 2031
# doesn't show on a review run today; a work-break starting in 2027 does.
CAPACITY_HORIZON_DAYS = 730

_HOLDINGS_SUBDIR: tuple[str, ...] = ("micro_thesis", "holdings")

# BreachStatus.value -> the coarse label the service reasons over. Read by
# ``.value`` (a str) rather than importing the enum, keeping this core light.
_BREAK_STATUS_LABEL: dict[str, str] = {
    "ok": "intact",
    "warn": "warn",
    "breach": "breach",
    "unresolved": "unresolved",
}


@dataclass(frozen=True)
class CapacityContext:
    """Deterministic, zero-LLM read of the owner's affirmed capacity context —
    the tenet-2 Phase 2 "capacity block" (§4 delivery seam 1). Every field is
    ``None``/``()`` when nothing on file supports it — a fact that hasn't been
    AFFIRMED (only ``proposed``) never populates a field here, so the block
    renders NOTHING (not a placeholder) until the owner ratifies via the
    packet walk. This is deliberately separate from ``PreAnalysis`` itself so
    it can be built, tested, and reasoned about on its own."""

    cash_buffer_months: float | None = None
    tax_bucket_total_usd: float | None = None
    tax_bucket_as_of: str | None = None  # ISO date the balances snapshot is from
    # One rendered line per dated life event within CAPACITY_HORIZON_DAYS of
    # today, e.g. "Work break (person_a): 2027-03-01 through 2027-09-01".
    upcoming_life_events: tuple[str, ...] = field(default_factory=tuple)
    # Set when `ticker` is a member of an AFFIRMED human_capital.<bucket> fact.
    human_capital_note: str | None = None
    # From integrations.wealthplan_capacity.read_cash_need_summary — band-level
    # only, no amounts. Attached by build_pre_analysis (a separate I/O leg),
    # not by build_capacity_context (which reads only owner_profile_facts).
    wealthplan_cash_need_note: str | None = None
    # From advisor.exit_quality.read_ticker_exit_quality — the tracker's
    # realized exit-quality read for THIS ticker, when the tracker has one.
    exit_quality_note: str | None = None

    def is_empty(self) -> bool:
        """True when every field is absent — the capacity block renders no
        lines at all in this case (byte-identical to pre-Phase-2 output)."""
        return (
            self.cash_buffer_months is None
            and self.tax_bucket_total_usd is None
            and not self.upcoming_life_events
            and self.human_capital_note is None
            and self.wealthplan_cash_need_note is None
            and self.exit_quality_note is None
        )


@dataclass(frozen=True)
class RiskContext:
    """Deterministic, zero-LLM read of a position's portfolio-risk footprint —
    the C7 risk-aware ``/review`` block. Mirrors :class:`CapacityContext`'s
    contract EXACTLY: every field is ``None``/``()`` when that sub-leg had
    nothing to report or failed to compute, and the WHOLE block renders
    nothing (not a placeholder) when every field is empty. Built by
    :func:`_build_risk_context`, which independently try/excepts each leg so
    one broken input (e.g. a pre-migration DB missing
    ``business_factor_exposures``) never blocks the others."""

    # Share of TOTAL BOOK RISK this holding carries (0-100), from
    # allocation.book_risk.build_book_risk's Euler risk-contribution split —
    # NOT the same as its portfolio weight (a volatile/correlated name can
    # carry more risk share than dollar share).
    risk_share_pct: float | None = None
    # This holding's correlation to the REST OF THE BOOK (marginal_risk's
    # corr_to_book), from the SAME build_book_risk call above — reused, not a
    # second covariance build.
    corr_to_book: float | None = None
    # Human-readable crowding-cluster membership (portfolio_correlation's
    # connected-components clusters at >=0.70 pairwise correlation), e.g.
    # "co-moves with NU (avg corr 0.75, 22% combined weight)" — None when the
    # ticker isn't in any multi-name cluster, or the read degraded.
    crowding_cluster: str | None = None
    # This ticker's OWN top business-factor loadings (C3
    # business_factor_exposures, is_latest rows only), highest first, capped
    # at 3 — e.g. (("Brazil consumer credit", 0.9), ("LatAm consumer/FX", 0.7)).
    top_factors: tuple[tuple[str, float], ...] = ()
    # EVENT_SCENARIOS (src/portfolio_montecarlo.py) ids where this ticker is a
    # NAMED member — e.g. ("joint_latam",). Membership only; the modeled
    # book-level stress return is the Risk tab's job, not this one-line read.
    event_scenarios: tuple[str, ...] = ()
    # Human-readable reasons any sub-leg above came back empty (network/DB
    # error, missing table, thin price history) — never surfaced to the
    # owner, logged for debugging only.
    degraded_reasons: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """True when every substantive field is absent — mirrors
        ``CapacityContext.is_empty()``; the renderer treats this identically
        to ``risk=None``."""
        return (
            self.risk_share_pct is None
            and self.corr_to_book is None
            and self.crowding_cluster is None
            and not self.top_factors
            and not self.event_scenarios
        )


@dataclass(frozen=True)
class PreAnalysis:
    """Grounded, LLM-free snapshot of a position — the input to the reasoning
    stage. Every field is a fact read from a store (or ``None`` when that store
    has nothing for the ticker), never an opinion."""

    ticker: str
    # --- sizing --------------------------------------------------------------
    weight_pct: float | None  # percent of book (0-100)
    weight_source: str  # "live" | "materialized" | "unknown"
    market_value_usd: float | None
    unrealized_pnl_usd: float | None
    target_weight_pct: float | None  # from position_sizing_intent (empty in prod)
    target_band: tuple[float, float] | None  # (lo, hi) percent, when a target is set
    weight_vs_band: str  # "above_band" | "in_band" | "below_band" | "no_band"
    conviction_1_5: int | None
    concentration_flag: bool  # zone >= "concentrated" (weight >= TRIM_ASSESSMENT_THRESHOLD_PCT)
    # --- fundamentals --------------------------------------------------------
    thesis_present: bool  # micro_thesis/holdings/<T>.json exists
    verdict_label: str | None  # holdings JSON "verdict" (e.g. "Intact")
    key_driver: str | None
    break_rule_status: str  # "intact" | "warn" | "breach" | "unresolved" | "no_thesis"
    tripped_rules: tuple[str, ...]
    # --- valuation -----------------------------------------------------------
    dcf_gap_pct: float | None  # percent, positive = OVER-valued (recomputed at at_price)
    npv_per_share: float | None
    dcf_live_price: float | None  # the price the stored DCF run used
    dcf_date: str | None
    at_price: float | None  # the level the question asked about ("above $70")
    mos_bar: float | None
    valuation_verdict: str  # "sell" | "trim" | "buy" | "fair" | "n/a"
    # --- convictions / instrument -------------------------------------------
    conviction_encoded: bool  # thesis_present AND (a current stance OR a decision note)
    has_stance: bool
    has_decision_note: bool
    is_index_instrument: bool  # ETF — a macro/factor sleeve, not a single-name thesis
    # --- tax (deterministic, advisor.position_tax) ---------------------------
    # None only on PreAnalysis objects built before the tax stage (tests, old
    # callers); build_pre_analysis always attaches a view, degraded or not.
    tax: PositionTaxView | None = None
    # --- owner-context capacity (tenet-2 Phase 2, deterministic) -------------
    # None on PreAnalysis objects built before the capacity stage (tests, old
    # callers) — treated identically to an empty CapacityContext by the
    # renderers. build_pre_analysis always attaches one (degraded-empty or not).
    capacity: CapacityContext | None = None
    # --- concentration zones (PRD §7.2, P0.2) --------------------------------
    # allocation.concentration.Zone name, or None when weight_pct is unknown.
    concentration_zone: str | None = None
    # allocation.concentration.ENTRY_INTENTIONAL / ENTRY_APPRECIATION, or None
    # when the entry method couldn't be derived (no transaction history).
    entry_method: str | None = None
    # One-line human text for concentration_zone (PRD §7.2 table), or None.
    zone_treatment: str | None = None
    # --- risk context (C7, deterministic) ------------------------------------
    # None on PreAnalysis objects built before the risk stage (tests, old
    # callers) — treated identically to an empty RiskContext by the
    # renderers. build_pre_analysis always attaches one (degraded-empty/None
    # or not); byte-identical to today's output when every sub-leg degrades.
    risk: RiskContext | None = None


# --------------------------------------------------------------------------- #
# Pure core (no I/O)
# --------------------------------------------------------------------------- #


def _over_under(price: float, fair_value_per_share: float) -> float:
    """(price - fair) / fair as a decimal; positive = over-valued.

    Duplicates the one-line ``dcf.valuation.over_under_pct`` body so the pure
    core carries no heavy import; the guard test pins the two to agree.
    """
    return (price - fair_value_per_share) / fair_value_per_share


def classify_valuation(over_under_dec: float | None, mos_bar: float | None) -> str:
    """Execute the trim/sell ladder on an over/under-valuation decimal.

    ``> SELL_OVER_UNDER`` → ``"sell"``; ``> TRIM_OVER_UNDER`` → ``"trim"``;
    ``< -mos_bar`` (an initiation-grade discount) → ``"buy"``; else ``"fair"``.
    ``None`` (no DCF on file) → ``"n/a"``. Falls back to ``DEFAULT_MOS_BAR`` when
    the holding JSON omits its own bar.
    """
    if over_under_dec is None:
        return "n/a"
    if over_under_dec > SELL_OVER_UNDER:
        return "sell"
    if over_under_dec > TRIM_OVER_UNDER:
        return "trim"
    bar = mos_bar if mos_bar is not None else DEFAULT_MOS_BAR
    if over_under_dec < -bar:
        return "buy"
    return "fair"


def classify_weight_band(
    weight_pct: float | None,
    target_weight_pct: float | None,
    *,
    tolerance: float = BAND_TOLERANCE,
) -> tuple[str, tuple[float, float] | None]:
    """Classify the live weight against a +/-tolerance band around the target.

    Returns ``(label, band)``. With no recorded target the band is ``None`` and
    the label is ``"no_band"`` — the caller leans on ``concentration_flag``
    instead (the prod reality: no target weights recorded yet). A known target
    but unknown live weight also yields ``"no_band"`` (band still returned).
    """
    if target_weight_pct is None:
        return "no_band", None
    lo = target_weight_pct * (1.0 - tolerance)
    hi = target_weight_pct * (1.0 + tolerance)
    band = (lo, hi)
    if weight_pct is None:
        return "no_band", band
    if weight_pct > hi:
        return "above_band", band
    if weight_pct < lo:
        return "below_band", band
    return "in_band", band


def summarize_verdict(verdict: ThesisVerdict | None) -> tuple[str, tuple[str, ...]]:
    """Reduce a ``ThesisVerdict`` to ``(status_label, tripped_rules)``.

    ``None`` (no holdings JSON to evaluate) → ``("no_thesis", ())``. Tripped
    rules are the WARN + BREACH evaluations, most-severe context first as the
    evaluator ordered them (universal tripwires before per-ticker breakers).
    Read via ``.value`` so this stays enum-import-free.
    """
    if verdict is None:
        return "no_thesis", ()
    label = _BREAK_STATUS_LABEL.get(verdict.overall_status.value, "unresolved")
    tripped = tuple(
        f"{ev.rule.rule_id} ({ev.status.value}): {ev.detail}"
        for ev in verdict.rule_evaluations
        if ev.status.value in ("warn", "breach")
    )
    return label, tripped


def _opt_float(payload: dict[str, object], key: str) -> float | None:
    """Read a JSON scalar as float; None for missing / bool / unparseable."""
    v = payload.get(key)
    if isinstance(v, bool):  # bool is an int subclass — reject it as a number
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _opt_str(payload: dict[str, object], key: str) -> str | None:
    v = payload.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def assemble_pre_analysis(
    ticker: str,
    *,
    weight_pct: float | None,
    weight_source: str,
    market_value_usd: float | None,
    unrealized_pnl_usd: float | None,
    target_weight_pct: float | None,
    conviction_1_5: int | None,
    break_rule_status: str,
    tripped_rules: tuple[str, ...],
    holdings_json: dict[str, object] | None,
    dcf_tuple: tuple[float | None, float | None, float | None, str | None] | None,
    has_stance: bool,
    has_decision_note: bool,
    is_index_instrument: bool,
    at_price: float | None,
    entry_method: str | None = None,
) -> PreAnalysis:
    """Compose primitives into a :class:`PreAnalysis`. Pure — no I/O.

    ``dcf_tuple`` is ``(fv_gap_pct, npv_per_share, live_price, valuation_date)``
    as returned by ``research_cockpit.latest_dcf_runs``. When ``at_price`` is
    supplied AND a positive NPV is on file, the over/under gap is RECOMPUTED at
    that level so "above $70" is answered against $70, not the stored close.

    ``entry_method`` (:data:`allocation.concentration.ENTRY_INTENTIONAL` /
    ``ENTRY_APPRECIATION`` / ``None``) is the caller's pre-derived read of
    whether the position reached its current size through a recent
    intentional buy or pure price appreciation — ``build_pre_analysis``
    derives it from the tracker's transaction history; this function is pure
    and never fetches it itself.
    """
    thesis_present = holdings_json is not None
    verdict_label = _opt_str(holdings_json, "verdict") if holdings_json is not None else None
    key_driver = _opt_str(holdings_json, "key_driver") if holdings_json is not None else None
    mos_bar = _opt_float(holdings_json, "mos_bar") if holdings_json is not None else None

    fv_gap_pct: float | None = None
    npv: float | None = None
    dcf_price: float | None = None
    dcf_date: str | None = None
    if dcf_tuple is not None:
        fv_gap_pct, npv, dcf_price, dcf_date = dcf_tuple

    over_under_dec: float | None = None
    dcf_gap_pct: float | None = None
    if at_price is not None and npv is not None and npv > 0:
        over_under_dec = _over_under(at_price, npv)
        dcf_gap_pct = over_under_dec * 100.0
    elif fv_gap_pct is not None:
        dcf_gap_pct = fv_gap_pct
        over_under_dec = fv_gap_pct / 100.0
    valuation_verdict = classify_valuation(over_under_dec, mos_bar)

    weight_vs_band, target_band = classify_weight_band(weight_pct, target_weight_pct)
    zone_assessment = classify_zone(weight_pct)
    concentration_flag = zone_assessment is not None and zone_assessment.trim_assessment_required
    concentration_zone = zone_assessment.zone if zone_assessment is not None else None
    zone_treatment = zone_assessment.treatment if zone_assessment is not None else None
    conviction_encoded = thesis_present and (has_stance or has_decision_note)

    return PreAnalysis(
        ticker=ticker.upper(),
        weight_pct=weight_pct,
        weight_source=weight_source,
        market_value_usd=market_value_usd,
        unrealized_pnl_usd=unrealized_pnl_usd,
        target_weight_pct=target_weight_pct,
        target_band=target_band,
        weight_vs_band=weight_vs_band,
        conviction_1_5=conviction_1_5,
        concentration_flag=concentration_flag,
        thesis_present=thesis_present,
        verdict_label=verdict_label,
        key_driver=key_driver,
        break_rule_status=break_rule_status,
        tripped_rules=tuple(tripped_rules),
        dcf_gap_pct=dcf_gap_pct,
        npv_per_share=npv,
        dcf_live_price=dcf_price,
        dcf_date=dcf_date,
        at_price=at_price,
        mos_bar=mos_bar,
        valuation_verdict=valuation_verdict,
        conviction_encoded=conviction_encoded,
        has_stance=has_stance,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
        concentration_zone=concentration_zone,
        entry_method=entry_method,
        zone_treatment=zone_treatment,
    )


# --------------------------------------------------------------------------- #
# I/O orchestrator
# --------------------------------------------------------------------------- #


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively; None on any
    read/parse failure or a non-object payload (so an unencoded name — FLKR —
    degrades cleanly to ``thesis_present=False``)."""
    path = repo_root.joinpath(*_HOLDINGS_SUBDIR) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def build_capacity_context(
    conn: sqlite3.Connection, ticker: str, *, today: date | None = None
) -> CapacityContext:
    """Deterministic, zero-LLM read of AFFIRMED ``owner_profile_facts`` rows
    for the ``/review`` capacity block (tenet-2 Phase 2, §4 delivery seam 1).

    Reads ONLY ``owner_profile.store.get_current_profile`` (capacity-category
    facts) — the wealthplan cash-need band and the tracker exit-quality read
    are separate I/O legs ``build_pre_analysis`` attaches afterward via
    ``dataclasses.replace``, so this function stays conn-scoped and testable
    against a bare fixture DB with no network/sibling-repo dependency.

    Degrades to an all-empty :class:`CapacityContext` on a missing table
    (pre-0159 substrate), a locked DB, or any row-shape surprise — never
    raises, and never guesses: a fact that exists only as ``proposed`` simply
    isn't in ``get_current_profile``'s output, so it contributes nothing here.
    """
    try:
        from pydantic import ValidationError

        from owner_profile.models import HumanCapitalBucket, LifeEventFact
        from owner_profile.store import get_current_profile

        grouped = get_current_profile(conn)
    except Exception as exc:  # missing table / locked DB / anything — degrade
        log.debug({"event": "capacity_context_load_failed", "ticker": ticker, "error": str(exc)})
        return CapacityContext()

    capacity_rows = grouped.get("capacity", [])
    today_ = today or date.today()
    horizon = today_ + timedelta(days=CAPACITY_HORIZON_DAYS)
    ticker_upper = ticker.upper()

    cash_buffer_months: float | None = None
    tax_total: float | None = None
    tax_as_of: str | None = None
    life_lines: list[str] = []
    human_capital_note: str | None = None

    for row in capacity_rows:
        if row.key == "cash_buffer_months":
            months = row.value.get("months")
            if isinstance(months, (int, float)) and not isinstance(months, bool):
                cash_buffer_months = float(months)
        elif row.key == "tax_bucket_balances":
            balances = row.value.get("balances")
            as_of = row.value.get("as_of")
            if isinstance(balances, dict):
                balances_obj = cast("dict[str, object]", balances)
                try:
                    tax_total = sum(
                        float(v)
                        for v in balances_obj.values()
                        if isinstance(v, (int, float, str)) and not isinstance(v, bool)
                    )
                except (TypeError, ValueError):
                    tax_total = None
            if isinstance(as_of, str) and as_of.strip():
                tax_as_of = as_of
        elif row.key.startswith("life_event."):
            try:
                life = LifeEventFact.model_validate(row.value)
            except ValidationError:
                continue  # parent_care (age-keyed) or an unrecognized shape
            if today_ <= life.date <= horizon:
                who = f" ({life.person})" if life.person else ""
                window = f" through {life.end_date.isoformat()}" if life.end_date else ""
                life_lines.append(f"{life.label}{who}: {life.date.isoformat()}{window}")
        elif row.key.startswith("human_capital."):
            try:
                bucket = HumanCapitalBucket.model_validate(row.value)
            except ValidationError:
                continue
            if ticker_upper in bucket.members:
                bucket_name = row.key.removeprefix("human_capital.")
                human_capital_note = (
                    f"this position stacks on your income's {bucket_name} bucket "
                    f"(cap {bucket.cap_pct:g}%)"
                )

    return CapacityContext(
        cash_buffer_months=cash_buffer_months,
        tax_bucket_total_usd=tax_total,
        tax_bucket_as_of=tax_as_of,
        upcoming_life_events=tuple(life_lines),
        human_capital_note=human_capital_note,
    )


def render_capacity_lines(capacity: CapacityContext | None) -> list[str]:
    """Markdown bullets for the capacity block — shared by the chat reply and
    the plain-ASCII (Telegram) reply, mirroring ``render_tax_lines``'s shape.

    Empty for ``capacity=None`` or an all-empty :class:`CapacityContext`: this
    is the common case today (no affirmed facts yet) and MUST render byte-
    identical to the pre-Phase-2 output — no header, no placeholder line."""
    if capacity is None or capacity.is_empty():
        return []
    lines: list[str] = []
    bits: list[str] = []
    if capacity.cash_buffer_months is not None:
        bits.append(f"cash buffer target {capacity.cash_buffer_months:g} months")
    if capacity.tax_bucket_total_usd is not None:
        as_of = f" as of {capacity.tax_bucket_as_of}" if capacity.tax_bucket_as_of else ""
        bits.append(f"tax-bucket balances ${capacity.tax_bucket_total_usd:,.0f} total{as_of}")
    if bits:
        lines.append(f"- Capacity: {'; '.join(bits)}")
    for ev in capacity.upcoming_life_events:
        lines.append(f"- Life event within horizon: {ev}")
    if capacity.human_capital_note:
        lines.append(f"- Human-capital: {capacity.human_capital_note}")
    if capacity.wealthplan_cash_need_note:
        lines.append(f"- Near-term cash need: {capacity.wealthplan_cash_need_note}")
    if capacity.exit_quality_note:
        lines.append(f"- {capacity.exit_quality_note}")
    return lines


def render_risk_lines(risk: RiskContext | None) -> list[str]:
    """Markdown bullets for the C7 risk block — shared by the chat reply and
    the plain-ASCII (Telegram) reply, mirroring ``render_capacity_lines``'s
    shape exactly.

    Empty for ``risk=None`` or an all-empty :class:`RiskContext` — the common
    degrade case (a pre-migration DB, an empty weights cache, or a thin price
    history) MUST render byte-identical to the pre-C7 output — no header, no
    placeholder line."""
    if risk is None or risk.is_empty():
        return []
    lines: list[str] = []
    bits: list[str] = []
    if risk.risk_share_pct is not None:
        bits.append(f"{risk.risk_share_pct:.0f}% of book risk")
    if risk.corr_to_book is not None:
        bits.append(f"corr-to-book {risk.corr_to_book:+.2f}")
    if bits:
        lines.append(f"- Risk: {'; '.join(bits)}")
    if risk.crowding_cluster:
        lines.append(f"- Crowding: {risk.crowding_cluster}")
    if risk.top_factors:
        factors = ", ".join(f"{factor} {loading:.1f}" for factor, loading in risk.top_factors)
        lines.append(f"- Business factors: {factors}")
    if risk.event_scenarios:
        lines.append(f"- Event scenarios: {', '.join(risk.event_scenarios)}")
    return lines


def _build_risk_context(
    ticker: str, db_path: Path | str | None, repo_root: Path
) -> RiskContext | None:
    """Assemble the C7 risk block for ``ticker`` (§ plan: "risk-aware
    /review"). Every sub-leg below is independently try/excepted into
    ``degraded_reasons`` — a broken/absent ``business_factor_exposures``
    table (pre-migration substrate: ``sqlite3.OperationalError``), an offline
    price cache, or a weights-cache miss each degrade THEIR OWN leg only, and
    never propagate to the others or raise out of this function.

    Returns ``None`` (leg absent — ``/review`` renders exactly as it did
    before this block existed) when EVERY leg above came back empty. This is
    the plan's explicit degrade test: an empty/absent
    ``business_factor_exposures`` table combined with an empty weights cache
    yields ``None`` here, and ``render_risk_lines(None)`` / the verdict
    prompt's risk block both render nothing.
    """
    ticker = ticker.upper()
    degraded: list[str] = []

    # --- leg 1+2: book risk share + corr-to-book (one build_book_risk call,
    # allocation.book_risk — the SAME assembly the next-dollar model and the
    # L7 risk-budget allocator share, so this never disagrees with the Risk
    # tab about "the book's risk") ---------------------------------------
    risk_share_pct: float | None = None
    corr_to_book: float | None = None
    weights: dict[str, float] = {}
    try:
        from portfolio_weights import read_materialized_weights

        weights = read_materialized_weights(repo_root)
        if not weights:
            degraded.append("materialized weights cache empty or unavailable")
    except Exception as exc:  # never let a cache-read glitch break /review
        log.debug({"event": "risk_context_weights_failed", "ticker": ticker, "error": str(exc)})
        degraded.append(f"weights cache read failed: {type(exc).__name__}")

    if weights:
        try:
            from allocation.book_risk import build_book_risk

            book = build_book_risk(repo_root, list(weights), weights)
            if book.hidden_reason is not None:
                degraded.append(f"book risk unavailable: {book.hidden_reason}")
            elif ticker not in book.risk_share:
                degraded.append(f"{ticker} not in the priced book-risk matrix")
            else:
                risk_share_pct = book.risk_share[ticker] * 100.0
                corr_to_book = book.corr_to_book.get(ticker)
        except Exception as exc:
            log.debug(
                {"event": "risk_context_book_risk_failed", "ticker": ticker, "error": str(exc)}
            )
            degraded.append(f"book-risk leg failed: {type(exc).__name__}")

    # --- leg 3: crowding-cluster membership (portfolio_correlation's
    # connected-components clusters — Portfolio Risk v2's crowding read) -----
    crowding_cluster: str | None = None
    if weights:
        try:
            from portfolio_correlation import build_holdings_correlation_from_disk

            corr_read = build_holdings_correlation_from_disk(repo_root, list(weights), weights)
            if corr_read is None:
                degraded.append("crowding-cluster read unavailable (thin/absent price history)")
            else:
                cluster = next((c for c in corr_read.clusters if ticker in c.tickers), None)
                if cluster is not None:
                    others = ", ".join(t for t in cluster.tickers if t != ticker)
                    crowding_cluster = (
                        f"co-moves with {others} (avg corr {cluster.avg_corr:.2f}, "
                        f"{cluster.combined_weight_pct:.0f}% combined weight)"
                    )
        except Exception as exc:
            log.debug(
                {"event": "risk_context_crowding_failed", "ticker": ticker, "error": str(exc)}
            )
            degraded.append(f"crowding-cluster leg failed: {type(exc).__name__}")

    # --- leg 4: this ticker's own top business-factor loadings (C3) ---------
    top_factors: tuple[tuple[str, float], ...] = ()
    try:
        from db_paths import resolve_db_path

        resolved = resolve_db_path(db_path)
        if resolved is None or not Path(resolved).exists():
            degraded.append("no database on file for business-factor exposures")
        else:
            conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
            try:
                rows = conn.execute(
                    "SELECT factor, loading FROM business_factor_exposures "
                    "WHERE ticker = ? AND is_latest = 1 ORDER BY loading DESC LIMIT 3",
                    (ticker,),
                ).fetchall()
                top_factors = tuple((str(f), float(loading)) for f, loading in rows)
            finally:
                conn.close()
    except sqlite3.OperationalError as exc:
        # Pre-migration substrate: business_factor_exposures doesn't exist yet.
        log.debug(
            {"event": "risk_context_factors_table_absent", "ticker": ticker, "error": str(exc)}
        )
        degraded.append("business_factor_exposures table not on this substrate")
    except Exception as exc:
        log.debug({"event": "risk_context_factors_failed", "ticker": ticker, "error": str(exc)})
        degraded.append(f"business-factor leg failed: {type(exc).__name__}")

    # --- leg 5: event-scenario membership (C5, src.portfolio_montecarlo) ----
    event_scenarios: tuple[str, ...] = ()
    try:
        from portfolio_montecarlo import EVENT_SCENARIOS

        event_scenarios = tuple(s.id for s in EVENT_SCENARIOS if ticker in s.named_tickers)
    except Exception as exc:
        log.debug({"event": "risk_context_scenarios_failed", "ticker": ticker, "error": str(exc)})
        degraded.append(f"event-scenario leg failed: {type(exc).__name__}")

    risk = RiskContext(
        risk_share_pct=risk_share_pct,
        corr_to_book=corr_to_book,
        crowding_cluster=crowding_cluster,
        top_factors=top_factors,
        event_scenarios=event_scenarios,
        degraded_reasons=tuple(degraded),
    )
    return None if risk.is_empty() else risk


def build_pre_analysis(
    repo_root: Path,
    ticker: str,
    *,
    at_price: float | None = None,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    db_path: Path | str | None = None,
) -> PreAnalysis:
    """Fetch every input and assemble the :class:`PreAnalysis` for ``ticker``.

    Degrades gracefully: an offline tracker falls back to the materialized
    weight cache; a name with no holdings JSON (an unencoded conviction like
    FLKR) yields ``thesis_present=False`` / ``break_rule_status="no_thesis"``
    rather than raising. Heavy modules are imported here, not at module top.
    """
    from integrations.portfolio_tracker_client import fetch_live_portfolio
    from portfolio_weights import read_materialized_weights

    ticker = ticker.upper()
    holdings_dir = repo_root.joinpath(*_HOLDINGS_SUBDIR)
    holdings_json = _load_holdings_json(repo_root, ticker)

    # --- weight + dollars: prefer the live tracker, fall back to the cache ---
    weight_pct: float | None = None
    weight_source = "unknown"
    market_value_usd: float | None = None
    unrealized_pnl_usd: float | None = None
    live = fetch_live_portfolio(api_url=api_url)
    pos = None
    if live.available:
        pos = next((p for p in live.positions if (p.ticker or "").upper() == ticker), None)
        if pos is not None:
            weight_pct = pos.percent_of_portfolio
            weight_source = "live"
            market_value_usd = pos.market_value
            unrealized_pnl_usd = pos.unrealized_pnl
    if weight_pct is None:
        frac = read_materialized_weights(repo_root).get(ticker)
        if frac is not None:
            weight_pct = frac * 100.0
            weight_source = "materialized"

    # --- stated sizing posture (empty in prod today) -------------------------
    from user_state.sizing import latest_intent

    target_row = latest_intent(
        user_id=user_id, ticker=ticker, intent_kind="target_weight_pct", db_path=db_path
    )
    conv_row = latest_intent(
        user_id=user_id, ticker=ticker, intent_kind="conviction", db_path=db_path
    )
    target_weight_pct = target_row.intent_value if target_row is not None else None
    conviction_1_5 = (
        int(conv_row.intent_value)
        if conv_row is not None and conv_row.intent_value is not None
        else None
    )

    # --- convictions on file -------------------------------------------------
    from synthesis.insights import list_insights
    from user_state.notes import list_notes

    has_stance = bool(
        list_insights(kind="stance", scope_key=ticker, status="current", db_path=db_path)
    )
    has_decision_note = bool(list_notes(ticker=ticker, kind="decision", db_path=db_path))

    # --- conn-backed reads: break-rules, DCF, instrument kind ----------------
    from compute.thesis_evaluator import evaluate_ticker_thesis
    from instrument_store import get_instrument_kind
    from models.companies import InstrumentType
    from pipeline.research_cockpit import latest_dcf_runs
    from user_state._db import open_conn

    conn = open_conn(db_path)
    try:
        verdict: ThesisVerdict | None
        try:
            verdict = evaluate_ticker_thesis(conn, ticker=ticker, holdings_dir=holdings_dir)
        except FileNotFoundError:
            verdict = None  # no holdings JSON — the FLKR case
        dcf_tuple = latest_dcf_runs(conn).get(ticker)
        instrument_kind = get_instrument_kind(conn, ticker)
        capacity = build_capacity_context(conn, ticker)
    finally:
        conn.close()

    break_rule_status, tripped_rules = summarize_verdict(verdict)
    is_index_instrument = instrument_kind is InstrumentType.ETF

    # --- capacity block, remaining legs: wealthplan cash-need band + tracker
    # exit-quality (both cross federation boundaries, both never-raise) -------
    try:
        from integrations.wealthplan_capacity import read_cash_need_summary

        wp_summary = read_cash_need_summary()
        wealthplan_note = (
            wp_summary.band
            if wp_summary.available and wp_summary.band == "normal"
            else (
                f"elevated ({', '.join(wp_summary.reasons)})"
                if wp_summary.available and wp_summary.band == "elevated"
                else None
            )
        )
    except Exception as exc:  # never let a sibling-repo boundary break /review
        log.debug({"event": "wealthplan_capacity_read_failed", "error": str(exc)})
        wealthplan_note = None
    try:
        from advisor.exit_quality import read_ticker_exit_quality, render_exit_quality_note

        eq = read_ticker_exit_quality(ticker, api_url=api_url)
        exit_note = render_exit_quality_note(eq) if eq is not None else None
    except Exception as exc:  # never let the tracker boundary break /review
        log.debug({"event": "exit_quality_read_failed", "ticker": ticker, "error": str(exc)})
        exit_note = None
    capacity = replace(
        capacity, wealthplan_cash_need_note=wealthplan_note, exit_quality_note=exit_note
    )

    # --- transaction history: fetched ONCE (only when the tracker holds a
    # live position — otherwise there is no tax leg to price either) and
    # reused for BOTH the entry-method zone classification below AND the
    # FIFO tax-lot reconstruction further down — never a second network
    # round-trip over the same window (PRD §7.2, P0.2 seam).
    history: list[LiveTransaction] | None = None
    entry_method: str | None = None
    if pos is not None:
        from integrations.portfolio_tracker_client import fetch_transaction_history

        history = fetch_transaction_history(api_url=api_url)
        if history is not None:
            buy_dates = [
                t.date
                for t in history
                if (t.ticker or "").strip().upper() == ticker
                and (
                    "buy" in (t.type or "").strip().lower()
                    or "buy" in (t.subtype or "").strip().lower()
                )
            ]
            entry_method = classify_entry_method(buy_dates, as_of=date.today())

    pre = assemble_pre_analysis(
        ticker,
        weight_pct=weight_pct,
        weight_source=weight_source,
        market_value_usd=market_value_usd,
        unrealized_pnl_usd=unrealized_pnl_usd,
        target_weight_pct=target_weight_pct,
        conviction_1_5=conviction_1_5,
        break_rule_status=break_rule_status,
        tripped_rules=tripped_rules,
        holdings_json=holdings_json,
        dcf_tuple=dcf_tuple,
        has_stance=has_stance,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
        at_price=at_price,
        entry_method=entry_method,
    )

    # --- tax view: lots from the SAME tracker history fetched above, honest
    # degrade otherwise ---------------------------------------------------
    from advisor.position_tax import (
        build_position_tax_view,
        load_tax_profile,
        propose_trim_fraction,
        unavailable_tax_view,
    )

    if pos is None:
        # live.error carries a full urllib3 pool dump — keep only the exception
        # type so the chat line stays one line.
        error_kind = (live.error or "unreachable").split(":", 1)[0]
        reason = (
            f"tracker offline ({error_kind})"
            if not live.available
            else "ticker not held in the live book"
        )
        tax_view = unavailable_tax_view(reason)
    else:
        from integrations.portfolio_tracker_client import TRANSACTION_HISTORY_LIMIT

        trim_fraction, trim_rationale = propose_trim_fraction(
            weight_pct=pre.weight_pct,
            target_band=pre.target_band,
            weight_vs_band=pre.weight_vs_band,
            zone=pre.concentration_zone,
        )
        tax_view = build_position_tax_view(
            pos,
            history,
            profile=load_tax_profile(repo_root),
            at_price=at_price,
            trim_fraction=trim_fraction,
            trim_rationale=trim_rationale,
            history_truncated=history is not None and len(history) >= TRANSACTION_HISTORY_LIMIT,
        )
    # --- risk context (C7): a separate I/O leg, wrapped defensively even
    # though _build_risk_context already guards every sub-leg internally —
    # matching this function's paranoid style for every cross-cutting block
    # (capacity's wealthplan/exit-quality legs above do the same) so a bug in
    # a NEW leg can never break /review.
    try:
        risk_ctx = _build_risk_context(ticker, db_path, repo_root)
    except Exception as exc:
        log.debug({"event": "risk_context_build_failed", "ticker": ticker, "error": str(exc)})
        risk_ctx = None

    return replace(pre, tax=tax_view, capacity=capacity, risk=risk_ctx)


# --------------------------------------------------------------------------- #
# Slice 2 — governed LLM verdict + deterministic behavioral guardrail
# --------------------------------------------------------------------------- #

POSITION_REVIEW_PURPOSE = "position_review"

# --------------------------------------------------------------------------- #
# Provenance + attestation context keys (read by the Coach P&L scoreboard —
# pipeline.allocation_decisions_panel — to keep agent/CI runs out of the
# owner-facing counts, and to count a "decision changed by the coach" ONLY on
# an explicit owner attestation, never on the silence-implies-heeded proxy).
# --------------------------------------------------------------------------- #
# Which surface persisted a review memo. Owner-driven surfaces (doorway/cli/
# telegram) count in the Coach P&L; ``agent`` (verification/CI runs) is excluded
# so an automated smoke run can never inflate the coach's scoreboard.
REVIEW_SOURCE_KEY = "source"
AGENT_SOURCE = "agent"
REVIEW_SOURCES: tuple[str, ...] = ("doorway", "cli", "telegram", AGENT_SOURCE)
# Set True only by :func:`attest_review_changed` (the owner's one-click "this
# review changed my call"); the Q3'26 "changed >= 1" bar counts these alone.
OWNER_ATTESTED_KEY = "owner_attested_change"

CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
SUGGESTED_EXPRESSIONS: tuple[str, ...] = (
    "trim-to-target",
    "LEAP-overlay",
    "do-nothing",
    "add-tranche",
    "encode-thesis-first",
)
_VERDICT_KEYS: tuple[str, ...] = (
    "verdict",
    "size",
    "reason",
    "confidence",
    "behavioral_check",
    "suggested_expression",
)


class _VerdictWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["buy", "add", "hold", "trim", "sell"]
    size: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1200)
    confidence: Literal["high", "medium", "low"]
    behavioral_check: str = Field(min_length=1, max_length=600)
    suggested_expression: Literal[
        "trim-to-target", "LEAP-overlay", "do-nothing", "add-tranche", "encode-thesis-first"
    ]


_VERDICT_ADAPTER = TypeAdapter(_VerdictWire)

# Fallback when no graded sells/trims exist yet (or the query fails) — the
# guard/prompt must never fabricate a ticker list.
_GENERIC_SELL_PATTERN_LINE = "your sell-winners-too-early pattern"


def graded_sell_record(db_path: Path | str | None) -> str | None:
    """The owner's live graded record on sell/trim recommendations, as one line.

    Reads ``decisions`` for his own rows (``decided_by = 'owner'``) recommending
    a sell or trim that have since been graded (``outcome_label != 'pending'``),
    and renders e.g. ``"Graded record on sells/trims: 5 of 8 wrong (AMZN,
    GOOGL, MU, NVDA, TSM) — n=8, low confidence"`` — the count of
    ``wrong``-graded calls over the total graded, followed by the DISTINCT
    wrong-graded tickers (alphabetical).

    Returns ``None`` when there is nothing graded yet (a fresh DB, or every
    sell/trim still ``pending``) — the caller omits the line rather than
    printing a hollow "0 of 0". Also ``None``/degraded on any DB error (missing
    file, pre-``decisions`` schema): never fabricate a count or ticker list.

    Below ``calibration_guard.MIN_CONFIDENT_N`` graded calls the line carries
    the guard's canonical hedge (``"— n=6, low confidence"``): this string is
    the coach's flagship evidence — it renders on the Position tab and is
    interpolated into the verdict prompt as proof of a "dominant flaw" — and
    the platform's own charter says a rate on a sparse denominator is reported
    but never asserted bare.
    """
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN outcome_label = 'wrong' THEN 1 ELSE 0 END) "
            "FROM decisions WHERE decided_by = 'owner' "
            "AND recommendation_kind IN ('sell', 'trim') AND outcome_label != 'pending'"
        ).fetchone()
        if row is None or not row[0]:
            return None
        total, wrong = int(row[0]), int(row[1] or 0)
        tickers = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT ticker FROM decisions WHERE decided_by = 'owner' "
                "AND recommendation_kind IN ('sell', 'trim') AND outcome_label = 'wrong' "
                "AND ticker IS NOT NULL ORDER BY ticker"
            ).fetchall()
        ]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    names = f" ({', '.join(tickers)})" if tickers else ""
    line = f"Graded record on sells/trims: {wrong} of {total} wrong{names}"
    if not is_confident(total):
        line += f" — {confidence_note(total)}"
    return line


def _sell_pattern_phrase(graded_line: str | None) -> str:
    """The parenthetical ticker clause for the guard/prompt copy — the derived
    ``graded_sell_record`` line's ticker list when available, else the generic
    phrase (never a hardcoded name list). The ticker parenthetical is the
    line's only parenthesised clause (the sparse-n hedge is a dash clause), so
    an unanchored search finds it whether or not the hedge follows."""
    if graded_line is None:
        return _GENERIC_SELL_PATTERN_LINE
    match = re.search(r"\(([^)]+)\)", graded_line)
    return f"sell-winners-too-early pattern ({match.group(1)})" if match else graded_line


# The canonical key for the seed's rule 1 (sell-winners-too-early) — the ONE
# behavioral fact whose evidence clause re-interpolates the LIVE
# graded_sell_record on every render, matching what the frozen seed text
# always did. Any OTHER affirmed behavioral fact renders its own affirmed
# narrative verbatim (tenet-2 Phase 4, §3.2 Tier C: "evidence counts still
# interpolated live... where the fact's slug matches").
SELL_WINNERS_KEY = "behavior.sell_winners_early"


def seed_behavioral_rules(graded_line: str | None) -> str:
    """The ORIGINAL frozen five-rule seed text (distilled once from
    ``data/ledger_seed/seed.json``), rendered byte-for-byte identical to
    before :func:`behavioral_rules_block` became a renderer. This is the
    fallback the renderer falls back to while zero behavioral
    ``owner_profile_facts`` are AFFIRMED — today's default, nothing has been
    ratified yet — so every existing prompt-hash/cache invariant over this
    block survives untouched. Rule 1's evidence still interpolates from the
    LIVE graded record rather than a stale hardcode, exactly as before this
    module split the renderer out."""
    sell_evidence = (
        f"his live graded record: {graded_line}"
        if graded_line is not None
        else "his self-diagnosed sell-winners-too-early flaw"
    )
    return f"""\
Calibrate the verdict to THIS owner's documented decision-making:
1. SELL-WINNERS-TOO-EARLY is his dominant flaw — {sell_evidence}. On an INTACT,
   high-conviction name, "it has run" or "it looks expensive on the tape" is NEVER a
   sufficient reason to trim. The ONLY price-agnostic reason to trim a healthy name is
   SIZING: weight above its target band, or an oversized single-name concentration.
2. LEAP OVERLAY is his prescribed antidote — when he wants less capital at risk on a name he
   still believes in, prefer keep-the-core + a far-OTM long-dated LEAP over an outright trim
   (suggested_expression="LEAP-overlay").
3. CATALYST TEST for adds — only add into weakness when there is a near-term catalyst AND a
   priced-in floor; cheap-without-catalyst is a value trap.
4. INSTRUMENT-SELECTION — express macro/sector themes via an index; hold dry powder in
   T-bills/SGOV; express single-name conviction via LEAPs.
5. KEEP THE VERDICT WITH THE FRAMEWORK, NOT THE FEELING — ground the call in the break-rule
   status and the DCF valuation ladder. If the framework is intact, the name is not
   overvalued, and it is not oversized, the answer is HOLD even if the position feels
   uncomfortable after a big run.
"""


def _affirmed_behavioral_rows(db_path: Path | str | None) -> list[OwnerProfileFactRow]:
    """AFFIRMED ``owner_profile_facts`` (category='behavioral'), oldest id
    first — the live rows the renderer switches to once the owner ratifies at
    least one (§7.1 gated assertion: nothing else may condition the prompt).
    Degrades to ``[]`` on no ``db_path``, a missing DB, a pre-0159 substrate,
    or any read error — never raises, matching every other anchor/profile
    reader in this codebase (e.g. ``llm.anchors.load_owner_profile_anchor``)."""
    if db_path is None or not Path(db_path).exists():
        return []
    try:
        from owner_profile.store import get_current_profile

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            grouped = get_current_profile(conn)
        finally:
            conn.close()
    except Exception as exc:  # missing table / locked DB / anything — degrade
        log.debug({"event": "behavioral_rules_profile_load_failed", "error": str(exc)})
        return []
    rows = grouped.get("behavioral", [])
    return sorted(rows, key=lambda r: r.id)


def behavioral_rules_block(graded_line: str | None, *, db_path: Path | str | None = None) -> str:
    """The behavioral-rules prompt block — a RENDERER over the owner's
    AFFIRMED behavioral ``owner_profile_facts``, not a frozen constant
    (tenet-2 Phase 4, ``docs/design/tenet2_advisory_program.md`` §3.2 Tier C /
    §7 ruling 3: "Live-derived, ratification-gated"). The deterministic
    guardrail (:func:`apply_behavioral_guard`) remains the backstop that
    ENFORCES the sell-winners rule regardless of what the model does or which
    rows are currently affirmed — only the PROMPT TEXT goes live here.

    Zero affirmed behavioral facts (today's default) renders
    :func:`seed_behavioral_rules` byte-for-byte identical to the original
    frozen string. The FIRST affirmed behavioral fact switches the entire
    block over to the live rows: each affirmed fact's narrative becomes one
    numbered rule, except :data:`SELL_WINNERS_KEY`, whose evidence
    re-interpolates the LIVE ``graded_sell_record`` line on every call rather
    than the snapshot baked into the fact at affirmation time.
    """
    rows = _affirmed_behavioral_rows(db_path)
    if not rows:
        return seed_behavioral_rules(graded_line)

    lines = [
        "Calibrate the verdict to THIS owner's documented decision-making "
        "(live, owner-ratified behavioral rules):"
    ]
    for i, row in enumerate(rows, start=1):
        if row.key == SELL_WINNERS_KEY:
            sell_evidence = (
                f"his live graded record: {graded_line}"
                if graded_line is not None
                else "his self-diagnosed sell-winners-too-early flaw"
            )
            lines.append(f"{i}. SELL-WINNERS-TOO-EARLY is his dominant flaw — {sell_evidence}.")
        else:
            lines.append(f"{i}. {row.narrative}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class VerdictOutput:
    """The reasoned trim/hold/add recommendation. ``verdict`` is validated
    against ``advisor.store.STANCES`` so it persists as a P2.5-scoreable stance.
    ``verdict_source`` records whether the deterministic behavioral guard
    overrode the model ("guard_override") or the name was unencoded
    ("encode_first")."""

    verdict: str  # in STANCES: buy | add | hold | trim | sell
    size: str
    reason: str
    confidence: str  # in CONFIDENCES
    behavioral_check: str
    suggested_expression: str  # in SUGGESTED_EXPRESSIONS
    verdict_source: str = "llm"  # "llm" | "guard_override" | "encode_first"


@dataclass(frozen=True)
class PositionReview:
    """A full review: the grounded pre-analysis, the verdict, and the persisted
    memo id (None when persistence was skipped or no DB was configured)."""

    pre: PreAnalysis
    output: VerdictOutput
    memo_id: int | None


def _req_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"position_review output field {key!r} must be a non-empty string")
    return value.strip()


def _coerce_choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    """Normalize a soft enum field to a member of ``allowed`` (default on miss).
    Softer than ``verdict`` — a bad confidence/expression degrades to a safe
    default rather than raising, because it never changes the stance itself."""
    norm = value.strip() if isinstance(value, str) else ""
    return norm if norm in allowed else default


def parse_verdict_output(payload: dict[str, object]) -> VerdictOutput:
    """Validate the LLM's JSON into a :class:`VerdictOutput`.

    ``verdict`` MUST be in ``STANCES`` (raises otherwise — an out-of-vocabulary
    stance isn't scoreable and must fail loudly, not silently degrade). The soft
    fields coerce to safe defaults.
    """
    verdict = _req_str(payload, "verdict").lower()
    if verdict not in STANCES:
        raise ValueError(f"position_review verdict {verdict!r} not in {STANCES}")
    return VerdictOutput(
        verdict=verdict,
        size=_req_str(payload, "size"),
        reason=_req_str(payload, "reason"),
        confidence=_coerce_choice(payload.get("confidence"), CONFIDENCES, "low"),
        behavioral_check=_req_str(payload, "behavioral_check"),
        suggested_expression=_coerce_choice(
            payload.get("suggested_expression"), SUGGESTED_EXPRESSIONS, "do-nothing"
        ),
    )


def _tax_cost_phrase(tax: PositionTaxView | None) -> str | None:
    """Compact dollar phrase for the proposed trim's tax cost — ``~$6,200`` or
    ``~$1,200-$2,000 (term unknown)`` — or None when there is nothing to say
    (no view, no trim estimate, or a zero/negative bill)."""
    if tax is None or not tax.available or tax.trim is None:
        return None
    trim = tax.trim
    if trim.tax_high_usd <= 0:
        return None
    if abs(trim.tax_high_usd - trim.tax_low_usd) < 0.5:
        return f"~${trim.tax_low_usd:,.0f}"
    return f"~${trim.tax_low_usd:,.0f}-${trim.tax_high_usd:,.0f} (term unknown)"


def apply_behavioral_guard(
    pre: PreAnalysis, out: VerdictOutput, *, graded_line: str | None = None
) -> VerdictOutput:
    """Enforce the sell-winners-too-early rule deterministically.

    A ``trim``/``sell`` on a thesis that is intact-or-warn, NOT over-valued, and
    NOT oversized is a price-only trim on a healthy name — the owner's signature
    mistake. Override it to ``hold`` and say why. Legitimate trims survive
    untouched: a breached thesis, an over-valued name (ladder trim/sell), an
    above-band weight, or an oversized concentration all justify trimming.

    "Oversized" (PRD §7.2, P0.2, rule 6) requires BOTH a concentrated-or-higher
    zone AND an intentional add — reaching a zone through price appreciation
    alone is explicitly NOT a sizing justification, so the guard's winner
    protection still applies to a name that simply ran. This is the reason
    zone status can never bypass the guard on its own: a zone only signals
    that a hold-vs-trim assessment is due (:data:`allocation.concentration
    .ZoneAssessment.trim_assessment_required`), not that a trim is justified.

    ``graded_line`` is the caller-supplied output of :func:`graded_sell_record`
    (built ONCE per review, not re-queried here) — the override text names the
    live wrong-graded tickers when available and falls back to a generic
    "sell-winners-too-early pattern" phrase (no hardcoded names) otherwise.

    When a tax view is on the pre-analysis, an override cites the trim's tax
    cost as supporting rationale — the tax never *drives* the verdict (that
    stays framework-only), it corroborates the hold.
    """
    if out.verdict not in ("trim", "sell"):
        return out
    thesis_healthy = pre.break_rule_status in ("intact", "warn")
    not_overvalued = pre.valuation_verdict in ("fair", "buy", "n/a")
    sizing_justified = pre.weight_vs_band == "above_band" or (
        pre.weight_vs_band == "no_band"
        and pre.concentration_zone is not None
        and zone_at_least(pre.concentration_zone, "concentrated")
        and pre.entry_method == ENTRY_INTENTIONAL
    )
    if thesis_healthy and not_overvalued and not sizing_justified:
        cost = _tax_cost_phrase(pre.tax)
        tax_support = f" Trimming now would also cost {cost} in taxes." if cost else ""
        pattern = _sell_pattern_phrase(graded_line)
        return replace(
            out,
            verdict="hold",
            reason=f"[behavioral guard] Overrode a price-only {out.verdict} on an intact, "
            f"non-oversized, non-overvalued name.{tax_support} {out.reason}",
            behavioral_check=(
                f"This is the {pattern}: trimming a healthy, non-oversized name that the "
                "framework says is not expensive. Hold the core; if you want less capital "
                f"at risk, use a LEAP overlay. {out.behavioral_check}"
            ),
            suggested_expression="do-nothing",
            verdict_source="guard_override",
        )
    return out


def encode_first_output(pre: PreAnalysis) -> VerdictOutput:
    """Deterministic degraded verdict for a name with no encoded thesis (FLKR):
    there is no framework to ground a call, so recommend encoding one rather than
    fabricating a fundamentals verdict."""
    index_note = (
        "This is an index instrument — a macro/factor sleeve, not a single-name thesis. "
        if pre.is_index_instrument
        else ""
    )
    return VerdictOutput(
        verdict="hold",
        size="none",
        reason=(
            f"No encoded thesis for {pre.ticker} (no holdings JSON, no DCF, no break-rules), "
            f"so there is no framework to ground a trim/add call. {index_note}"
            "Your own note tags it low-conviction."
        ),
        confidence="low",
        behavioral_check=(
            "Can't check this against your framework because the conviction isn't encoded. "
            "If you hold it on a real single-name thesis, encode it so the next review is "
            "grounded — and don't trim on price alone (the sell-winners-too-early reflex)."
        ),
        suggested_expression="encode-thesis-first",
        verdict_source="encode_first",
    )


def _convictions_block(repo_root: Path, ticker: str, db_path: Path | str | None) -> str:
    """Assemble the owner's captured convictions for ``ticker`` (synthesized
    stance + recent decisions/musings), spotlighted as untrusted input before it
    reaches the prompt (stance text chains LLM-synthesized content)."""
    from llm.untrusted import spotlight
    from synthesis.insights import list_insights
    from user_state.notes import list_notes

    parts: list[str] = []
    stances = list_insights(kind="stance", scope_key=ticker, status="current", db_path=db_path)
    if stances:
        parts.append(f"STANCE (synthesized): {stances[0].body_md}")
    for note in list_notes(ticker=ticker, kind="decision", db_path=db_path)[:3]:
        tail = ""
        if note.context:
            conv = note.context.get("conviction")
            fals = note.context.get("falsifier")
            bits = [
                b
                for b in (
                    f"conviction={conv}" if conv else "",
                    f"falsifier: {fals}" if fals else "",
                )
                if b
            ]
            tail = f" [{'; '.join(bits)}]" if bits else ""
        parts.append(f"DECISION: {note.body}{tail}")
    for note in list_notes(ticker=ticker, kind="musing", db_path=db_path)[:5]:
        parts.append(f"MUSING: {note.body}")
    raw = "\n\n".join(parts)
    return spotlight(raw, source="the owner's own captured convictions (the Ledger)")


def _risk_prompt_block(risk: RiskContext | None) -> str:
    """Compact ``RISK: ...`` line for the verdict prompt — mirrors the tax/
    capacity legs' degrade contract: ``risk=None`` or an all-empty
    :class:`RiskContext` renders nothing (``""``), never a placeholder line,
    so a fully-degraded risk leg leaves the prompt byte-identical to before
    C7."""
    if risk is None or risk.is_empty():
        return ""
    bits: list[str] = []
    if risk.risk_share_pct is not None:
        bits.append(f"{risk.risk_share_pct:.0f}% of book risk")
    if risk.corr_to_book is not None:
        bits.append(f"corr-to-book {risk.corr_to_book:+.2f}")
    if risk.crowding_cluster:
        bits.append(f"cluster: {risk.crowding_cluster}")
    if risk.top_factors:
        bits.append(
            "factors: "
            + ", ".join(f"{factor} {loading:.1f}" for factor, loading in risk.top_factors)
        )
    if risk.event_scenarios:
        bits.append("scenarios: " + ", ".join(risk.event_scenarios))
    return f"RISK: {'; '.join(bits)}\n" if bits else ""


def _build_verdict_prompt(
    pre: PreAnalysis,
    convictions_block: str,
    *,
    graded_line: str | None = None,
    owner_profile_anchor: str = "",
    db_path: Path | str | None = None,
) -> str:
    zone_facts = (
        f"concentration_zone={pre.concentration_zone or 'n/a'} (weight {pre.weight_pct}%; "
        f"{pre.zone_treatment or 'n/a'}; entry: {pre.entry_method or 'unknown'})\n"
        "A zone never forces a trim; a trim needs an additional reason (thesis impairment, "
        "poor forward risk/reward, correlation/capacity pressure, an affirmed limit, or a "
        "superior alternative).\n"
    )
    facts = (
        f"Ticker: {pre.ticker}\n"
        f"Weight: {pre.weight_pct}% of book (source: {pre.weight_source}); "
        f"market value {pre.market_value_usd}; unrealized P&L {pre.unrealized_pnl_usd}\n"
        f"Target band: {pre.target_band} -> {pre.weight_vs_band}\n"
        f"{zone_facts}"
        f"Break-rule status: {pre.break_rule_status}; "
        f"tripped/watch: {list(pre.tripped_rules) or 'none'}\n"
        f"Thesis verdict label: {pre.verdict_label}; key driver: {pre.key_driver}\n"
        f"Valuation: DCF fair value {pre.npv_per_share}, asked-at price {pre.at_price}, "
        f"over/under {pre.dcf_gap_pct}% (+ = over-valued), mos_bar {pre.mos_bar}; "
        f"ladder verdict: {pre.valuation_verdict}\n"
        f"Conviction encoded: {pre.conviction_encoded}\n"
        f"{_risk_prompt_block(pre.risk)}"
    )
    profile_block = f"{owner_profile_anchor}\n" if owner_profile_anchor else ""
    return (
        "You are the owner's position-review analyst. Using the GROUNDED FACTS "
        "(deterministic, computed from the platform) and the owner's OWN convictions, "
        "return a single trim/hold/add/sell verdict for this ONE position.\n\n"
        f"{behavioral_rules_block(graded_line, db_path=db_path)}\n"
        f"{profile_block}"
        f"## GROUNDED FACTS\n{facts}\n"
        f"## THE OWNER'S CONVICTIONS ON THIS NAME\n{convictions_block or '(none on file)'}\n\n"
        "## OUTPUT — return ONLY a JSON object with exactly these keys:\n"
        f'  "verdict": one of {list(STANCES)} (his exact vocabulary)\n'
        '  "size": short phrase, e.g. "trim ~2pp to top of band" or "none"\n'
        '  "reason": the SPECIFIC driver — which break-rule/threshold, which sizing band, '
        "or which DCF ladder step\n"
        f'  "confidence": one of {list(CONFIDENCES)}\n'
        '  "behavioral_check": one sentence naming which of HIS documented patterns this '
        "decision risks repeating\n"
        f'  "suggested_expression": one of {list(SUGGESTED_EXPRESSIONS)}\n'
        "No markdown fences, no prose outside the JSON object.\n"
    )


def _render_memo_body(pre: PreAnalysis, out: VerdictOutput) -> str:
    lines = [
        f"**{pre.ticker}** — position-review verdict: **{out.verdict}** "
        f"({out.confidence} confidence)",
        "",
        f"- Size: {out.size}",
        f"- Reason: {out.reason}",
        f"- Suggested expression: {out.suggested_expression}",
        f"- Behavioral check: {out.behavioral_check}",
        "",
        f"Grounded facts — weight {pre.weight_pct}% ({pre.weight_source}); "
        f"break-rules {pre.break_rule_status}; valuation {pre.valuation_verdict} "
        f"(over/under {pre.dcf_gap_pct}%); concentration_flag={pre.concentration_flag}.",
    ]
    # The tax block persists with the memo whichever way the verdict went, so
    # P2.5 grading sees the cost the decision was made against.
    tax_lines = render_tax_lines(pre.tax)
    if tax_lines:
        lines.append("")
        lines.extend(tax_lines)
    if out.verdict_source != "llm":
        lines.append(f"\n_verdict source: {out.verdict_source}_")
    lines.append(f"\nSTANCE: {out.verdict}")
    return "\n".join(lines)


def _tax_context(tax: PositionTaxView | None) -> dict[str, object] | None:
    """JSON-safe tax summary for the memo ``context`` (grading + audits)."""
    if tax is None or not tax.available:
        return None
    trim = tax.trim
    out: dict[str, object] = {
        "approximate": tax.approximate,
        "taxable_pct_of_position": tax.taxable_pct_of_position,
        "st_unrealized_usd": tax.st_unrealized_usd,
        "lt_unrealized_usd": tax.lt_unrealized_usd,
    }
    if trim is not None:
        out.update(
            trim_rationale=trim.trim_rationale,
            trim_usd=trim.trim_usd,
            est_tax_low_usd=trim.tax_low_usd,
            est_tax_high_usd=trim.tax_high_usd,
            days_until_lt=trim.days_until_lt,
            wait_savings_usd=trim.wait_savings_usd,
            wash_sale_risk=trim.wash_sale_risk,
        )
    return out


def attest_review_changed(
    db_path: Path | str | None, memo_id: int, *, user_id: str = DEFAULT_USER_ID
) -> bool:
    """Record the owner's explicit attestation that a position-review memo
    changed their call — merges ``owner_attested_change=True`` into the memo's
    ``context_json``.

    This is the SOLE input that moves the Coach P&L's Q3'26 "changed >= 1" bar:
    the silence-implies-heeded window heuristic feeds only the separate
    "candidate" line, never the target counter (see
    ``pipeline.allocation_decisions_panel._coach_change_tally``). So attestation
    must be an explicit owner action, not something an agent run or the passage
    of time can synthesize.

    Returns True when a matching, not-yet-attested ``position_review`` memo owned
    by ``user_id`` was updated; False on any miss (unknown / other-kind /
    other-owner memo, already attested, or a DB/JSON error). Never raises and
    never fabricates a positive — the counter it feeds must stay honest.
    """
    from db_paths import resolve_db_path

    resolved = resolve_db_path(db_path)
    if resolved is None or not Path(resolved).exists():
        return False
    try:
        conn = connect_sqlite(
            resolved,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT context_json FROM advisor_memos WHERE id = ? AND user_id = ? AND kind = ?",
            (memo_id, user_id, POSITION_REVIEW_PURPOSE),
        ).fetchone()
        if row is None:
            return False
        ctx: dict[str, object] = {}
        if row[0]:
            try:
                parsed = json.loads(str(row[0]))
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                ctx = cast("dict[str, object]", parsed)
        if ctx.get(OWNER_ATTESTED_KEY) is True:
            return False  # idempotent — a second click is not a second changed decision
        ctx[OWNER_ATTESTED_KEY] = True
        conn.execute(
            "UPDATE advisor_memos SET context_json = ? WHERE id = ?", (json.dumps(ctx), memo_id)
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _persist_review(
    pre: PreAnalysis,
    out: VerdictOutput,
    *,
    user_id: str,
    db_path: Path | str | None,
    source: str,
) -> int | None:
    from advisor.memos import DEFAULT_HORIZON_DAYS, persist_memo
    from db_paths import resolve_db_path

    resolved = resolve_db_path(db_path)
    if resolved is None:
        return None
    context: dict[str, object] = {
        "weight_pct": pre.weight_pct,
        "dcf_gap_pct": pre.dcf_gap_pct,
        "break_rule_status": pre.break_rule_status,
        "valuation_verdict": pre.valuation_verdict,
        "confidence": out.confidence,
        "suggested_expression": out.suggested_expression,
        "verdict_source": out.verdict_source,
        "at_price": pre.at_price,
        # Which surface ran this review — the Coach P&L excludes 'agent' so a
        # verification/CI run never enters the owner-facing scoreboard.
        REVIEW_SOURCE_KEY: source,
    }
    tax_context = _tax_context(pre.tax)
    if tax_context is not None:
        context["tax"] = tax_context
    result = persist_memo(
        db_path=resolved,
        user_id=user_id,
        kind=POSITION_REVIEW_PURPOSE,
        ticker=pre.ticker,
        counter_ticker=None,
        title=f"Position review: {pre.ticker} -> {out.verdict}",
        body_md=_render_memo_body(pre, out),
        context=context,
        write_ledger=True,
        stance=out.verdict,
        horizon_days=DEFAULT_HORIZON_DAYS,
    )
    return result.memo_id


def review_position(
    repo_root: Path,
    ticker: str,
    *,
    at_price: float | None = None,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    db_path: Path | str | None = None,
    persist: bool = True,
    source: str = AGENT_SOURCE,
) -> PositionReview:
    """End-to-end position review: grounded pre-analysis -> governed LLM verdict
    -> deterministic behavioral guard -> persisted memo.

    A name with no encoded thesis short-circuits to a deterministic low-confidence
    "encode a thesis first" verdict WITHOUT an LLM call — there is nothing to
    ground a fundamentals call on. Otherwise the verdict is produced by the
    governed ``position_review`` LLM purpose (model-picked, cost-logged,
    schema-validated) and then passed through the behavioral guard.

    ``source`` (one of :data:`REVIEW_SOURCES`) tags the persisted memo with the
    surface that ran it. It DEFAULTS to ``agent`` — the integrity-safe default —
    so a library/CLI/test invocation never silently inflates the owner-facing
    Coach P&L; owner surfaces (the Holding-band doorway, the Ask/Telegram
    ``/review`` command, a hand-run CLI) opt in with their own source.
    """
    pre = build_pre_analysis(
        repo_root, ticker, at_price=at_price, user_id=user_id, api_url=api_url, db_path=db_path
    )
    if not pre.thesis_present:
        out = encode_first_output(pre)
    else:
        from db_paths import resolve_db_path
        from llm.structured import call_llm_structured

        # Built ONCE per review and threaded into both the prompt and the guard
        # override, so the model's rationale and the deterministic backstop
        # always cite the same live count (never a stale hardcode, never a
        # query race between the two call sites). Also reused for the
        # behavioral-rules renderer's affirmed-facts read (§3.2 Tier C) — one
        # resolved DB path, one live count, everywhere it's needed this review.
        resolved_db_path = resolve_db_path(db_path)
        graded_line = graded_sell_record(resolved_db_path)
        # Owner-profile anchor (§4 delivery seam 2 of tenet2_advisory_program.md).
        # This site composes the prompt by hand rather than via
        # compose_anchor_block, so spotlight-wrap here (same pattern as
        # chat_session.py's hand-composed worldview block) — a load failure
        # must never break the review, so it degrades to "" like every other
        # anchor loader.
        try:
            from llm.anchors import load_owner_profile_anchor
            from llm.untrusted import spotlight

            raw_owner_profile_anchor = load_owner_profile_anchor(repo_root)
            owner_profile_anchor = (
                spotlight(raw_owner_profile_anchor, source="the owner's affirmed profile facts")
                if raw_owner_profile_anchor
                else ""
            )
        except Exception as exc:
            log.debug({"event": "owner_profile_anchor_load_failed", "error": str(exc)})
            owner_profile_anchor = ""
        prompt = _build_verdict_prompt(
            pre,
            _convictions_block(repo_root, pre.ticker, db_path),
            graded_line=graded_line,
            owner_profile_anchor=owner_profile_anchor,
            db_path=resolved_db_path,
        )
        # db_path= keeps the call's DB-backed layers (llm_calls cost ledger,
        # budget, pins) on the caller's DB — without it a library invocation
        # with an explicit db_path and no db.set_db_path bootstrap logged its
        # cost row against db.DB_PATH ("no such table: llm_calls").
        payload = call_llm_structured(
            prompt,
            purpose=POSITION_REVIEW_PURPOSE,
            ticker=pre.ticker,
            required_keys=_VERDICT_KEYS,
            schema=_VERDICT_ADAPTER,
            db_path=db_path,
        )
        payload = _VerdictWire.model_validate(payload).model_dump()
        out = apply_behavioral_guard(
            pre, parse_verdict_output(cast("dict[str, object]", payload)), graded_line=graded_line
        )

    memo_id = (
        _persist_review(pre, out, user_id=user_id, db_path=db_path, source=source)
        if persist
        else None
    )
    return PositionReview(pre=pre, output=out, memo_id=memo_id)


# --------------------------------------------------------------------------- #
# Slice 3 — deterministic chat rendering (the instant, no-LLM /review surface)
# --------------------------------------------------------------------------- #


def mechanical_read(pre: PreAnalysis) -> str:
    """A one-line deterministic trim/hold read mirroring the behavioral guard —
    no LLM. This is what the instant ``/review`` command shows; the LLM-calibrated
    narrative comes from :func:`review_position`."""
    if not pre.thesis_present:
        kind = "an index / macro sleeve" if pre.is_index_instrument else "unencoded"
        return f"No encoded thesis ({kind}) — encode one first; don't trim on price alone."
    if pre.break_rule_status == "breach":
        return "Thesis BREACHED — reassess; a legitimate trim/exit candidate."
    if pre.valuation_verdict in ("trim", "sell"):
        return f"Over-valued (ladder: {pre.valuation_verdict}) — valuation supports trimming."
    sizing_oversized = pre.weight_vs_band == "above_band" or (
        pre.weight_vs_band == "no_band" and pre.concentration_flag
    )
    if sizing_oversized:
        return (
            "Framework intact but the position is OVERSIZED — trim-to-target is the sizing "
            "case (keep the core; a LEAP keeps the upside)."
        )
    return (
        "HOLD — framework intact, not over-valued, not oversized. Trimming here would be "
        "price-only (your sell-winners-too-early trap)."
    )


def render_tax_lines(tax: PositionTaxView | None, *, plain: bool = False) -> list[str]:
    """Markdown bullets for the tax block — shared by the chat reply, the CLI
    summary, and the persisted memo body. Empty for a pre-tax-stage
    :class:`PreAnalysis` (``tax=None``); a single honest line when the tracker
    couldn't supply a view; the dense block otherwise.

    ``plain=True`` (the Telegram surface) drops the ``_footnote_`` Markdown
    italics — the one marker the caller's plain-ASCII pass CAN'T strip, since an
    underscore is ASCII and indistinguishable from data. The unicode separators
    (``→``/``·``/``—``) are left as-is for :func:`render_pre_analysis_plain`'s
    single ASCII pass to normalize. The default Markdown form is unchanged for
    the web chat, the CLI, and the persisted memo, which render Markdown."""
    if tax is None:
        return []
    if not tax.available:
        return [f"- Tax: unavailable ({tax.reason})"]
    approx = " (approx)" if tax.approximate else ""
    pct = f"{tax.taxable_pct_of_position:.0f}%" if tax.taxable_pct_of_position is not None else "—"
    if tax.st_unrealized_usd is not None and tax.lt_unrealized_usd is not None:
        embedded = (
            f"embedded gain ST ${tax.st_unrealized_usd:,.0f} / LT ${tax.lt_unrealized_usd:,.0f}"
        )
    else:
        embedded = "term split unknown"
    lines = [f"- Tax{approx}: taxable {pct} of position · {embedded}"]
    trim = tax.trim
    if trim is not None:
        cost = _tax_cost_phrase(tax) or "$0"
        seg = f"- Proposed trim ({trim.trim_rationale}): ~${trim.trim_usd:,.0f} → est. tax {cost}"
        if (
            trim.days_until_lt is not None
            and trim.wait_savings_usd is not None
            and trim.wait_savings_usd > 0
        ):
            seg += f" · waiting {trim.days_until_lt}d (ST→LT) saves ~${trim.wait_savings_usd:,.0f}"
        if trim.wash_sale_risk:
            seg += " · wash-sale risk (buy within 30d of a loss lot)"
        lines.append(seg)
    if tax.sheltered_note:
        lines.append(f"- Placement: {tax.sheltered_note}")
    if tax.approximate and tax.approx_reasons:
        lines.append(f"- Tax approx because: {'; '.join(tax.approx_reasons[:2])}")
    if tax.footnote:
        lines.append(f"- {tax.footnote}" if plain else f"- _{tax.footnote}_")
    return lines


def _render_zone(pre: PreAnalysis) -> str:
    """The concentration-zone read for the /review renders (PRD §7.2, P0.2) —
    e.g. ``"concentrated (13.4%)"``. ``"n/a"`` when no weight is known."""
    if pre.concentration_zone and pre.weight_pct is not None:
        return f"{pre.concentration_zone} ({pre.weight_pct:.1f}%)"
    return pre.concentration_zone or "n/a"


def render_pre_analysis_chat(pre: PreAnalysis, *, db_path: Path | str | None = None) -> str:
    """Compact Markdown reply for the ``/review`` chat command — the grounded
    facts plus the mechanical read, the tax block, the live graded-sells base
    rate (when any sells/trims are graded yet), and a pointer to the full
    calibrated verdict. ``db_path`` is optional — omitted, the base-rate line
    simply doesn't render (same degrade as a fresh/ungraded DB)."""
    wt = f"{pre.weight_pct:.1f}%" if pre.weight_pct is not None else "—"
    conc = _render_zone(pre)
    fv = f"${pre.npv_per_share:,.2f}" if pre.npv_per_share is not None else "—"
    asked = f"${pre.at_price:,.2f}" if pre.at_price is not None else "—"
    gap = f"{pre.dcf_gap_pct:+.1f}%" if pre.dcf_gap_pct is not None else "—"
    watch = f" · watch: {'; '.join(pre.tripped_rules)}" if pre.tripped_rules else ""
    at_flag = f" --at-price {pre.at_price:g}" if pre.at_price is not None else ""
    graded_line = graded_sell_record(db_path)
    return "\n".join(
        [
            f"**{pre.ticker}** — position review (deterministic read; weight source: "
            f"{pre.weight_source})",
            f"- Sizing: {wt} of book · concentration: {conc} · band: {pre.weight_vs_band}",
            f"- Fundamentals: thesis {pre.verdict_label or '—'} · "
            f"break-rules {pre.break_rule_status}{watch}",
            f"- Valuation: fair {fv} vs asked {asked} → {gap} (+ = over-valued) · "
            f"ladder: {pre.valuation_verdict}",
            *render_tax_lines(pre.tax),
            *render_capacity_lines(pre.capacity),
            *render_risk_lines(pre.risk),
            f"- Mechanical read: {mechanical_read(pre)}",
            *([f"- {graded_line}"] if graded_line is not None else []),
            f"- Full calibrated verdict (LLM + behavioral guard): "
            f"`python execution/review_position.py {pre.ticker}{at_flag}`",
        ]
    )


# Non-ASCII glyphs the Markdown renderers use for polish (em/en dashes, arrow,
# middot, ellipsis) mapped to their ASCII equivalents. Applied once to the
# assembled Telegram reply so it is truly plain-ASCII regardless of which shared
# helper (mechanical_read, render_tax_lines, a break-rule detail, ...) produced
# the glyph -- one normalization point instead of ASCII-handling scattered across
# every helper. send_message sets no parse_mode, so this is cosmetic on Telegram;
# the pass just keeps the plain surface consistent and dependable. Keys use chr()
# code points so the source carries no ambiguous-character glyphs (ruff RUF001).
_PLAIN_ASCII: dict[str, str] = {
    chr(0x2014): "-",  # em dash
    chr(0x2013): "-",  # en dash
    chr(0x2192): "->",  # right arrow
    chr(0x00B7): "-",  # middle dot
    chr(0x2026): "...",  # ellipsis
}


def _to_plain_ascii(text: str) -> str:
    """Fold the cosmetic non-ASCII glyphs in ``text`` to ASCII (see ``_PLAIN_ASCII``)."""
    for glyph, ascii_equiv in _PLAIN_ASCII.items():
        text = text.replace(glyph, ascii_equiv)
    return text


def render_pre_analysis_plain(pre: PreAnalysis, *, db_path: Path | str | None = None) -> str:
    """Plain-ASCII reply for the ``/review`` command on Telegram — the SAME
    grounded facts as :func:`render_pre_analysis_chat` (including the live
    graded-sells base rate), with no ``**``/backtick Markdown markers
    (Telegram's ``send_message`` never sets ``parse_mode``, so a
    Markdown-shaped body arrives as a raw text wall — literal asterisks and
    backticks). Also drops the CLI pointer line: ``python execution/...`` is a
    broken instruction in a chat channel with no shell. Ends instead with a
    pointer to the calibrated review surface in the web app.

    A final :func:`_to_plain_ascii` pass folds the cosmetic non-ASCII glyphs the
    shared helpers emit (``mechanical_read``'s em dashes, the tax block's ``→``/
    ``·``) so the whole reply is plain-ASCII, and ``render_tax_lines(plain=True)``
    drops the one Markdown marker that pass can't (the ``_footnote_`` italics)."""
    wt = f"{pre.weight_pct:.1f}%" if pre.weight_pct is not None else "-"
    conc = _render_zone(pre)
    fv = f"${pre.npv_per_share:,.2f}" if pre.npv_per_share is not None else "-"
    asked = f"${pre.at_price:,.2f}" if pre.at_price is not None else "-"
    gap = f"{pre.dcf_gap_pct:+.1f}%" if pre.dcf_gap_pct is not None else "-"
    watch = f" - watch: {'; '.join(pre.tripped_rules)}" if pre.tripped_rules else ""
    graded_line = graded_sell_record(db_path)
    body = "\n".join(
        [
            f"{pre.ticker} - position review (deterministic read; weight source: "
            f"{pre.weight_source})",
            f"- Sizing: {wt} of book - concentration: {conc} - band: {pre.weight_vs_band}",
            f"- Fundamentals: thesis {pre.verdict_label or '-'} - "
            f"break-rules {pre.break_rule_status}{watch}",
            f"- Valuation: fair {fv} vs asked {asked} -> {gap} (+ = over-valued) - "
            f"ladder: {pre.valuation_verdict}",
            *render_tax_lines(pre.tax, plain=True),
            *render_capacity_lines(pre.capacity),
            *render_risk_lines(pre.risk),
            f"- Mechanical read: {mechanical_read(pre)}",
            *([f"- {graded_line}"] if graded_line is not None else []),
            "- Full calibrated review: from the desk (Holding -> Review).",
        ]
    )
    return _to_plain_ascii(body)


def _resolve_review_target(body: str, roster: RosterIndex | None) -> str:
    """Resolve the free-text name/symbol after ``/review`` to a canonical ticker.

    Prefers a roster name/alias match — ``Rubrik`` → ``RBRK``, ``Nubank`` →
    ``NU``, multi-word ``Novo Nordisk`` → ``NVO`` — via the same deterministic
    ``capture.matcher`` lanes the capture pipeline uses (so the two never drift).
    Falls back to the first token treated as a literal symbol (uppercased,
    leading ``$`` stripped, then canonicalized through ``capture.matcher``'s
    index builder for symbol aliases like ``GOOGL`` → ``GOOG``) when the roster
    has no name match — so a typed ticker still resolves and an un-rostered name
    still reviews as typed.
    """
    from capture.matcher import build_roster_index, match_ticker

    if roster is not None:
        matched = match_ticker(body, roster).ticker
        if matched is not None:
            return matched
    # Fall back to the first token as a literal symbol, canonicalized (GOOGL ->
    # GOOG) through the same typed index builder the roster uses — this reuses
    # capture.matcher's symbol-alias boundary instead of touching the untyped
    # alias_manager directly.
    tokens = body.split()
    raw = (tokens[0] if tokens else body).upper().lstrip("$")
    canonical = build_roster_index(symbols=[raw]).symbol_to_ticker
    return next(iter(canonical.values()), raw)


def parse_review_command(
    text: str, *, roster: RosterIndex | None = None
) -> tuple[str, float | None] | None:
    """Parse ``/review <NAME|TICKER> [at $PRICE]`` into ``(ticker, at_price)``.

    Returns ``None`` when nothing follows the command (the usage-message case).
    The ONE parser for this command shape — shared by ``review_reply_text`` (the
    instant Slice-1 reply), ``ask.commands``'s background Slice-2 kickoff, and
    the Telegram poller — so the at-price regex and the ticker-resolution rule
    can never drift between callers.

    With a ``roster`` (the prod/Telegram/web surfaces all supply one via
    :func:`resolve_review_target`) the target is resolved through the roster's
    name/alias lane, so a company NAME resolves to its ticker: ``/review Rubrik``
    → ``RBRK``, ``/review Nubank at $12`` → ``(NU, 12.0)``, ``/review Novo
    Nordisk`` → ``NVO``. Without a roster the target is the first token used
    as-typed (uppercased, leading ``$`` stripped) — the pre-resolution behavior.
    """
    parts = text.split()
    if len(parts) < 2:
        return None
    match = _AT_PRICE_RX.search(text)
    at_price = float(match.group(1)) if match else None
    # The name/symbol portion is everything after the command word, minus the
    # trailing "at $X" clause — so a multi-word name ("Novo Nordisk") survives
    # while the price level is still stripped out before resolution.
    body = _AT_PRICE_RX.sub("", text.split(None, 1)[1]).strip()
    ticker = _resolve_review_target(body, roster)
    return ticker, at_price


def _load_review_roster(repo_root: Path) -> RosterIndex | None:
    """Best-effort roster for ``/review`` name→ticker resolution; ``None`` on any
    failure (the parser then falls back to the token-as-symbol behavior). Loads
    ``tracked_companies`` merged with the distinctive-alias seed, so a name like
    ``Rubrik`` resolves even when the DB is absent (the seed carries it)."""
    try:
        from capture.matcher import load_roster

        return load_roster(repo_root / "data" / "portfolio.db")
    except Exception:
        return None


def resolve_review_target(repo_root: Path, text: str) -> tuple[str, float | None] | None:
    """Parse ``/review <NAME|TICKER> [at $X]`` AND resolve the name/symbol to a
    canonical ticker via the roster — the ONE resolution entry shared by the
    instant reply (:func:`review_reply_text`) and ``ask.commands``'s background
    full-verdict kickoff, so both act on the SAME ticker (``/review Rubrik``
    reviews AND schedules the governed verdict for ``RBRK``, not ``RUBRIK``)."""
    return parse_review_command(text, roster=_load_review_roster(repo_root))


def review_reply_text(repo_root: Path, text: str, *, plain: bool = False) -> str:
    """``/review <TICKER> [at $PRICE]`` end-to-end: parse the ticker + at-price
    out of ``text`` and return the rendered instant pre-analysis reply.

    The ONE place that renders this command shape — the Ask-tab chat command
    (``ask.commands._review_command``) and the Telegram poller both call this
    instead of each re-implementing the parsing, so the at-price regex and the
    usage/error copy can never drift between the two surfaces. LLM-free and
    fast: ``build_pre_analysis`` never imports a model client.

    ``plain=True`` (the Telegram callers) renders via
    :func:`render_pre_analysis_plain` — no Markdown, since ``send_message``
    never sets ``parse_mode`` there. The web chat (Ask tab) keeps the default
    Markdown variant, which its renderer displays properly.

    The target is resolved through the roster (:func:`resolve_review_target`) so
    a company NAME works, not just a ticker symbol — ``/review Rubrik`` reviews
    ``RBRK`` — on both the Telegram and web surfaces.
    """
    if plain:
        usage = "Usage: /review <NAME|TICKER> [at $PRICE] - e.g. /review Rubrik at $70."
    else:
        usage = "Usage: /review <NAME|TICKER> [at $PRICE] — e.g. `/review Rubrik at $70`."
    parsed = resolve_review_target(repo_root, text)
    if parsed is None:
        return usage
    ticker, at_price = parsed
    db_path = repo_root / "data" / "portfolio.db"
    try:
        pre = build_pre_analysis(repo_root, ticker, at_price=at_price, db_path=db_path)
    except Exception as exc:
        return f"Couldn't build a review for {ticker}: {type(exc).__name__}: {exc}"
    if plain:
        return render_pre_analysis_plain(pre, db_path=db_path)
    return render_pre_analysis_chat(pre, db_path=db_path)
