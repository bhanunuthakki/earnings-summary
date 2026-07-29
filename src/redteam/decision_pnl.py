"""Decision P&L — score the Red Team's forced responses (monthly_red_team.md
Phase 3, PR7).

Every REFUTE / ACCEPT / DEFER on a monthly Red Team item is a real claim about
the future ("this attack's downside won't materialize" / "de-risking here is
worth the cost" / "not resolvable yet"). This module reads it back N quarters
later (default 2 — long enough for a quarter's price action to be more than
noise, short enough that the read stays legible) and scores it against what the
ticker's price actually did, using ONLY local, offline data: the nearest
``dcf_runs`` snapshot to the response date for "price then", and the latest
``dcf_runs`` row for "price now" (the same source ``bear_lint`` reads —
documented in ``directives/monthly_red_team.md`` Phase 3 as
"local price cache / dcf_runs live_price — offline reads only").

Scoring arithmetic (deliberately simple and legible — this is a directional
read, not a real weight-adjusted counterfactual P&L; historical position
weight isn't tracked, so the CURRENT materialized weight is used as a size
proxy and documented as such):

  price_move_pct = (price_now - price_then) / price_then

  REFUTE  — the owner bet the attack's downside would NOT materialize.
            scored_pct = +price_move_pct * weight_pct
            (price held/rose -> positive -> the refute was vindicated;
             price fell -> negative -> the attack's concern showed up anyway.)

  ACCEPT  — the owner acted defensively (trimmed / added a rule / cut a
            scenario) in response to the attack.
            scored_pct = -price_move_pct * weight_pct
            (price fell -> positive -> de-risking avoided the pain;
             price rose -> negative -> de-risking cost the upside.)

  DEFER   — no action; informational only. price_move_pct is shown with a
            neutral frame, never scored cost/save (there was no bet to grade).

Cross-book items (``ticker is None``) are not price-scorable and are reported
separately with an honest "not price-scorable" reason, never silently dropped.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from clock import now_naive_utc
from redteam.models import RedTeamItemRow, Status
from redteam.store import list_responded_items
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

DEFAULT_MIN_QUARTERS = 2
_DAYS_PER_QUARTER = 91  # documented approximation — a legible cutoff, not a fiscal calendar

ScoreDirection = Literal["refute", "accept", "defer"]


@dataclass(frozen=True, slots=True)
class PriceRead:
    price: float
    as_of: str  # dcf_runs.created_at this price snapshot came from
    source: str = "dcf_runs"


@dataclass(frozen=True, slots=True)
class DecisionPnlRow:
    """One responded item's scored (or honestly-unscorable) outcome read."""

    item_id: int
    ticker: str | None
    kind: str  # per_name | cross_book
    lens: str
    status: Status  # refuted | accepted | deferred
    severity: str
    responded_at: str
    weight_pct: float | None  # current materialized weight — a size PROXY, see module docstring
    price_then: PriceRead | None
    price_now: PriceRead | None
    price_move_pct: float | None
    scored_pct: float | None  # None for DEFER (informational) or when unscorable
    note: str  # human-readable provenance / honesty note


@dataclass(frozen=True, slots=True)
class DecisionPnlReport:
    """The full pass over responded items."""

    min_quarters: int
    as_of: str
    rows: list[DecisionPnlRow] = field(default_factory=list[DecisionPnlRow])
    n_due: int = 0  # responded items >= min_quarters old — the set actually scored
    n_not_yet_due: int = 0  # responded but too recent to score yet
    n_unscorable: int = 0  # due, but no price data (cross-book, or no dcf_runs on file)

    @property
    def scored_rows(self) -> list[DecisionPnlRow]:
        return [r for r in self.rows if r.scored_pct is not None]

    @property
    def total_scored_pct(self) -> float:
        return sum(r.scored_pct for r in self.rows if r.scored_pct is not None)


def _cutoff(now: datetime, min_quarters: int) -> datetime:
    return now - timedelta(days=_DAYS_PER_QUARTER * min_quarters)


