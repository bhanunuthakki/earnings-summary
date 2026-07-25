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

THE SHARED SKILL-DECOMPOSITION ENGINE (Close-the-Loops L8, §6)
--------------------------------------------------------------
``decompose_alpha`` is the ONE place realized book alpha is split into the
owner's repeatable decisions — WHICH names (selection), HOW MUCH (sizing),
WHEN (timing) — so the coach can name his edge vs his leak, and so the future
Brinson allocation-vs-selection view consumes the SAME decomposition rather
than a second divergent one. It is deliberately separate from the per-position
narrative above: the narrative tells one name's story; the decomposition reads
the whole book's tracker rows at once. Same P2.1 discipline — it only
arithmetises the tracker's per-name dollar alpha + start/end value + window
flows; it never recomputes a return or a benchmark.

The split is Brinson-shaped and honest about its limits:
  * selection + sizing is an EXACT additive split of the decomposed dollar
    alpha (over the names with a usable start value);
  * timing is a flow-lean DIAGNOSTIC over the same names — the tracker's alpha
    already nets benchmark-matched flows, so it measures whether within-window
    adds/trims pointed at alpha, not a separable fourth return bucket;
  * the shared minimum-n guard frames a thin book ("n=3, low confidence")
    rather than asserting a skill verdict from noise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from calibration_guard import confidence_note, is_confident
from decision_calibration import bucket_for_conviction
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


# ===========================================================================
# Shared skill-decomposition engine (L8, §6) — selection / sizing / timing
# ===========================================================================

# Top-contributor list cap on the by-name breakdown (the biggest stories only).
_MAX_CONTRIBUTORS = 6

# Plausibility ceiling on a name's implied window alpha RETURN (|alpha| / capital
# base). 10.0 = 1000% — beyond any real single-name outcome in a one-year window,
# but the first thing a data artifact produces: a dust/stale start value (a $0.01
# remnant of a closed position) under real dollar alpha makes rate = a/value
# explode by 5-6 orders of magnitude, and ``sel_i = mean_w * rate`` with it.
# Prod rendered selection -$38.59bn / sizing +$38.59bn on a book five orders of
# magnitude smaller — and the documented additive identity (selection + sizing
# == total) held throughout, because sizing is computed as the residual. The
# invariant is true by construction and therefore pins nothing about magnitude;
# plausibility has to be gated per input, exactly like the financial-data rule:
# a figure 10x off is a unit/data error, not an outlier — exclude and surface.
_MAX_PLAUSIBLE_ALPHA_RATE = 10.0


@dataclass(frozen=True, slots=True)
class ConvictionAlphaRow:
    """Realized alpha grouped by stated-conviction bucket — the conviction →
    outcome join: did the names the owner believed in actually deliver, and did
    his weighting back them? ``sizing_usd`` is the bucket's share of the
    weight-tilt term, so a positive ``alpha_usd`` with a negative ``sizing_usd``
    reads as "your winners delivered, but you under-sized them"."""

    conviction: str  # high | medium | low | unstated (bucket_for_conviction)
    n: int
    alpha_usd: float
    sizing_usd: float
    mean_conviction: float | None  # average 1-5 rating in the bucket, when stated


@dataclass(frozen=True, slots=True)
class NameContribution:
    """One name's share of each leg — the audit trail behind the rollups."""

    ticker: str
    alpha_usd: float
    selection_usd: float
    sizing_usd: float
    conviction: float | None


@dataclass(frozen=True, slots=True)
class SkillDecomposition:
    """Book alpha decomposed into the owner's repeatable decisions.

    ``selection_usd + sizing_usd == total_alpha_usd`` exactly (over ``n_names``
    — the names carrying a usable start value). ``timing_usd`` is the flow-lean
    diagnostic over the same names (None when nothing was traded in the window).
    ``confident`` gates whether the coach may state a skill verdict or must
    hedge — the shared minimum-n guard over ``n_names``."""

    window_start: str | None
    window_end: str | None
    n_names: int
    total_alpha_usd: float | None
    selection_usd: float | None
    sizing_usd: float | None
    timing_usd: float | None
    n_timed: int
    by_conviction: list[ConvictionAlphaRow]
    top_contributors: list[NameContribution]
    excluded_no_value: int
    confident: bool
    notes: list[str]
    # Which counterfactual the alpha is measured against (L-seam 4): "policy"
    # (the designated anti-cherry-pick benchmark) when one is configured, else
    # "spy". Defaulted so any pre-seam hand-construction still builds.
    benchmark_basis: str = "spy"


