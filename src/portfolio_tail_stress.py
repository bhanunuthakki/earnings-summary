"""Scenario-tail stress: what the book loses if EVERY bear case fires.

The DCF layer already persists a bull/base/bear fair-value range per name
(``dcf_runs.assumption_snapshot_json`` scenarios — ``dcf.scenario_reward``
parses it), and the risk-reward gap consumes it name-by-name. What nothing
answered is the BOOK-level tail: "if every holding fell to its bear-case fair
value tomorrow, how far does the whole book draw down?" — the one number that
turns eleven per-name bear cases into a portfolio risk read.

The arithmetic is deliberately naive-and-legible: each holding's bear return
is ``bear_fv / live_price - 1`` (the L6 daily-repriced ``live_price``), its
contribution is ``weight x bear_return``, and the book drawdown is the sum
over the names that HAVE a bear leg. No correlation adjustment, no
probabilities — this is the joint-tail floor the scenario ranges imply, not
an expectation (``scenario_reward`` owns the probability-weighted read).

Honesty discipline mirrors ``risk_reward``: names without a DCF or without a
persisted bear scenario are listed with a reason (coverage is explicit, the
headline states how much of the book it actually models), and a stale fair
value / price marks the row low-confidence rather than silently counting.
Local DB only — renders with the tracker down.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from dcf.scenario_reward import parse_scenario_fair_values

__all__ = [
    "FV_STALE_DAYS",
    "PRICE_STALE_DAYS",
    "TailStress",
    "TailStressRow",
    "build_tail_stress",
]

# Freshness bars — same rationale (and values) as risk_reward's reward leg:
# fair values move on a fundamentals refresh (~monthly bar), the price leg is
# re-priced daily by morning stage 0e so a week-old price is itself a tell.
FV_STALE_DAYS = 30
PRICE_STALE_DAYS = 7


@dataclass(slots=True)
class TailStressRow:
    """One holding's leg of the all-bears stress."""

    ticker: str
    weight_pct: float  # fraction-of-book, in percent
    live_price: float | None
    bear_fv: float | None
    bear_return_pct: float | None  # bear_fv / live_price - 1, in percent
    contribution_pct: float | None  # weight x bear_return, in percent of book
    low_confidence: bool = False
    confidence_reason: str | None = None
    excluded_reason: str | None = None  # set when the name has no modelable bear leg


@dataclass(slots=True)
class TailStress:
    """The book-level all-bears read + the coverage to trust it."""

    rows: list[TailStressRow]  # worst contribution first, excluded rows last
    book_drawdown_pct: float  # sum of contributions over the modeled slice
    covered_weight_pct: float  # book weight carrying a bear leg, in percent
    stale_weight_pct: float  # modeled weight flagged low-confidence, in percent
    names_with_bear: int
    names_total: int
    notes: list[str] = field(default_factory=list[str])


def _parse_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


@dataclass(slots=True)
class _DcfLeg:
    live_price: float | None
    bear_fv: float | None
    valuation_date: date | None
    priced_date: date | None


