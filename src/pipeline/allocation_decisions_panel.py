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

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from statistics import median
from typing import cast

from attribution import SkillDecomposition, decompose_alpha
from calibration_guard import confidence_note, is_confident
from compute.thesis_evaluation_episodes import episode_history_source
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
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui import living_grid as lg
from ui.controls import chip_tone_class, pill_tone_class, thesis_status_tone, ticker_label
from ui.prose import render_prose
from user_state.ledger import list_recent_entries
from user_state.notes import list_notes
from user_state.sizing import PositionSizingIntentRow, list_intents

# Intent kinds the audit columns read; the editor writes these two.
CONVICTION_KIND = "conviction"
TARGET_WEIGHT_KIND = "target_weight_pct"

# Thesis-verdict tones route through the shared kit resolver
# (ui.controls.thesis_status_tone): the local map here missed `broken`/`watch`/
# `intact`, which rendered as muted gray while other surfaces colored them.


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
    source = episode_history_source(conn)
    rows = _safe_rows(
        conn,
        f"SELECT ticker, overall_status FROM {source.relation} "
        f"ORDER BY ticker, {source.latest_checked_column} DESC",  # nosec B608 -- trusted closed relation
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
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
    n_graded = calibration.graded if calibration is not None else 0
    return compose_decisions_page(
        audit,
        timeline,
        live,
        analytics.position_alpha,
        calibration=calibration,
        attribution=attribution,
        scorecard_html=_scorecard_html(db_path, n_graded=n_graded),
        beta=analytics.beta,
        coach_pnl_html=_coach_pnl_section(db_path, user_id=user_id),
        coach_pings_html=_coach_pings_section(db_path),
        coach_mutes_html=_coach_mutes_section(db_path),
        coach_digest_html=_coach_digest_section(db_path),
        redteam_pnl_html=_redteam_pnl_html(db_path, user_id=user_id),
        annual_letter_html=_annual_letter_html(db_path),
        decision_journal_html=_decision_journal_section(db_path),
    )


def _annual_letter_html(db_path: Path) -> str:
    """The annual letter-to-self section (monthly_red_team.md Phase 3). Never
    lets a failure vanish the section."""
    try:
        from pipeline.annual_letter_panel import render_annual_letter_section

        repo_root = db_path.resolve().parent.parent
        return render_annual_letter_section(repo_root)
    except Exception:  # pragma: no cover - this section must never break the page
        return (
            '<section class="panel"><h2>Letter to self</h2>'
            '<p class="muted">Letter to self unavailable — see logs.</p></section>'
        )


def _redteam_pnl_html(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """The Decision P&L (Red Team) section (monthly_red_team.md Phase 3), mounted
    beside Coach P&L. Never lets a failure vanish the section (same posture as
    ``_scorecard_html``): an exception renders a visible one-line failure stub."""
    try:
        from pipeline.redteam_pnl_panel import REDTEAM_PNL_CSS, render_redteam_pnl_section
        from redteam.decision_pnl import build_decision_pnl, build_yearly_scorecard

        repo_root = db_path.resolve().parent.parent
        report = build_decision_pnl(db_path=db_path, repo_root=repo_root)
        scorecard = build_yearly_scorecard(db_path=db_path, user_id=user_id)
        section = render_redteam_pnl_section(report, scorecard)
        return f"<style>{REDTEAM_PNL_CSS}</style>{section}"
    except Exception:  # pragma: no cover - this section must never break the page
        return (
            '<section class="panel"><h2>Decision P&amp;L (Red Team)</h2>'
            '<p class="muted">Decision P&amp;L unavailable — see logs.</p></section>'
        )


def _scorecard_html(db_path: Path, *, n_graded: int = 0) -> str:
    """The L8 coach's-read section (latest persisted scorecard) as ready-to-mount
    HTML with its own <style>. Lazy-imports the coach + scorecard panel so this
    module — which the coach imports — stays cycle-free. Never lets a coach-side
    error silently vanish the section (REQ-6): an ungenerated scorecard renders
    the panel's own starvation stub (``n_graded`` threaded through so the caption
    reads as progress); an exception renders a visible one-line failure stub
    instead of swallowing to ""."""
    try:
        from calibration_coach import load_latest_scorecard
        from pipeline.calibration_scorecard_panel import SCORECARD_CSS, render_scorecard_section

        card = load_latest_scorecard(db_path.resolve().parent.parent)
        section = render_scorecard_section(card, n_graded=n_graded)
        return f"<style>{SCORECARD_CSS}</style>{section}"
    except Exception:  # pragma: no cover - coaching must never break the page
        return (
            '<section class="panel"><h2>Calibration coach</h2>'
            '<p class="muted">Coach&rsquo;s read: failed to load — see logs.</p></section>'
        )


def _conviction_by_ticker(audit: list[SizingAuditRow]) -> dict[str, float]:
    """The stated-conviction map the shared attribution engine joins on — pulled
    off the audit's conviction column so the engine never imports this panel."""
    return {r.ticker: r.conviction for r in audit if r.conviction is not None}


# --------------------------------------------------------------------------- #
# Coach P&L — REQ-7: "has the coach ever been right?" as its own scoreboard.
# --------------------------------------------------------------------------- #

# The literal Q3'26 success bar the 2026-07-02 thought-partner review set: the
# coach must change >= 1 real decision by Q3'26. Rendered in the KPI line and
# the title tooltip alongside the heuristic that counts toward it.
COACH_CHANGED_TARGET = 1

# How long after a guard_override memo the window runs: only once it has fully
# ELAPSED with no contradicting owner sell/trim is the review a "candidate"
# (never counted toward the target — see _coach_change_tally docstring).
_COACH_CHANGE_WINDOW_DAYS = 30
_SELL_TRIM_KINDS = ("trim", "sell")


@dataclass(frozen=True, slots=True)
class CoachPnl:
    """The guard's public scoreboard — every count the coach must answer for.

    ``reviews_run`` — total ``position_review`` memos ever persisted.
    ``guard_fired`` / ``overridden`` — both count memos whose
    ``context.verdict_source == 'guard_override'`` (apply_behavioral_guard has
    no "fired but didn't change anything" state: it either overrides a
    trim/sell to hold, or passes the LLM verdict through untouched) — kept as
    two named lines because the spec's guard/review line and the literal
    target counter read them as distinct questions ("did the guard ever
    speak?" vs "did it ever change the outcome?"), even though today they are
    the same query.
    ``graded_right`` — of the reviews that have been graded (``stance_scores``
    joined by ``memo_id``), how many graded ``correct``.
    ``changed`` / ``candidate`` — see ``_coach_change_tally``. ``changed`` counts
    ONLY guard_override reviews the owner explicitly attested changed their call
    (the Q3'26 target); ``candidate`` counts the eligible-but-unconfirmed ones
    (window elapsed, no contradicting owner sell/trim, no attestation yet) —
    surfaced honestly but kept OUT of the target so silence can't satisfy it.

    Every count excludes ``source == 'agent'`` memos (verification/CI runs), so
    an automated review never enters the owner-facing scoreboard.
    """

    reviews_run: int
    guard_fired: int
    overridden: int
    graded_right: int
    graded_total: int
    changed: int
    candidate: int


def _memo_context(raw: object) -> dict[str, object]:
    """Parse an ``advisor_memos.context_json`` cell to a dict; ``{}`` on NULL,
    non-JSON, or a non-object payload (so a malformed row degrades, never
    raises)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _query_coach_pnl(db_path: Path, *, user_id: str, now: datetime | None = None) -> CoachPnl:
    """Read-only aggregate over advisor_memos (+ stance_scores, + decisions for
    the change heuristic). Every query degrades to zero on a missing/partial
    table — a thin DB must never 500 the panel (REQ-7's all-zero state IS the
    honest answer, not an error).

    ``source == 'agent'`` reviews (verification/CI runs) are dropped up front so
    they never enter ANY count — the scoreboard reflects owner-driven reviews
    only. ``now`` (defaults to :func:`datetime.now`) is injected so the
    window-elapsed candidate rule is deterministic under test.
    """
    # Lazy import: ``advisor.context`` imports THIS module, so importing anything
    # from the ``advisor`` package at module top would form a cycle. By render
    # time the package is fully initialized, so the import here is safe + cheap.
    from advisor.position_review import AGENT_SOURCE, OWNER_ATTESTED_KEY, REVIEW_SOURCE_KEY

    now = now or datetime.now()
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        raw_rows = _safe_rows(
            conn,
            "SELECT id, ticker, context_json, created_at FROM advisor_memos "
            "WHERE user_id = ? AND kind = 'position_review'",
            (user_id,),
        )
        review_rows: list[sqlite3.Row] = []
        guard_ids: list[int] = []
        attested_ids: set[int] = set()
        for r in raw_rows:
            ctx = _memo_context(r["context_json"])
            if ctx.get(REVIEW_SOURCE_KEY) == AGENT_SOURCE:
                continue  # an automated run — never counts on the owner's scoreboard
            review_rows.append(r)
            if ctx.get("verdict_source") == "guard_override":
                gid = int(r["id"])
                guard_ids.append(gid)
                if ctx.get(OWNER_ATTESTED_KEY) is True:
                    attested_ids.add(gid)
        reviews_run = len(review_rows)
        guard_fired = len(guard_ids)

        graded_total = 0
        graded_right = 0
        if review_rows:
            marks = ",".join("?" for _ in review_rows)
            ids = [int(r["id"]) for r in review_rows]
            score_rows = _safe_rows(
                conn,
                f"SELECT memo_id, verdict FROM stance_scores WHERE memo_id IN ({marks}) "
                "ORDER BY memo_id, created_at DESC, id DESC",
                tuple(ids),
            )
            latest_by_memo: dict[int, str] = {}
            for r in score_rows:
                latest_by_memo.setdefault(int(r["memo_id"]), str(r["verdict"]))
            graded_total = len(latest_by_memo)
            graded_right = sum(1 for v in latest_by_memo.values() if v == "correct")

        changed, candidate = _coach_change_tally(
            conn, review_rows, guard_ids, attested_ids, now=now
        )
    finally:
        conn.close()
    return CoachPnl(
        reviews_run=reviews_run,
        guard_fired=guard_fired,
        overridden=guard_fired,
        graded_right=graded_right,
        graded_total=graded_total,
        changed=changed,
        candidate=candidate,
    )


def _coach_change_tally(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    guard_ids: list[int],
    attested_ids: set[int],
    *,
    now: datetime,
) -> tuple[int, int]:
    """Split guard_override reviews into ``(changed, candidate)`` for the Coach
    P&L's Q3'26 "changed >= 1" bar.

    ``changed`` counts ONLY reviews the owner explicitly attested changed their
    call (``owner_attested_change`` in ``attested_ids``). That attestation is
    authoritative: it is counted regardless of the window or any later sell —
    the owner said so directly. This is the only thing that moves the target,
    because owner inaction is the platform's DEFAULT state (sells enter the
    ledger only via irregular tracker reconciles/pledges), so "no contradicting
    sell" cannot by itself be evidence the coach changed anything.

    ``candidate`` counts the eligible-but-UNCONFIRMED reviews — the old v1
    proxy, demoted out of the target: a guard_override whose
    ``_COACH_CHANGE_WINDOW_DAYS`` window has fully ELAPSED (``now`` past the
    window end — a memo created today is never eligible, it just hasn't had time
    to be contradicted) with no owner sell/trim recorded on that ticker inside
    the window, and no attestation yet. These are surfaced honestly as
    "candidates" and invite a one-click confirmation, but never satisfy the bar
    on their own.
    """
    if not guard_ids:
        return 0, 0
    by_id = {int(r["id"]): r for r in review_rows}
    changed = 0
    candidate = 0
    for gid in guard_ids:
        if gid in attested_ids:
            changed += 1  # explicit owner attestation — authoritative, window-independent
            continue
        row = by_id.get(gid)
        if row is None or not row["ticker"]:
            continue
        try:
            memo_at = datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            continue
        window_end = memo_at + timedelta(days=_COACH_CHANGE_WINDOW_DAYS)
        if now < window_end:
            continue  # window still open — silence so far proves nothing yet
        ticker = str(row["ticker"]).upper()
        marks = ",".join("?" for _ in _SELL_TRIM_KINDS)
        contradicting = _safe_rows(
            conn,
            f"SELECT 1 FROM decisions WHERE ticker = ? AND decided_by = 'owner' "
            f"AND recommendation_kind IN ({marks}) "
            "AND made_at > ? AND made_at <= ? LIMIT 1",
            (ticker, *_SELL_TRIM_KINDS, row["created_at"], window_end.isoformat()),
        )
        if not contradicting:
            candidate += 1
    return changed, candidate


def _coach_pnl_section(
    db_path: Path, *, user_id: str = DEFAULT_USER_ID, now: datetime | None = None
) -> str:
    """The Coach P&L block (REQ-7) — mounted beside the coach's-read scorecard.
    Never raises: a missing advisor_memos/stance_scores/decisions table (a
    hand-DDL test fixture, or a DB stamped pre-0077) degrades every count to
    zero, which IS the honest all-zero line, not a swallowed error."""
    try:
        pnl = _query_coach_pnl(db_path, user_id=user_id, now=now)
    except sqlite3.OperationalError:
        pnl = CoachPnl(
            reviews_run=0,
            guard_fired=0,
            overridden=0,
            graded_right=0,
            graded_total=0,
            changed=0,
            candidate=0,
        )
    head = (
        '<section class="panel cpnl"><h2>Coach P&amp;L</h2>'
        '<p class="sub">The guard\'s public scoreboard — every position review it has ever run, '
        "whether it ever overrode a call, and how those calls graded. All-zero is the honest "
        "answer until the guard has actually been exercised.</p>"
    )
    if pnl.reviews_run == 0:
        review_line = (
            '<p class="cpnl-line">Reviews run: <b>0</b> — the guard has never been exercised.</p>'
        )
    else:
        graded_bits = (
            f"{pnl.graded_right}/{pnl.graded_total} graded right"
            if pnl.graded_total
            else "graded right so far: 0"
        )
        review_line = (
            f'<p class="cpnl-line">Reviews run: <b>{pnl.reviews_run}</b> &middot; '
            f"guard fired: <b>{pnl.guard_fired}</b> &middot; "
            f"overridden: <b>{pnl.overridden}</b> &middot; {graded_bits}</p>"
        )
    tooltip = (
        "Counts a guard_override review toward the target ONLY when the owner explicitly "
        "attested it changed their call (one click on the review). Silence never counts: "
        "owner inaction is the default state, so 'no contradicting sell' is not evidence."
    )
    target_line = (
        f'<p class="cpnl-line" title="{escape(tooltip)}">Decisions changed by the coach: '
        f"<b>{pnl.changed}</b> &middot; Q3&rsquo;26 target: <b>{COACH_CHANGED_TARGET}</b></p>"
    )
    cand_tooltip = (
        f"Eligible but unconfirmed: a guard hold that has stuck {_COACH_CHANGE_WINDOW_DAYS}d "
        "with no owner sell/trim on that ticker, but the owner hasn't attested it changed "
        "their call. A proxy, not a causal claim — so it does NOT count toward the target. "
        "Confirm it on the review to promote it to a changed decision."
    )
    candidate_line = (
        f'<p class="cpnl-line cpnl-candidate" title="{escape(cand_tooltip)}">'
        f"Candidates (eligible, unconfirmed): <b>{pnl.candidate}</b> "
        "&mdash; confirm on the review to count.</p>"
        if pnl.candidate
        else ""
    )
    doorway = (
        '<p class="cpnl-line"><a href="#advisor_memos" '
        'data-peek-url="/api/peek/memo/position_review" '
        'data-peek-title="Latest position review">reviews &rarr;</a></p>'
        if pnl.reviews_run
        else ""
    )
    return f"{head}{review_line}{target_line}{candidate_line}{doorway}</section>"


# --------------------------------------------------------------------------- #
# Pings / mutes / digest — REQ-12: the coach's initiation ledger, rendered for
# the first time (digest_pings() and unmute() had zero production callers
# before this panel; coach_pings/coach_mutes rendered nowhere in the app).
# --------------------------------------------------------------------------- #

_PING_STATUS_TONE: dict[str, str] = {
    "sent": "ok",
    "digest": "warn",
    "dismissed": "muted",
    "acted": "ok",
    "skipped_muted": "muted",
    "skipped_stale": "muted",
}


def _month_bounds_iso(now: datetime | None = None) -> tuple[str, str]:
    stamp = now or datetime.now()
    start = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def _coach_pings_section(db_path: Path, *, now: datetime | None = None) -> str:
    """ "Coach pings (this month)" — the first production renderer of
    coach_pings. One dense line per ping: class, ticker, status, date. Never
    raises: coach_pings is a 0131+ table, absent on any DB stamped before it."""
    start_iso, end_iso = _month_bounds_iso(now)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        rows = _safe_rows(
            conn,
            "SELECT class_, ticker, status, created_at FROM coach_pings "
            "WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
            (start_iso, end_iso),
        )
    finally:
        conn.close()
    head = (
        '<section class="panel"><h2>Coach pings (this month)</h2>'
        '<p class="sub">Every initiation moment the governor considered this month — '
        "the falsifier-breach / retro-annotation / intent-followup nudges, whether they "
        "sent, waited in the digest, or were dismissed.</p>"
    )
    if not rows:
        return f'{head}<p class="muted">No pings this month.</p></section>'
    lines = "".join(_ping_line(r) for r in rows)
    return f'{head}<div class="cpnl-list">{lines}</div></section>'


def _ping_line(r: sqlite3.Row) -> str:
    ticker = escape(str(r["ticker"])) if r["ticker"] else "&mdash;"
    status = str(r["status"])
    when = str(r["created_at"])[:10]
    pill = f'<span class="k-pill{_pill_tone(_PING_STATUS_TONE.get(status, "muted"))}">{escape(status)}</span>'
    return (
        f'<p class="cpnl-line">{escape(str(r["class_"]))} &middot; {ticker} &middot; '
        f"{pill} &middot; {escape(when)}</p>"
    )


def _coach_mutes_section(db_path: Path) -> str:
    """ "Active mutes" — coach_mutes rows with an inline Unmute button (REQ-12:
    visible AND reversible). Empty state is one muted line, not absent."""
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        rows = _safe_rows(
            conn, "SELECT class_, muted_at, reason FROM coach_mutes ORDER BY muted_at DESC"
        )
    finally:
        conn.close()
    head = (
        '<section class="panel cpnl-mutes"><h2>Active mutes</h2>'
        '<p class="sub">A class the owner dismissed three times in a row mutes itself until '
        "cleared here — dismissals train the coach; it never argues.</p>"
    )
    if not rows:
        return f'{head}<p class="muted">No active mutes.</p></section>'
    lines = "".join(
        '<p class="cpnl-line" data-mute-row data-class="'
        f'{escape(str(r["class_"]))}">{escape(str(r["class_"]))} &mdash; muted since '
        f"{escape(str(r['muted_at'])[:10])}"
        f"{' (' + escape(str(r['reason'])) + ')' if r['reason'] else ''} "
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm cpnl-unmute-btn" '
        f'data-class="{escape(str(r["class_"]))}">unmute</button></p>'
        for r in rows
    )
    return f'{head}<div class="cpnl-list">{lines}</div>{_UNMUTE_JS}</section>'


def _coach_digest_section(db_path: Path) -> str:
    """ "Digest queue" — research.governor.digest_pings() rows, the capped-out
    / send-failed nudges the audit found vanishing silently. One line each
    with class + ticker + age (days since the ping was created)."""
    try:
        from research.governor import digest_pings
    except ImportError:  # pragma: no cover - module always present in prod
        return ""
    try:
        rows = digest_pings(db_path, limit=20)
    except Exception:  # pragma: no cover - the digest must never break the page
        rows = []
    head = (
        '<section class="panel"><h2>Digest queue</h2>'
        '<p class="sub">Nudges that hit the daily/weekly frequency cap or failed to send — '
        "surfaced quietly here instead of vanishing silently.</p>"
    )
    if not rows:
        return f'{head}<p class="muted">Digest is empty.</p></section>'
    ticker_and_age = _digest_tickers_and_ages(db_path, [pid for pid, _cls, _body in rows])
    lines = "".join(_digest_line(pid, cls, ticker_and_age.get(pid)) for pid, cls, _body in rows)
    return f'{head}<div class="cpnl-list">{lines}</div></section>'


def _digest_tickers_and_ages(
    db_path: Path, ping_ids: list[int]
) -> dict[int, tuple[str | None, str]]:
    """Join ticker + created_at back onto the digest rows — digest_pings()
    returns only (id, class, body), so the ticker/age this section renders is
    read directly off coach_pings by id (read-only; never raises)."""
    if not ping_ids:
        return {}
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in ping_ids)
        rows = _safe_rows(
            conn,
            f"SELECT id, ticker, created_at FROM coach_pings WHERE id IN ({marks})",
            tuple(ping_ids),
        )
    finally:
        conn.close()
    out: dict[int, tuple[str | None, str]] = {}
    for r in rows:
        out[int(r["id"])] = (str(r["ticker"]) if r["ticker"] else None, str(r["created_at"]))
    return out


def _digest_line(ping_id: int, class_: str, ticker_age: tuple[str | None, str] | None) -> str:
    ticker = escape(ticker_age[0]) if ticker_age and ticker_age[0] else "&mdash;"
    age = _age_str(ticker_age[1]) if ticker_age else "&mdash;"
    return f'<p class="cpnl-line">{escape(class_)} &middot; {ticker} &middot; {age}</p>'


def _age_str(created_at: str) -> str:
    try:
        made = datetime.fromisoformat(created_at)
    except ValueError:
        return "&mdash;"
    days = max(0, (datetime.now() - made).days)
    return f"{days}d" if days else "today"


# --------------------------------------------------------------------------- #
# Decision journal (tenet-2 Phase 5, §5.1/§5.2) — v_decision_journal renderer.
# --------------------------------------------------------------------------- #

# outcome_label vocabulary (decisions.outcome_label, 0046): correct/wrong/mixed
# grade to a tone; pending/unfalsifiable stay neutral (nothing to color yet).
_OUTCOME_TONE: dict[str, str] = {"correct": "ok", "wrong": "bad", "mixed": "warn"}

_DECISION_JOURNAL_LIMIT = 30


def _decision_journal_section(db_path: Path, *, limit: int = _DECISION_JOURNAL_LIMIT) -> str:
    """Owner-first journal over ``v_decision_journal``.

    Advisor-authored legacy rows remain preserved in the view and are counted,
    but they never appear as Owner Decisions by default.
    """
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        rows = _safe_rows(
            conn,
            "SELECT decision_id, ticker, decided_by, recommendation_kind, made_at, "
            "linked_memo_id, linked_memo_kind, advice_before_memo_id, advice_before_memo_kind, "
            "guard_override_flag, owner_attested_change, coach_ping_class, decision_nudge_id, "
            "user_action_kind, outcome_label, outcome_pct, stance_verdict "
            "FROM v_decision_journal WHERE decided_by = 'owner' "
            "ORDER BY made_at DESC, decision_id DESC LIMIT ?",
            (limit,),
        )
        advisor_count_rows = _safe_rows(
            conn,
            "SELECT COUNT(*) AS n FROM v_decision_journal "
            "WHERE COALESCE(decided_by, 'advisor') <> 'owner'",
        )
    finally:
        conn.close()
    advisor_count = int(advisor_count_rows[0]["n"]) if advisor_count_rows else 0
    head = (
        '<section class="panel"><h2>Owner Decision journal</h2>'
        '<p class="sub">Owner Decisions are the learning unit: what you decided, '
        "the advice available beforehand, and how process and outcome graded. "
        "Unadopted advisor views stay outside this default.</p>"
    )
    preserved = (
        '<p class="muted">'
        f"{advisor_count} advisor {'record' if advisor_count == 1 else 'records'} preserved "
        "outside the Owner-default journal. "
        '<a href="/api/panel/ledger_decisions?filter=advisor">Review advisor history</a>.</p>'
        if advisor_count
        else ""
    )
    if not rows:
        return f'{head}{preserved}<p class="muted">No Owner Decisions recorded yet.</p></section>'
    lines = "".join(_journal_row(r) for r in rows)
    return f'{head}{preserved}<div class="cpnl-list">{lines}</div></section>'


def _journal_advice_chips(r: sqlite3.Row) -> str:
    memo_kind = r["advice_before_memo_kind"] or r["linked_memo_kind"]
    chips: list[str] = []
    if memo_kind:
        chips.append(f'<span class="k-chip k-chip-mono">{escape(str(memo_kind))}</span>')
    if r["guard_override_flag"]:
        chips.append(f'<span class="k-chip{chip_tone_class("bad")}">guard</span>')
    if r["owner_attested_change"] == 1:
        chips.append(f'<span class="k-chip{chip_tone_class("ok")}">attested</span>')
    if r["coach_ping_class"]:
        chips.append('<span class="k-chip">ping</span>')
    if r["decision_nudge_id"] is not None:
        chips.append('<span class="k-chip">nudge</span>')
    return "".join(chips) if chips else '<span class="muted">&mdash;</span>'


def _journal_row(r: sqlite3.Row) -> str:
    ticker = str(r["ticker"]) if r["ticker"] else None
    ticker_html = (
        ticker_label(ticker, href=f"/ticker/{ticker}")
        if ticker
        else '<span class="muted">portfolio</span>'
    )
    kind = escape(str(r["recommendation_kind"]))
    decided_by = escape(str(r["decided_by"] or "advisor"))
    kind_chip = f'<span class="k-chip k-chip-mono">{decided_by}: {kind}</span>'
    advice = _journal_advice_chips(r)
    action = r["user_action_kind"]
    disposition = (
        f'<span class="k-pill">{escape(str(action))}</span>'
        if action
        else '<span class="muted">&mdash;</span>'
    )
    outcome_label = r["outcome_label"]
    if outcome_label:
        tone = _OUTCOME_TONE.get(str(outcome_label), "")
        pct = r["outcome_pct"]
        pct_html = (
            f' <span class="k-num-{"pos" if (pct or 0) >= 0 else "neg"}">{pct:+.1f}%</span>'
            if pct is not None
            else ""
        )
        outcome = (
            f'<span class="k-pill{_pill_tone(tone)}">{escape(str(outcome_label))}</span>{pct_html}'
        )
    else:
        outcome = '<span class="muted">pending</span>'
    when = str(r["made_at"])[:10]
    memo_kind = r["advice_before_memo_kind"] or r["linked_memo_kind"]
    doorway = (
        f' <a href="/ticker/{escape(str(ticker))}" '
        f'data-peek-url="/api/peek/memo/{escape(str(memo_kind))}" '
        f'data-peek-title="Latest {escape(str(memo_kind))} memo">detail &rarr;</a>'
        if ticker and memo_kind
        else ""
    )
    return (
        f'<p class="cpnl-line">{ticker_html} &middot; {kind_chip} &middot; {advice} &middot; '
        f"{disposition} &middot; {outcome} &middot; {escape(when)}{doorway}</p>"
    )


_UNMUTE_JS = """<script>
(function () {
  document.querySelectorAll('.cpnl-unmute-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var cls = btn.getAttribute('data-class');
      var row = btn.closest('[data-mute-row]');
      CCAction.busy(btn);
      fetch('/api/coach/unmute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ class_: cls })
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function () {
        if (row) CCAction.leave(row);
      }).catch(function () {
        CCAction.release(btn);
      });
    });
  });
})();
</script>"""


def compose_decisions_page(
    audit: list[SizingAuditRow],
    timeline: list[TimelineEvent],
    live: LivePortfolio,
    alpha: PositionAlpha | None,
    calibration: CalibrationStats | None = None,
    attribution: SkillDecomposition | None = None,
    scorecard_html: str = "",
    beta: BetaStats | None = None,
    coach_pnl_html: str = "",
    coach_pings_html: str = "",
    coach_mutes_html: str = "",
    coach_digest_html: str = "",
    redteam_pnl_html: str = "",
    annual_letter_html: str = "",
    decision_journal_html: str = "",
) -> str:
    """Pure page assembly (testable without network or DB). ``calibration``
    None (pre-0046 substrate) hides the section entirely; ``attribution`` None
    (tracker offline / nothing to decompose) hides the skill block;
    ``scorecard_html`` always renders (starvation stub or failure stub —
    REQ-6, never ""); ``beta`` carries the Jensen alpha joined beside the
    decomposition (L-seam 5). The coach section is pre-rendered upstream (with
    its own <style>) so this module never imports calibration_coach (which
    imports this one). ``coach_pnl_html`` / ``coach_pings_html`` /
    ``coach_mutes_html`` / ``coach_digest_html`` / ``decision_journal_html``
    default to "" so existing callers (and every hand-built test page) are
    unaffected; the KPI vitals strip is hoisted above the sizing audit as the
    page's opening line when a calibration section is present."""
    kpi_strip = _calibration_kpi_strip(calibration) if calibration is not None else ""
    return "".join(
        [
            _PANEL_CSS,
            kpi_strip,
            _audit_section(audit, live, alpha),
            _calibration_section(calibration) if calibration is not None else "",
            _skill_decomposition_section(attribution, beta) if attribution is not None else "",
            scorecard_html,
            coach_pnl_html,
            redteam_pnl_html,
            annual_letter_html,
            coach_pings_html,
            coach_mutes_html,
            coach_digest_html,
            decision_journal_html,
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
    """Map an ok/warn/bad/muted tone word to the kit .k-pill suffix (muted →
    bare). Thin alias of the kit's :func:`ui.controls.pill_tone_class` so this
    panel's many tone tables share the one whitelist."""
    return pill_tone_class(tone)


def _audit_row(r: SizingAuditRow) -> str:
    name_attr = f' title="{escape(r.name)}"' if r.name else ""
    verdict = (
        f'<span class="k-pill{_pill_tone(thesis_status_tone(r.verdict))}">{escape(r.verdict)}'
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
        gap_tone = "k-num-neg" if r.fv_gap_pct > 0 else "k-num-pos"
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
        a_tone = "k-num-pos" if r.alpha_usd >= 0 else "k-num-neg"
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
        f"{ticker_label(r.ticker, href='/ticker/' + escape(r.ticker))}</td>"
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
        tone = "k-num-pos" if stats.improving else ("muted" if abs(pp) < 0.5 else "k-num-neg")
        headline = (
            f'Latest {grain} hit-rate is <span class="{tone}">{word} ({pp:+.0f}pp</span> '
            "vs the prior period)."
        )

    def _gap_cell(c: CohortPeriod) -> str:
        if c.conviction_gap is None:
            return '<span class="muted">&mdash;</span>'
        g = c.conviction_gap * 100.0
        tone = "k-num-pos" if g >= 0 else "k-num-neg"
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


def _calibration_kpi_html(stats: CalibrationStats) -> str:
    """The one-line calibration KPI vitals strip (decisions / graded / hit
    rate / reversed) — shared by the full calibration section and the
    mirror-first hoisted copy at the top of the page."""
    hit = f"{stats.overall_hit_rate * 100.0:.0f}%" if stats.overall_hit_rate is not None else "—"
    rev_n = len(stats.reversals)
    return (
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


def _calibration_kpi_strip(stats: CalibrationStats) -> str:
    """Mirror-first hoist (confirmed minor finding): the calibration KPI
    vitals strip repeated as the page's opening line, above the sizing audit,
    so "how am I actually doing" is visible without scrolling to the
    calibration section. Only when calibration has recorded decisions —
    matches the empty-state handling in ``_calibration_section`` (an empty
    ``adc-kpis`` strip with nothing behind it would be worse than no strip)."""
    if stats.total == 0:
        return ""
    return f'<div class="cpnl-hoist">{_calibration_kpi_html(stats)}</div>'


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

    kpis = _calibration_kpi_html(stats)

    conviction_rows = "".join(
        "<tr>"
        f"<td>{escape(b.conviction)}</td>"
        f'<td class="num">{b.graded}</td>'
        f'<td class="num"><span class="k-num-pos">{b.correct}</span></td>'
        f'<td class="num"><span class="k-num-neg">{b.wrong}</span></td>'
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
    process_block = _process_matrix_block(stats)

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
        f"{process_block}{mix_line}{timing_line}{omission_block}{reversal_table}</section>"
    )


def _process_matrix_block(stats: CalibrationStats) -> str:
    """Process quality × outcome (Track B seam 8) — the two-axis read the flat
    hit-rate can't give: how many calls were right for the WRONG reasons, and
    wrong for the RIGHT ones. Hidden until at least one call is process-scored."""
    m = stats.process_outcome
    if m is None or not m.total_scored:
        return ""
    outcomes = ("correct", "wrong", "mixed")
    body = "".join(
        "<tr>"
        f"<td>{escape(pq)}</td>"
        + "".join(f'<td class="num">{m.cells.get((pq, o), 0)}</td>' for o in outcomes)
        + "</tr>"
        for pq in ("sound", "flawed", "lucky")
    )
    return (
        '<h3 class="adc-sub">Process &times; outcome</h3>'
        '<p class="adc-line">Process quality is a separate axis from outcome — '
        f"<b>{m.right_for_wrong_reasons}</b> right for the wrong reasons "
        "(flawed/lucky yet correct), "
        f"<b>{m.wrong_for_right_reasons}</b> wrong for the right reasons (sound yet wrong), "
        f"over {m.total_scored} process-scored call(s).</p>"
        '<table class="ad-table adc-table"><thead><tr>'
        '<th>Process</th><th class="num">Correct</th><th class="num">Wrong</th>'
        '<th class="num">Mixed</th>'
        f"</tr></thead><tbody>{body}</tbody></table>"
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
            f"winners avg {_signed_usd0(exp.avg_win)} vs losers {_signed_usd0(-exp.avg_loss)}{slug}"
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
        f'<span class="k-num-neg">{om.missed}</span> ran away (missed), '
        f'<span class="k-num-pos">{om.dodged}</span> correctly dodged, {om.mixed} mixed — '
        f"miss rate {miss} ({escape(confidence_note(om.graded))}).</p>"
    )
    if not om.worst_misses:
        return head
    rows = "".join(
        "<tr>"
        f'<td class="when">{escape(m.made_at)}</td>'
        f'<td class="tk">{escape(m.ticker)}</td>'
        '<td class="num"><span class="k-num-neg">'
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
        return f'{badge} <span class="k-num-pos">reversal vindicated</span>'
    if vindicated is False:
        return f'{badge} <span class="k-num-neg">reversal cost</span>'
    return badge


def _skill_decomposition_section(d: SkillDecomposition, beta: BetaStats | None = None) -> str:
    """The shared selection/sizing/timing decomposition (L8 §6).

    Wave 1 (surface_density_jit_redesign.md D4/D7): the HONEST VERDICT leads —
    one sentence reconciling the dollar figure with the Jensen-α luck test,
    because the walkthrough caught the old layout asserting "+$41,774 total
    alpha" and "not distinguishable from zero — could be luck" in adjacent,
    unreconciled lines ("I can't make out my skill from this"). The KPI strip
    and the edge/leak read follow; the conviction → outcome join renders only
    when at least one bucket carries a STATED conviction — an all-unstated
    table is an empty ritual and shows as one unlock line instead. The
    arithmetic is the engine's; this only frames it."""
    window = (
        f"{escape(d.window_start)} → {escape(d.window_end)}"
        if d.window_start and d.window_end
        else "the tracker window"
    )
    basis_short = "your policy benchmark" if d.benchmark_basis == "policy" else "SPY"
    head = (
        '<section class="panel"><h2>Skill decomposition</h2>'
        '<p class="sub">Is the alpha repeatable — and which decision produces it? '
        f"Realized dollar alpha over {window} vs {basis_short}, split into "
        '<b title="which names, size-neutral">selection</b> / '
        '<b title="did your weighting amplify selection">sizing</b> / '
        '<b title="did within-window adds/trims lean into alpha (flow-lean '
        'diagnostic, not part of the exact split)">timing</b>.</p>'
    )

    # No dollar total (tracker gap) → fall back to the bare Jensen line so the
    # luck read still shows; the verdict subsumes it otherwise.
    verdict = _verdict_line(d, beta) or _jensen_line(beta)

    def kpi(label: str, v: float | None) -> str:
        body = _money(v, signed=True) if v is not None else "&mdash;"
        tone = "" if v is None else (" k-num-pos" if v >= 0 else " k-num-neg")
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

    def _rating(v: float | None) -> str:
        return f"{v:.1f}/5" if v is not None else "&mdash;"

    has_stated_conviction = any(c.conviction != "unstated" for c in d.by_conviction)
    if has_stated_conviction:
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
        )
    elif d.by_conviction:
        # D4: an all-unstated join says what unlocks it, in one line — never a
        # one-row table whose only cell reads "unstated".
        conv_table = (
            '<p class="muted ad-note">Conviction &rarr; outcome is locked: none of the '
            f"{d.n_names} priced name(s) carries a stated conviction. Rate a position "
            "(1&ndash;5, on its decision) and this join starts answering whether the "
            "names you believe in actually deliver.</p>"
        )
    else:
        conv_table = ""
    notes = "".join(f'<p class="muted ad-note">{escape(n)}</p>' for n in d.notes)
    return f"{head}{verdict}{kpis}{read}{conv_table}{notes}</section>"


def _verdict_line(d: SkillDecomposition, beta: BetaStats | None) -> str:
    """The one-sentence reconciliation of the dollar alpha and the luck test.

    A positive dollar figure and an insignificant Jensen α are NOT a
    contradiction — one is realized money, the other says the process is not
    yet statistically distinguishable from chance — but the surface has to say
    that in one breath, not leave the owner to reconcile two adjacent lines."""
    if d.total_alpha_usd is None:
        return ""
    money = _money(d.total_alpha_usd, signed=True)
    made = "made" if d.total_alpha_usd >= 0 else "gave up"
    luck = ""
    if beta is not None and beta.alpha_significant is not None:
        t = f", t={beta.alpha_t_stat:.1f}" if beta.alpha_t_stat is not None else ""
        pct = (
            f" {beta.alpha_annualized_pct:+.1f}% annualized"
            if beta.alpha_annualized_pct is not None
            else ""
        )
        # The Jensen number stays VISIBLE inside the verdict (it used to be its
        # own line; subsuming it must not lose the value).
        luck = (
            f" — Jensen &alpha;{pct} is statistically distinguishable from zero{t}: "
            "real skill, not luck"
            if beta.alpha_significant
            else f" — but Jensen &alpha;{pct} is not distinguishable from zero{t}: "
            f"at n={d.n_names} this is not yet statistically distinguishable from "
            "luck; treat the split as directional"
        )
    elif not d.confident:
        luck = f" — thin book (n={d.n_names}); treat the split as directional"
    return (
        f'<p class="adc-line sk-verdict"><b>Verdict:</b> you {made} {money} of realized '
        f"alpha this window{luck}.</p>"
    )


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
            f' &mdash; <span class="k-num-pos">statistically distinguishable from zero{t}</span>'
            if beta.alpha_significant
            else f' &mdash; <span class="muted">not distinguishable from zero — '
            f"could be luck{t}</span>"
        )
    return (
        f'<p class="adc-line">Jensen &alpha; (annualized regression intercept vs {bench}): '
        f'<b class="{tone}">{val}</b>{sig}.</p>'
    )


def _signed_span(v: float) -> str:
    return f'<span class="{"k-num-pos" if v >= 0 else "k-num-neg"}">{_money(v, signed=True)}</span>'


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
.adc-sub { font-size: var(--fs-body); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin: 12px 0 4px; }
/* Timeline kinds: the filled status pill is now the control kit's .k-pill
   (+ k-pill-bad for bear append, k-pill-ok for thesis update; every other kind
   is the neutral bare .k-pill). Tone is mapped in _TIMELINE_PILL_TONE. */
.ad-body { font-size: var(--fs-body); line-height: 1.5; }
/* Cohort trend curve (L8): sparkline + per-period table. */
.adc-spark { width: 100%; max-width: 560px; height: 56px; display: block; margin: 2px 0 8px; }
.adc-trend th, .adc-trend td { padding-top: 2px; padding-bottom: 2px; }
.adc-trend sup { color: var(--muted); font-size: 0.7em; }
/* Skill decomposition (L8): money KPIs stack a value over a label. */
.sk-kpis .adc-kpi { display: flex; flex-direction: column; gap: 1px; }
.sk-val { font-variant-numeric: tabular-nums; font-size: var(--fs-body); }
.sk-lbl { font-size: var(--fs-caption); color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; }
.sk-read { font-size: var(--fs-body); color: var(--fg); margin: 2px 0 10px; }
/* Mirror-first hoist (S15 PR3): the calibration KPI strip repeated as the
   page's opening line, above the sizing audit. */
.cpnl-hoist { margin: 0 0 12px; }
.cpnl-hoist .adc-kpis { margin: 0; }
/* Coach P&L + pings/mutes/digest (S15 PR3, REQ-6/7/12): dense one-liners,
   the established .adc-line rhythm. */
.cpnl-line { font-size: var(--fs-caption); color: var(--muted); margin: 4px 0; }
.cpnl-line b { color: var(--fg); font-variant-numeric: tabular-nums; }
.cpnl-list { display: flex; flex-direction: column; gap: 2px; }
.cpnl-unmute-btn { margin-left: 6px; }
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
