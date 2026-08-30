"""Peek fragments (UX9) — the small HTML payloads behind the shell's quick-look.

The report and operational-panel peek primitive
fetches one of these head/foot-less fragments and injects it into a positioned
popover, so reviewing an alert, reading a source excerpt, or glancing at a
ticker no longer means navigating away from the panel you were on:

* :func:`render_alert_peek` / :func:`render_alerts_list_peek` — full alert
  card(s) (evidence drawer, queued actions with their live approve/dismiss
  links) for the inbox "review →" links and the cockpit's pending-alert pills.
* :func:`render_ticker_peek` — the hover mini-card for ticker links: price +
  day move, thesis verdict, DCF gap, live P/E (TTM, bottoms-up metrics
  engine), next ER, unreviewed count, and the open-the-holding link. Reuses
  the cockpit's own per-ticker readers so the card can never disagree with
  the cockpit row it annotates.
* :func:`render_memo_peek` — the latest advisor memo of a kind, for the
  portfolio insights "full memo →" link.
* :func:`render_provenance_peek` — per-source data freshness (brief build,
  FMP pulls, IR docs, news, the earnings calendar) with inline refresh
  buttons that POST the existing ``/actions/*`` endpoints and stream the job
  log into the peek — the click-through behind the freshness dots and the
  Home tier strip (UX9d).
* :func:`render_review_peek` — the instant, LLM-free ``/review`` pre-analysis
  (grounded facts, tax block, mechanical read, the live graded-sells base
  rate) plus a footer button that escalates to the full LLM-calibrated,
  memo-persisting review — the click-through behind the Holding band's
  "Review" link and the portfolio cockpit's review pill (PR5).

Source-document peeks are NOT here — ``/source/<doc_id>?fragment=1`` serves
those straight from ``pipeline.source_viewers``.

All builders are read-only and degrade on missing tables (``None`` → the
route 404s; the peek shows its failed-to-load empty state). The provenance
peek goes further: it always renders, with missing sources as em-dash ages.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from html import escape
from io import StringIO
from pathlib import Path
from typing import NamedTuple, cast

from alerts import AlertRow, get_alert, list_alerts, list_queued_actions_for_alert
from compute.metrics_engine.io import latest_ttm_value
from compute.thesis_evaluation_episodes import episode_history_source
from dashboard._card import render_alert_card
from dashboard.evidence_drawer import load_brief_provenance
from identity import DEFAULT_USER_ID
from models.facts import DerivedInputRef, DerivedRef, FactLocator, LocatorKind, VendorFieldRef
from pipeline.research_cockpit import (
    AttractivenessBreakdown,
    AttractivenessFactor,
    attractiveness_tone,
    compute_attractiveness,
    latest_dcf_runs,
    next_earnings,
    profile_quote,
)
from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from pipeline.source_viewers import (
    _STATEMENT_JSON_DOC_TYPES,  # pyright: ignore[reportPrivateUsage]
    load_document,
    render_form10k_page,
    render_pdf_page_view,
    render_statement_json_page,
    render_transcript_page,
)
from pipeline.source_viewers import _DocRow as _SourceDocRow  # pyright: ignore[reportPrivateUsage]
from pipeline.you_said import render_you_said_strip
from report.renderers.numfmt import fmt_date, fmt_pct, fmt_reltime
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import pill_tone_class, thesis_status_tone, ticker_label
from ui.prose import render_prose
from ui.time import stamp_html

__all__ = [
    "render_alert_peek",
    "render_alerts_list_peek",
    "render_derived_peek",
    "render_discovery_compare_peek",
    "render_earnings_readout_peek",
    "render_fact_provenance_peek",
    "render_fit_peek",
    "render_memo_peek",
    "render_new_docs_peek",
    "render_provenance_peek",
    "render_review_peek",
    "render_score_peek",
    "render_ticker_peek",
    "render_what_if_peek",
]

# Thesis-status tones route through the shared kit resolver
# (ui.controls.thesis_status_tone) — the same vocabulary the cockpit's
# verdict badge uses, kept in one place.

# Mirrors ck_advisor_memos_kind (alembic 0077 + 0140's position_review widen).
_MEMO_KINDS = frozenset({"next_dollar", "swap_check", "socratic", "position_review"})


# ----------------------------------------------------------------------------
# Alert peeks
# ----------------------------------------------------------------------------


def render_alert_peek(db_path: Path, alert_id: int) -> str | None:
    """One full alert card — evidence drawer open, queued actions with their
    approve/dismiss links — for the inbox cards' "review →" peek. None when
    no such alert exists (the route 404s)."""
    try:
        alert = get_alert(alert_id, db_path=db_path)
    except (LookupError, sqlite3.Error):
        return None
    return _alert_cards_html([alert], db_path)


def render_alerts_list_peek(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 8,
) -> str:
    """Up to ``limit`` full alert cards (drawers collapsed — it's a scan list)
    for the cockpit's pending-alert pills, with the full feed as the overflow
    path. Always renders — an empty slice is a valid answer."""
    try:
        alerts = list_alerts(
            user_id=user_id, ticker=ticker, status=status, limit=limit, db_path=db_path
        )
    except sqlite3.Error:
        alerts = []
    if not alerts:
        return '<div class="cc-empty">No matching alerts.</div>'
    params = [("ticker", ticker), ("status", status)]
    qs = "&".join(f"{k}={escape(v)}" for k, v in params if v)
    feed_href = f"/feed?{qs}" if qs else "/feed"
    footer = f'<div class="cc-peek-foot"><a href="{feed_href}">open in the full feed →</a></div>'
    return _alert_cards_html(alerts, db_path) + footer


def _alert_cards_html(alerts: list[AlertRow], db_path: Path) -> str:
    """Shared card-list body: per-ticker brief provenance looked up once, the
    drawer expanded only when a single alert is being reviewed."""
    out = StringIO()
    out.write('<div class="cc-peek-alerts">')
    prov_cache: dict[str, Mapping[str, object] | None] = {}
    for alert in alerts:
        t = alert.ticker.upper()
        if t not in prov_cache:
            prov_cache[t] = load_brief_provenance(t, db_path=db_path)
        try:
            actions = list_queued_actions_for_alert(alert.id, db_path=db_path)
        except sqlite3.Error:
            actions = []
        render_alert_card(
            out,
            alert,
            actions=actions,
            show_status_badge=True,
            brief_provenance=prov_cache[t],
            drawer_open=len(alerts) == 1,
        )
    out.write("</div>")
    return out.getvalue()


# ----------------------------------------------------------------------------
# New-documents peek (cockpit "N new docs" pill)
# ----------------------------------------------------------------------------


def render_new_docs_peek(db_path: Path, *, ticker: str, limit: int = 12) -> str:
    """The documents fetched for ``ticker`` since its last report build — the
    click-through behind the cockpit's "N new docs" pill. Mirrors
    :func:`render_alerts_list_peek`: it renders the very rows the count was
    derived from (``fetched_at`` after ``tracked_companies.last_built_at``),
    newest first, each linking by its existing id to the ``/source/<id>``
    viewer (clicking a row retargets the peek in place). The ``documents`` table
    has no title column, so the label is derived from ``doc_type`` + the
    ``file_path`` basename. Always renders — an empty slice is a valid answer
    (the same window the count uses); a missing DB/table degrades to empty."""
    t = (ticker or "").strip().upper()
    if not t:
        return '<div class="cc-empty">No ticker.</div>'
    rows = _new_doc_rows(db_path, t, limit)
    if not rows:
        return '<div class="cc-empty">No documents fetched since the last build.</div>'
    body = "".join(_doc_row_html(r) for r in rows)
    foot = (
        f'<div class="cc-peek-foot"><a href="/#holding={escape(t, quote=True)}">'
        "open the holding →</a></div>"
    )
    return f'<div class="cc-peek-docs">{body}</div>{foot}<style>{_DOCS_CSS}</style>'


class _DocRow(NamedTuple):
    doc_id: int
    kind: str  # humanized doc_type
    name: str  # file basename (or source host) — the muted secondary label
    fetched_at: str | None


def _new_doc_rows(db_path: Path, t: str, limit: int) -> list[_DocRow]:
    """Documents whose ``fetched_at`` is after the ticker's ``last_built_at`` —
    the same "new since the build" window :func:`_new_doc_counts` counts. The
    ``ticker = ?`` predicate rides ``ix_documents_ticker_doctype_period``; the
    threshold is a scalar subquery (NULL last_built_at → no rows, matching the
    count's ``last_built_at IS NOT NULL`` guard)."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        cur = conn.execute(
            "SELECT d.id, d.doc_type, d.file_path, d.source_url, d.fetched_at "
            "FROM documents d "
            "WHERE d.ticker = ? AND julianday(d.fetched_at) > ("
            "  SELECT julianday(last_built_at) FROM tracked_companies "
            "  WHERE ticker = ? AND last_built_at IS NOT NULL) "
            "ORDER BY d.fetched_at DESC, d.id DESC LIMIT ?",
            (t, t, limit),
        )
        fetched = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[_DocRow] = []
    for doc_id, doc_type, file_path, source_url, fetched_at in fetched:
        kind = str(doc_type or "").replace("_", " ").strip().title() or "Document"
        name = Path(str(file_path)).name if file_path else _host(str(source_url or ""))
        out.append(_DocRow(int(doc_id), kind, name, str(fetched_at) if fetched_at else None))
    return out


def _host(url: str) -> str:
    """The bare host of an external-only document's source URL, for its label."""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0] if rest else ""


def _doc_row_html(row: _DocRow) -> str:
    name = f'<span class="cc-doc-name" title="{escape(row.name, quote=True)}">{escape(row.name)}</span>'
    when = stamp_html(row.fetched_at, css="cc-doc-when")
    return (
        f'<a class="cc-doc-row" href="/source/{row.doc_id}">'
        f'<span class="cc-doc-kind">{escape(row.kind)}</span>'
        f"{name}{when}</a>"
    )


_DOCS_CSS = RESEARCH_PANEL_STYLE.removeprefix("<style>").removesuffix("</style>").strip()


# ----------------------------------------------------------------------------
# Ticker hover mini-card
# ----------------------------------------------------------------------------