def _usable_value(row: PositionAlphaRow) -> float | None:
    """The capital-at-risk proxy for the alpha rate: the window's START value,
    falling back to END value for a name opened inside the window (start 0).
    None when neither is positive — that name can't carry a weight, so it is
    excluded from the selection/sizing split (counted, noted)."""
    for candidate in (row.value_at_start, row.value_at_end):
        if candidate is not None and candidate > 0:
            return float(candidate)
    return None


def _net_flow(row: PositionAlphaRow) -> float:
    """Net within-window dollar flow: bought − sold. Positive = net buyer
    (added), negative = net seller (trimmed). Missing legs read as 0."""
    return float(row.bought_in_window or 0.0) - float(row.sold_in_window or 0.0)


def _row_alpha(row: PositionAlphaRow, *, use_policy: bool) -> float | None:
    """The per-name alpha on the chosen benchmark basis (L-seam 4). When a policy
    benchmark is configured, the policy counterfactual is primary — the
    designated, anti-cherry-pick benchmark the owner committed to — falling back
    to the SPY counterfactual only when a row lacks a policy figure (or no policy
    is set). All three legs (selection / sizing / timing) read the SAME basis so
    the decomposition is internally consistent."""
    if use_policy and row.alpha_vs_policy is not None:
        return row.alpha_vs_policy
    return row.alpha


