"""Peek fragments (UX9) — the small HTML payloads behind the shell's quick-look.

The command-center shell's peek primitive (``command_center_shell.SHELL_JS``)
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
from datetime import UTC, datetime
from html import escape
from io import StringIO
from pathlib import Path
from typing import NamedTuple, cast

from alerts import AlertRow, get_alert, list_alerts, list_queued_actions_for_alert
from compute.metrics_engine.io import latest_ttm_value
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
from pipeline.source_viewers import (
    _STATEMENT_JSON_DOC_TYPES,  # pyright: ignore[reportPrivateUsage]
    load_document,
    render_form10k_page,
    render_pdf_page_view,
    render_statement_json_page,
    render_transcript_page,
)
from pipeline.source_viewers import _DocRow as _SourceDocRow  # pyright: ignore[reportPrivateUsage]
from report.renderers.numfmt import fmt_date, fmt_pct, fmt_reltime
from ui.controls import pill_tone_class, thesis_status_tone
from ui.prose import render_prose
from ui.time import stamp_html

__all__ = [
    "render_alert_peek",
    "render_alerts_list_peek",
    "render_derived_peek",
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


_DOCS_CSS = """
.cc-peek-docs { display: flex; flex-direction: column; }
.cc-doc-row { display: flex; align-items: baseline; gap: 10px; padding: 6px 2px;
  border-bottom: 1px solid var(--hairline); text-decoration: none; color: var(--fg);
  font-size: var(--fs-body); }
.cc-doc-row:last-child { border-bottom: none; }
.cc-doc-row:hover { background: var(--paper); }
.cc-doc-kind { flex: 0 0 auto; font-weight: 600; }
.cc-doc-name { flex: 1 1 auto; min-width: 0; color: var(--muted); font-family: var(--mono);
  font-size: var(--fs-caption); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cc-doc-when { flex: 0 0 auto; color: var(--muted); font-size: var(--fs-caption); }
""".strip()


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
    rows_html = "".join(
        f'<div class="cc-mini-row"><span>{label}</span><b>{value}</b></div>'
        for label, value in rows
    )
    return (
        f'<div class="cc-mini">{head}{name_html}{rows_html}'
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
        row = conn.execute(
            "SELECT overall_status FROM thesis_evaluations "
            "WHERE ticker = ? ORDER BY evaluated_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


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


_SCORE_CSS = """
.cc-score-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.cc-score-cap { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.05em; }
.cc-score-big { font-family: var(--mono); font-size: var(--fs-display); font-weight: 600; }
.cc-score-big.score-hi { color: var(--ok); }
.cc-score-big.score-lo { color: var(--muted); }
.cc-score-big.score-warn { color: var(--warn); }
.cc-score-rows { display: flex; flex-direction: column; margin: 10px 0 6px; }
.cc-score-row { display: grid; grid-template-columns: 104px 1fr 84px 52px; gap: 10px;
  align-items: center; padding: 6px 2px; border-bottom: 1px solid var(--hairline);
  font-size: var(--fs-body); }
.cc-score-row:last-child { border-bottom: none; }
.cc-score-label { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.05em; }
.cc-score-detail { font-variant-numeric: tabular-nums; }
.cc-score-detail.muted { color: var(--muted); }
.cc-score-bar { display: block; height: 6px; background: var(--paper);
  border-radius: var(--radius-full); overflow: hidden; }
.cc-score-fill { display: block; height: 100%; border-radius: var(--radius-full); }
.cc-score-fill.bar-pos { background: var(--ok); }
.cc-score-fill.bar-neg { background: var(--bad); }
.cc-score-fill.bar-mid { background: var(--muted); }
.cc-score-mult { font-family: var(--mono); font-weight: 600; text-align: right; }
.cc-score-mult.mult-pos { color: var(--ok); }
.cc-score-mult.mult-neg { color: var(--bad); }
.cc-score-mult.mult-mid { color: var(--muted); }
.cc-score-formula { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  margin-top: 4px; }
.cc-score-legend { color: var(--muted); font-size: var(--fs-caption); margin-top: 6px; }
.cc-fit-degraded { color: var(--warn); font-size: var(--fs-caption); margin-top: 6px; }
.cc-fit-group { color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.05em; margin-top: 10px; }
.cc-fit-strip { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  margin-top: 8px; }
.cc-wi-weights { display: flex; gap: 4px; }
.cc-wi-w-on { background: var(--accent-soft); }
.cc-wi-row { display: grid; grid-template-columns: 104px 1fr 24px 1fr; gap: 10px;
  align-items: center; padding: 6px 2px; border-bottom: 1px solid var(--hairline);
  font-size: var(--fs-body); }
.cc-wi-row:last-child { border-bottom: none; }
.cc-wi-val { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.cc-wi-arrow { color: var(--muted); text-align: center; }
.cc-wi-corrs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
""".strip()


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
    repo_root: Path, ticker: str, weight: float, *, funding_mode: str = "new_cash"
) -> str | None:
    """The book BEFORE → AFTER adding one name at a chosen weight — vol, Sharpe
    (Δ in bps), growth tilt, resulting post-trade weight + concentration zone
    (PRD §7.2, P0.2), and the candidate's top correlations to individual
    holdings, from ``allocation.what_if`` (module-cached; a cold compute is a
    user-initiated click, never the table render). The weight selector chips
    are peek doorways themselves — clicking a preset re-renders the popover at
    that weight, carrying the current ``funding_mode`` along.

    ``funding_mode`` (``"new_cash"`` | ``"pro_rata_reallocation"``) is FRAMING
    ONLY — see ``allocation.what_if``'s module docstring: it never changes the
    modeled vol/Sharpe/tilt numbers, only how the add is explained. An
    out-of-range weight, or an unknown funding mode, renders a small inline
    error naming the preset menu instead of raising. ``None`` when the
    weights cache is empty (the route 404s)."""
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
        f"{delta}{zone_html}{corr_html}{legend}</div>{foot}<style>{_SCORE_CSS}</style>"
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


_ATTEST_CSS = """
.cc-peek-attest { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--hairline);
  display: flex; align-items: center; gap: 10px; }
.cc-peek-attest-done { color: var(--fg-soft); font-size: var(--fs-caption); }
.cc-attest-msg { font-size: var(--fs-caption); color: var(--fg-soft); }
""".strip()

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
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Recording\\u2026';
    fetch('/api/coach/attest-change', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ memo_id: Number(btn.getAttribute('data-attest-memo-id')) })
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, body: j }; });
    }).then(function (r) {
      if (r.ok && r.body && r.body.attested) {
        btn.textContent = '\\u2713 Recorded';
        say('Counts toward the coach\\u2019s Q3\\u201926 bar.');
      } else {
        btn.disabled = false;
        btn.textContent = label;
        say((r.body && r.body.error) || 'Already recorded.');
      }
    }).catch(function (e) { btn.disabled = false; btn.textContent = label; say(e.message); });
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


_REVIEW_CSS = """
.cc-review-peek .synthesis-body { font-size: var(--fs-body); }
.cc-review-foot { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--hairline);
  display: flex; flex-direction: column; gap: 8px; }
