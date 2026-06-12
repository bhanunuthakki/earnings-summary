"""Per-position attribution narrative — deterministic skeleton (S15 PR2).

The tracker computes each position's window dollar alpha (its
``/position-alpha`` counterfactual: the same buy/sell flows routed into
SPY); the lifecycle ledger (0088) knows what the analyst paid and believed
at entry; the thesis ledger and alerts record what happened to the thesis
during the window. Nothing joined them — the alpha number had no story.

``build_position_attribution`` assembles the join for one ticker and writes
the narrative as plain composed sentences:

    Beat its SPY counterfactual by $4,210 over 2026-03-12 → 2026-06-11
    (P&L +$6,100 vs SPY-matched +$1,890). Held since 2026-01-15 (entry
    $11.20, high conviction). Window events: 2 thesis updates · 1 alert
    (kpi_inflection) · 1 decision graded correct.

Deliberately deterministic — every clause traces to a queryable row, so the
narrative is auditable and free (no LLM call, no budget row, nothing to
eval). The directive's optional LLM prose polish is exactly that — optional;
it can ride later as a purpose with the standing pin/budget/eval trio
without touching this skeleton.

Benchmark math is never rebuilt here (the P2.1 architecture rule): alpha
numbers come from the tracker's PositionAlphaRow verbatim; this module only
joins and phrases. Every DB read is best-effort (missing tables → zero
events), matching the panel posture.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import PositionAlpha, PositionAlphaRow
from position_lifecycle import PositionEntry, list_entries

# Without a tracker window (offline / no alpha for the name), events still
# get counted over a recent default window so the narrative degrades to
# "what happened" rather than vanishing.
_DEFAULT_WINDOW_DAYS = 90

_LEDGER_LABELS: dict[str, str] = {
    "thesis_update": "thesis update",
    "bear_append": "bear-case append",
    "earnings_prep_append": "earnings-prep note",
    "sizing_update": "sizing change",
    "advisor_memo": "advisor memo",
}


@dataclass(frozen=True, slots=True)
class WindowEvents:
    """What touched the thesis during the attribution window."""

    ledger_counts: dict[str, int] = field(default_factory=dict[str, int])  # entry_kind → n
    alert_counts: dict[str, int] = field(default_factory=dict[str, int])  # trigger_kind → n
    decisions_graded: list[tuple[str, str]] = field(
        default_factory=list[tuple[str, str]]
    )  # (recommendation_kind, outcome_label)

    @property
    def empty(self) -> bool:
        return not (self.ledger_counts or self.alert_counts or self.decisions_graded)


@dataclass(frozen=True, slots=True)
class PositionAttribution:
    """One position's attribution: alpha + entry context + events + prose."""

    ticker: str
    window_start: str | None
    window_end: str | None
    alpha_usd: float | None
    actual_pl: float | None
    spy_pl: float | None
    alpha_incomplete: bool
    entry: PositionEntry | None  # the OPEN lifecycle row, when one exists
    events: WindowEvents
    narrative: str


# ---------------------------------------------------------------------------
# Window events (best-effort SQL)
# ---------------------------------------------------------------------------