def _price_near(conn: sqlite3.Connection, ticker: str, target_iso: str) -> PriceRead | None:
    """Nearest-in-time ``dcf_runs`` snapshot (before or after) to ``target_iso``
    — every historical build carries its own ``live_price``, so this is a real
    (if coarse) point-in-time read, not an interpolation."""
    try:
        row = conn.execute(
            "SELECT live_price, created_at FROM dcf_runs "
            "WHERE ticker = ? AND live_price IS NOT NULL "
            "ORDER BY ABS(JULIANDAY(created_at) - JULIANDAY(?)) ASC LIMIT 1",
            (ticker, target_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["live_price"] is None:
        return None
    return PriceRead(price=float(row["live_price"]), as_of=str(row["created_at"]))


def _price_latest(conn: sqlite3.Connection, ticker: str) -> PriceRead | None:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dcf_runs)").fetchall()}
        is_latest_pred = "COALESCE(is_latest, 1) = 1" if "is_latest" in cols else "1 = 1"
        seg_pred = "COALESCE(segment_name, '') = ''" if "segment_name" in cols else "1 = 1"
        row = conn.execute(
            f"SELECT live_price, created_at FROM dcf_runs WHERE ticker = ? "
            f"AND {is_latest_pred} AND {seg_pred} AND live_price IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None:
            # Fall back to the most recent row of any vintage — a stale-but-real
            # price beats no price for a "what does the book know now?" read.
            row = conn.execute(
                "SELECT live_price, created_at FROM dcf_runs WHERE ticker = ? "
                "AND live_price IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["live_price"] is None:
        return None
    return PriceRead(price=float(row["live_price"]), as_of=str(row["created_at"]))


def _direction(status: Status) -> ScoreDirection | None:
    if status == "refuted":
        return "refute"
    if status == "accepted":
        return "accept"
    if status == "deferred":
        return "defer"
    return None


def _score_row(
    conn: sqlite3.Connection,
    item: RedTeamItemRow,
    *,
    weights: dict[str, float],
) -> DecisionPnlRow:
    direction = _direction(item.status)
    responded_at = item.responded_at.isoformat() if item.responded_at is not None else ""
    if item.ticker is None:
        return DecisionPnlRow(
            item_id=item.id,
            ticker=None,
            kind=item.kind,
            lens=item.lens,
            status=item.status,
            severity=item.severity,
            responded_at=responded_at,
            weight_pct=None,
            price_then=None,
            price_now=None,
            price_move_pct=None,
            scored_pct=None,
            note="cross-book item — not price-scorable (no single ticker).",
        )
    ticker = item.ticker.upper()
    weight = weights.get(ticker)
    then = _price_near(conn, ticker, responded_at)
    now_px = _price_latest(conn, ticker)
    if then is None or now_px is None or then.price <= 0:
        return DecisionPnlRow(
            item_id=item.id,
            ticker=ticker,
            kind=item.kind,
            lens=item.lens,
            status=item.status,
            severity=item.severity,
            responded_at=responded_at,
            weight_pct=weight,
            price_then=then,
            price_now=now_px,
            price_move_pct=None,
            scored_pct=None,
            note="no dcf_runs price on file near the response date and/or now — unscorable.",
        )
    move_pct = (now_px.price - then.price) / then.price
    w = weight if weight is not None else 1.0
    note_w = (
        f"weight {weight:.2%} (current materialized weight, a proxy — historical "
        "weight at response time isn't tracked)."
        if weight is not None
        else "weight unknown (ticker not in current materialized weights) — scored unweighted."
    )
    if direction == "refute":
        scored = move_pct * w
        note = f"REFUTE: price {'held/rose' if move_pct >= 0 else 'fell'} — {note_w}"
    elif direction == "accept":
        scored = -move_pct * w
        note = f"ACCEPT: price {'fell' if move_pct < 0 else 'rose'} — {note_w}"
    else:
        scored = None
        note = f"DEFER: informational only, not scored cost/save — {note_w}"
    return DecisionPnlRow(
        item_id=item.id,
        ticker=ticker,
        kind=item.kind,
        lens=item.lens,
        status=item.status,
        severity=item.severity,
        responded_at=responded_at,
        weight_pct=weight,
        price_then=then,
        price_now=now_px,
        price_move_pct=move_pct,
        scored_pct=scored,
        note=note,
    )


def build_decision_pnl(
    *,
    db_path: Path | str,
    repo_root: Path | str | None = None,
    min_quarters: int = DEFAULT_MIN_QUARTERS,
    now: datetime | None = None,
) -> DecisionPnlReport:
    """Score every responded red_team_item older than ``min_quarters``.
    Offline reads only (``red_team_items`` + ``dcf_runs`` + the materialized
    weights cache) — no tracker/network round-trip, matching the directive's
    "offline reads only" contract."""
    ts = now or now_naive_utc()
    cutoff = _cutoff(ts, min_quarters)
    items = list_responded_items(db_path=db_path)

    weights: dict[str, float] = {}
    if repo_root is not None:
        try:
            from portfolio_weights import read_materialized_weights

            weights = {t.upper(): w for t, w in read_materialized_weights(Path(repo_root)).items()}
        except Exception:  # best-effort — an unresolved weight degrades to unweighted
            weights = {}

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        rows: list[DecisionPnlRow] = []
        n_due = n_not_due = n_unscorable = 0
        for item in items:
            if item.responded_at is None or item.responded_at > cutoff:
                n_not_due += 1
                continue
            n_due += 1
            row = _score_row(conn, item, weights=weights)
            if row.scored_pct is None and row.status != "deferred" and row.ticker is not None:
                n_unscorable += 1
            rows.append(row)
    finally:
        conn.close()

    return DecisionPnlReport(
        min_quarters=min_quarters,
        as_of=ts.isoformat(),
        rows=rows,
        n_due=n_due,
        n_not_yet_due=n_not_due,
        n_unscorable=n_unscorable,
    )


# ---------------------------------------------------------------------------
# Yearly scorecard — the three headline numbers (monthly_red_team.md Phase 3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScorecardNumber:
    """One headline number: either a real value + supporting n, or an honest
    empty state naming what's missing — never a fabricated placeholder."""

    label: str
    available: bool
    value_text: str  # rendered value, or the honest "no data yet" reason
    detail: str = ""  # extra context (n, method, what's missing)


@dataclass(frozen=True, slots=True)
class YearlyScorecard:
    brier_trend: ScorecardNumber
    cut_discipline_hit_rate: ScorecardNumber
    rule_execution_fidelity: ScorecardNumber


# exit_reason free-text keywords that count as "rule-triggered" for the
# cut-discipline read — position_entries.exit_reason has no controlled
# vocabulary (owner-authored prose at exit time), so this is a documented,
# deliberately narrow keyword match rather than a guess-fix on ambiguous text.
_RULE_KEYWORDS: tuple[str, ...] = (
    "break rule",
    "break_rule",
    "thesis broke",
    "breach",
    "rule fired",
)
_MIN_CUT_DISCIPLINE_N = 3


def _is_rule_triggered(exit_reason: str | None) -> bool:
    if not exit_reason:
        return False
    lowered = exit_reason.lower()
    return any(kw in lowered for kw in _RULE_KEYWORDS)


def _brier_trend_number(db_path: Path | str) -> ScorecardNumber:
    from decision_calibration import build_calibration

    cal = build_calibration(db_path=db_path)
    if cal is None or cal.conviction_calibration is None or cal.conviction_calibration.n == 0:
        return ScorecardNumber(
            label="Brier trend",
            available=False,
            value_text="no data yet",
            detail=(
                "needs graded correct/wrong decisions with a stated conviction — "
                "decision_calibration.build_calibration currently has none scorable."
            ),
        )
    cc = cal.conviction_calibration
    beats = cc.beats_baseline
    beat_note = "beats" if beats else "does not beat"
    return ScorecardNumber(
        label="Brier trend",
        available=True,
        value_text=f"{cc.brier:.3f} (baseline {cc.baseline_brier:.3f})",
        detail=f"n={cc.n} graded conviction calls; {beat_note} the no-discrimination baseline.",
    )


def _cut_discipline_number(db_path: Path | str, *, user_id: str) -> ScorecardNumber:
    from position_lifecycle import list_entries

    entries = [
        e for e in list_entries(db_path=db_path, user_id=user_id, limit=500) if not e.is_open
    ]
    rule_exits = [e for e in entries if _is_rule_triggered(e.exit_reason)]
    if len(rule_exits) < _MIN_CUT_DISCIPLINE_N:
        return ScorecardNumber(
            label="Cut-discipline hit rate",
            available=False,
            value_text="no data yet",
            detail=(
                f"needs >= {_MIN_CUT_DISCIPLINE_N} closed positions whose exit_reason names a "
                f"rule/break; found {len(rule_exits)} of {len(entries)} closed positions."
            ),
        )
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        hits = 0
        scored = 0
        for e in rule_exits:
            if e.exit_date is None or e.exit_price is None or e.exit_price <= 0:
                continue
            latest = _price_latest(conn, e.ticker.upper())
            if latest is None:
                continue
            scored += 1
            # Counterfactual hold: did the price fall further after the cut (the
            # rule-triggered exit was "right" to leave) vs rally back (a cut that
            # cost you the recovery)?
            if latest.price <= e.exit_price:
                hits += 1
    finally:
        conn.close()
    if scored < _MIN_CUT_DISCIPLINE_N:
        return ScorecardNumber(
            label="Cut-discipline hit rate",
            available=False,
            value_text="no data yet",
            detail=(
                f"{len(rule_exits)} rule-triggered exits on file, but only {scored} have a "
                f"resolvable post-exit price read (need >= {_MIN_CUT_DISCIPLINE_N})."
            ),
        )
    rate = hits / scored
    return ScorecardNumber(
        label="Cut-discipline hit rate",
        available=True,
        value_text=f"{rate:.0%}",
        detail=(
            f"n={scored} rule-triggered exits; a hit = price stayed at/below the exit price "
            "since (the cut avoided a further decline or a round-trip)."
        ),
    )


def _rule_execution_fidelity_number() -> ScorecardNumber:
    # Directive: "placeholder honest-empty until drawdown data exists" — this
    # needs a per-drawdown log of which rules FIRED vs which the owner actually
    # acted on, which nothing in the schema captures yet (red_team_items tracks
    # the monthly forced-response loop, not a continuous drawdown-rule trace).
    # Deliberately not approximated from a proxy — an honest empty beats a
    # confident-looking number built on the wrong data.
    return ScorecardNumber(
        label="Rule-execution fidelity in drawdowns",
        available=False,
        value_text="no data yet",
        detail=(
            "needs a per-drawdown record of which break rules fired vs. which the owner "
            "actually acted on — not yet captured anywhere in the schema."
        ),
    )


def build_yearly_scorecard(*, db_path: Path | str, user_id: str = "bhanu") -> YearlyScorecard:
    """The three headline numbers the directive names. Each degrades to an
    honest, self-explaining empty state rather than a fabricated placeholder —
    per the directive, most of these are expected to read "no data yet" today."""
    return YearlyScorecard(
        brier_trend=_brier_trend_number(db_path),
        cut_discipline_hit_rate=_cut_discipline_number(db_path, user_id=user_id),
        rule_execution_fidelity=_rule_execution_fidelity_number(),
    )


__all__ = [
    "DEFAULT_MIN_QUARTERS",
    "DecisionPnlReport",
    "DecisionPnlRow",
    "PriceRead",
    "ScorecardNumber",
    "YearlyScorecard",
    "build_decision_pnl",
    "build_yearly_scorecard",
]
