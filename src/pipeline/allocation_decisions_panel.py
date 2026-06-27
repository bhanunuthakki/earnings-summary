"""Allocation-decisions record — the Portfolio theme's Decisions tab (master build P2.2).

Two views on one page:

* **Sizing audit** — one row per research holding lining up the four sizing
  inputs the advisor reasons over: stated conviction + target weight (latest
  ``position_sizing_intent`` rows — the owner's *recorded* posture, never
  inferred), actual weight of book (live tracker), the DCF fair-value gap
  (latest ``dcf_runs``, recomputed convention-proof via
  ``research_cockpit.latest_dcf_runs``), and window dollar alpha vs SPY (the
  tracker's ``/position-alpha`` — benchmark math is never rebuilt here).
  Tension between stated posture and actual sizing is scored by transparent
  heuristics and ranked; every point of score renders its reason chip, so the
  ranking is fully explainable. Advisor posture (directive): evidence +
  framing — the chips describe tension, they never instruct.
* **Decisions timeline** — the durable allocation-decisions record:
  ``thesis_ledger_entries`` (accepted thesis edits), ``position_sizing_intent``
  appends (stated sizing-posture changes), and ``analyst_notes`` rows of kind
  ``decision``, merged newest-first. This folds the old Decisions tab's intent
  into the Portfolio theme per the master-build kill list.

The page is also where sizing posture gets *recorded*: each audit row carries
an inline editor that POSTs to ``/api/sizing-intents`` (append-only history —
``user_state.sizing`` never updates in place), so conviction/target columns
fill with real owner statements over time.

Mismatch score weights are deliberately coarse — the score exists to RANK rows
for attention, not to measure anything. Components (each gated on its inputs
being present, each emitting a chip):

* target drift        — |actual − stated target| in pp, when both exist
* conviction inversion — high conviction sitting well below the median weight,
                         or low conviction well above it
* thesis stress at size — warn/breach verdict carried at >= median weight
* valuation tension   — rich vs DCF fair value at size, or cheap with high
                         conviction but a below-median weight
* alpha drag          — window alpha <= −10% of position value at >= median
                         weight (small weight: one window is weak evidence)

Degrades gracefully (the established tracker pattern): tracker offline → the
audit still renders thesis/conviction/valuation columns with weight/alpha
dashed and one offline note; empty intent history → a hint to record posture.
Every DB read tolerates a missing table (hand-rolled test schemas render a
sparser page).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from statistics import median

from attribution import SkillDecomposition, decompose_alpha
from calibration_guard import confidence_note, is_confident
from decision_calibration import (
    CalibrationStats,
    CohortPeriod,
    ConvictionBucket,
    build_calibration,
    realized_magnitudes,
)
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import (
    BetaStats,
    LivePortfolio,
    PositionAlpha,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
)
from pipeline.research_cockpit import latest_dcf_runs, latest_dcf_scenarios
from ui import living_grid as lg
from ui.prose import render_prose
from user_state.ledger import list_recent_entries
from user_state.notes import list_notes
from user_state.sizing import PositionSizingIntentRow, list_intents

# Intent kinds the audit columns read; the editor writes these two.
CONVICTION_KIND = "conviction"
TARGET_WEIGHT_KIND = "target_weight_pct"

_VERDICT_TONE: dict[str, str] = {
    "ok": "ok",
    "warn": "warn",
    "breach": "bad",
    "unresolved": "muted",
}


@dataclass(frozen=True, slots=True)
class SizingAuditRow:
    """One holding's sizing-audit line, fully assembled and scored."""

    ticker: str
    name: str | None
    verdict: str | None  # thesis_evaluations.overall_status (ok/warn/breach/unresolved)
    conviction: float | None  # latest 'conviction' intent value (1-5)
    conviction_at: str | None  # ISO date the conviction was stated
    target_weight_pct: float | None  # latest 'target_weight_pct' intent value
    target_at: str | None
    weight_pct: float | None  # live % of book (tracker)
    market_value: float | None
    fv_gap_pct: float | None  # live_price/npv_per_share - 1 (% -- + = above FV)
    alpha_usd: float | None  # window dollar alpha vs SPY (tracker)
    alpha_frac: float | None  # alpha / value_at_end, when value > 0
    mismatch_score: float = 0.0
    mismatch_reasons: list[str] = field(default_factory=list[str])
    # Scenario-range legs (asymmetry): price vs the bull / bear fair value, in
    # the same +=above-FV convention as fv_gap_pct. None when the run carries no
    # scenario range. Appended (defaulted) so existing constructors are unchanged.
    bull_gap_pct: float | None = None
    bear_gap_pct: float | None = None


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One row of the merged decisions timeline."""

    when: datetime
    ticker: str | None
    kind: str  # ledger entry_kind | 'sizing_intent' | 'decision_note'
    label: str  # pill text
    body: str


# --------------------------------------------------------------------------- #
# Sizing audit — scoring
# --------------------------------------------------------------------------- #


def _gap_vs(price: float | None, fair_value: float | None) -> float | None:
    """price / fair_value − 1 in percent (the fv_gap_pct convention: + = price
    above this fair value). None when either leg is missing or non-positive."""
    if price is None or fair_value is None or price <= 0.0 or fair_value <= 0.0:
        return None
    return (price / fair_value - 1.0) * 100.0


def score_row(
    *,
    verdict: str | None,
    conviction: float | None,
    target_weight_pct: float | None,
    weight_pct: float | None,
    fv_gap_pct: float | None,
    alpha_usd: float | None,
    alpha_frac: float | None,
    median_weight: float | None,
    bull_gap_pct: float | None = None,
    bear_gap_pct: float | None = None,
) -> tuple[float, list[str]]:
    """Score one holding's stated-vs-actual sizing tension. Returns
    ``(points, reason chips)`` — every point contribution has a chip.

    ``bull_gap_pct`` / ``bear_gap_pct`` (price vs the bull / bear scenario fair
    value, same sign as ``fv_gap_pct``: + = price above that case) let the
    valuation-tension heuristic read the *asymmetry* of the DCF range, not just
    the base point estimate — both default None (no scenario range → the base-
    only behaviour, unchanged)."""
    pts = 0.0
    reasons: list[str] = []
    rel = (
        weight_pct / median_weight
        if weight_pct is not None and median_weight is not None and median_weight > 0
        else None
    )

    # 1. Target drift — the owner stated a target; the book disagrees.
    if target_weight_pct is not None and weight_pct is not None:
        drift = weight_pct - target_weight_pct
        if abs(drift) >= 1.0:
            pts += min(abs(drift), 10.0)
            reasons.append(f"{drift:+.1f}pp vs stated target {target_weight_pct:.1f}%")

    # 2. Conviction inversion — stated conviction and relative weight disagree.
    if conviction is not None and rel is not None:
        if conviction >= 4.0 and rel <= 0.75:
            pts += min((conviction - 3.0) * (0.75 - rel) * 10.0, 8.0)
            reasons.append(
                f"conviction {conviction:g}/5 but {weight_pct:.1f}% of book "
                f"(median {median_weight:.1f}%)"
            )
        elif conviction <= 2.0 and rel >= 1.25:
            pts += min((3.0 - conviction) * (rel - 1.25) * 10.0, 8.0)
            reasons.append(f"conviction {conviction:g}/5 at {rel:.1f}x median weight")

    # 3. Thesis stress at size — a stressed thesis carried at >= median weight.
    if verdict in ("warn", "breach") and rel is not None and rel >= 1.0:
        base = 6.0 if verdict == "breach" else 3.0
        pts += base * min(rel, 2.0)
        reasons.append(f"thesis {verdict} at {weight_pct:.1f}% of book")

    # 4. Valuation tension — price vs DCF fair value against the size held.
    if fv_gap_pct is not None and rel is not None:
        if fv_gap_pct >= 25.0 and rel >= 1.0:
            pts += min(fv_gap_pct / 25.0, 3.0) * min(rel, 2.0) * 1.5
            reasons.append(f"+{fv_gap_pct:.0f}% vs DCF FV at {weight_pct:.1f}% of book")
        elif fv_gap_pct <= -25.0 and conviction is not None and conviction >= 4.0 and rel <= 0.9:
            pts += min(-fv_gap_pct / 25.0, 3.0) * 2.0
            reasons.append(
                f"{fv_gap_pct:.0f}% vs DCF FV with conviction {conviction:g}/5 "
                "at below-median weight"
            )

    # 4b. Scenario-range asymmetry — the point estimate can hide that even the
    # bull case has no headroom, or that the bear case still clears the price.
    # Additive over #4 and only firing when the run carries a scenario range.
    if rel is not None:
        if bull_gap_pct is not None and bull_gap_pct >= 0.0 and rel >= 1.0:
            # Price at/above even the optimistic fair value: no upside if the
            # thesis goes right, at size.
            pts += min(bull_gap_pct / 25.0, 2.0) * min(rel, 2.0)
            reasons.append(
                f"+{bull_gap_pct:.0f}% vs the bull case (no upside even if it works) "
                f"at {weight_pct:.1f}% of book"
            )
        if (
            bear_gap_pct is not None
            and bear_gap_pct <= 0.0
            and conviction is not None
            and conviction >= 4.0
            and rel <= 0.9
        ):
            # Price below even the pessimistic fair value: downside-protected,
            # yet sized below the median despite high conviction.
            pts += min(-bear_gap_pct / 25.0, 2.0) * 1.5
            reasons.append(
                f"{bear_gap_pct:.0f}% vs the bear case (downside-protected) with conviction "
                f"{conviction:g}/5 at below-median weight"
            )

    # 5. Alpha drag — one window is weak evidence, so this contributes little.
    if (
        alpha_usd is not None
        and alpha_frac is not None
        and alpha_frac <= -0.10
        and rel is not None
        and rel >= 1.0
    ):
        pts += 1.5
        reasons.append(
            f"window alpha -${abs(alpha_usd):,.0f} ({alpha_frac * 100.0:.0f}% of position)"
        )

    return round(pts, 1), reasons


def build_sizing_audit_rows(
    holdings: list[tuple[str, str | None]],
    verdicts: dict[str, str],
    dcf_gaps: dict[str, tuple[float | None, float | None, float | None, str | None]],
    intents: list[PositionSizingIntentRow],
    live: LivePortfolio,
    alpha: PositionAlpha | None,
    dcf_scenarios: dict[str, tuple[float | None, float | None]] | None = None,
) -> list[SizingAuditRow]:
    """Assemble + score the audit, ranked worst-first (then by weight).

    Pure over already-fetched inputs — no network, no DB — so the scorer is
    directly testable. ``holdings`` is the research portfolio list; tracker
    positions outside it are deliberately not audited (see the coverage
    footnote in the renderer). ``dcf_scenarios`` (ticker -> (bull_fv, bear_fv),
    from ``research_cockpit.latest_dcf_scenarios``) lets the valuation-tension
    heuristic read the asymmetry of each run's range; None / a missing name
    keeps the base-only behaviour.
    """
    dcf_scenarios = dcf_scenarios or {}
    latest_by_kind: dict[tuple[str, str], PositionSizingIntentRow] = {}
    for row in intents:  # list_intents returns newest-first; first one wins
        key = (row.ticker.upper(), row.intent_kind)
        latest_by_kind.setdefault(key, row)

    weights: dict[str, tuple[float | None, float | None]] = {}
    if live.available:
        for pos in live.positions:
            if pos.ticker:
                weights[pos.ticker.upper()] = (pos.percent_of_portfolio, pos.market_value)

    alpha_by_ticker: dict[str, tuple[float | None, float | None]] = {}
    if alpha is not None:
        for r in alpha.rows:
            if r.ticker is None or r.alpha is None:
                continue
            frac = (
                r.alpha / r.value_at_end
                if r.value_at_end is not None and r.value_at_end > 0
                else None
            )
            alpha_by_ticker[r.ticker.upper()] = (r.alpha, frac)

    held = [
        w
        for w, _mv in (weights.get(t.upper(), (None, None)) for t, _n in holdings)
        if w is not None
    ]
    median_weight = median(held) if held else None

    rows: list[SizingAuditRow] = []
    for ticker, name in holdings:
        t = ticker.upper()
        conv = latest_by_kind.get((t, CONVICTION_KIND))
        target = latest_by_kind.get((t, TARGET_WEIGHT_KIND))
        weight_pct, market_value = weights.get(t, (None, None))
        _gap, _npv, live_price, _vd = dcf_gaps.get(t, (None, None, None, None))
        fv_gap = _gap
        bull_fv, bear_fv = dcf_scenarios.get(t, (None, None))
        bull_gap = _gap_vs(live_price, bull_fv)
        bear_gap = _gap_vs(live_price, bear_fv)
        alpha_usd, alpha_frac = alpha_by_ticker.get(t, (None, None))
        verdict = verdicts.get(t)
        score, reasons = score_row(
            verdict=verdict,
            conviction=conv.intent_value if conv else None,
            target_weight_pct=target.intent_value if target else None,
            weight_pct=weight_pct,
            fv_gap_pct=fv_gap,
            alpha_usd=alpha_usd,
            alpha_frac=alpha_frac,
            median_weight=median_weight,
            bull_gap_pct=bull_gap,
            bear_gap_pct=bear_gap,
        )
        rows.append(
            SizingAuditRow(
                ticker=t,
                name=name,
                verdict=verdict,
                conviction=conv.intent_value if conv else None,
                conviction_at=conv.created_at.date().isoformat() if conv else None,
                target_weight_pct=target.intent_value if target else None,
                target_at=target.created_at.date().isoformat() if target else None,
                weight_pct=weight_pct,
                market_value=market_value,
                fv_gap_pct=fv_gap,
                alpha_usd=alpha_usd,
                alpha_frac=alpha_frac,
                mismatch_score=score,
                mismatch_reasons=reasons,
                bull_gap_pct=bull_gap,
                bear_gap_pct=bear_gap,
            )
        )
    rows.sort(key=lambda r: (-r.mismatch_score, -(r.weight_pct or 0.0), r.ticker))
    return rows


# --------------------------------------------------------------------------- #
# Decisions timeline
# --------------------------------------------------------------------------- #

_LEDGER_LABELS: dict[str, str] = {
    "thesis_update": "Thesis update",
    "bear_append": "Bear case",
    "earnings_prep_append": "Earnings prep",
}


def _intent_body(row: PositionSizingIntentRow) -> str:
    if row.intent_kind == CONVICTION_KIND and row.intent_value is not None:
        head = f"conviction {row.intent_value:g}/5"
    elif row.intent_kind == TARGET_WEIGHT_KIND and row.intent_value is not None:
        head = f"target weight {row.intent_value:g}%"
    elif row.intent_value is not None:
        head = f"{row.intent_kind} = {row.intent_value:g}"
    else:
        head = row.intent_kind
    return f"{head} — {row.narrative}" if row.narrative else head


def build_decisions_timeline(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    intents: list[PositionSizingIntentRow] | None = None,
    limit: int = 150,
) -> list[TimelineEvent]:
    """The merged allocation-decisions record, newest-first.

    Folds three histories: accepted thesis edits (``thesis_ledger_entries``),
    stated sizing-posture changes (``position_sizing_intent``), and decision
    notes (``analyst_notes`` kind=decision, live statuses). Each source
    degrades to empty when its table is missing.
    """
    events: list[TimelineEvent] = []
    try:
        for e in list_recent_entries(user_id=user_id, limit=limit, db_path=db_path):
            events.append(
                TimelineEvent(
                    when=e.created_at,
                    ticker=e.ticker,
                    kind=e.entry_kind,
                    label=_LEDGER_LABELS.get(e.entry_kind, e.entry_kind),
                    body=e.body,
                )
            )
    except sqlite3.OperationalError:
        pass
    try:
        intent_rows = (
            intents if intents is not None else list_intents(user_id=user_id, db_path=db_path)
        )
        for row in intent_rows:
            events.append(
                TimelineEvent(
                    when=row.created_at,
                    ticker=row.ticker,
                    kind="sizing_intent",
                    label="Sizing intent",
                    body=_intent_body(row),
                )
            )
    except sqlite3.OperationalError:
        pass
    try:
        for note in list_notes(user_id=user_id, kind="decision", db_path=db_path):
            events.append(
                TimelineEvent(
                    when=note.created_at,
                    ticker=note.ticker,
                    kind="decision_note",
                    label="Decision note",
                    body=note.body,
                )
            )
    except sqlite3.OperationalError:
        pass
    events.sort(key=lambda e: e.when.replace(tzinfo=None), reverse=True)
    return events[:limit]


# --------------------------------------------------------------------------- #
# DB reads (panel-local; each tolerates a missing table)
# --------------------------------------------------------------------------- #


def _safe_rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return []
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


def portfolio_holdings(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    rows = _safe_rows(
        conn,
        "SELECT ticker, name FROM tracked_companies "
        "WHERE list_type = 'portfolio' AND archived_at IS NULL ORDER BY ticker",
    )
    return [(str(r["ticker"]), str(r["name"]) if r["name"] else None) for r in rows]


def latest_verdicts(conn: sqlite3.Connection) -> dict[str, str]:
    rows = _safe_rows(
        conn,
        "SELECT ticker, overall_status FROM thesis_evaluations ORDER BY ticker, evaluated_at DESC",
    )
    out: dict[str, str] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        if t not in out and r["overall_status"]:
            out[t] = str(r["overall_status"])
    return out


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #


def render_allocation_decisions_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
) -> str:
    """The Decisions tab fragment: sizing audit (ranked) + decisions timeline.

    Fetches the tracker's live weights + position alpha (only those two call
    families — ``only={"position_alpha"}`` skips the rest) and joins them onto
    the research holdings; never raises on a tracker problem.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        holdings = portfolio_holdings(conn)
        verdicts = latest_verdicts(conn)
        dcf_gaps = latest_dcf_runs(conn)
        dcf_scenarios = latest_dcf_scenarios(conn)
    finally:
        conn.close()
    try:
        intents = list_intents(user_id=user_id, db_path=db_path)
    except sqlite3.OperationalError:
        intents = []
    live = fetch_live_portfolio(api_url=api_url)
    # exit_quality is opt-in — the sell-side magnitude for the batting-vs-slugging
    # view (L-seam 1) that rides beside the hit-rate buckets.
    # beta carries the Jensen alpha joined beside the decomposition (L-seam 5).
    analytics = fetch_portfolio_analytics(
        api_url=api_url, only={"position_alpha", "exit_quality", "beta"}
    )
    audit = build_sizing_audit_rows(
        holdings, verdicts, dcf_gaps, intents, live, analytics.position_alpha, dcf_scenarios
    )
    timeline = build_decisions_timeline(db_path, user_id=user_id, intents=intents)
    calibration = build_calibration(
        db_path=db_path,
        magnitudes_by_ticker=realized_magnitudes(analytics.position_alpha, analytics.exit_quality),
    )
    attribution = decompose_alpha(
        analytics.position_alpha, conviction_by_ticker=_conviction_by_ticker(audit)
    )
    return compose_decisions_page(
        audit,
        timeline,
        live,
        analytics.position_alpha,
        calibration=calibration,
        attribution=attribution,
        scorecard_html=_scorecard_html(db_path),
        beta=analytics.beta,
    )


