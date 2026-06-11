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

from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    PositionAlpha,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
)
from pipeline.research_cockpit import latest_dcf_runs
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
) -> tuple[float, list[str]]:
    """Score one holding's stated-vs-actual sizing tension. Returns
    ``(points, reason chips)`` — every point contribution has a chip."""
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
) -> list[SizingAuditRow]:
    """Assemble + score the audit, ranked worst-first (then by weight).

    Pure over already-fetched inputs — no network, no DB — so the scorer is
    directly testable. ``holdings`` is the research portfolio list; tracker
    positions outside it are deliberately not audited (see the coverage
    footnote in the renderer).
    """
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
        fv_gap = dcf_gaps.get(t, (None, None, None, None))[0]
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
    finally:
        conn.close()
    try:
        intents = list_intents(user_id=user_id, db_path=db_path)
    except sqlite3.OperationalError:
        intents = []
    live = fetch_live_portfolio(api_url=api_url)
    analytics = fetch_portfolio_analytics(api_url=api_url, only={"position_alpha"})
    audit = build_sizing_audit_rows(
        holdings, verdicts, dcf_gaps, intents, live, analytics.position_alpha
    )
    timeline = build_decisions_timeline(db_path, user_id=user_id, intents=intents)
    return compose_decisions_page(audit, timeline, live, analytics.position_alpha)


def compose_decisions_page(
    audit: list[SizingAuditRow],
    timeline: list[TimelineEvent],
    live: LivePortfolio,
    alpha: PositionAlpha | None,
) -> str:
    """Pure page assembly (testable without network or DB)."""
    return "".join(
        [
            _PANEL_CSS,
            _audit_section(audit, live, alpha),
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


def _audit_row(r: SizingAuditRow) -> str:
    name_attr = f' title="{escape(r.name)}"' if r.name else ""
    verdict = (
        f'<span class="ad-badge b-{_VERDICT_TONE.get(r.verdict, "muted")}">{escape(r.verdict)}'
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
        gap = f'<span class="{gap_tone}">{r.fv_gap_pct:+.0f}%</span>'
    else:
        gap = '<span class="muted">&mdash;</span>'
    if r.alpha_usd is not None:
        a_tone = "pos" if r.alpha_usd >= 0 else "neg"
        alpha = f'<span class="{a_tone}">{_money(r.alpha_usd, signed=True)}</span>'
    else:
        alpha = '<span class="muted">&mdash;</span>'
    if r.mismatch_reasons:
        chips = "".join(f'<span class="ad-chip">{escape(c)}</span>' for c in r.mismatch_reasons)
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
        f'<td><button type="button" class="ad-edit-btn" data-ticker="{escape(r.ticker)}">'
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
        f'<button type="button" class="ad-save-btn" data-ticker="{escape(r.ticker)}">'
        "Save intent</button>"
        f'<span class="ad-status muted"></span>'
        "</div></td></tr>"
    )
    return main + editor


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
    rows = "".join(
        "<tr>"
        f'<td class="when">{escape(e.when.date().isoformat())}</td>'
        f'<td class="tk">{escape(e.ticker or "—")}</td>'
        f'<td><span class="ad-pill p-{escape(e.kind)}">{escape(e.label)}</span></td>'
        f'<td class="ad-body">{escape(e.body)}</td>'
        "</tr>"
        for e in timeline
    )
    return (
        f"{head}"
        '<table class="ad-timeline"><thead><tr>'
        "<th>Date</th><th>Ticker</th><th>Kind</th><th>Decision</th>"
        "</tr></thead><tbody>"
        f"{rows}</tbody></table></section>"
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
.ad-note { font-size: 12px; margin: 0 0 10px; }
.ad-badge { display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.4px; font-weight: 600; }
.ad-badge.b-ok { background: #14532d; color: var(--ok); }
.ad-badge.b-warn { background: #422006; color: var(--warn); }
.ad-badge.b-bad { background: #450a0a; color: var(--bad); }
.ad-badge.b-muted { background: #2a2c30; color: var(--muted); }
.ad-score { color: var(--warn); font-variant-numeric: tabular-nums; margin-right: 8px; }
.ad-chip { display: inline-block; margin: 1px 4px 1px 0; padding: 1px 7px; border-radius: 10px;
  font-size: 11px; font-family: var(--mono); background: var(--paper);
  border: 1px solid var(--border); color: var(--muted); }
.ad-mismatch { max-width: 420px; }
.ad-aligned { font-size: 12px; }
.ad-edit-btn { background: transparent; color: var(--muted); border: 1px solid var(--border);
  border-radius: 4px; padding: 2px 9px; font-size: 11px; cursor: pointer;
  font-family: var(--mono); }
.ad-edit-btn:hover { color: var(--fg); border-color: var(--border-2); }
.ad-editor { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px;
  padding: 4px 0; }
.ad-editor span { color: var(--muted); }
/* Editor inputs/selects: skinned by the shared control kit (ui/controls.py). */
.ad-editor select, .ad-editor input { padding: 3px 6px; font-size: var(--fs-caption); }
.ad-editor select { padding-right: 26px; }
.ad-editor input.ad-target { width: 70px; }
.ad-editor input.ad-note-input { flex: 1; min-width: 180px; }
.ad-save-btn { background: var(--accent); color: var(--accent-contrast); border: none;
  border-radius: var(--radius); padding: 4px 12px; font-size: var(--fs-caption);
  font-weight: 600; cursor: pointer; }
.ad-timeline td.tk { font-weight: 600; white-space: nowrap; }
.ad-timeline td.when { color: var(--muted); white-space: nowrap; }
.ad-pill { display: inline-block; padding: 2px 9px; border-radius: 10px; font-size: 11px;
  font-weight: 600; white-space: nowrap; background: #1f2b3a; color: #8fb6e6; }
.ad-pill.p-bear_append { background: #3a1f1f; color: #f0a0a0; }
.ad-pill.p-thesis_update { background: #14361f; color: #6ee7a0; }
.ad-pill.p-sizing_intent { background: #2b2440; color: #c4b5fd; }
.ad-pill.p-decision_note { background: #103039; color: #7dd3fc; }
.ad-body { font-size: 12.5px; line-height: 1.5; }
td.pos, span.pos { color: var(--ok); }
td.neg, span.neg { color: var(--bad); }
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