def decompose_alpha(
    alpha: PositionAlpha | None,
    *,
    conviction_by_ticker: dict[str, float] | None = None,
) -> SkillDecomposition | None:
    """Split realized book alpha into selection / sizing / timing.

    Reads the tracker's ``PositionAlpha`` rows verbatim (per-name dollar alpha,
    start/end value, window flows) plus an optional ``ticker → conviction``
    (1-5) map for the conviction join — supplied by the caller (the sizing
    audit) so this engine never imports the panel. Pure; returns None when the
    tracker is offline (``alpha is None``) or no name carries a usable value
    (nothing to decompose) — the surface then hides rather than asserting.

    Math (W = total usable value, wᵢ = valueᵢ/W, w̄ = 1/N, rᵢ = alphaᵢ/valueᵢ):
      selection = W · w̄ · Σrᵢ   (the alpha if capital were equal-weighted —
                                  pure pick skill, size-neutral)
      sizing    = Σ alphaᵢ − selection = W · Σ(wᵢ − w̄)·rᵢ   (the weight tilt:
                                  did dollar-weighting amplify selection?)
      timing    = Σ sign(net_flowᵢ)·alphaᵢ over traded names   (did adds/trims
                                  lean into subsequent alpha? — diagnostic)
    """
    if alpha is None:
        return None
    conv_map = {k.upper(): v for k, v in (conviction_by_ticker or {}).items()}
    # Policy is the primary basis when configured (L-seam 4); SPY is the fallback.
    use_policy = bool(alpha.has_policy)
    benchmark_basis = "policy" if use_policy else "spy"

    priced: list[tuple[str, float, float, float | None]] = []  # ticker, alpha, value, conviction
    excluded_no_value = 0
    excluded_implausible: list[str] = []
    timing_usd = 0.0
    n_timed = 0
    for row in alpha.rows:
        if row.ticker is None:
            continue
        a_val = _row_alpha(row, use_policy=use_policy)
        if a_val is None:
            continue
        ticker = row.ticker.upper()
        a = float(a_val)
        flow = _net_flow(row)
        if flow != 0.0:
            timing_usd += (1.0 if flow > 0 else -1.0) * a
            n_timed += 1
        value = _usable_value(row)
        if value is None:
            excluded_no_value += 1
            continue
        if abs(a) / value > _MAX_PLAUSIBLE_ALPHA_RATE:
            excluded_implausible.append(ticker)
            continue
        priced.append((ticker, a, value, conv_map.get(ticker)))

    n_names = len(priced)
    if n_names == 0:
        return None

    total_w = sum(value for _, _, value, _ in priced)
    if total_w <= 0:  # pragma: no cover - _usable_value already guarantees > 0
        return None

    # selection = (W/N)·Σrᵢ ; each name's selection share = (W/N)·rᵢ = alphaᵢ/(N·wᵢ).
    mean_w = total_w / n_names
    contributions: list[NameContribution] = []
    selection_usd = 0.0
    for ticker, a, value, conviction in priced:
        rate = a / value
        sel_i = mean_w * rate
        siz_i = a - sel_i
        selection_usd += sel_i
        contributions.append(
            NameContribution(
                ticker=ticker,
                alpha_usd=a,
                selection_usd=sel_i,
                sizing_usd=siz_i,
                conviction=conviction,
            )
        )
    total_alpha_usd = sum(c.alpha_usd for c in contributions)
    sizing_usd = total_alpha_usd - selection_usd

    by_conviction = _roll_up_by_conviction(contributions)
    top = sorted(contributions, key=lambda c: -abs(c.alpha_usd))[:_MAX_CONTRIBUTORS]

    confident = is_confident(n_names)
    basis_label = (
        "your policy benchmark (the designated, anti-cherry-pick benchmark)"
        if use_policy
        else "SPY (no policy benchmark configured)"
    )
    notes: list[str] = [
        f"Alpha basis: vs {basis_label}.",
        f"Selection vs sizing split over {confidence_note(n_names)} priced name"
        f"{'s' if n_names != 1 else ''}.",
    ]
    if not confident:
        notes.append("Thin book — read the split as directional, not a settled skill verdict.")
    if excluded_no_value:
        notes.append(
            f"{excluded_no_value} name(s) had no usable window value and are out of the "
            "selection/sizing split (still in raw alpha upstream)."
        )
    if excluded_implausible:
        notes.append(
            f"{len(excluded_implausible)} name(s) excluded as data-implausible "
            f"({', '.join(sorted(excluded_implausible))}): window alpha implies a "
            f">{_MAX_PLAUSIBLE_ALPHA_RATE:.0%} return on the recorded capital base — "
            "a dust/stale position value, not skill. Out of the split (still in raw "
            "alpha upstream); fix the tracker row to re-admit it."
        )
    notes.append(
        "Timing is a flow-lean diagnostic: the tracker's alpha already nets "
        "benchmark-matched flows, so it scores trade DIRECTION, not a separable bucket."
    )

    return SkillDecomposition(
        window_start=alpha.start_date,
        window_end=alpha.end_date,
        n_names=n_names,
        total_alpha_usd=total_alpha_usd,
        selection_usd=selection_usd,
        sizing_usd=sizing_usd,
        timing_usd=timing_usd if n_timed else None,
        n_timed=n_timed,
        by_conviction=by_conviction,
        top_contributors=top,
        excluded_no_value=excluded_no_value,
        confident=confident,
        notes=notes,
        benchmark_basis=benchmark_basis,
    )


def _roll_up_by_conviction(contributions: list[NameContribution]) -> list[ConvictionAlphaRow]:
    """Group the per-name contributions by conviction bucket — the conviction →
    realized-outcome join. Ordered high → unstated (CONVICTION_ORDER), empty
    buckets dropped."""
    from decision_calibration import CONVICTION_ORDER

    acc: dict[str, dict[str, float]] = {}
    for c in contributions:
        bucket = bucket_for_conviction(c.conviction)
        slot = acc.setdefault(bucket, {"n": 0.0, "alpha": 0.0, "sizing": 0.0, "conv_sum": 0.0,
                                       "conv_n": 0.0})  # fmt: skip
        slot["n"] += 1
        slot["alpha"] += c.alpha_usd
        slot["sizing"] += c.sizing_usd
        if c.conviction is not None:
            slot["conv_sum"] += c.conviction
            slot["conv_n"] += 1
    rows: list[ConvictionAlphaRow] = []
    for bucket in CONVICTION_ORDER:
        slot = acc.get(bucket)
        if slot is None:
            continue
        conv_n = slot["conv_n"]
        rows.append(
            ConvictionAlphaRow(
                conviction=bucket,
                n=int(slot["n"]),
                alpha_usd=slot["alpha"],
                sizing_usd=slot["sizing"],
                mean_conviction=(slot["conv_sum"] / conv_n) if conv_n else None,
            )
        )
    return rows