def _scorecard_html(db_path: Path) -> str:
    """The L8 coach's-read section (latest persisted scorecard) as ready-to-mount
    HTML with its own <style>, or "" when none exists. Lazy-imports the coach +
    scorecard panel so this module — which the coach imports — stays cycle-free,
    and never lets a coach-side error break the decisions page."""
    try:
        from calibration_coach import load_latest_scorecard
        from pipeline.calibration_scorecard_panel import SCORECARD_CSS, render_scorecard_section

        card = load_latest_scorecard(db_path.resolve().parent.parent)
        section = render_scorecard_section(card)
        return f"<style>{SCORECARD_CSS}</style>{section}" if section else ""
    except Exception:  # pragma: no cover - coaching must never break the page
        return ""


def _conviction_by_ticker(audit: list[SizingAuditRow]) -> dict[str, float]:
    """The stated-conviction map the shared attribution engine joins on — pulled
    off the audit's conviction column so the engine never imports this panel."""
    return {r.ticker: r.conviction for r in audit if r.conviction is not None}


def compose_decisions_page(
    audit: list[SizingAuditRow],
    timeline: list[TimelineEvent],
    live: LivePortfolio,
    alpha: PositionAlpha | None,
    calibration: CalibrationStats | None = None,
    attribution: SkillDecomposition | None = None,
    scorecard_html: str = "",
    beta: BetaStats | None = None,
) -> str:
    """Pure page assembly (testable without network or DB). ``calibration``
    None (pre-0046 substrate) hides the section entirely; ``attribution`` None
    (tracker offline / nothing to decompose) hides the skill block;
    ``scorecard_html`` "" (no L8 scorecard generated yet) hides the coach's-read
    section; ``beta`` carries the Jensen alpha joined beside the decomposition
    (L-seam 5). The coach section is pre-rendered upstream (with its own <style>)
    so this module never imports calibration_coach (which imports this one)."""
    return "".join(
        [
            _PANEL_CSS,
            _audit_section(audit, live, alpha),
            _calibration_section(calibration) if calibration is not None else "",
            _skill_decomposition_section(attribution, beta) if attribution is not None else "",
            scorecard_html,
            _timeline_section(timeline),
            f"<script>{_EDITOR_JS}</script>",
        ]
    )