def render_ticker_peek(
    conn: sqlite3.Connection,
    repo_root: Path,
    ticker: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """The hover mini-card for a tracked ticker; None when the ticker isn't
    tracked (the route 404s and the hover card simply doesn't show).

    Reuses the cockpit's per-ticker readers (`profile_quote`,
    `next_earnings`, `latest_dcf_runs`) so the card and the cockpit row
    can't drift apart. Rows render hide-don't-stub: a missing price/DCF/ER
    drops its row rather than showing an em-dash pile.

    Phase 3 (bottoms-up metrics engine): also shows the latest computed
    ``pe_ttm`` (compute.metrics_engine, live-price-wired) when
    ``compute_derived_metrics`` has run for this ticker — the first
    UI-consuming surface for an engine valuation metric. Reuses the existing
    row-tuple rendering shape (no new component); a missing/not-yet-computed
    value simply drops the row, same as every other optional row here.
    """
    t = ticker.strip().upper()
    if not t:
        return None
    try:
        row = conn.execute(
            "SELECT name FROM tracked_companies WHERE UPPER(ticker) = ? AND archived_at IS NULL",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    name = str(row[0]) if row[0] else None

    ref = now or datetime.now(UTC)
    price, day_move, _asof = profile_quote(repo_root, t)
    verdict = _latest_overall_status(conn, t)
    next_er = next_earnings(repo_root, t, ref)
    fv_gap = latest_dcf_runs(conn).get(t, (None, None, None, None))[0]
    pending = _pending_alert_count(conn, t)

    badge = ""
    if verdict:
        pill_tone = pill_tone_class(thesis_status_tone(verdict))
        badge = f'<span class="k-pill{pill_tone}">{escape(verdict)}</span>'
    head = f'<div class="cc-mini-head"><span class="cc-mini-ticker">{escape(t)}</span>{badge}</div>'
    name_html = f'<div class="cc-mini-name">{escape(name)}</div>' if name else ""
    # "You said" strip (owner-ratified design review, 2026-08-02): the
    # owner's own last decision on this ticker, ABOVE the mini-card facts —
    # the most personalized read the card can offer, before price/DCF/ER.
    you_said_html = f'<div class="cc-mini-yousaid">{render_you_said_strip(conn, t)}</div>'

    rows: list[tuple[str, str]] = []
    if price is not None:
        move = ""
        if day_move is not None:
            mtone = "k-num-pos" if day_move >= 0 else "k-num-neg"
            move = f' <span class="{mtone}">{escape(fmt_pct(day_move, signed=True))}</span>'
        rows.append(("Price", f"${price:,.2f}{move}"))
    if fv_gap is not None:
        # > 0 — price above fair value (rich); < 0 — below (cheap).
        gtone = "k-num-neg" if fv_gap > 0 else "k-num-pos"
        rows.append(
            ("vs DCF FV", f'<span class="{gtone}">{escape(fmt_pct(fv_gap, signed=True))}</span>')
        )
    pe_ttm = _latest_pe_ttm(conn, t)
    if pe_ttm is not None:
        rows.append(("P/E (TTM)", f"{pe_ttm:,.1f}x"))
    if next_er:
        days = (datetime.fromisoformat(next_er).date() - ref.date()).days
        rel = "today" if days == 0 else f"in {days}d"
        rows.append(("Next ER", f"{escape(fmt_date(next_er, include_year=False))} · {escape(rel)}"))
    if pending:
        rows.append(("Unreviewed", f"{pending} alert{'s' if pending != 1 else ''}"))
    n_events = _events_since_call_count(conn, t)
    if n_events:
        # Pull-only doorway (owner ruling 2026-07-31: news never alerts) —
        # the count retargets the peek to the since-last-call events list;
        # middle-click still lands on the holding.
        rows.append(
            (
                "Since last call",
                f'<a href="/#holding={escape(t)}" '
                f'data-peek-url="/api/peek/news-events?ticker={escape(t)}" '
                f'data-peek-title="Material events">'
                f"{n_events} event{'s' if n_events != 1 else ''}</a>",
            )
        )
    rows_html = "".join(
        f'<div class="cc-mini-row"><span>{label}</span><b>{value}</b></div>'
        for label, value in rows
    )
    return (
        f'<div class="cc-mini">{head}{name_html}{you_said_html}{rows_html}'
        f'<div class="cc-mini-open"><a href="/#holding={escape(t)}">open holding →</a></div>'
        "</div>"
    )


def _latest_pe_ttm(conn: sqlite3.Connection, ticker: str) -> float | None:
    """Latest computed ``pe_ttm`` (compute.metrics_engine, Phase 3 valuation)
    for the hover mini-card. Reuses ``metrics_engine.io.latest_ttm_value`` —
    the same reader the parity harness uses — rather than a bespoke query, so
    this card can never disagree with what the engine actually persisted.
    Best-effort: any error (missing table on a pre-metrics-engine DB) is a
    silent None, same degrade contract as every other peek reader here."""
    try:
        value = latest_ttm_value(conn, ticker, "pe_ttm")
    except sqlite3.Error:
        return None
    return float(value) if value is not None else None


def _latest_overall_status(conn: sqlite3.Connection, ticker: str) -> str | None:
    try:
        source = episode_history_source(conn)
        row = conn.execute(
            f"SELECT overall_status FROM {source.relation} WHERE ticker = ? "
            f"ORDER BY {source.latest_checked_column} DESC LIMIT 1",  # nosec B608 -- trusted closed relation
            (ticker,),
        ).fetchone()
    except (sqlite3.Error, ValueError):
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _events_since_call_count(conn: sqlite3.Connection, ticker: str) -> int:
    """Distinct material events noted since the last reported ER (fallback:
    120 days) — the mini-card's pull-only doorway count. 0 on a pre-0262 DB
    or any error (the row simply drops, hide-don't-stub)."""
    try:
        since: str | None = None
        try:
            from expected_earnings import last_reported_by_ticker

            last = last_reported_by_ticker(conn, datetime.now(UTC).date()).get(ticker)
            since = last.isoformat() if last else None
        except Exception:
            since = None
        if since is None:
            since = (datetime.now(UTC) - timedelta(days=120)).date().isoformat()
        row = conn.execute(
            "SELECT COUNT(DISTINCT CASE WHEN event_key != '' THEN event_key "
            "ELSE 'row:' || id END) FROM news_events "
            "WHERE ticker = ? AND published_at >= ?",
            (ticker, since),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _pending_alert_count(conn: sqlite3.Connection, ticker: str) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status = 'pending' AND ticker = ?",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


# ----------------------------------------------------------------------------
# Next-dollar score breakdown (cockpit Score chip)
# ----------------------------------------------------------------------------

# Band multipliers run [0.5, 1.8] (see research_cockpit's _*_BANDS); the bar
# maps that span to [0, 100]% so a lifting factor reads visibly fuller than a
# dragging one. Mirrors the constants by value, not by import — these are the
# render layer's, not the scorer's.
_BAR_MIN, _BAR_MAX = 0.5, 1.8


def render_score_peek(conn: sqlite3.Connection, repo_root: Path, ticker: str) -> str | None:
    """The next-dollar attractiveness breakdown for an evaluation name — the
    click-through behind the cockpit's Score chip. One row per factor (DCF
    upside · Revenue growth · FCF margin · PEG) with the band multiplier and
    the input it scored, then the product that makes the chip's number, then a
    legend. Reuses :func:`research_cockpit.compute_attractiveness` so the peek
    and the cockpit row read the same inputs and can't disagree. None when the
    ticker isn't a tracked, non-archived name (the route 404s)."""
    t = ticker.strip().upper()
    caption = "Next-dollar attractiveness"
    bd: AttractivenessBreakdown | None
    if _is_etf(conn, t):
        # ETFs score on fund factors (risk-adj return · expense · factor
        # premium · basket valuation) from the Stage 0f cache — the render
        # path never runs the Sharpe window / style OLS (mirrors the fit peek).
        from etf_score_cache import read_materialized_etf_scores

        bd = read_materialized_etf_scores(repo_root).get(t)
        caption = "Next-dollar attractiveness (ETF factors)"
    else:
        bd = compute_attractiveness(conn, repo_root, ticker)
    if bd is None:
        return None
    tone = attractiveness_tone(bd.score)
    head = (
        '<div class="cc-score-head">'
        f'<span class="cc-score-cap">{caption}</span>'
        f'<span class="cc-score-big score-{tone or "mid"}">{bd.score:.2f}</span>'
        "</div>"
    )
    rows = "".join(_score_factor_row(f) for f in bd.factors)
    product = " &times; ".join(f"{f.multiplier:.2f}" for f in bd.factors)
    formula = f'<div class="cc-score-formula">1.00 &times; {product} = <b>{bd.score:.2f}</b></div>'
    legend_bits = ["&times;&gt;1 lifts", "&times;&lt;1 drags"]
    if bd.partial:
        legend_bits.append("missing input scores &times;0.85")
    legend = f'<div class="cc-score-legend">{" · ".join(legend_bits)}</div>'
    foot = (
        f'<div class="cc-peek-foot"><a href="/ticker/{escape(t, quote=True)}">'
        "open the evaluation report &rarr;</a></div>"
    )
    return (
        f'<div class="cc-score">{head}'
        f'<div class="cc-score-rows">{rows}</div>'
        f"{formula}{legend}</div>{foot}<style>{_SCORE_CSS}</style>"
    )


def _is_etf(conn: sqlite3.Connection, ticker: str) -> bool:
    """Instrument-kind check for the score peek's ETF branch; a pre-0044
    substrate (missing column) reads as equity, the established default."""
    try:
        row = conn.execute(
            "SELECT instrument_type FROM tracked_companies WHERE UPPER(ticker) = ? LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0] or "").lower() == "etf"


def _factor_row_html(
    label: str, multiplier: float, detail: str, missing: bool, *, bar_min: float, bar_max: float
) -> str:
    """One breakdown-peek factor row: label · the input it scored · a multiplier
    bar · the multiplier. Shared by the Score and Fit peeks (their factors carry
    the same shape); a missing input shows "no data" and no bar. ``bar_min`` /
    ``bar_max`` scale the bar to the factor family's multiplier range."""
    if missing:
        return (
            '<div class="cc-score-row cc-score-row-missing">'
            f'<span class="cc-score-label">{escape(label)}</span>'
            '<span class="cc-score-detail muted">no data</span>'
            '<span class="cc-score-bar"></span>'
            f'<span class="cc-score-mult mult-mid">&times;{multiplier:.2f}</span>'
            "</div>"
        )
    tone = "pos" if multiplier > 1.0 else "neg" if multiplier < 1.0 else "mid"
    pct = max(0.0, min(100.0, (multiplier - bar_min) / (bar_max - bar_min) * 100.0))
    bar = (
        '<span class="cc-score-bar">'
        f'<span class="cc-score-fill bar-{tone}" style="width:{pct:.0f}%"></span></span>'
    )
    return (
        '<div class="cc-score-row">'
        f'<span class="cc-score-label">{escape(label)}</span>'
        f'<span class="cc-score-detail">{escape(detail)}</span>'
        f"{bar}"
        f'<span class="cc-score-mult mult-{tone}">&times;{multiplier:.2f}</span>'
        "</div>"
    )


def _score_factor_row(f: AttractivenessFactor) -> str:
    return _factor_row_html(
        f.label, f.multiplier, f.detail, f.missing, bar_min=_BAR_MIN, bar_max=_BAR_MAX
    )


_SCORE_CSS = RESEARCH_PANEL_STYLE.removeprefix("<style>").removesuffix("</style>")


# ----------------------------------------------------------------------------
# Portfolio-fit breakdown (cockpit Fit chip)
# ----------------------------------------------------------------------------

# Fit multipliers are centered on 1.0 over a tighter range than the score's
# (~0.8 to 1.25); the bar maps [0.7, 1.3] to [0, 100]% so a lift reads fuller
# than a drag. Reuses the cc-score-* layout (the breakdown anatomy is identical).
_FIT_BAR_MIN, _FIT_BAR_MAX = 0.7, 1.3


def render_fit_peek(repo_root: Path, ticker: str) -> str | None:
    """The portfolio-fit breakdown for an evaluation name — the click-through
    behind the cockpit's Fit chip. One row per factor (Marginal Sharpe ·
    Diversification · Factor fit · Sector fit) with the band multiplier and the
    reading it scored, then the product that makes the chip's number. Read from
    the materialized ``candidate_fit.json`` (the same cache the chip's number
    came from — never recomputed on the render path). None when the ticker has no
    cached fit (the route 404s)."""
    from allocation.candidate_fit import fit_tone
    from candidate_fit_cache import read_materialized_candidate_fit

    t = ticker.strip().upper()
    cf = read_materialized_candidate_fit(repo_root).get(t)
    if cf is None:
        return None
    from candidate_fit_cache import read_materialized_fit_meta

    meta = read_materialized_fit_meta(repo_root)
    target_block = meta.get("target")
    target_active = (
        isinstance(target_block, dict)
        and cast("dict[str, object]", target_block).get("source") == "intent"
    )

    tone = fit_tone(cf.fit)
    big_tone = "score-hi" if tone == "hi" else "score-warn" if tone == "lo" else "mid"
    head = (
        '<div class="cc-score-head">'
        '<span class="cc-score-cap">Portfolio fit to the held book</span>'
        f'<span class="cc-score-big {big_tone}">{cf.fit:.2f}</span>'
        "</div>"
    )
    degraded_html = (
        '<div class="cc-fit-degraded">&#9888; book context degraded: '
        + escape(" · ".join(cf.degraded))
        + "</div>"
        if cf.degraded
        else ""
    )
    rows = "".join(
        _factor_row_html(
            f.label, f.multiplier, f.detail, f.missing, bar_min=_FIT_BAR_MIN, bar_max=_FIT_BAR_MAX
        )
        for f in cf.factors
    )
    product = " &times; ".join(f"{f.multiplier:.2f}" for f in cf.factors)
    formula = f'<div class="cc-score-formula">1.00 &times; {product} = <b>{cf.fit:.2f}</b></div>'

    # Fit v2: the target group — only when an owner intent (not the book
    # default) is active; under the default the gaps are zero by construction
    # and the group would be noise.
    target_html = ""
    if target_active and cf.target_factors and cf.fit_target is not None:
        target_rows = "".join(
            _factor_row_html(
                f.label,
                f.multiplier,
                f.detail,
                f.missing,
                bar_min=_FIT_BAR_MIN,
                bar_max=_FIT_BAR_MAX,
            )
            for f in cf.target_factors
        )
        narrative = ""
        if isinstance(target_block, dict):
            raw_narr = cast("dict[str, object]", target_block).get("narrative")
            if isinstance(raw_narr, str) and raw_narr.strip():
                narrative = f' title="{escape(raw_narr.strip(), quote=True)}"'
        target_html = (
            f'<div class="cc-fit-group"{narrative}>vs your positioning target</div>'
            f'<div class="cc-score-rows">{target_rows}</div>'
            f'<div class="cc-score-formula">{cf.fit:.2f} &times; '
            + " &times; ".join(f"{f.multiplier:.2f}" for f in cf.target_factors)
            + f" = <b>{cf.fit_target:.2f}</b> (fit to target)</div>"
        )

    # ΔSR + correlation-trend strip, and the doorway into the full what-if.
    strip_bits: list[str] = []
    if cf.sharpe_delta_bps is not None:
        strip_bits.append(f"&Delta;SR at 3% = <b>{cf.sharpe_delta_bps:+.0f}bp</b> (modeled book)")
    if cf.corr_trend is not None and cf.corr_recent is not None:
        arrow = {"rising": "&uarr;", "falling": "&darr;", "stable": "&rarr;"}[cf.corr_trend]
        strip_bits.append(f"corr trend {arrow} {cf.corr_trend} (63d {cf.corr_recent:+.2f})")
    strip = f'<div class="cc-fit-strip">{" · ".join(strip_bits)}</div>' if strip_bits else ""

    legend_bits = ["&times;&gt;1 accretive", "&times;&lt;1 dilutive"]
    if cf.partial:
        legend_bits.append("missing factor scores neutral")
    legend = f'<div class="cc-score-legend">{" · ".join(legend_bits)}</div>'
    tq = escape(t, quote=True)
    foot = (
        f'<div class="cc-peek-foot">'
        f'<a class="k-chip k-chip-btn" data-peek-url="/api/peek/whatif?ticker={tq}" '
        f'data-peek-title="What-if · {tq}" href="/ticker/{tq}">full what-if workup &rarr;</a>'
        f' <a href="/ticker/{tq}">open the evaluation report &rarr;</a></div>'
    )
    return (
        f'<div class="cc-score">{head}{degraded_html}'
        f'<div class="cc-score-rows">{rows}</div>'
        f"{formula}{target_html}{strip}{legend}</div>{foot}<style>{_SCORE_CSS}</style>"
    )


# ----------------------------------------------------------------------------
# What-if peek (cockpit ΔSR chip + the fit peek's doorway)
# ----------------------------------------------------------------------------


# funding_mode query-param shorthand <-> allocation.what_if.FUNDING_MODES full
# name, shared by the chip links (built here) and the route (comments_server
# ``peek_whatif``) that parses them back — one mapping, never duplicated.
_FUNDING_PARAM_TO_MODE: dict[str, str] = {
    "new_cash": "new_cash",
    "pro_rata": "pro_rata_reallocation",
}
_MODE_TO_FUNDING_PARAM: dict[str, str] = {v: k for k, v in _FUNDING_PARAM_TO_MODE.items()}


def _whatif_error_html(message: str) -> str:
    """Small inline error snippet for a rejected weight/funding_mode — never a
    500; the peek just shows why the request didn't compute."""
    return (
        '<div class="cc-score">'
        f'<div class="cc-fit-degraded">&#9888; {escape(message)}</div>'
        f"</div><style>{_SCORE_CSS}</style>"
    )


def render_what_if_peek(
    repo_root: Path,
    ticker: str,
    weight: float,
    *,
    funding_mode: str = "new_cash",
    db_path: Path | str | None = None,
) -> str | None:
    """The book BEFORE → AFTER adding one name at a chosen weight — vol, Sharpe
    (Δ in bps), growth tilt, resulting post-trade weight + concentration zone
    (PRD §7.2, P0.2), the candidate's top correlations to individual
    holdings, and (C7, when ``db_path`` is given) the book's top-3 C3
    business-factor movers, from ``allocation.what_if`` (module-cached; a cold
    compute is a user-initiated click, never the table render). The weight
    selector chips are peek doorways themselves — clicking a preset re-renders
    the popover at that weight, carrying the current ``funding_mode`` along.

    ``funding_mode`` (``"new_cash"`` | ``"pro_rata_reallocation"``) is FRAMING
    ONLY — see ``allocation.what_if``'s module docstring: it never changes the
    modeled vol/Sharpe/tilt numbers, only how the add is explained. An
    out-of-range weight, or an unknown funding mode, renders a small inline
    error naming the preset menu instead of raising. ``None`` when the
    weights cache is empty (the route 404s).

    ``db_path`` is optional (defaults ``None``, matching every caller before
    C7): omitting it keeps this peek exactly as it rendered before the C3
    factor substrate existed — no business-factor section, same cache
    behavior. Passing it degrades cleanly to the same absent section on a
    pre-migration DB or an empty ``business_factor_exposures`` table
    (``compute_what_if`` never raises on that path)."""
    from allocation.what_if import ALLOWED_WEIGHTS, compute_what_if, validate_weight
    from candidate_fit_cache import read_materialized_fit_meta
    from portfolio_weights import read_materialized_weights

    t = ticker.strip().upper()
    weights = read_materialized_weights(repo_root)
    if not weights:
        return None
    meta = read_materialized_fit_meta(repo_root)
    book_block = cast("dict[str, object]", meta.get("book") or {})

    def _opt(key: str) -> float | None:
        v = book_block.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    try:
        w = validate_weight(weight)
    except ValueError as exc:
        return _whatif_error_html(str(exc))

    try:
        r = compute_what_if(
            repo_root,
            t,
            w,
            book_weights=weights,
            risk_free_annual=_opt("risk_free_annual"),
            book_growth_tilt=_opt("growth_tilt"),
            funding_mode=funding_mode,
            db_path=db_path,
        )
    except ValueError as exc:
        return _whatif_error_html(str(exc))

    tq = escape(t, quote=True)
    funding_param = _MODE_TO_FUNDING_PARAM.get(funding_mode, "new_cash")
    selector = "".join(
        (
            f'<span class="k-chip k-chip-mono cc-wi-w-on">{aw * 100:g}%</span>'
            if aw == w
            else f'<a class="k-chip k-chip-mono k-chip-btn" '
            f'data-peek-url="/api/peek/whatif?ticker={tq}&amp;w={aw}'
            f'&amp;funding={funding_param}" '
            f'data-peek-title="What-if · {tq}" href="/ticker/{tq}">{aw * 100:g}%</a>'
        )
        for aw in ALLOWED_WEIGHTS
    )
    if w not in ALLOWED_WEIGHTS:
        # An exact off-menu weight (validate_weight allows any (0, 25%]) still
        # gets an active chip so the current evaluation is visible.
        selector = f'<span class="k-chip k-chip-mono cc-wi-w-on">{w * 100:g}%</span>' + selector
    head = (
        '<div class="cc-score-head">'
        f'<span class="cc-score-cap">What-if: add {escape(t)} at</span>'
        f'<span class="cc-wi-weights">{selector}</span></div>'
    )

    def _row(label: str, before: float | None, after: float | None, fmt: str, mult: float) -> str:
        b = format(before * mult, fmt) if before is not None else "—"
        a = format(after * mult, fmt) if after is not None else "—"
        tone = "mid"
        if before is not None and after is not None:
            tone = "pos" if after > before else "neg" if after < before else "mid"
        return (
            '<div class="cc-wi-row">'
            f'<span class="cc-score-label">{escape(label)}</span>'
            f'<span class="cc-wi-val">{b}</span>'
            '<span class="cc-wi-arrow">&rarr;</span>'
            f'<span class="cc-wi-val mult-{tone}">{a}</span></div>'
        )

    rows = [
        _row("Vol (ann.)", r.vol_before_ann, r.vol_after_ann, ".1f", 100.0),
        _row("Sharpe", r.sharpe_before, r.sharpe_after, "+.3f", 1.0),
        _row("Growth tilt", r.growth_tilt_before, r.growth_tilt_after, "+.2f", 1.0),
    ]
    delta = (
        f'<div class="cc-score-formula">&Delta;Sharpe = <b>{r.sharpe_delta_bps:+.0f}bp</b></div>'
        if r.sharpe_delta_bps is not None
        else ""
    )
    # Resulting post-trade single-name weight + its PRD §7.2 concentration
    # zone — the P0.2 seam this peek adds on top of the before/after stats.
    zone_html = (
        '<div class="cc-score-formula">resulting weight '
        f"<b>{r.resulting_weight_pct:.1f}%</b>"
        + (f" &mdash; <b>{escape(r.zone)}</b>" if r.zone else "")
        + "</div>"
    )
    corr_html = ""
    if r.top_correlations:
        chips = " ".join(
            f'<span class="k-chip k-chip-mono">{escape(sym)} {c:+.2f}</span>'
            for sym, c in r.top_correlations
        )
        corr_html = (
            '<div class="cc-fit-group">top correlations to holdings</div>'
            f'<div class="cc-wi-corrs">{chips}</div>'
        )
    # C7 — the book's top-3 C3 business-factor movers (by |delta|), same
    # doorway shape as top_correlations above; absent whenever db_path wasn't
    # passed, the substrate predates 0195, or no holding has a loading yet.
    factor_html = ""
    if r.factor_vector_before is not None and r.factor_vector_after is not None:
        factors = set(r.factor_vector_before) | set(r.factor_vector_after)
        deltas = sorted(
            (
                (f, r.factor_vector_after.get(f, 0.0) - r.factor_vector_before.get(f, 0.0))
                for f in factors
            ),
            key=lambda kv: abs(kv[1]),
            reverse=True,
        )[:3]
        if deltas:
            chips = " ".join(
                f'<span class="k-chip k-chip-mono">{escape(f)} {d * 100:+.1f}pp</span>'
                for f, d in deltas
                if abs(d) > 1e-6
            )
            if chips:
                factor_html = (
                    '<div class="cc-fit-group">top business-factor shifts</div>'
                    f'<div class="cc-wi-corrs">{chips}</div>'
                )
    degraded_html = (
        '<div class="cc-fit-degraded">&#9888; ' + escape(" · ".join(r.degraded)) + "</div>"
        if r.degraded
        else ""
    )
    obs = f" · {r.obs} common days" if r.obs else ""
    through = f" · prices through {escape(r.prices_through)}" if r.prices_through else ""
    funding_note = (
        "funded via a new-cash deposit (no implied sales)"
        if r.funding_mode == "new_cash"
        else "funded via pro-rata reallocation (selling this fraction of every holding)"
    )
    legend = (
        f'<div class="cc-score-legend">modeled book (local price caches); {funding_note} '
        f"((1&minus;w)&middot;book + w&middot;{escape(t)}){obs}{through}</div>"
    )
    foot = (
        f'<div class="cc-peek-foot"><a href="/ticker/{tq}">open the evaluation report '
        "&rarr;</a></div>"
    )
    return (
        f'<div class="cc-score">{head}{degraded_html}'
        f'<div class="cc-score-rows">{"".join(rows)}</div>'
        f"{delta}{zone_html}{corr_html}{factor_html}{legend}</div>{foot}"
        f"<style>{_SCORE_CSS}</style>"
    )


# ----------------------------------------------------------------------------
# Advisor memo peek
# ----------------------------------------------------------------------------


def render_memo_peek(db_path: Path, kind: str) -> str | None:
    """The latest advisor memo of ``kind``, markdown-rendered, for the
    portfolio insights' "full memo →" peek. None on an unknown kind or when
    no memo of that kind exists yet.

    A guard_override ``position_review`` memo also gets the owner's one-click
    "this review changed my call" attestation (see :func:`_memo_attest_foot`) —
    the sole surface that can move the Coach P&L's Q3'26 "changed >= 1" bar,
    reached from that panel's "reviews →" doorway."""
    if kind not in _MEMO_KINDS or not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id, title, body_md, created_at, context_json FROM advisor_memos "
            "WHERE kind = ? ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    memo_id, title, body_md, created_at = int(row[0]), str(row[1]), str(row[2]), str(row[3])
    foot = _memo_attest_foot(kind, memo_id, row[4])
    return (
        '<div class="cc-peek-memo">'
        f'<div class="cc-peek-memo-head"><h2>{escape(title)}</h2>'
        f"{stamp_html(created_at, mode='date')}</div>"
        f'<div class="synthesis-body">{render_prose(body_md[:20000])}</div>'
        f"{foot}"
        "</div>"
    )


def _memo_attest_foot(kind: str, memo_id: int, context_raw: object) -> str:
    """The owner's one-click "this review changed my call" attestation, rendered
    ONLY on a guard_override ``position_review`` memo that hasn't been attested
    yet. Attestation is the sole input the Coach P&L counts toward its Q3'26
    "changed >= 1" bar (the silence-implies-heeded heuristic feeds only the
    separate "candidate" line), so this button is the one surface that can move
    it. Any other kind, a non-override review, or an already-attested memo
    renders nothing."""
    if kind != "position_review":
        return ""
    ctx: dict[str, object] = {}
    if context_raw:
        try:
            parsed = json.loads(str(context_raw))
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            ctx = cast("dict[str, object]", parsed)
    if ctx.get("verdict_source") != "guard_override":
        return ""
    if ctx.get("owner_attested_change") is True:
        return (
            '<div class="cc-peek-attest cc-peek-attest-done">'
            "&#10003; You confirmed this review changed your call &mdash; it counts toward "
            "the coach&rsquo;s Q3&rsquo;26 bar.</div>"
        )
    return (
        '<div class="cc-peek-attest">'
        '<button type="button" class="k-btn k-btn-sm cc-attest-btn" '
        f'data-attest-memo-id="{memo_id}">This review changed my call</button>'
        '<span class="cc-attest-msg" hidden></span>'
        f"</div><style>{_ATTEST_CSS}</style><script>{_ATTEST_JS}</script>"
    )


_ATTEST_CSS = RESEARCH_PANEL_STYLE.removeprefix("<style>").removesuffix("</style>")

# POSTs {memo_id} to /api/coach/attest-change (the same Origin-guarded action
# family /api/coach/unmute uses), then reflects the result in place. A confirmed
# attestation is what promotes a "candidate" to a counted "changed" decision on
# the Coach P&L; the button self-disables so a second click can't double-count
# (the server is idempotent regardless — attest_review_changed no-ops if set).
_ATTEST_JS = """
(function () {
  if (window.__ccAttestWired) return;
  window.__ccAttestWired = true;
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-attest-memo-id]') : null;
    if (!btn || btn.disabled) return;
    var wrap = btn.closest('.cc-peek-attest');
    var msg = wrap ? wrap.querySelector('.cc-attest-msg') : null;
    function say(t) { if (msg) { msg.hidden = false; msg.textContent = t; } }
    CCAction.busy(btn, 'Recording\\u2026');
    fetch('/api/coach/attest-change', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ memo_id: Number(btn.getAttribute('data-attest-memo-id')) })
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, body: j }; });
    }).then(function (r) {
      if (r.ok && r.body && r.body.attested) {
        CCAction.receipt(btn, '\\u2713 Recorded');
        say('Counts toward the coach\\u2019s Q3\\u201926 bar.');
      } else {
        CCAction.release(btn);
        say((r.body && r.body.error) || 'Already recorded.');
      }
    }).catch(function (e) { CCAction.release(btn); say(e.message); });
  });
})();
""".strip()


# ----------------------------------------------------------------------------
# Review peek (PR5) — the instant /review pre-analysis + full-review doorway
# ----------------------------------------------------------------------------


def render_review_peek(repo_root: Path, db_path: Path, ticker: str) -> str:
    """The instant, LLM-free position-review read for the Holding band's
    "Review" link and the portfolio cockpit's review pill: the same grounded
    facts + mechanical read + tax block + live graded-sells base rate
    ``render_pre_analysis_chat`` renders for ``/review``, run through
    :func:`ui.prose.render_prose` (it's markdown-ish text, not raw HTML), plus
    a footer button that escalates to the full LLM-calibrated review — the
    ``position_review`` dashboard action, which PERSISTS a gradeable memo.

    Always renders (never None / 404): a name with no encoded thesis degrades
    to the deterministic "encode a thesis first" read, same as ``/review``
    proper. ``build_pre_analysis`` degrades tracker-offline / no-DCF on its
    own, so this stays a thin render wrapper with no extra guarding.
    """
    from advisor.position_review import build_pre_analysis, render_pre_analysis_chat

    t = ticker.strip().upper()
    pre = build_pre_analysis(repo_root, t, db_path=db_path)
    body = render_prose(render_pre_analysis_chat(pre, db_path=db_path))
    escaped_t = escape(t, quote=True)
    foot = (
        '<div class="cc-review-foot">'
        f'<button type="button" class="k-btn k-btn-primary k-btn-sm cc-review-btn" '
        f'data-review-ticker="{escaped_t}">Full calibrated review (LLM)</button>'
        '<pre class="cc-review-log" hidden></pre>'
        "</div>"
    )
    return f'<div class="cc-review-peek">{body}{foot}</div><style>{_REVIEW_CSS}</style><script>{_REVIEW_JS}</script>'


_REVIEW_CSS = RESEARCH_PANEL_STYLE.removeprefix("<style>").removesuffix("</style>")

# The escalation button POSTs /actions/position-review (single-flight per
# ticker, same registry every /actions/* endpoint shares) then streams the job
# log over the standard /actions/stream/<job_id> SSE contract — the same
# fetch-then-EventSource shape _PROV_JS uses. A run PERSISTS a position_review
# memo (write_ledger=True), which is the point: the peek closing mid-stream
# just means the memo lands without a visible "done" line, not a lost write.
_REVIEW_JS = """
(function () {
  if (window.__ccReviewWired) return;
  window.__ccReviewWired = true;
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-review-ticker]') : null;
    if (!btn || btn.disabled) return;
    var wrap = btn.closest('.cc-review-peek');
    var log = wrap ? wrap.querySelector('.cc-review-log') : null;
    if (!log) return;
    function line(t) { log.hidden = false; log.textContent += t + '\\n'; log.scrollTop = log.scrollHeight; }
    CCAction.busy(btn, 'Running…');
    fetch('/actions/position-review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: btn.getAttribute('data-review-ticker') })
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, status: resp.status, body: j }; });
    }).then(function (r) {
      if (!r.ok) {
        line('! ' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
        CCAction.release(btn);
        return;
      }
      line('> position review started (job ' + r.body.job_id + ')');
      var es = new EventSource(r.body.stream_url);
      var finished = false;
      es.onmessage = function (e) {
        var m;
        try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'log') { line(m.line); }
        else if (m.event === 'done') {
          finished = true;
          line('# exit code ' + m.exit_code);
          es.close();
          if (m.exit_code === 0) {
            CCAction.receipt(btn, '✓ Review recorded');
          } else {
            line('! review run failed — exit code ' + m.exit_code + ', see log above');
            CCAction.release(btn);
          }
        }
      };
      es.onerror = function () {
        if (finished) return;
        line('! stream interrupted — is the server still running?');
        es.close();
        CCAction.release(btn);
      };
    }).catch(function (e) { line('! ' + e.message); CCAction.release(btn); });
  });
})();
""".strip()


# ----------------------------------------------------------------------------
# Data-provenance peek (UX9d) — freshness rows + in-place refresh
# ----------------------------------------------------------------------------


class _ProvAction(NamedTuple):
    """One inline refresh control: POSTs an EXISTING /actions/* endpoint (the
    same job kinds the System actions console starts — no new job types)."""

    post_url: str
    body: dict[str, object]
    label: str
    title: str


class _ProvRow(NamedTuple):
    label: str
    stamp: str | None  # ISO timestamp rendered relative (ui.time rules)
    prefix: str  # rides inside the stamp span ("oldest ")
    note: str | None  # plain-text aside ("12 endpoints · oldest 3w ago")
    action: _ProvAction | None
    cron_hint: str | None  # no on-demand action — name the cron that owns it


def render_provenance_peek(db_path: Path, ticker: str | None) -> str:
    """Per-source data freshness with in-place refresh — the click-through
    behind the freshness dots (cockpit rows, the holding-header dot) and the
    Home tier strip.

    ``ticker`` scopes to one holding; ``None`` renders portfolio-wide
    aggregates. Each row is one provenance source — report brief, FMP
    endpoint pulls, IR documents, news, the earnings calendar — with a
    relative age (exact UTC in the tooltip) and, where a registered
    ``/actions/*`` endpoint can refresh that source, a button that streams
    the job log into the peek over the standard ``/actions/stream/<job_id>``
    SSE channel (button disabled while its job runs). Sources with no
    on-demand action show which cron owns them instead. The System →
    Actions console stays the deep path (the peek footer links it).

    Always renders: a missing DB / table / column / row degrades that row to
    an em-dash age, never an error.
    """
    t = (ticker or "").strip().upper() or None
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        except sqlite3.Error:
            conn = None
    try:
        rows = _prov_ticker_rows(conn, t) if t else _prov_portfolio_rows(conn)
    finally:
        if conn is not None:
            conn.close()
    scope = escape(t or "portfolio", quote=True)
    return (
        f'<div class="cc-prov" data-prov-scope="{scope}">'
        f'<div class="cc-prov-rows">{"".join(_prov_row_html(r) for r in rows)}</div>'
        '<pre class="cc-prov-log" hidden></pre>'
        '<div class="cc-peek-foot"><a href="/#actions">open the Actions console →</a></div>'
        f"<style>{_PROV_CSS}</style><script>{_PROV_JS}</script>"
        "</div>"
    )


def _prov_ticker_rows(conn: sqlite3.Connection | None, t: str) -> list[_ProvRow]:
    rows: list[_ProvRow] = []

    r = _prov_first(
        conn,
        "SELECT last_built_at FROM tracked_companies "
        "WHERE UPPER(ticker) = ? AND archived_at IS NULL",
        (t,),
    )
    rows.append(
        _ProvRow(
            "Report brief",
            _ts(r, 0),
            "",
            None,
            _ProvAction(
                "/actions/refresh",
                {"ticker": t, "mode": "stale"},
                "Refresh",
                "Run the stale refresh chain (FMP → transcripts → IR docs → KPIs → report rebuild)",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_pulled), MIN(last_pulled), COUNT(*) "
        "FROM fmp_endpoint_status WHERE UPPER(ticker) = ?",
        (t,),
    )
    newest, oldest, n = _ts(r, 0), _ts(r, 1), _count(r, 2)
    note = None
    if n:
        note = f"{n} endpoint{'s' if n != 1 else ''}"
        if oldest and oldest != newest:
            note += f" · oldest {fmt_reltime(oldest)}"
    rows.append(
        _ProvRow(
            "FMP endpoints",
            newest,
            "",
            note,
            _ProvAction(
                "/actions/refresh",
                {"ticker": t, "mode": "stale", "steps": ["fmp"]},
                "Refresh",
                "Re-pull this ticker's FMP endpoints (cadence-aware — fresh ones are skipped)",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(fetched_at), COUNT(*) FROM documents "
        "WHERE UPPER(ticker) = ? AND doc_type LIKE 'ir_%'",
        (t,),
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "IR documents",
            _ts(r, 0),
            "",
            f"{n} doc{'s' if n != 1 else ''}" if n else None,
            _ProvAction(
                "/actions/refresh-ir",
                {"ticker": t},
                "Refresh",
                "Discover + ingest the issuer's IR historical-data spreadsheet (headless browser)",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(fetched_at), COUNT(*) FROM news WHERE UPPER(ticker) = ?",
        (t,),
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "News",
            _ts(r, 0),
            "",
            f"{n} stor{'ies' if n != 1 else 'y'}" if n else None,
            _ProvAction(
                "/actions/refresh",
                {"ticker": t, "mode": "stale", "steps": ["news"]},
                "Refresh",
                "Re-fetch this ticker's news feeds",
            ),
            None,
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_seen_at), "
        "MIN(CASE WHEN expected_date >= date('now') THEN expected_date END) "
        "FROM expected_earnings WHERE UPPER(ticker) = ?",
        (t,),
    )
    nxt = _ts(r, 1)
    rows.append(
        _ProvRow(
            "Earnings calendar",
            _ts(r, 0),
            "",
            f"next ER {fmt_date(nxt)}" if nxt else None,
            None,
            "the daily calendar refresher",
        )
    )
    return rows


def _prov_portfolio_rows(conn: sqlite3.Connection | None) -> list[_ProvRow]:
    rows: list[_ProvRow] = []

    r = _prov_first(
        conn,
        "SELECT COUNT(*), COUNT(last_built_at), MIN(last_built_at) "
        "FROM tracked_companies WHERE archived_at IS NULL",
    )
    total, built = _count(r, 0), _count(r, 1)
    rows.append(
        _ProvRow(
            "Report briefs",
            _ts(r, 2),
            "oldest ",
            f"{built} of {total} built" if total else None,
            None,
            "the tier crons (P1 daily · P2 weekly · P3 monthly)",
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_pulled), COUNT(DISTINCT ticker), COUNT(*) FROM fmp_endpoint_status",
    )
    n_tickers, n_rows = _count(r, 1), _count(r, 2)
    rows.append(
        _ProvRow(
            "FMP endpoints",
            _ts(r, 0),
            "",
            f"{n_tickers} tickers · {n_rows} endpoint rows" if n_rows else None,
            None,
            "the daily fetch cron",
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(fetched_at), COUNT(*) FROM documents WHERE doc_type LIKE 'ir_%'",
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "IR documents",
            _ts(r, 0),
            "",
            f"{n} doc{'s' if n != 1 else ''}" if n else None,
            _ProvAction(
                "/actions/maintenance",
                {"action": "process_inbox"},
                "Process inbox",
                "Register documents dropped into the inbox folder "
                "(register_dropped_documents --all); the weekly IR cron does the fetching",
            ),
            None,
        )
    )

    r = _prov_first(conn, "SELECT MAX(fetched_at), COUNT(*) FROM news")
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "News",
            _ts(r, 0),
            "",
            f"{n} stor{'ies' if n != 1 else 'y'}" if n else None,
            None,
            "the daily news fetch",
        )
    )

    r = _prov_first(
        conn,
        "SELECT MAX(last_seen_at), COUNT(DISTINCT ticker) FROM expected_earnings",
    )
    n = _count(r, 1)
    rows.append(
        _ProvRow(
            "Earnings calendar",
            _ts(r, 0),
            "",
            f"{n} tickers dated" if n else None,
            None,
            "the daily calendar refresher",
        )
    )
    return rows


def _prov_first(
    conn: sqlite3.Connection | None, sql: str, params: tuple[object, ...] = ()
) -> tuple[object, ...] | None:
    """First row or None — a missing table/column (pre-migration DB, the
    minimal test substrates) degrades the row, never the peek."""
    if conn is None:
        return None
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _ts(row: tuple[object, ...] | None, idx: int) -> str | None:
    if row is None or idx >= len(row) or not row[idx]:
        return None
    return str(row[idx])


def _count(row: tuple[object, ...] | None, idx: int) -> int:
    if row is None or idx >= len(row):
        return 0
    v = row[idx]
    return int(v) if isinstance(v, int | float) else 0


def _prov_row_html(row: _ProvRow) -> str:
    note = f' <span class="cc-prov-note">{escape(row.note)}</span>' if row.note else ""
    if row.action is not None:
        control = (
            '<button type="button" class="cc-prov-btn k-btn k-btn-quiet k-btn-sm" '
            f'data-prov-post="{escape(row.action.post_url, quote=True)}" '
            f'data-prov-body="{escape(json.dumps(row.action.body), quote=True)}" '
            f'title="{escape(row.action.title, quote=True)}">{escape(row.action.label)}</button>'
        )
    elif row.cron_hint:
        control = (
            '<span class="cc-prov-cron k-chip" title="No on-demand action — refreshed by '
            f'{escape(row.cron_hint, quote=True)}">cron</span>'
        )
    else:
        control = ""
    stamp = stamp_html(row.stamp, css="cc-prov-age", prefix=row.prefix)
    return (
        '<div class="cc-prov-row">'
        f'<span class="cc-prov-src">{escape(row.label)}</span>'
        f'<span class="cc-prov-when">{stamp}{note}</span>'
        f"{control}</div>"
    )


_PROV_CSS = RESEARCH_PANEL_STYLE.removeprefix("<style>").removesuffix("</style>")

# In-peek refresh wiring: POST the row's /actions/* endpoint, then stream the
# job's SSE frames ({event: start|log|done}) into the shared .cc-prov-log —
# the same contract the System actions console uses (dashboard_html). One
# document-level delegated listener (guarded) so re-injected fragments never
# double-wire; the clicked button alone is disabled while ITS job runs (other
# rows stay actionable — the registry single-flights per (ticker, kind)). A
# peek closed mid-stream leaves the EventSource draining into detached nodes
# until its done frame — harmless on this single-user localhost tool.
_PROV_JS = """
(function () {
  if (window.__ccProvWired) return;
  window.__ccProvWired = true;
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-prov-post]') : null;
    if (!btn || btn.disabled) return;
    var wrap = btn.closest('.cc-prov');
    var log = wrap ? wrap.querySelector('.cc-prov-log') : null;
    if (!log) return;
    function line(t) { log.hidden = false; log.textContent += t + '\\n'; log.scrollTop = log.scrollHeight; }
    CCAction.busy(btn, 'Running…');
    fetch(btn.getAttribute('data-prov-post'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: btn.getAttribute('data-prov-body') || '{}'
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, status: resp.status, body: j }; });
    }).then(function (r) {
      if (!r.ok) {
        line('! ' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
        CCAction.release(btn);
        return;
      }
      line('> ' + r.body.kind + ' started (job ' + r.body.job_id + ')');
      var es = new EventSource(r.body.stream_url);
      var finished = false;
      es.onmessage = function (e) {
        var m;
        try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'log') { line(m.line); }
        else if (m.event === 'done') {
          finished = true;
          line('# exit code ' + m.exit_code);
          es.close();
          if (m.exit_code === 0) {
            CCAction.receipt(btn, '✓ Refreshed');
          } else {
            line('! refresh failed — exit code ' + m.exit_code + ', see log above');
            CCAction.release(btn);
          }
        }
      };
      es.onerror = function () {
        if (finished) return;
        line('! stream interrupted — is the server still running?');
        es.close();
        CCAction.release(btn);
      };
    }).catch(function (e) { line('! ' + e.message); CCAction.release(btn); });
  });
})();
""".strip()


# ----------------------------------------------------------------------------
# Fact-provenance peek (provenance click-through Phase A, section 2) --
# GET /api/peek/provenance/<fact_ref>, dispatched on the fact row's
# FactLocator.effective_kind(). Every branch degrades per section 2.7 rather
# than 404ing once the row itself is found -- a 404 means "no such fact,"
# never "couldn't render its evidence."
# ----------------------------------------------------------------------------

_PROVENANCE_TABLES = frozenset({"financial_facts", "kpi_facts", "segment_dimensions"})

# DerivedInputRef.ref -> peek-dispatcher table name (docs/design/
# provenance_clickthrough.md §1.2's DerivedInputRef.ref vocabulary is the
# singular "financial_fact"/"kpi_fact"/"segment_fact"; the peek dispatcher's
# fact_ref uses the plural table names -- this is the one mapping between
# the two so render_derived_peek's recursive doorways resolve correctly).
_DERIVED_REF_TO_TABLE: dict[str, str] = {
    "financial_fact": "financial_facts",
    "kpi_fact": "kpi_facts",
    "segment_fact": "segment_dimensions",
}

# Recursion depth cap for render_derived_peek (docs/design/
# provenance_clickthrough.md §2.5) -- mirrors source_chip._MAX_LINEAGE_INPUTS'
# existing "a formula tree deeper than N is itself a smell" reasoning.
_MAX_DERIVED_DEPTH = 4


class _FactRow(NamedTuple):
    """The slice of a financial_facts/kpi_facts/segment_dimensions row the
    peek needs, common to all three (kpi_facts joined to kpi_definitions for
    its name; segment_dimensions joined to segment_periods for ticker/doc)."""

    ticker: str
    label: str  # line_item, kpi_definitions.name, or "<dim_type>: <dim_name> <metric>"
    source_doc_id: int | None
    locator_json: str | None
    confidence: float | None
    extracted_by: str | None
    # kpi_facts only (financial_facts/segment_dimensions have no such column)
    # -- the pre-locator `derived` lineage shape (alembic 0087). Lets
    # render_fact_provenance_peek recover a derived-formula tree for a row
    # written before the `derived` locator kind existed at all
    # (pipeline.locators.derived_locator_from_computed_from).
    computed_from_json: str | None = None
    formula_id: int | None = None


def _load_fact_row(db_path: Path, table: str, fact_id: int) -> _FactRow | None:
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    try:
        if table == "financial_facts":
            row = conn.execute(
                "SELECT ticker, line_item, source_doc_id, locator, confidence, extracted_by "
                "FROM financial_facts WHERE id = ?",
                (fact_id,),
            ).fetchone()
        elif table == "segment_dimensions":
            row = conn.execute(
                "SELECT sp.ticker, "
                " (sd.dim_type || ': ' || sd.dim_name || ' ' || sd.metric), "
                " sp.source_doc_id, sd.locator, sd.confidence, sd.extracted_by "
                "FROM segment_dimensions sd "
                "JOIN segment_periods sp ON sp.id = sd.period_id "
                "WHERE sd.id = ?",
                (fact_id,),
            ).fetchone()
        else:
            try:
                # kpi_facts.computed_from/.formula_id (alembic 0087/#905) --
                # feature-detected rather than assumed, since several
                # lightweight test/legacy schemas create a minimal kpi_facts
                # without them; falling back keeps this dispatcher working
                # against any pre-existing kpi_facts shape.
                row = conn.execute(
                    "SELECT kf.ticker, kd.name, kf.source_doc_id, kf.locator, "
                    "kf.confidence, kf.extracted_by, kf.computed_from, kf.formula_id "
                    "FROM kpi_facts kf "
                    "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id WHERE kf.id = ?",
                    (fact_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = conn.execute(
                    "SELECT kf.ticker, kd.name, kf.source_doc_id, kf.locator, "
                    "kf.confidence, kf.extracted_by "
                    "FROM kpi_facts kf "
                    "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id WHERE kf.id = ?",
                    (fact_id,),
                ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _FactRow(
        ticker=str(row[0]),
        label=str(row[1]),
        source_doc_id=int(row[2]) if row[2] is not None else None,
        locator_json=str(row[3]) if row[3] is not None else None,
        confidence=float(row[4]) if row[4] is not None else None,
        extracted_by=str(row[5]) if row[5] is not None else None,
        computed_from_json=str(row[6]) if len(row) > 6 and row[6] is not None else None,
        formula_id=int(row[7]) if len(row) > 7 and row[7] is not None else None,
    )


def render_fact_provenance_peek(db_path: Path, repo_root: Path, fact_ref: str) -> str | None:
    """Click-through behind a source chip's fmp_json_table / vendor_field /
    transcript_span / derived locator kinds. fact_ref is <table>:<id>."""
    table, _, raw_id = fact_ref.partition(":")
    if table not in _PROVENANCE_TABLES or not raw_id.isdigit():
        return None
    row = _load_fact_row(db_path, table, int(raw_id))
    if row is None:
        return None
    normalized_ref = f"{table}:{raw_id}"
    return _dispatch_fact_provenance_peek(
        db_path, repo_root, row, visited=frozenset({normalized_ref})
    )


def _dispatch_fact_provenance_peek(
    db_path: Path,
    repo_root: Path,
    row: _FactRow,
    *,
    depth: int = 0,
    visited: frozenset[str] = frozenset(),
) -> str:
    """Dispatches on FactLocator.effective_kind(); always returns something
    -- the legacy floor (section 2.7) is the worst case, never a dead end."""
    locator = FactLocator.from_json(row.locator_json)
    if locator is None and row.computed_from_json is not None:
        # Pre-#905 kpi_facts row: no `locator` column at all, but a
        # `computed_from` blob in the SAME shape -- upgrade it on the fly
        # rather than leaving a derived value stuck on the legacy floor.
        from pipeline.locators import derived_locator_from_computed_from

        locator = derived_locator_from_computed_from(
            row.computed_from_json, formula_id=row.formula_id
        )
    doc = load_document(db_path, row.source_doc_id) if row.source_doc_id is not None else None
    kind = locator.effective_kind() if locator is not None else None

    if kind == LocatorKind.DERIVED and locator is not None and locator.derived is not None:
        return render_derived_peek(
            db_path, repo_root, locator.derived, depth=depth, visited=visited
        )

    if kind == LocatorKind.FMP_JSON_TABLE and locator is not None and doc is not None:
        html = _render_fmp_json_table_peek(repo_root, db_path, doc, locator)
        if html is not None:
            return html
    if (
        kind == LocatorKind.PDF_SLIDE
        and locator is not None
        and locator.pdf_page is not None
        and doc is not None
    ):
        # Phase B (§2.3): the page image with the bbox highlighted when the
        # locator carries one, plus the verbatim snippet callout. Covers v1
        # bare-pdf_page rows too (effective_kind infers PDF_SLIDE) — those
        # became renderable the moment this capability shipped, no data
        # change needed (§5.2).
        html = render_pdf_page_view(
            repo_root,
            db_path,
            doc.id,
            locator.pdf_page,
            bbox=locator.pdf_bbox,
            snippet=locator.verbatim_snippet,
            fragment=True,
        )
        if html is not None:
            return html
    if kind == LocatorKind.TRANSCRIPT_SPAN and doc is not None:
        html = render_transcript_page(repo_root, db_path, doc.id, fragment=True)
        if html is not None:
            return html
    if (
        kind == LocatorKind.VENDOR_FIELD
        and locator is not None
        and locator.vendor_field is not None
    ):
        return _render_vendor_field_peek(db_path, repo_root, row.ticker, locator.vendor_field)
    return _render_legacy_provenance_peek(doc, row, locator)


# Input rows shown before truncating with a "+N more" footer -- mirrors
# source_chip._MAX_LINEAGE_INPUTS's own reasoning (a formula carrying more
# than this is a misbehaving writer, not a UI case).
_MAX_DERIVED_INPUT_ROWS = 6


def render_derived_peek(
    db_path: Path,
    repo_root: Path,
    derived: DerivedRef,
    *,
    depth: int = 0,
    visited: frozenset[str] = frozenset(),
) -> str:
    """The recursive formula-tree peek (docs/design/provenance_clickthrough.md
    §2.5): the formula header, then one row per input.

    A DERIVED input becomes a ``data-peek-url`` doorway (clicking re-fetches
    the SAME dispatcher one level deeper — the existing self-referential peek
    pattern ``render_fit_peek``'s footer link already uses) rather than being
    eagerly rendered here, so a wide/deep tree costs O(inputs-at-this-level),
    not an exponential eager walk. A LEAF input (any other concrete kind, or
    an unresolvable/legacy reference) renders its own evidence INLINE instead
    — the recursion terminates at whichever concrete kind actually grounds
    the number, exactly as the design calls for.

    Depth-capped at ``_MAX_DERIVED_DEPTH`` and cycle-guarded via ``visited``
    (a set of already-rendered ``<table>:<id>`` fact_refs on this call chain)
    — both cases degrade to an inline notice rather than recursing further.
    """
    header = f'<div class="cc-prov-row"><b>{escape(derived.display or "Derived value")}</b></div>'
    meta_bits: list[str] = []
    if derived.formula_id is not None:
        meta_bits.append(f"formula #{derived.formula_id}")
    if derived.method_flags:
        meta_bits.append(", ".join(derived.method_flags))
    meta = (
        f'<div class="cc-prov-row mono">{escape(" · ".join(meta_bits))}</div>' if meta_bits else ""
    )

    inputs = derived.inputs[:_MAX_DERIVED_INPUT_ROWS]
    rows_html: list[str] = []
    for inp in inputs:
        rows_html.append(
            _render_derived_input_row(db_path, repo_root, inp, depth=depth, visited=visited)
        )
    footer = ""
    if len(derived.inputs) > _MAX_DERIVED_INPUT_ROWS:
        footer = (
            '<div class="cc-prov-row mono">'
            f"+{len(derived.inputs) - _MAX_DERIVED_INPUT_ROWS} more inputs</div>"
        )

    return (
        '<div class="cc-prov cc-prov-derived">'
        f'<div class="cc-prov-rows">{header}{meta}{"".join(rows_html)}{footer}</div>'
        "</div>"
    )


def _render_derived_input_row(
    db_path: Path,
    repo_root: Path,
    inp: DerivedInputRef,
    *,
    depth: int,
    visited: frozenset[str],
) -> str:
    """One ``DerivedInputRef`` row: a doorway link for a deeper derived input,
    an inline-rendered evidence blob for a leaf, or a plain (non-clickable)
    label when the input can't be resolved to a real row at all."""
    period_str = f" · {escape(inp.period_end)}" if inp.period_end else ""
    tier_str = inp.tier or ""
    label = f"{escape(inp.item)}{period_str}"

    table = _DERIVED_REF_TO_TABLE.get(inp.ref)
    if table is None or inp.fact_id is None:
        # No fact row to resolve at all (e.g. an input predating fact_id
        # capture) -- the honest floor, same spirit as section 2.7.
        return f'<div class="cc-prov-row cc-prov-input">{label}{_tier_suffix(tier_str)}</div>'

    fact_ref = f"{table}:{inp.fact_id}"
    if fact_ref in visited:
        return (
            f'<div class="cc-prov-row cc-prov-input">{label}{_tier_suffix(tier_str)} '
            '<span class="k-chip k-chip-warn">cycle detected</span></div>'
        )

    input_row = _load_fact_row(db_path, table, inp.fact_id)
    if input_row is None:
        return f'<div class="cc-prov-row cc-prov-input">{label}{_tier_suffix(tier_str)}</div>'

    input_locator = FactLocator.from_json(input_row.locator_json)
    if input_locator is None and input_row.computed_from_json is not None:
        from pipeline.locators import derived_locator_from_computed_from

        input_locator = derived_locator_from_computed_from(
            input_row.computed_from_json, formula_id=input_row.formula_id
        )
    input_kind = input_locator.effective_kind() if input_locator is not None else None

    if input_kind == LocatorKind.DERIVED:
        if depth + 1 >= _MAX_DERIVED_DEPTH:
            return (
                f'<div class="cc-prov-row cc-prov-input">{label}{_tier_suffix(tier_str)} '
                '<span class="k-chip k-chip-warn">max depth reached</span></div>'
            )
        # A doorway: clicking re-fetches this SAME dispatcher one level
        # deeper (peek popover click convention, data-peek-url).
        return (
            f'<div class="cc-prov-row cc-prov-input" data-peek-url="/api/peek/provenance/{escape(fact_ref)}" '
            f'data-peek-title="{escape(inp.item)}">{label}{_tier_suffix(tier_str)} '
            '<span class="cc-prov-doorway">→</span></div>'
        )

    # A leaf: render its own evidence inline, terminating the recursion here.
    inline = _dispatch_fact_provenance_peek(
        db_path, repo_root, input_row, depth=depth + 1, visited=visited | {fact_ref}
    )
    return (
        f'<div class="cc-prov-row cc-prov-input">{label}{_tier_suffix(tier_str)}</div>'
        f'<div class="cc-prov-input-inline">{inline}</div>'
    )


# Mirrors ui.source_chip.SOURCE_CHIP_ABBREV -- duplicated (not imported) to
# keep pipeline.peeks free of a ui.* import purely for a 5-entry label map;
# ui.source_chip already documents this same abbreviation table as the
# canonical one for the tier vocabulary.
SOURCE_CHIP_ABBREV_FALLBACK: dict[str, str] = {
    "sec_official": "SEC",
    "fmp_normalized": "FMP",
    "llm_extracted": "LLM",
    "yfinance_fallback": "YF",
    "s1_provisional": "S-1",
}


def _tier_suffix(tier_str: str) -> str:
    if not tier_str:
        return ""
    abbrev = SOURCE_CHIP_ABBREV_FALLBACK.get(tier_str, tier_str[:3].upper())
    chip_cls = f"src-chip src-{escape(tier_str.replace('_', '-'))}"
    return f' <span class="{chip_cls}">{escape(abbrev)}</span>'


def _render_fmp_json_table_peek(
    repo_root: Path, db_path: Path, doc: _SourceDocRow, locator: FactLocator
) -> str | None:
    """Dispatches an fmp_json_table locator to the matching renderer."""
    cell = locator.table_cell
    if doc.doc_type in {"fmp_10k_json", "fmp_10q_json"}:
        section = (cell.section if cell is not None else None) or locator.section
        return render_form10k_page(repo_root, db_path, doc.id, section, fragment=True)
    if doc.doc_type in _STATEMENT_JSON_DOC_TYPES:
        return render_statement_json_page(
            repo_root,
            db_path,
            doc.id,
            json_path=(cell.json_path if cell is not None else None) or locator.json_path,
            row_label=cell.row_label if cell is not None else None,
            column_header=cell.column_header if cell is not None else None,
            fragment=True,
        )
    return None


def _render_vendor_field_peek(
    db_path: Path, repo_root: Path, ticker: str, vendor_field: VendorFieldRef
) -> str:
    """The honest floor for a vendor_field locator (section 2.6): endpoint +
    field + period, the fetched-at timestamp (fmp_endpoint_status), and the
    raw cached value for that field when the endpoint's cache file is
    findable -- never a bare tier chip with nothing behind it."""
    fetched_at: str | None = None
    raw_value: object = None
    if db_path.exists():
        conn: sqlite3.Connection | None
        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                status_row = conn.execute(
                    "SELECT file_path, last_pulled FROM fmp_endpoint_status "
                    "WHERE UPPER(ticker) = ? AND endpoint = ? "
                    "ORDER BY last_pulled DESC LIMIT 1",
                    (ticker.upper(), vendor_field.endpoint),
                ).fetchone()
            except sqlite3.Error:
                status_row = None
            finally:
                conn.close()
            if status_row is not None:
                file_path, fetched_at_raw = status_row[0], status_row[1]
                fetched_at = str(fetched_at_raw) if fetched_at_raw else None
                if file_path:
                    try:
                        payload: object = json.loads(
                            (repo_root / str(file_path)).read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        payload = None
                    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                        raw_value = cast("dict[str, object]", payload[0]).get(vendor_field.field)
                    elif isinstance(payload, dict):
                        raw_value = cast("dict[str, object]", payload).get(vendor_field.field)
    rows = [
        f'<div class="cc-prov-row"><span class="cc-prov-src">Endpoint</span>'
        f'<span class="cc-prov-when">{escape(vendor_field.endpoint)}</span></div>',
        f'<div class="cc-prov-row"><span class="cc-prov-src">Field</span>'
        f'<span class="cc-prov-when mono">{escape(vendor_field.field)}</span></div>',
    ]
    if vendor_field.period:
        rows.append(
            f'<div class="cc-prov-row"><span class="cc-prov-src">Period</span>'
            f'<span class="cc-prov-when">{escape(vendor_field.period)}</span></div>'
        )
    if fetched_at:
        rows.append(
            '<div class="cc-prov-row"><span class="cc-prov-src">Fetched</span>'
            f"{stamp_html(fetched_at, css='cc-prov-age')}</div>"
        )
    if raw_value is not None:
        rows.append(
            '<div class="cc-prov-row"><span class="cc-prov-src">Value</span>'
            f'<span class="cc-prov-when mono">{escape(json.dumps(raw_value, default=str))}</span></div>'
        )
    caption = (
        '<div class="cc-peek-attest-done">Vendor field &mdash; no underlying filing; '
        "this is the value FMP's API returned.</div>"
    )
    return f'<div class="cc-prov"><div class="cc-prov-rows">{"".join(rows)}</div>{caption}</div>'


def _render_legacy_provenance_peek(
    doc: _SourceDocRow | None, row: _FactRow, locator: FactLocator | None
) -> str:
    """Never a dead end (section 2.7): whichever of the document identity,
    the /source/<doc_id> link, and the raw locator JSON exist, best-effort,
    plus a `provenance: legacy` badge (composes the existing .k-chip
    primitive -- no new freehand pill)."""
    bits: list[str] = [
        f'<div class="cc-prov-row"><b>{escape(row.label)}</b> &middot; {escape(row.ticker)}</div>'
    ]
    if doc is not None:
        bits.append(f'<div class="cc-prov-row">{escape(doc.doc_type)} &middot; doc #{doc.id}</div>')
        bits.append(
            f'<div class="cc-prov-row"><a href="/source/{doc.id}" target="_blank" '
            'rel="noopener">open in viewer &#8599;</a></div>'
        )
    if row.confidence is not None:
        bits.append(f'<div class="cc-prov-row">confidence {round(row.confidence * 100)}%</div>')
    if row.extracted_by:
        bits.append(f'<div class="cc-prov-row mono">via {escape(row.extracted_by)}</div>')
    if locator is not None:
        raw = locator.to_json()
        if raw:
            bits.append(f'<div class="cc-prov-row mono src-pop-locator">{escape(raw)}</div>')
    badge = '<span class="k-chip k-chip-warn">provenance: legacy</span>'
    return f'<div class="cc-prov"><div class="cc-prov-rows">{"".join(bits)}</div>{badge}</div>'


# ----------------------------------------------------------------------------
# Discovery compare peek (PRD §8.2, P1-B) — up to 3 candidates side by side
# ----------------------------------------------------------------------------

# TickerMetrics field -> row label. Kept as a plain tuple (not the dataclass
# itself) so the row order is the display order, independent of field order.
_COMPARE_METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("sector", "Sector"),
    ("industry", "Industry"),
    ("market_cap", "Market cap"),
    ("rev_yoy", "Revenue YoY"),
    ("roic_ttm", "ROIC (TTM)"),
    ("fcf_yield_ttm", "FCF yield (TTM, proxy — no P/E cached)"),
    ("nd_to_ebitda_ttm", "ND / EBITDA (TTM)"),
    ("gross_margin_ttm", "Gross margin (TTM)"),
    ("op_margin_ttm", "Op margin (TTM)"),
)

# need_rank blob key -> row label (mirrors discovery.need_rank.NeedRank).
_COMPARE_NEED_RANK_ROWS: tuple[tuple[str, str], ...] = (
    ("eval_adjacency", "Evaluation-list adjacency"),
    ("diversifier", "Diversifier (coarse, preliminary)"),
    ("garp", "GARP"),
    ("signal", "Source signal strength"),
    ("effort", "Estimated effort"),
    ("first_rejection_reason", "First rejection risk"),
    ("composite", "Composite"),
)

_COMPARE_PCT_FIELDS: frozenset[str] = frozenset(
    {"rev_yoy", "roic_ttm", "fcf_yield_ttm", "gross_margin_ttm", "op_margin_ttm"}
)


def _compare_metric_cell(metrics: object, key: str) -> str:
    v = getattr(metrics, key, None)
    if v is None:
        return "&mdash;"
    if key == "market_cap":
        return escape(f"${float(cast('float', v)) / 1e9:.1f}B")
    if key in _COMPARE_PCT_FIELDS:
        return escape(fmt_pct(float(cast("float", v))))
    if key == "nd_to_ebitda_ttm":
        return escape(f"{float(cast('float', v)):.1f}x")
    return escape(str(v))


def _compare_rank_cell(rank: dict[str, object] | None, key: str) -> str:
    if rank is None:
        return "&mdash;"
    v = rank.get(key)
    if v is None:
        return "clean" if key == "first_rejection_reason" else "&mdash;"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}"
    return escape(str(v))


def render_discovery_compare_peek(repo_root: Path, db_path: Path, tickers: list[str]) -> str:
    """Side-by-side deterministic table for up to 3 Discovery candidates
    (PRD §8.2's Compare action): the cached ``TickerMetrics`` bundle plus the
    ``need_rank`` breakdown when the ticker carries a ``discovery_candidates``
    row (any status, including dismissed — Compare is a lookup, not a queue
    filter). Always renders (degrades per missing metric/candidate); the
    route's ``safe_ticker`` + 1..3-count validation is what 404s, this
    renderer never does."""
    from discovery.screens import load_ticker_metrics
    from discovery.store import get_candidate_by_ticker

    fmp_dir = repo_root / "data" / "historical" / "fmp"
    cols = [t.strip().upper() for t in tickers if t.strip()][:3]
    metrics_by_t = {t: load_ticker_metrics(fmp_dir, t, None) for t in cols}
    ranks_by_t: dict[str, dict[str, object] | None] = {}
    for t in cols:
        cand = get_candidate_by_ticker(t, db_path=db_path)
        rank: dict[str, object] | None = None
        if cand is not None and isinstance(cand.score_json, dict):
            raw = cand.score_json.get("need_rank")
            if isinstance(raw, dict):
                rank = cast("dict[str, object]", raw)
        ranks_by_t[t] = rank

    if not cols:
        return '<p class="cc-score-legend">No tickers to compare.</p>'

    head_cells = "".join(f"<th>{ticker_label(t, metrics_by_t[t].name)}</th>" for t in cols)
    body_rows: list[str] = []
    for key, label in _COMPARE_METRIC_ROWS:
        cells = "".join(f"<td>{_compare_metric_cell(metrics_by_t[t], key)}</td>" for t in cols)
        body_rows.append(f"<tr><td>{escape(label)}</td>{cells}</tr>")
    body_rows.append(f'<tr><td colspan="{len(cols) + 1}"><b>Portfolio-need ranking</b></td></tr>')
    for key, label in _COMPARE_NEED_RANK_ROWS:
        cells = "".join(f"<td>{_compare_rank_cell(ranks_by_t[t], key)}</td>" for t in cols)
        body_rows.append(f"<tr><td>{escape(label)}</td>{cells}</tr>")

    table = (
        f'<table class="p-table"><thead><tr><th></th>{head_cells}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )
    foot = (
        '<p class="cc-score-legend">FCF yield stands in for a P/E/PEG multiple '
        "(not cached); the diversifier leg is a coarse, preliminary reuse of the "
        "candidate-fit machinery, never full evaluation-grade precision.</p>"
    )
    # cc-score-legend is defined in _SCORE_CSS; include it so the Compare peek
    # renders correctly even when opened without a prior Score/Fit peek on the
    # page (repeated <style> blocks are harmless — same rules, later wins).
    return f"{table}{foot}<style>{_SCORE_CSS}</style>"


# --------------------------------------------------------------------------- #
# Earnings-prep peek (Wave 2, surface_density_jit_redesign.md D2 + walkthrough
# #4): the one-page prep memo, assembled ON DEMAND when the strip's "prep"
# chip is clicked — never a scheduled artifact. Deterministic composition of
# what the platform already knows about the name; the governed-LLM leg is the
# Ask doorway in the footer (click → the ask engine synthesizes the narrative
# on the same grounding).
# --------------------------------------------------------------------------- #


def render_earnings_prep_peek(
    db_path: Path,
    repo_root: Path,
    ticker: str,
    *,
    artifact_id: int | None = None,
) -> str | None:
    """The on-demand earnings-prep memo for one upcoming name.

    Sections, each best-effort (a missing table drops its block, never the
    memo): next-ER header with thesis status · the owner's open watch items /
    questions (ask doorways, including alert-derived next-call prompts) · the
    valuation stance (latest consolidated DCF). ``None`` (→ the route
    404s) only when the ticker isn't a tracked, non-archived name."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    try:
        try:
            tracked = conn.execute(
                "SELECT list_type FROM tracked_companies WHERE ticker = ? AND archived_at IS NULL",
                (t,),
            ).fetchone()
        except sqlite3.Error:
            tracked = None
        if tracked is None:
            return None
        header = _prep_header(conn, repo_root, t)
        valuation = _prep_valuation(conn, t)
        try:
            from expected_earnings import upcoming_by_ticker

            next_er = upcoming_by_ticker(conn, datetime.now(UTC).date()).get(t)
        except Exception:
            next_er = None
    finally:
        conn.close()

    brief = _prep_brief_block(
        db_path,
        t,
        next_er.isoformat() if next_er else None,
        artifact_id=artifact_id,
    )
    if brief is None:
        return None
    events = _prep_events_block(db_path, t)
    watch = _prep_watch_items(db_path, t)

    ask_q = (
        f"Prep me for {t}'s upcoming earnings call — synthesize what to listen for "
        "from my open questions, watch items, prior-quarter tone, and the thesis."
    )
    foot = (
        '<div class="cc-peek-foot">'
        f'<button type="button" class="k-chip k-chip-btn" data-ask-q="{escape(ask_q, quote=True)}">'
        "ask for the narrative →</button>"
        f'<a href="/#holding={escape(t, quote=True)}">open the holding →</a></div>'
    )
    return (
        f'<div class="cc-prep">{header}{brief}{events}{watch}{valuation}</div>{foot}'
        f"<style>{_PREP_CSS}</style>"
    )


def _prep_brief_block(
    db_path: Path,
    t: str,
    er_iso: str | None,
    *,
    artifact_id: int | None = None,
) -> str | None:
    """The pre-generated pre-earnings brief (owner ruling 2026-07-31), served
    instantly when one exists for THIS upcoming ER date — keyed exactly the
    way the stage-1c generator persisted it, so a brief for a past quarter
    can never masquerade as current. Absent (no calendar date, no artifact,
    pre-0260 DB) the peek simply keeps its deterministic assembly."""
    if artifact_id is not None:
        try:
            from earnings_brief import PURPOSE as _BRIEF_PURPOSE
            from llm_artifact_store import read_artifact

            art = read_artifact(artifact_id, db_path=db_path)
        except Exception:
            return None
        if (
            art is None
            or art.ticker != t
            or art.scope != "ticker"
            or art.purpose != _BRIEF_PURPOSE
            or art.superseded_by_id is not None
            or not (art.content_md or "").strip()
        ):
            return None
        exact_period = str(art.fiscal_period or "")
        stamp = art.generated_at.date().isoformat()
        return (
            '<div class="prep-sec"><h4>Pre-earnings brief</h4>'
            f'<div class="synthesis-body">{render_prose((art.content_md or "")[:20000])}</div>'
            f'<p class="muted">{escape(f"generated {stamp} · for ER {exact_period}")}</p></div>'
        )
    if not er_iso:
        return ""
    try:
        from earnings_brief import PURPOSE as _BRIEF_PURPOSE
        from llm_artifact_store import read_current

        art = read_current(ticker=t, purpose=_BRIEF_PURPOSE, fiscal_period=er_iso, db_path=db_path)
    except Exception:
        return ""
    if art is None or not (art.content_md or "").strip():
        return ""
    stamp = ""
    try:
        stamp = art.generated_at.date().isoformat()
    except Exception:
        stamp = ""
    receipt_bits = [b for b in (f"generated {stamp}" if stamp else "", f"for ER {er_iso}") if b]
    return (
        '<div class="prep-sec"><h4>Pre-earnings brief</h4>'
        f'<div class="synthesis-body">{render_prose((art.content_md or "")[:20000])}</div>'
        f'<p class="muted">{escape(" · ".join(receipt_bits))}</p></div>'
    )


def _prep_header(conn: sqlite3.Connection, repo_root: Path, t: str) -> str:
    when: str | None = None
    try:
        from expected_earnings import upcoming_by_ticker

        cal = upcoming_by_ticker(conn, datetime.now(UTC).date())
        d = cal.get(t)
        when = d.isoformat() if d is not None else None
    except Exception:
        when = None
    if when is None:
        try:
            when = next_earnings(repo_root, t, datetime.now(UTC))
        except Exception:
            when = None
    status = ""
    try:
        row = conn.execute(
            "SELECT breach_status FROM thesis_state WHERE ticker = ?", (t,)
        ).fetchone()
        if row is not None and row[0]:
            pill_tone = pill_tone_class(thesis_status_tone(str(row[0])))
            status = f' <span class="k-pill{pill_tone}">{escape(str(row[0]))}</span>'
    except sqlite3.Error:
        status = ""
    when_txt = escape(when) if when else "date not on the calendar yet"
    return f'<div class="prep-head"><span class="prep-when">reports {when_txt}</span>{status}</div>'


class _NewsEventRow(NamedTuple):
    published: str  # YYYY-MM-DD
    headline: str
    url: str
    why: str


def _news_events_since(db_path: Path, t: str, *, limit: int = 12) -> list[_NewsEventRow]:
    """Material events noted for ``t`` since its last reported ER (fallback:
    120 days), newest first, one row per real-world event (latest per
    event_key — the write path already suppresses most duplicates; this
    read-side pass keeps the list one-per-event even across guard windows).
    Empty on a pre-0262 DB or any sqlite error — the section simply drops.
    """
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        since: str | None = None
        try:
            from expected_earnings import last_reported_by_ticker

            last = last_reported_by_ticker(conn, datetime.now(UTC).date()).get(t)
            since = last.isoformat() if last else None
        except Exception:
            since = None
        if since is None:
            since = (datetime.now(UTC) - timedelta(days=120)).date().isoformat()
        try:
            rows = conn.execute(
                "SELECT published_at, headline, url, why_material, event_key "
                "FROM news_events WHERE ticker = ? AND published_at >= ? "
                "ORDER BY published_at DESC",
                (t, since),
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    out: list[_NewsEventRow] = []
    seen_keys: set[str] = set()
    for published_at, headline, url, why, event_key in rows:
        key = str(event_key or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        out.append(
            _NewsEventRow(
                published=str(published_at or "")[:10],
                headline=str(headline or ""),
                url=str(url or ""),
                why=str(why or ""),
            )
        )
        if len(out) >= limit:
            break
    return out


def _news_events_list_html(events: list[_NewsEventRow]) -> str:
    """The shared one-line-per-event list body (prep section + events peek)."""
    items: list[str] = []
    for e in events:
        head = (
            f'<a href="{escape(e.url, quote=True)}" target="_blank" rel="noopener">'
            f"{escape(e.headline)}</a>"
            if e.url.startswith(("http://", "https://"))
            else escape(e.headline)
        )
        why = f' <span class="muted">— {escape(e.why)}</span>' if e.why else ""
        items.append(f'<li><span class="muted">{escape(e.published)}</span> · {head}{why}</li>')
    return f"<ul>{''.join(items)}</ul>"


def _prep_events_block(db_path: Path, t: str) -> str:
    """ "Since last call" — the material primary events noted between the last
    reported ER and now (the news_events store; owner ruling 2026-07-31: news
    never alerts, the catch-up happens HERE). Renders nothing when the store
    is empty for the window — a quiet quarter is one line less, not a stub."""
    events = _news_events_since(db_path, t)
    if not events:
        return ""
    return (
        '<div class="prep-sec"><h4>Since last call — material events</h4>'
        f"{_news_events_list_html(events)}</div>"
    )


def render_news_events_peek(db_path: Path, ticker: str) -> str:
    """The ticker peek's events doorway: the same since-last-call list as the
    prep memo, standalone. Always renders — an empty window is a valid answer."""
    t = (ticker or "").strip().upper()
    if not t:
        return '<div class="cc-empty">No ticker.</div>'
    events = _news_events_since(db_path, t)
    if not events:
        return '<div class="cc-empty">No material events noted since the last call.</div>'
    foot = (
        f'<div class="cc-peek-foot"><a href="/#holding={escape(t, quote=True)}">'
        "open the holding →</a></div>"
    )
    return (
        f'<div class="cc-prep"><div class="prep-sec">'
        f"<h4>{escape(t)} — material events since last call</h4>"
        f"{_news_events_list_html(events)}</div></div>{foot}"
        f"<style>{_PREP_CSS}</style>"
    )


def _prep_watch_items(db_path: Path, t: str, *, heading: str = "What you said to watch") -> str:
    try:
        from user_state.notes import list_notes

        notes = list_notes(ticker=t, status="open", db_path=db_path)
    except Exception:
        return ""
    kind_rank = {"watch": 0, "question": 1}
    notes = [note for note in notes if note.kind in kind_rank]
    notes = sorted(notes, key=lambda n: kind_rank[n.kind])[:6]
    if not notes:
        return (
            f'<div class="prep-sec"><h4>{escape(heading)}</h4>'
            '<p class="muted">No open watch items or questions on this name — capture one '
            "and it becomes this list.</p></div>"
        )
    items = "".join(
        '<li><button type="button" class="prep-ask" '
        f'data-ask-q="{escape(f"{n.body.strip()} ({t})", quote=True)}" '
        f'title="open in Ask"><span class="k-chip">{escape(n.kind)}</span> '
        f"{escape(n.body.strip())}</button></li>"
        for n in notes
    )
    return f'<div class="prep-sec"><h4>{escape(heading)}</h4><ul>{items}</ul></div>'


def _prep_valuation(conn: sqlite3.Connection, t: str) -> str:
    try:
        row = conn.execute(
            "SELECT live_price, npv_per_share, over_under_pct, "
            "COALESCE(sanity_flag, '') FROM dcf_runs "
            "WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '') "
            "ORDER BY valuation_date DESC, rowid DESC LIMIT 1",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    live, fair, ou, sanity = row
    bits: list[str] = []
    if live is not None:
        bits.append(f"live ${float(live):,.0f}")
    if fair is not None:
        bits.append(f"fair ${float(fair):,.0f}")
    if ou is not None:
        bits.append(f"{float(ou) * 100.0:+.0f}% vs fair")
    if not bits:
        return ""
    pill = (
        ' <span class="k-pill k-pill-warn" title="model failed the DCF trust gate; '
        'read the gap as unreviewed">unreviewed model</span>'
        if sanity
        else ""
    )
    return (
        '<div class="prep-sec"><h4>Valuation stance</h4>'
        f"<p>{escape(' · '.join(bits))}{pill}</p></div>"
    )


_PREP_CSS = RESEARCH_PANEL_STYLE.removeprefix("<style>").removesuffix("</style>")


# ----------------------------------------------------------------------------
# Post-earnings readout peek — the diet page's post-ER doorway (2026-07-30)
# ----------------------------------------------------------------------------


def render_earnings_readout_peek(
    db_path: Path,
    repo_root: Path,
    ticker: str,
    *,
    artifact_id: int | None = None,
) -> str | None:
    """The persisted POST-earnings readout plus its deterministic source template.

    Portfolio artifacts pre-generate in the morning pipeline. Evaluation-name
    artifacts are generated only by the explicit action rendered here. Until
    an artifact exists, this remains a zero-token deterministic template
    grounded in what the platform recorded about the just-reported quarter:

    header (last reported date + thesis breach pill) · beat/miss vs street
    (latest quarter + the 8-quarter base rate) · tier-1 KPI moves latest-vs-
    prior · the call-tone shift summary (``earnings_tone`` alert, when one
    fired) · the transcript doorway (``/source/<doc_id>``) · your open watch
    items ("did they answer it?") · the valuation stance — then the dedicated persisted-generation
    doorway. Every block is best-effort (a missing table
    drops its block, never the memo); ``None`` (→ the route 404s) only when
    the ticker isn't a tracked, non-archived name."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    try:
        try:
            tracked = conn.execute(
                "SELECT list_type FROM tracked_companies WHERE ticker = ? AND archived_at IS NULL",
                (t,),
            ).fetchone()
        except sqlite3.Error:
            tracked = None
        if tracked is None:
            return None
        list_type = str(tracked[0])
        if artifact_id is not None:
            from earnings_readout import PURPOSE as _READOUT_PURPOSE
            from llm_artifact_store import read_artifact

            exact_artifact = read_artifact(artifact_id, db_path=db_path)
            if (
                exact_artifact is None
                or exact_artifact.ticker != t
                or exact_artifact.scope != "ticker"
                or exact_artifact.purpose != _READOUT_PURPOSE
                or exact_artifact.superseded_by_id is not None
                or not (exact_artifact.content_md or "").strip()
            ):
                return None
            persisted = (
                '<div class="prep-sec"><h4>Persisted post-earnings readout</h4>'
                f'<div class="synthesis-body">{render_prose((exact_artifact.content_md or "")[:30000])}</div>'
                f'<p class="muted">quarter ended {escape(str(exact_artifact.fiscal_period or ""))} · '
                f"artifact #{exact_artifact.id}</p></div>"
            )
            foot = (
                '<div class="cc-peek-foot">'
                f'<a href="/#holding={escape(t, quote=True)}">open the holding →</a></div>'
            )
            return (
                f'<div class="cc-prep"><div class="prep-head"><span class="prep-when">'
                f"{escape(t)} · quarter-stable artifact</span></div>{persisted}</div>{foot}"
                f"<style>{_PREP_CSS}</style>"
            )
        header = _readout_header(conn, t)
        surprise = _readout_surprise(conn, t)
        kpis = _readout_kpi_moves(conn, t)
        transcript = _readout_transcript_link(conn, t)
        valuation = _prep_valuation(conn, t)
    finally:
        conn.close()

    tone = _readout_tone(db_path, t)
    watch = _prep_watch_items(db_path, t, heading="What you said to watch — did they answer it?")

    persisted = ""
    quarter = None
    try:
        from earnings_readout import PURPOSE as _READOUT_PURPOSE
        from earnings_readout import latest_reported_quarter
        from llm_artifact_store import read_current

        quarter = latest_reported_quarter(db_path, t)
        art = (
            read_current(
                ticker=t,
                purpose=_READOUT_PURPOSE,
                fiscal_period=quarter.period_end,
                db_path=db_path,
            )
            if quarter is not None
            else None
        )
    except Exception:
        art = None
    if art is not None and (art.content_md or "").strip():
        persisted = (
            '<div class="prep-sec"><h4>Persisted post-earnings readout</h4>'
            f'<div class="synthesis-body">{render_prose((art.content_md or "")[:30000])}</div>'
            f'<p class="muted">quarter ended {escape(str(art.fiscal_period or ""))} · '
            f"artifact #{art.id}</p></div>"
        )

    action = ""
    if not persisted and quarter is not None:
        note = (
            "Evaluation names generate only when you request them."
            if list_type == "evaluation"
            else "Portfolio names also generate automatically after the morning trigger pass."
        )
        action = (
            f'<button type="button" class="k-btn k-btn-primary k-btn-sm" '
            f'data-generate-readout="{escape(t, quote=True)}">generate persisted readout</button>'
            f'<span class="muted">{escape(note)}</span>'
        )
    elif quarter is None:
        action = '<span class="muted">Cannot persist yet: no selected quarterly transcript.</span>'
    foot = (
        '<div class="cc-peek-foot">'
        f"{action}"
        f'<a href="/#holding={escape(t, quote=True)}">open the holding →</a></div>'
    )
    return (
        f'<div class="cc-prep">{header}{persisted}{surprise}{kpis}{tone}{transcript}{watch}'
        f"{valuation}</div>{foot}<style>{_PREP_CSS}</style>"
    )


def _readout_header(conn: sqlite3.Connection, t: str) -> str:
    """ "reported <date> (Nd ago)" + the thesis breach pill. The date prefers an
    actual report event (``earnings_surprises.release_date``), falling back to
    the earnings calendar's kept-as-history past rows."""
    when: str | None = None
    try:
        row = conn.execute(
            "SELECT MAX(release_date) FROM earnings_surprises WHERE ticker = ?", (t,)
        ).fetchone()
        if row is not None and row[0]:
            when = str(row[0])[:10]
    except sqlite3.Error:
        when = None
    if when is None:
        try:
            from expected_earnings import last_reported_by_ticker

            d = last_reported_by_ticker(conn, datetime.now(UTC).date()).get(t)
            when = d.isoformat() if d is not None else None
        except Exception:
            when = None
    status = ""
    try:
        row = conn.execute(
            "SELECT breach_status FROM thesis_state WHERE ticker = ?", (t,)
        ).fetchone()
        if row is not None and row[0]:
            pill_tone = pill_tone_class(thesis_status_tone(str(row[0])))
            status = f' <span class="k-pill{pill_tone}">{escape(str(row[0]))}</span>'
    except sqlite3.Error:
        status = ""
    if when:
        try:
            days = (datetime.now(UTC).date() - date.fromisoformat(when)).days
            when_txt = f"reported {escape(when)} ({days}d ago)"
        except ValueError:
            when_txt = f"reported {escape(when)}"
    else:
        when_txt = "no reported quarter on record yet"
    return f'<div class="prep-head"><span class="prep-when">{when_txt}</span>{status}</div>'


def _readout_surprise(conn: sqlite3.Connection, t: str) -> str:
    """Latest quarter's beat/miss vs street plus the 8-quarter base rate, from
    ``earnings_surprises`` — absent entirely when the ingest hasn't landed."""
    try:
        row = conn.execute(
            "SELECT eps_estimate, eps_actual, eps_surprise_pct, "
            "revenue_estimate, revenue_actual, revenue_surprise_pct "
            "FROM earnings_surprises WHERE ticker = ? "
            "ORDER BY release_date DESC LIMIT 1",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""

    def _side(label: str, est: object, act: object, pct: object) -> str:
        if act is None or pct is None:
            return ""
        try:
            p = float(str(pct))
        except ValueError:
            return ""
        tone = "k-num-pos" if p >= 0 else "k-num-neg"
        verb = "beat" if p >= 0 else "miss"
        vs = ""
        if est is not None:
            vs = f" vs {escape(str(est))} est"
        return (
            f"{label} {escape(str(act))}{vs} "
            f'<span class="{tone}">({verb} {escape(fmt_pct(p, signed=True))})</span>'
        )

    bits = [
        b
        for b in (
            _side("EPS", row[0], row[1], row[2]),
            _side("Revenue", row[3], row[4], row[5]),
        )
        if b
    ]
    if not bits:
        return ""
    rate = ""
    try:
        from compute.earnings_surprise import surprise_scorecard_for

        conn.row_factory = sqlite3.Row
        try:
            sc = surprise_scorecard_for(conn, t, lookback_quarters=8)
        finally:
            conn.row_factory = None
        if sc.total_quarters and sc.eps.beat_rate_pct is not None:
            rate = (
                f'<p class="muted">EPS beat rate {sc.eps.beat_rate_pct}% '
                f"over the last {sc.total_quarters} quarter(s)</p>"
            )
    except Exception:
        rate = ""
    return f'<div class="prep-sec"><h4>Vs street</h4><p>{" · ".join(bits)}</p>{rate}</div>'


def _readout_kpi_moves(conn: sqlite3.Connection, t: str) -> str:
    """Tier-1 KPI moves, latest vs prior fact — the same deltas the cockpit
    chips render (one reader, so the peek can't disagree with the cockpit)."""
    try:
        from pipeline.research_cockpit import (
            _tier1_kpi_deltas,  # pyright: ignore[reportPrivateUsage]
        )

        deltas = _tier1_kpi_deltas(conn, {t}, as_of=datetime.now(UTC).date()).get(t, [])
    except Exception:
        return ""
    if not deltas:
        return ""
    deltas = sorted(deltas, key=lambda d: d.magnitude, reverse=True)[:8]
    rows: list[str] = []
    for d in deltas:
        tone_cls = {"ok": "k-num-pos", "bad": "k-num-neg", "warn": "k-num-neg"}.get(d.tone, "")
        why = f' title="{escape(d.tone_why, quote=True)}"' if d.tone_why else ""
        delta_html = (
            f'<span class="{tone_cls}"{why}>{escape(d.delta_display)}</span>'
            if tone_cls
            else f"<span{why}>{escape(d.delta_display)}</span>"
        )
        rows.append(
            f"<li>{escape(d.name)}: {d.latest_value:g} {escape(d.unit)} "
            f"({delta_html} vs {d.prior_value:g}, {escape(d.prior_period[:10])})</li>"
        )
    return (
        '<div class="prep-sec"><h4>Tier-1 KPI moves, latest vs prior</h4>'
        f"<ul>{''.join(rows)}</ul></div>"
    )


def _readout_tone(db_path: Path, t: str) -> str:
    """The latest ``earnings_tone`` alert's LLM diff summary + shifts — the
    call-tone read the trigger already produced (never recomputed here)."""
    try:
        alerts = list_alerts(ticker=t, limit=50, db_path=db_path)
    except sqlite3.Error:
        return ""
    tone = next((a for a in alerts if a.trigger_kind == "earnings_tone"), None)
    if tone is None or not tone.evidence_json:
        return ""
    try:
        parsed = json.loads(tone.evidence_json)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    ev = cast("dict[str, object]", parsed)
    summary = str(ev.get("summary") or "").strip()
    if not summary:
        return ""
    shifts = ev.get("shifts")
    shift_items = ""
    if isinstance(shifts, list) and shifts:
        shift_list = cast("list[object]", shifts)
        shift_items = (
            "<ul>"
            + "".join(f"<li>{escape(str(s))}</li>" for s in shift_list[:4] if str(s).strip())
            + "</ul>"
        )
    stamp = escape(str(tone.fired_at)[:10])
    return (
        '<div class="prep-sec"><h4>Call tone vs prior quarters</h4>'
        f'<p>{escape(summary)} <span class="muted">({stamp})</span></p>{shift_items}</div>'
    )


def _readout_transcript_link(conn: sqlite3.Connection, t: str) -> str:
    """The latest selected transcript as a ``/source/<doc_id>`` doorway with
    its period label — the primary source every readout claim traces to."""
    try:
        from provenance.selection import selected_transcripts_relation

        rel = selected_transcripts_relation(conn)
        row = conn.execute(
            f"SELECT document_id, fiscal_period_type, period_end FROM {rel.sql} "  # nosec B608 -- trusted internal SQL shape; values remain bound
            + "WHERE UPPER(ticker) = ? AND period_end IS NOT NULL "
            + "ORDER BY period_end DESC LIMIT 1",
            (t,),
        ).fetchone()
    except Exception:
        return ""
    if row is None or row[0] is None:
        return ""
    doc_id, fpt, period_end = int(row[0]), str(row[1] or "").strip(), str(row[2] or "")
    label = f"{fpt} {period_end[:4]}".strip() or "latest call"
    return (
        '<div class="prep-sec"><h4>Primary source</h4>'
        f'<p><a href="/source/{doc_id}">{escape(label)} earnings-call transcript →</a></p></div>'
    )