def _latest_dcf_legs(db_path: Path, tickers: Sequence[str]) -> dict[str, _DcfLeg]:
    """Latest ``dcf_runs`` row per name — the L6-fresh ``live_price`` plus the
    bear fair value out of the scenario snapshot. Mirrors
    ``risk_reward._dcf_reward_legs``'s query shape (column presence probed, the
    newest row per ticker wins). ``{}`` on a missing DB / table."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
        if not cols:
            return {}
        snap_sel = ", assumption_snapshot_json" if "assumption_snapshot_json" in cols else ""
        priced_sel = ", live_price_at" if "live_price_at" in cols else ""
        rows = conn.execute(
            f"SELECT ticker, valuation_date, live_price{priced_sel}{snap_sel} "
            "FROM dcf_runs ORDER BY ticker, created_at DESC, id DESC"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    want = {t.upper() for t in tickers}
    out: dict[str, _DcfLeg] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        if t not in want or t in out:
            continue
        snapshot = r["assumption_snapshot_json"] if snap_sel else None
        bear = parse_scenario_fair_values(snapshot).get("bear")
        out[t] = _DcfLeg(
            live_price=float(r["live_price"]) if r["live_price"] is not None else None,
            bear_fv=bear,
            valuation_date=_parse_date(r["valuation_date"]),
            priced_date=_parse_date(r["live_price_at"]) if priced_sel else None,
        )
    return out


def build_tail_stress(
    db_path: Path,
    weights: Mapping[str, float],
    *,
    today: date | None = None,
) -> TailStress | None:
    """The all-bears stress for the book described by ``weights`` (ticker ->
    fraction-of-book). ``None`` when there are no weighted names (nothing to
    stress — the caller renders the empty state)."""
    positive = {t.upper(): w for t, w in weights.items() if w > 0}
    if not positive:
        return None
    today = today or date.today()
    legs = _latest_dcf_legs(db_path, list(positive))

    rows: list[TailStressRow] = []
    book_drawdown = 0.0
    covered_w = 0.0
    stale_w = 0.0
    with_bear = 0
    for t in sorted(positive):
        w = positive[t]
        leg = legs.get(t)
        if leg is None:
            rows.append(
                TailStressRow(
                    ticker=t,
                    weight_pct=w * 100.0,
                    live_price=None,
                    bear_fv=None,
                    bear_return_pct=None,
                    contribution_pct=None,
                    excluded_reason="no DCF run on file",
                )
            )
            continue
        if leg.bear_fv is None or leg.live_price is None or leg.live_price <= 0:
            reason = (
                "no bear scenario persisted"
                if leg.live_price is not None and leg.live_price > 0
                else "DCF has no usable live price"
            )
            rows.append(
                TailStressRow(
                    ticker=t,
                    weight_pct=w * 100.0,
                    live_price=leg.live_price,
                    bear_fv=leg.bear_fv,
                    bear_return_pct=None,
                    contribution_pct=None,
                    excluded_reason=reason,
                )
            )
            continue
        bear_ret = leg.bear_fv / leg.live_price - 1.0
        contribution = w * bear_ret * 100.0
        stale_bits: list[str] = []
        if leg.valuation_date is None:
            stale_bits.append("fair value undated")
        elif (today - leg.valuation_date).days > FV_STALE_DAYS:
            stale_bits.append(f"fair value {(today - leg.valuation_date).days}d stale")
        if leg.priced_date is None:
            stale_bits.append("price never recorded")
        elif (today - leg.priced_date).days > PRICE_STALE_DAYS:
            stale_bits.append(f"price {(today - leg.priced_date).days}d stale")
        rows.append(
            TailStressRow(
                ticker=t,
                weight_pct=w * 100.0,
                live_price=leg.live_price,
                bear_fv=leg.bear_fv,
                bear_return_pct=bear_ret * 100.0,
                contribution_pct=contribution,
                low_confidence=bool(stale_bits),
                confidence_reason="; ".join(stale_bits) if stale_bits else None,
            )
        )
        book_drawdown += contribution
        covered_w += w * 100.0
        with_bear += 1
        if stale_bits:
            stale_w += w * 100.0

    # Worst drag first; the excluded names sit under the modeled slice.
    rows.sort(
        key=lambda r: (
            r.contribution_pct is None,
            r.contribution_pct if r.contribution_pct is not None else 0.0,
            r.ticker,
        )
    )
    notes: list[str] = []
    uncovered = [r.ticker for r in rows if r.excluded_reason is not None]
    if uncovered:
        notes.append(
            "outside the stress (no bear leg): "
            + "; ".join(
                f"{r.ticker} ({r.excluded_reason})" for r in rows if r.excluded_reason is not None
            )
        )
    return TailStress(
        rows=rows,
        book_drawdown_pct=book_drawdown,
        covered_weight_pct=covered_w,
        stale_weight_pct=stale_w,
        names_with_bear=with_bear,
        names_total=len(positive),
        notes=notes,
    )