def _audit_section(
    audit: list[SizingAuditRow], live: LivePortfolio, alpha: PositionAlpha | None
) -> str:
    head = (
        '<section class="panel"><h2>Sizing audit</h2>'
        '<p class="sub">Stated posture (conviction · target, from your recorded sizing '
        "intents) vs the live book (weight · window alpha from the tracker) vs the model "
        "(thesis verdict · DCF gap). Tension is scored to rank attention — every point "
        "renders its reason; nothing here is a directive.</p>"
    )
    if not audit:
        return (
            f"{head}"
            '<p class="muted">No research portfolio holdings found '
            "(tracked_companies has no portfolio rows).</p></section>"
        )
    notes: list[str] = []
    if not live.available:
        notes.append(
            "Tracker offline — weight and alpha columns are dashed "
            f"(<code>{escape(live.api_url)}</code>"
            f"{f' — {escape(live.error)}' if live.error else ''})."
        )
    elif alpha is None:
        notes.append("Tracker reachable but /position-alpha failed — alpha column dashed.")
    else:
        covered = sum(r.weight_pct or 0.0 for r in audit)
        window = f"{alpha.start_date or '?'} → {alpha.end_date or '?'}"
        notes.append(
            f"Alpha window {escape(window)} · research holdings cover "
            f"{covered:.0f}% of the live book (the rest is unaudited — index funds, cash, "
            "non-research names)."
        )
    if not any(r.conviction is not None or r.target_weight_pct is not None for r in audit):
        notes.append(
            "No sizing intents recorded yet — use <b>record</b> on a row to state "
            "conviction (1-5) and a target weight; the audit compares stated posture "
            "to the book from then on."
        )
    note_html = "".join(f'<p class="muted ad-note">{n}</p>' for n in notes)
    rows = "".join(_audit_row(r) for r in audit)
    return (
        f"{head}{note_html}"
        '<table class="ad-table"><thead><tr>'
        "<th>Ticker</th><th>Thesis</th>"
        '<th class="num">Conviction</th><th class="num">Target</th>'
        '<th class="num">Weight</th><th class="num">vs DCF FV</th>'
        f'<th class="num">{chr(0x03B1)} vs SPY</th>'
        "<th>Mismatch</th><th></th>"
        "</tr></thead><tbody>"
        f"{rows}"
        "</tbody></table></section>"
    )