.cc-review-log { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  background: var(--paper); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 8px 10px; margin: 0; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; }
""".strip()

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
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Running…';
    fetch('/actions/position-review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: btn.getAttribute('data-review-ticker') })
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, status: resp.status, body: j }; });
    }).then(function (r) {
      if (!r.ok) {
        line('! ' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
        btn.disabled = false;
        btn.textContent = label;
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
          btn.disabled = false;
          btn.textContent = label;
        }
      };
      es.onerror = function () {
        if (finished) return;
        line('! stream interrupted — is the server still running?');
        es.close();
        btn.disabled = false;
        btn.textContent = label;
      };
    }).catch(function (e) { line('! ' + e.message); btn.disabled = false; btn.textContent = label; });
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
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


_PROV_CSS = """
.cc-prov-rows { display: flex; flex-direction: column; }
.cc-prov-row { display: flex; align-items: baseline; gap: 10px; padding: 6px 2px;
  border-bottom: 1px solid var(--hairline); font-size: var(--fs-body); }
.cc-prov-row:last-child { border-bottom: none; }
.cc-prov-src { flex: 0 0 128px; color: var(--muted); font-size: var(--fs-caption);
  text-transform: uppercase; letter-spacing: 0.05em; }
.cc-prov-when { flex: 1 1 auto; min-width: 0; }
.cc-prov-age { font-weight: 600; }
.cc-prov-note { color: var(--muted); font-size: var(--fs-caption); }
/* cron marker + refresh button compose the kit (.k-chip / .k-btn-quiet.k-btn-sm);
   only the flex-child layout stays local. */
.cc-prov-cron { flex: none; }
.cc-prov-btn { flex: none; }
.cc-prov-log { font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  background: var(--paper); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 8px 10px; margin: 10px 0 0; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word; }
""".strip()

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
    function release() { btn.disabled = false; btn.textContent = btn.getAttribute('data-prov-label') || 'Refresh'; }
    btn.setAttribute('data-prov-label', btn.textContent);
    btn.disabled = true;
    btn.textContent = 'Running…';
    fetch(btn.getAttribute('data-prov-post'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: btn.getAttribute('data-prov-body') || '{}'
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, status: resp.status, body: j }; });
    }).then(function (r) {
      if (!r.ok) {
        line('! ' + ((r.body && r.body.error) || ('HTTP ' + r.status)));
        release();
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
          release();
        }
      };
      es.onerror = function () {
        if (finished) return;
        line('! stream interrupted — is the server still running?');
        es.close();
        release();
      };
    }).catch(function (e) { line('! ' + e.message); release(); });
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