def _safe_rows(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def gather_window_events(
    ticker: str,
    *,
    db_path: Path | str,
    window_start: str,
    window_end: str,
    user_id: str = DEFAULT_USER_ID,
) -> WindowEvents:
    """Count the thesis-relevant rows for one name inside [start, end].

    Date comparison is lexicographic over ISO strings — the house format for
    every stamp involved (ledger created_at, alerts fired_at, decisions
    outcome_at), and the window bounds are dates, so the prefix compare is
    exact."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return WindowEvents()
    conn.row_factory = sqlite3.Row
    t = ticker.upper()
    # End bound: fired_at/created_at carry times; 'YYYY-MM-DD~' sorts after
    # every same-day timestamp ('~' > 'T' > ' ').
    end_hi = window_end + "~"
    try:
        ledger_counts: dict[str, int] = {}
        for row in _safe_rows(
            conn,
            "SELECT entry_kind, COUNT(*) AS n FROM thesis_ledger_entries "
            "WHERE user_id = ? AND ticker = ? AND created_at >= ? AND created_at <= ? "
            "GROUP BY entry_kind",
            (user_id, t, window_start, end_hi),
        ):
            ledger_counts[str(row["entry_kind"])] = int(row["n"])
        alert_counts: dict[str, int] = {}
        for row in _safe_rows(
            conn,
            "SELECT trigger_kind, COUNT(*) AS n FROM alerts "
            "WHERE user_id = ? AND ticker = ? AND fired_at >= ? AND fired_at <= ? "
            "GROUP BY trigger_kind",
            (user_id, t, window_start, end_hi),
        ):
            alert_counts[str(row["trigger_kind"])] = int(row["n"])
        decisions_graded = [
            (str(row["recommendation_kind"]), str(row["outcome_label"]))
            for row in _safe_rows(
                conn,
                "SELECT recommendation_kind, outcome_label FROM decisions "
                "WHERE ticker = ? AND outcome_at IS NOT NULL "
                "AND COALESCE(outcome_label, 'pending') NOT IN ('pending') "
                "AND outcome_at >= ? AND outcome_at <= ? ORDER BY outcome_at DESC",
                (t, window_start, end_hi),
            )
        ]
        return WindowEvents(
            ledger_counts=ledger_counts,
            alert_counts=alert_counts,
            decisions_graded=decisions_graded,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Narrative skeleton
# ---------------------------------------------------------------------------


def _money(v: float) -> str:
    return f"${abs(v):,.0f}" if abs(v) >= 1000 else f"${abs(v):,.2f}"


def _signed_money(v: float) -> str:
    return ("+" if v >= 0 else "-") + _money(v)


def _alpha_sentence(att_window: str, row: PositionAlphaRow | None) -> str:
    if row is None or row.alpha is None:
        return f"No tracker alpha for this window{att_window}."
    verb = "Beat" if row.alpha >= 0 else "Trailed"
    sentence = f"{verb} its SPY counterfactual by {_money(row.alpha)}{att_window}"
    if row.actual_pl is not None and row.spy_counterfactual_pl is not None:
        sentence += (
            f" (P&L {_signed_money(row.actual_pl)} vs SPY-matched "
            f"{_signed_money(row.spy_counterfactual_pl)})"
        )
    sentence += "."
    if row.incomplete:
        sentence += " Window data incomplete — treat the split as approximate."
    return sentence


def _entry_sentence(entry: PositionEntry | None) -> str:
    if entry is None:
        return ""
    if entry.entry_date is None:
        bits = ["Held — opening predates the lifecycle ledger"]
    else:
        bits = [f"Held since {entry.entry_date}"]
    detail: list[str] = []
    if entry.entry_price is not None:
        detail.append(f"entry ${entry.entry_price:,.2f}")
    if entry.entry_conviction:
        detail.append(f"{entry.entry_conviction} conviction")
    if detail:
        bits.append(f"({', '.join(detail)})")
    return " ".join(bits) + "."


def _events_sentence(events: WindowEvents) -> str:
    if events.empty:
        return (
            "No thesis, alert, or decision events in the window — the move was "
            "market/flow-driven, not thesis-event-driven."
        )
    parts: list[str] = []
    for kind, n in sorted(events.ledger_counts.items(), key=lambda kv: -kv[1]):
        label = _LEDGER_LABELS.get(kind, kind.replace("_", " "))
        parts.append(f"{n} {label}{'s' if n != 1 else ''}")
    if events.alert_counts:
        total_alerts = sum(events.alert_counts.values())
        kinds = ", ".join(
            f"{n} {k}" for k, n in sorted(events.alert_counts.items(), key=lambda kv: -kv[1])
        )
        parts.append(f"{total_alerts} alert{'s' if total_alerts != 1 else ''} ({kinds})")
    for kind, label in events.decisions_graded:
        parts.append(f"{kind.upper()} graded {label}")
    return "Window events: " + " · ".join(parts) + "."


def compose_narrative(
    *,
    window_start: str | None,
    window_end: str | None,
    alpha_row: PositionAlphaRow | None,
    entry: PositionEntry | None,
    events: WindowEvents,
) -> str:
    att_window = f" over {window_start} → {window_end}" if window_start and window_end else ""
    sentences = [
        _alpha_sentence(att_window, alpha_row),
        _entry_sentence(entry),
        _events_sentence(events),
    ]
    return " ".join(s for s in sentences if s)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _default_window() -> tuple[str, str]:
    today = datetime.now(UTC).replace(tzinfo=None).date()
    return (today - timedelta(days=_DEFAULT_WINDOW_DAYS)).isoformat(), today.isoformat()


def build_position_attribution(
    ticker: str,
    *,
    db_path: Path | str,
    alpha_row: PositionAlphaRow | None,
    window_start: str | None,
    window_end: str | None,
    user_id: str = DEFAULT_USER_ID,
    entry: PositionEntry | None = None,
) -> PositionAttribution:
    """The full join for one name. ``entry`` injects an already-fetched open
    lifecycle row (the book-level builder batches them); None fetches."""
    t = ticker.upper()
    if entry is None:
        entry = next(
            (
                e
                for e in list_entries(db_path=db_path, ticker=t, user_id=user_id, limit=5)
                if e.is_open
            ),
            None,
        )
    start, end = (window_start, window_end) if window_start and window_end else _default_window()
    events = gather_window_events(
        t, db_path=db_path, window_start=start, window_end=end, user_id=user_id
    )
    return PositionAttribution(
        ticker=t,
        window_start=window_start or start,
        window_end=window_end or end,
        alpha_usd=alpha_row.alpha if alpha_row else None,
        actual_pl=alpha_row.actual_pl if alpha_row else None,
        spy_pl=alpha_row.spy_counterfactual_pl if alpha_row else None,
        alpha_incomplete=bool(alpha_row.incomplete) if alpha_row else False,
        entry=entry,
        events=events,
        narrative=compose_narrative(
            window_start=window_start or start,
            window_end=window_end or end,
            alpha_row=alpha_row,
            entry=entry,
            events=events,
        ),
    )


def attributions_for_book(
    *,
    db_path: Path | str,
    alpha: PositionAlpha | None,
    user_id: str = DEFAULT_USER_ID,
) -> list[PositionAttribution]:
    """One attribution per RESEARCH position — the names with an open
    lifecycle row (0088). Tracker-only holdings (index funds, cash sweeps)
    are deliberately out of scope: there is no thesis to attribute against.
    Ordered by |alpha| descending (the biggest stories first), alpha-less
    names last."""
    open_entries = [
        e for e in list_entries(db_path=db_path, user_id=user_id, limit=200) if e.is_open
    ]
    if not open_entries:
        return []
    rows_by_ticker: dict[str, PositionAlphaRow] = {}
    window_start = window_end = None
    if alpha is not None:
        window_start, window_end = alpha.start_date, alpha.end_date
        for row in alpha.rows:
            if row.ticker:
                rows_by_ticker[row.ticker.upper()] = row
    out = [
        build_position_attribution(
            e.ticker,
            db_path=db_path,
            alpha_row=rows_by_ticker.get(e.ticker.upper()),
            window_start=window_start,
            window_end=window_end,
            user_id=user_id,
            entry=e,
        )
        for e in open_entries
    ]
    out.sort(key=lambda a: (a.alpha_usd is None, -abs(a.alpha_usd or 0.0), a.ticker))
    return out