def _pill_tone(tone: str) -> str:
    """Map an ok/warn/bad/muted tone word to the kit .k-pill suffix (muted → bare)."""
    return f" k-pill-{tone}" if tone in ("ok", "warn", "bad") else ""


def _audit_row(r: SizingAuditRow) -> str:
    name_attr = f' title="{escape(r.name)}"' if r.name else ""
    verdict = (
        f'<span class="k-pill{_pill_tone(_VERDICT_TONE.get(r.verdict, "muted"))}">{escape(r.verdict)}'
        "</span>"
        if r.verdict
        else '<span class="muted">&mdash;</span>'
    )
    conviction = (
        f'<span title="stated {escape(r.conviction_at or "?")}">{r.conviction:g}/5</span>'
        if r.conviction is not None
        else '<span class="muted">&mdash;</span>'
    )
    target = (
        f'<span title="stated {escape(r.target_at or "?")}">{r.target_weight_pct:g}%</span>'
        if r.target_weight_pct is not None
        else '<span class="muted">&mdash;</span>'
    )
    weight = (
        f'<span title="{_money(r.market_value)}">{r.weight_pct:.1f}%</span>'
        if r.weight_pct is not None
        else '<span class="muted">&mdash;</span>'
    )
    if r.fv_gap_pct is not None:
        gap_tone = "neg" if r.fv_gap_pct > 0 else "pos"
        # When the run carries a scenario range, the cell hover spells out the
        # asymmetry the base gap hides (price vs the bear / bull fair value).
        range_bits = [
            f"vs {label} case {g:+.0f}%"
            for label, g in (("bear", r.bear_gap_pct), ("bull", r.bull_gap_pct))
            if g is not None
        ]
        title = f' title="{escape(" · ".join(range_bits))}"' if range_bits else ""
        gap = f'<span class="{gap_tone}"{title}>{r.fv_gap_pct:+.0f}%</span>'
    else:
        gap = '<span class="muted">&mdash;</span>'
    if r.alpha_usd is not None:
        a_tone = "pos" if r.alpha_usd >= 0 else "neg"
        alpha = f'<span class="{a_tone}">{_money(r.alpha_usd, signed=True)}</span>'
    else:
        alpha = '<span class="muted">&mdash;</span>'
    if r.mismatch_reasons:
        chips = "".join(
            f'<span class="k-chip ad-chip">{escape(c)}</span>' for c in r.mismatch_reasons
        )
        mismatch = f'<b class="ad-score">{r.mismatch_score:g}</b>{chips}'
    else:
        mismatch = '<span class="muted ad-aligned">aligned</span>'
    main = (
        "<tr>"
        f'<td class="ticker"{name_attr}>'
        f'<a href="/ticker/{escape(r.ticker)}" class="ticker-link">{escape(r.ticker)}</a></td>'
        f"<td>{verdict}</td>"
        f'<td class="num">{conviction}</td>'
        f'<td class="num">{target}</td>'
        f'<td class="num">{weight}</td>'
        f'<td class="num">{gap}</td>'
        f'<td class="num">{alpha}</td>'
        f'<td class="ad-mismatch">{mismatch}</td>'
        f'<td><button type="button" class="k-btn k-btn-quiet k-btn-sm ad-edit-btn" '
        f'data-ticker="{escape(r.ticker)}">'
        "record</button></td>"
        "</tr>"
    )
    conv_val = f"{r.conviction:g}" if r.conviction is not None else ""
    target_val = f"{r.target_weight_pct:g}" if r.target_weight_pct is not None else ""
    editor = (
        f'<tr class="ad-editor-row" data-ticker="{escape(r.ticker)}" hidden><td colspan="9">'
        '<div class="ad-editor">'
        f"<span>Conviction</span>"
        f'<select class="ad-conv"><option value="">&mdash;</option>'
        + "".join(
            f'<option value="{v}"{" selected" if conv_val == str(v) else ""}>{v}</option>'
            for v in (1, 2, 3, 4, 5)
        )
        + "</select>"
        f"<span>Target %</span>"
        f'<input type="number" class="ad-target" min="0" max="100" step="0.5" '
        f'value="{escape(target_val)}">'
        f"<span>Why</span>"
        f'<input type="text" class="ad-note-input" placeholder="optional — one line on why">'
        f'<button type="button" class="k-btn k-btn-primary k-btn-sm ad-save-btn" '
        f'data-ticker="{escape(r.ticker)}">'
        "Save intent</button>"
        f'<span class="ad-status muted"></span>'
        "</div></td></tr>"
    )
    return main + editor


_OUTCOME_TONE: dict[str, str] = {"correct": "ok", "wrong": "bad", "mixed": "warn"}

# How many trailing periods the trend curve renders (older ones roll off the
# left so the recent trajectory stays legible).
_MAX_TREND_PERIODS = 12


def _trend_sparkline(cohorts: list[CohortPeriod]) -> str:
    """An inline SVG of the period hit-rates — the 'am I getting better?' curve,
    not a static number. Graded periods are plotted (filled dot = confident n,
    hollow = thin); ungraded periods leave a gap in the line. y maps 0→100%."""
    w, h, pad = max(160, 44 * len(cohorts)), 56, 8
    span_x = max(1, len(cohorts) - 1)
    plot_h = h - 2 * pad

    def x_at(i: int) -> float:
        return pad + (w - 2 * pad) * (i / span_x)

    def y_at(rate: float) -> float:
        return pad + plot_h * (1.0 - rate)

    # The polyline only connects consecutive graded periods; a gap (ungraded
    # period) breaks the line into separate segments so we never imply a value.
    segments: list[list[str]] = []
    current: list[str] = []
    dots: list[str] = []
    for i, c in enumerate(cohorts):
        if c.hit_rate is None:
            if current:
                segments.append(current)
                current = []
            continue
        px, py = x_at(i), y_at(c.hit_rate)
        current.append(f"{px:.1f},{py:.1f}")
        fill = "var(--accent)" if is_confident(c.graded) else "var(--bg)"
        dots.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{fill}" '
            f'stroke="var(--accent)" stroke-width="1.5"><title>'
            f"{escape(c.period)}: {c.hit_rate * 100:.0f}% (n={c.graded})</title></circle>"
        )
    if current:
        segments.append(current)
    mid_y = y_at(0.5)
    lines = "".join(
        f'<polyline points="{" ".join(seg)}" fill="none" stroke="var(--accent)" stroke-width="2" />'
        for seg in segments
        if len(seg) >= 2
    )
    return (
        f'<svg class="adc-spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
        f'role="img" aria-label="Decision hit-rate by period">'
        f'<line x1="{pad}" y1="{mid_y:.1f}" x2="{w - pad}" y2="{mid_y:.1f}" '
        'stroke="var(--border)" stroke-width="1" stroke-dasharray="3 3" />'
        f"{lines}{''.join(dots)}</svg>"
    )


def _cohort_trend_block(stats: CalibrationStats) -> str:
    """The period-over-period trend: a headline direction + the sparkline + a
    compact per-period table (hit-rate, conviction gap, reversal cost). Hidden
    when fewer than two periods carry a graded call (no trend to draw yet)."""
    graded_periods = [c for c in stats.cohorts if c.hit_rate is not None]
    if len(graded_periods) < 2:
        return ""
    cohorts = stats.cohorts[-_MAX_TREND_PERIODS:]
    grain = stats.cohort_granularity
    if stats.hit_rate_delta is None:
        headline = "Trend forming — keep grading to read the direction."
    else:
        pp = stats.hit_rate_delta * 100.0
        word = "improving" if stats.improving else ("flat" if abs(pp) < 0.5 else "slipping")
        tone = "pos" if stats.improving else ("muted" if abs(pp) < 0.5 else "neg")
        headline = (
            f'Latest {grain} hit-rate is <span class="{tone}">{word} ({pp:+.0f}pp</span> '
            "vs the prior period)."
        )

    def _gap_cell(c: CohortPeriod) -> str:
        if c.conviction_gap is None:
            return '<span class="muted">&mdash;</span>'
        g = c.conviction_gap * 100.0
        tone = "pos" if g >= 0 else "neg"
        return f'<span class="{tone}">{g:+.0f}pp</span>'

    rows = "".join(
        "<tr>"
        f"<td>{escape(c.period)}</td>"
        f'<td class="num">{c.graded}/{c.total}</td>'
        f'<td class="num">{f"{c.hit_rate * 100:.0f}%" if c.hit_rate is not None else "&mdash;"}'
        + ("" if c.hit_rate is None or is_confident(c.graded) else '<sup title="thin n">*</sup>')
        + "</td>"
        f'<td class="num">{_gap_cell(c)}</td>'
        f'<td class="num">{c.reversals_cost or "&mdash;" if c.reversals_cost else "&mdash;"}</td>'
        "</tr>"
        for c in cohorts
    )
    return (
        '<h3 class="adc-sub">Am I getting better? — hit rate by ' + escape(grain) + "</h3>"
        f'<p class="adc-line">{headline}</p>'
        f"{_trend_sparkline(cohorts)}"
        '<table class="ad-table adc-table adc-trend"><thead><tr>'
        f"<th>{escape(grain).capitalize()}</th>"
        '<th class="num">Graded/made</th><th class="num">Hit rate</th>'
        '<th class="num" title="high-conviction hit-rate minus the rest, that period">'
        "Conviction gap</th>"
        '<th class="num" title="reversals that cost — you overrode a call that graded correct">'
        "Reversal cost</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _calibration_section(stats: CalibrationStats) -> str:
    """The decisions-history analysis (S15 PR2): hit rate by conviction,
    action mix + reversal track record, time-to-outcome. Deterministic
    aggregates only — the sub-line states the denominator rule."""
    head = (
        '<section class="panel"><h2>Decision calibration</h2>'
        '<p class="sub">How the recorded recommendations actually graded — by stated '
        "conviction, by your response to them, and by how long a call takes to resolve. "
        "Hit rate = correct &divide; graded (correct + wrong + mixed); pending and "
        "unfalsifiable calls are shown but never scored.</p>"
    )
    if stats.total == 0:
        return (
            f"{head}"
            '<p class="muted">No decisions recorded yet. The morning pipeline\'s stage 0b '
            "extracts ADD/TRIM/HOLD/SELL verdicts from five-minute rereads and Socratic "
            "memos into the ledger; grading accrues via the outcome rung.</p></section>"
        )

    hit = f"{stats.overall_hit_rate * 100.0:.0f}%" if stats.overall_hit_rate is not None else "—"
    rev_n = len(stats.reversals)
    kpis = (
        '<div class="adc-kpis">'
        f'<span class="adc-kpi"><b>{stats.total}</b> decisions</span>'
        f'<span class="adc-kpi"><b>{stats.graded}</b> graded</span>'
        f'<span class="adc-kpi"><b>{hit}</b> hit rate</span>'
        f'<span class="adc-kpi"><b>{rev_n}</b> reversed'
        + (
            f" ({stats.reversals_vindicated} vindicated &middot; {stats.reversals_cost} cost)"
            if rev_n
            else ""
        )
        + "</span></div>"
    )

    conviction_rows = "".join(
        "<tr>"
        f"<td>{escape(b.conviction)}</td>"
        f'<td class="num">{b.graded}</td>'
        f'<td class="num"><span class="pos">{b.correct}</span></td>'
        f'<td class="num"><span class="neg">{b.wrong}</span></td>'
        f'<td class="num">{b.mixed}</td>'
        f'<td class="num muted">{b.ungraded}</td>'
        f'<td class="num">{f"{b.hit_rate * 100.0:.0f}%" if b.hit_rate is not None else "&mdash;"}'
        "</td>"
        f'<td class="num muted">{_wilson_cell(b)}</td>'
        "</tr>"
        for b in stats.by_conviction
    )
    conviction_table = (
        '<table class="ad-table adc-table"><thead><tr>'
        '<th>Conviction</th><th class="num">Graded</th><th class="num">Correct</th>'
        '<th class="num">Wrong</th><th class="num">Mixed</th><th class="num">Ungraded</th>'
        '<th class="num">Hit rate</th><th class="num">95% CI</th>'
        f"</tr></thead><tbody>{conviction_rows}</tbody></table>"
        if conviction_rows
        else ""
    )
    brier_line = _brier_line(stats)
    expectancy_line = _expectancy_line(stats)

    mix_bits = " &middot; ".join(
        f"{label} {stats.action_mix.get(key, 0)}"
        for key, label in (
            ("followed", "followed"),
            ("partial", "partial"),
            ("ignored", "ignored"),
            ("reversed", "reversed"),
            ("unacted", "no action recorded"),
        )
        if stats.action_mix.get(key, 0)
    )
    mix_line = (
        f'<p class="adc-line">Your response to recommendations: {mix_bits}.</p>' if mix_bits else ""
    )

    timing_bits = " &middot; ".join(
        f"{t.kind.upper()} avg {t.avg_days:.0f}d, median {t.median_days:.0f}d (n={t.n})"
        for t in stats.time_to_outcome
    )
    timing_line = f'<p class="adc-line">Time to outcome: {timing_bits}.</p>' if timing_bits else ""

    reversal_rows = "".join(
        "<tr>"
        f'<td class="when">{escape(r.made_at)}</td>'
        f'<td class="tk">{escape(r.ticker)}</td>'
        f"<td>{escape(r.kind.upper())}</td>"
        f"<td>{_reversal_verdict(r.outcome_label, r.vindicated)}</td>"
        "</tr>"
        for r in stats.reversals[:8]
    )
    reversal_table = (
        '<h3 class="adc-sub">Reversals</h3>'
        '<table class="ad-timeline adc-table"><thead><tr>'
        "<th>Made</th><th>Ticker</th><th>Call</th><th>How it graded</th>"
        f"</tr></thead><tbody>{reversal_rows}</tbody></table>"
        if reversal_rows
        else ""
    )

    trend_block = _cohort_trend_block(stats)
    omission_block = _omission_block(stats)
    return (
        f"{head}{kpis}{trend_block}{conviction_table}{brier_line}{expectancy_line}"
        f"{mix_line}{timing_line}{omission_block}{reversal_table}</section>"
    )


def _signed_usd0(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def _wilson_cell(b: ConvictionBucket) -> str:
    """The bucket's Wilson 95% CI (L-seam 3) as a band, or em-dash when nothing
    is graded — so a thin denominator reads as a range, not a false point."""
    if b.wilson_low is None or b.wilson_high is None:
        return "&mdash;"
    return f"{b.wilson_low * 100.0:.0f}&ndash;{b.wilson_high * 100.0:.0f}%"


def _brier_line(stats: CalibrationStats) -> str:
    """Proper-scoring Brier on the owner's own conviction (L-seam 2) — does the
    conviction language discriminate, or is it just loud? Min-n framed."""
    cc = stats.conviction_calibration
    if cc is None or cc.brier is None or cc.baseline_brier is None:
        return ""
    verdict = (
        "beats a flat base-rate guess &mdash; your conviction labels carry signal"
        if cc.beats_baseline
        else "does not beat a flat base-rate guess &mdash; the labels add little over your average"
    )
    return (
        f'<p class="adc-line">Conviction Brier <b>{cc.brier:.3f}</b> vs '
        f"{cc.baseline_brier:.3f} baseline ({escape(confidence_note(cc.n))}): {verdict}.</p>"
    )


def _expectancy_line(stats: CalibrationStats) -> str:
    """Batting-vs-slugging (L-seam 1): the SIZE of the wins, from the tracker's
    realized per-name magnitudes. Hidden offline (no magnitudes) or below the
    min-n guard (a slugging ratio on a handful of names is noise)."""
    exp = stats.expectancy
    if exp is None or not is_confident(exp.n):
        return ""
    parts = [f"expected <b>{_signed_usd0(exp.expectancy)}</b> realized alpha/call"]
    if exp.avg_win is not None and exp.avg_loss is not None:
        slug = f", slugging {exp.slugging:.1f}&times;" if exp.slugging is not None else ""
        parts.append(
            f"winners avg {_signed_usd0(exp.avg_win)} vs losers "
            f"{_signed_usd0(-exp.avg_loss)}{slug}"
        )
    return (
        f'<p class="adc-line">Batting vs slugging ({escape(confidence_note(exp.n))}): '
        + "; ".join(parts)
        + ".</p>"
    )


def _omission_block(stats: CalibrationStats) -> str:
    """The errors-of-omission ledger (L11): how the names the owner PASSED on
    graded — a pass that ran away is the miss. Min-n framed; hidden until at
    least one pass has been graded. Kit classes only (S1-conformant)."""
    om = stats.omissions
    if om is None or not om.graded:
        return ""
    miss = f"{om.miss_rate * 100.0:.0f}%" if om.miss_rate is not None else "—"
    head = (
        '<h3 class="adc-sub">Errors of omission</h3>'
        f'<p class="adc-line">Of <b>{om.graded}</b> passed names graded, '
        f'<span class="neg">{om.missed}</span> ran away (missed), '
        f'<span class="pos">{om.dodged}</span> correctly dodged, {om.mixed} mixed — '
        f"miss rate {miss} ({escape(confidence_note(om.graded))}).</p>"
    )
    if not om.worst_misses:
        return head
    rows = "".join(
        "<tr>"
        f'<td class="when">{escape(m.made_at)}</td>'
        f'<td class="tk">{escape(m.ticker)}</td>'
        '<td class="num"><span class="neg">'
        f"{f'+{m.outcome_pct * 100.0:.0f}%' if m.outcome_pct is not None else 'ran'}</span></td>"
        "</tr>"
        for m in om.worst_misses
    )
    table = (
        '<table class="ad-timeline adc-table"><thead><tr>'
        '<th>Passed</th><th>Ticker</th><th class="num">Move since</th>'
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )
    return head + table


def _reversal_verdict(outcome_label: str | None, vindicated: bool | None) -> str:
    if outcome_label is None:
        return '<span class="muted">unresolved</span>'
    tone = _OUTCOME_TONE.get(outcome_label, "muted")
    badge = f'<span class="k-pill{_pill_tone(tone)}">{escape(outcome_label)}</span>'
    if vindicated is True:
        return f'{badge} <span class="pos">reversal vindicated</span>'
    if vindicated is False:
        return f'{badge} <span class="neg">reversal cost</span>'
    return badge


def _skill_decomposition_section(d: SkillDecomposition, beta: BetaStats | None = None) -> str:
    """The shared selection/sizing/timing decomposition (L8 §6), rendered as a
    KPI strip + an edge-vs-leak read + the conviction → outcome join, with the
    tracker's Jensen alpha (L-seam 5) joined in beside it. The arithmetic is the
    engine's; this only frames it."""
    window = (
        f"{escape(d.window_start)} → {escape(d.window_end)}"
        if d.window_start and d.window_end
        else "the tracker window"
    )
    basis_text = (
        "your policy benchmark (the designated, anti-cherry-pick benchmark)"
        if d.benchmark_basis == "policy"
        else "the SPY counterfactual"
    )
    head = (
        '<section class="panel"><h2>Skill decomposition</h2>'
        f'<p class="sub">Realized dollar alpha over {window}, split into the repeatable '
        "decisions behind it — <b>selection</b> (which names, size-neutral), <b>sizing</b> "
        "(did your weighting amplify selection), <b>timing</b> (did within-window adds/trims "
        f"lean into alpha). Selection + sizing is an exact split; timing is a flow-lean "
        f"diagnostic. Alpha is the tracker's {basis_text} — never recomputed here.</p>"
    )

    def kpi(label: str, v: float | None) -> str:
        body = _money(v, signed=True) if v is not None else "&mdash;"
        tone = "" if v is None else (" pos" if v >= 0 else " neg")
        return (
            f'<span class="adc-kpi"><b class="sk-val{tone}">{body}</b>'
            f'<span class="sk-lbl">{label}</span></span>'
        )

    kpis = (
        '<div class="adc-kpis sk-kpis">'
        + kpi("total alpha", d.total_alpha_usd)
        + kpi("selection", d.selection_usd)
        + kpi("sizing", d.sizing_usd)
        + kpi("timing", d.timing_usd)
        + "</div>"
    )

    read = _skill_read(d)
    jensen = _jensen_line(beta)

    def _rating(v: float | None) -> str:
        return f"{v:.1f}/5" if v is not None else "&mdash;"

    conv_rows = "".join(
        "<tr>"
        f"<td>{escape(c.conviction)}</td>"
        f'<td class="num">{c.n}</td>'
        f'<td class="num">{_signed_span(c.alpha_usd)}</td>'
        f'<td class="num">{_signed_span(c.sizing_usd)}</td>'
        f'<td class="num">{_rating(c.mean_conviction)}</td></tr>'
        for c in d.by_conviction
    )
    conv_table = (
        '<h3 class="adc-sub">Conviction &rarr; outcome</h3>'
        '<table class="ad-table adc-table"><thead><tr>'
        '<th>Conviction</th><th class="num">Names</th><th class="num">Alpha</th>'
        '<th class="num" title="this bucket\'s share of the weight-tilt term">Sizing</th>'
        '<th class="num">Avg rating</th>'
        f"</tr></thead><tbody>{conv_rows}</tbody></table>"
        if conv_rows
        else ""
    )
    notes = "".join(f'<p class="muted ad-note">{escape(n)}</p>' for n in d.notes)
    return f"{head}{kpis}{jensen}{read}{conv_table}{notes}</section>"


def _jensen_line(beta: BetaStats | None) -> str:
    """The tracker's Jensen alpha (L-seam 5) — the regression-intercept alpha vs
    one benchmark, annualized — joined beside the dollar decomposition. Carries
    the skill-vs-luck verdict from the new beta trio (is it distinguishable from
    zero?). Pure presentation; the value is the tracker's, never recomputed."""
    if beta is None or beta.alpha_annualized_pct is None:
        return ""
    bench = escape(beta.benchmark or "the benchmark")
    val = f"{beta.alpha_annualized_pct:+.1f}%"
    tone = "pos" if beta.alpha_annualized_pct >= 0 else "neg"
    sig = ""
    if beta.alpha_significant is not None:
        t = f", t={beta.alpha_t_stat:.1f}" if beta.alpha_t_stat is not None else ""
        sig = (
            f' &mdash; <span class="pos">statistically distinguishable from zero{t}</span>'
            if beta.alpha_significant
            else f' &mdash; <span class="muted">not distinguishable from zero — '
            f"could be luck{t}</span>"
        )
    return (
        f'<p class="adc-line">Jensen &alpha; (annualized regression intercept vs {bench}): '
        f'<b class="{tone}">{val}</b>{sig}.</p>'
    )


def _signed_span(v: float) -> str:
    return f'<span class="{"pos" if v >= 0 else "neg"}">{_money(v, signed=True)}</span>'


def _skill_read(d: SkillDecomposition) -> str:
    """One PM-voice line naming the edge (most positive leg) and the leak (most
    negative), hedged when the book is too thin to assert a verdict."""
    legs = [("selection", d.selection_usd), ("sizing", d.sizing_usd), ("timing", d.timing_usd)]
    present = [(name, v) for name, v in legs if v is not None]
    if not present:
        return ""
    edge_name, edge_v = max(present, key=lambda kv: kv[1])
    leak_name, leak_v = min(present, key=lambda kv: kv[1])
    bits: list[str] = []
    if edge_v > 0:
        bits.append(f"your edge was <b>{edge_name}</b> ({_money(edge_v, signed=True)})")
    if leak_v < 0 and leak_name != edge_name:
        bits.append(f"your leak was <b>{leak_name}</b> ({_money(leak_v, signed=True)})")
    if not bits:
        return ""
    lead = "This window, " if d.confident else "Directional only (thin book) — this window, "
    return f'<p class="adc-line sk-read">{lead}{"; ".join(bits)}.</p>'


# Timeline kind → semantic tone word for its filled status pill. Tones exist
# only where they carry meaning (bear append = bad, thesis update = ok); every
# other kind (sizing intent, decision note, the old accent default) maps to no
# tone and renders the neutral bare .k-pill — color is for meaning, not
# category, and accent on a non-interactive timeline pill was decoration.
_TIMELINE_PILL_TONE: dict[str, str] = {
    "bear_append": "bad",
    "thesis_update": "ok",
}


def _timeline_row(e: TimelineEvent) -> str:
    iso = e.when.date().isoformat()
    ticker = e.ticker or ""
    data = (
        lg.data_text(f"{ticker} {e.label} {e.body[:200]}")
        + lg.data_text_key("date", iso)
        + lg.data_text_key("ticker", ticker)
        + lg.data_text_key("kind", e.label)
    )
    return (
        f"<tr{data}>"
        f'<td class="when">{escape(iso)}</td>'
        f'<td class="tk">{escape(e.ticker or "—")}</td>'
        f'<td><span class="k-pill{_pill_tone(_TIMELINE_PILL_TONE.get(e.kind, "muted"))}">'
        f"{escape(e.label)}</span></td>"
        f'<td class="ad-body">{render_prose(e.body, inline=True)}</td>'
        "</tr>"
    )


def _timeline_section(timeline: list[TimelineEvent]) -> str:
    head = (
        '<section class="panel"><h2>Decisions timeline</h2>'
        '<p class="sub">The durable record of allocation decisions — accepted thesis edits '
        "(ledger), stated sizing intents, and decision notes — merged newest-first.</p>"
    )
    if not timeline:
        return (
            f"{head}"
            '<p class="muted">Nothing recorded yet. Approving an alert action, recording a '
            "sizing intent above, or capturing a decision note all land here.</p></section>"
        )
    rows = "".join(_timeline_row(e) for e in timeline)
    return (
        f"{head}"
        + lg.grid_open()
        + lg.filter_bar(
            len(timeline), noun="decisions", placeholder="Filter by ticker / kind / text…"
        )
        + '<table class="ad-timeline"><thead><tr>'
        + lg.th("Date", "date", "text", num=False)
        + lg.th("Ticker", "ticker", "text", num=False)
        + lg.th("Kind", "kind", "text", num=False)
        + "<th>Decision</th>"
        + "</tr></thead><tbody>"
        + f"{rows}</tbody></table>"
        + lg.grid_close()
        + "</section>"
    )


def _money(v: float | None, *, signed: bool = False) -> str:
    if v is None:
        return "—"
    sign = "+" if signed and v >= 0 else ""
    if abs(v) >= 1000:
        return f"{sign}${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}"
    return f"{sign}${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


_PANEL_CSS = """<style>
.ad-table td, .ad-timeline td { vertical-align: middle; }
.ad-note { font-size: var(--fs-caption); margin: 0 0 10px; }
/* verdict / outcome badges → the kit filled status pill (.k-pill + tone). */
.ad-score { color: var(--warn); font-variant-numeric: tabular-nums; margin-right: 8px; }
/* reason chips compose the kit .k-chip (micro/uppercase/outline); the surface
   adds only the wrap-spacing so several chips stack inside the Mismatch cell. */
.ad-chip { margin: 1px 4px 1px 0; }
.ad-mismatch { max-width: 420px; }
.ad-aligned { font-size: var(--fs-caption); }
.ad-editor { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: var(--fs-caption); padding: 4px 0; }
.ad-editor span { color: var(--muted); }
/* Editor inputs/selects: skinned by the shared control kit (ui/controls.py). */
.ad-editor select, .ad-editor input { padding: 3px 6px; font-size: var(--fs-caption); }
.ad-editor select { padding-right: 26px; }
.ad-editor input.ad-target { width: 70px; }
.ad-editor input.ad-note-input { flex: 1; min-width: 180px; }
.ad-timeline td.tk { font-weight: 600; white-space: nowrap; font-family: var(--mono); }
.ad-timeline td.when { color: var(--muted); white-space: nowrap; }
/* Decision calibration (S15): KPI strip + compact aggregate tables. */
.adc-kpis { display: flex; gap: 18px; flex-wrap: wrap; margin: 2px 0 10px;
  font-size: var(--fs-body); color: var(--muted); }
.adc-kpi b { color: var(--fg); font-variant-numeric: tabular-nums; margin-right: 4px; }
.adc-table { margin-bottom: 10px; }
.adc-line { font-size: var(--fs-caption); color: var(--muted); margin: 4px 0; }
.adc-sub { font-size: var(--fs-section); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin: 12px 0 4px; }
/* Timeline kinds: the filled status pill is now the control kit's .k-pill
   (+ k-pill-bad for bear append, k-pill-ok for thesis update; every other kind
   is the neutral bare .k-pill). Tone is mapped in _TIMELINE_PILL_TONE. */
.ad-body { font-size: var(--fs-body); line-height: 1.5; }
td.pos, span.pos { color: var(--ok); }
td.neg, span.neg { color: var(--bad); }
/* Cohort trend curve (L8): sparkline + per-period table. */
.adc-spark { width: 100%; max-width: 560px; height: 56px; display: block; margin: 2px 0 8px; }
.adc-trend th, .adc-trend td { padding-top: 2px; padding-bottom: 2px; }
.adc-trend sup { color: var(--muted); font-size: 0.7em; }
/* Skill decomposition (L8): money KPIs stack a value over a label. */
.sk-kpis .adc-kpi { display: flex; flex-direction: column; gap: 1px; }
.sk-val { font-variant-numeric: tabular-nums; font-size: var(--fs-body); }
.sk-val.pos { color: var(--ok); }
.sk-val.neg { color: var(--bad); }
.sk-lbl { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; }
.sk-read { font-size: var(--fs-body); color: var(--fg); margin: 2px 0 10px; }
</style>"""

# Editor wiring: toggle a row's intent editor, POST to /api/sizing-intents,
# then refetch this panel fragment (re-running its scripts — innerHTML alone
# does not execute them; same idiom as the portfolio window bar). Plain string,
# not an f-string: braces are literal JS.
_EDITOR_JS = r"""
(function () {
  function panelBody(el) {
    return el.closest('.cc-panel-body') || el.closest('.cc-drawer-sec-body') || document.body;
  }
  function refetch(target) {
    fetch('/api/panel/decisions_record')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) {
        target.innerHTML = html;
        var scripts = target.querySelectorAll('script');
        for (var i = 0; i < scripts.length; i++) {
          var old = scripts[i];
          var s = document.createElement('script');
          if (old.src) s.src = old.src; else s.textContent = old.textContent;
          old.parentNode.replaceChild(s, old);
        }
      })
      .catch(function (e) {
        target.innerHTML = '<div class="cc-empty">Failed to reload (' + e.message + ').</div>';
      });
  }
  document.querySelectorAll('.ad-edit-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var t = btn.getAttribute('data-ticker');
      var row = document.querySelector('.ad-editor-row[data-ticker="' + t + '"]');
      if (row) row.hidden = !row.hidden;
    });
  });
  document.querySelectorAll('.ad-save-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var row = btn.closest('.ad-editor-row');
      var status = row.querySelector('.ad-status');
      var conv = row.querySelector('.ad-conv').value;
      var target = row.querySelector('.ad-target').value;
      var note = row.querySelector('.ad-note-input').value;
      var payload = { ticker: btn.getAttribute('data-ticker') };
      if (conv) payload.conviction = parseFloat(conv);
      if (target) payload.target_weight_pct = parseFloat(target);
      if (note) payload.narrative = note;
      if (payload.conviction === undefined && payload.target_weight_pct === undefined) {
        status.textContent = 'set a conviction and/or a target first';
        return;
      }
      status.textContent = 'saving…';
      fetch('/api/sizing-intents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function () {
        refetch(panelBody(btn));
      }).catch(function (e) {
        status.textContent = 'failed (' + e.message + ')';
      });
    });
  });
})();
""".strip()
