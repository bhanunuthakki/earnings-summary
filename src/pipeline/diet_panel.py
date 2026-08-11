"""Information-diet panel for the command-center shell — the PULL lane.

The inverse product of the inbox: where the inbox is a decaying PUSH lane
("what needs your action — your thesis may be breaking"), this is a
non-decaying PULL lane ("what a diligent analyst should ingest"). It reads the
typed `signals` substrate (alembic 0095) directly and renders two lenses over
it:

  * the INGEST STREAM — recent sell-side rating changes (``consensus_rating``,
    routed free from the yf_grades feed), EDGAR filings, and curated podcast
    appearances, newest first. NON-decaying: a story does not lose its place
    because the clock moved (design_language "Diet-vs-alert"). General news
    headlines are DELIBERATELY absent (owner ruling 2026-07-30: "surface level
    poor quality work") — ``general_news`` rows render ONLY when EDGAR-fed
    (the filings block); scraped headline aggregation never reaches this
    surface. The substrate keeps ingesting news (the material_news alert
    pipeline still reads it); this is a presentation-lane removal, not a feed
    removal.
  * the FORWARD AGENDA — upcoming investor/analyst days (``investor_day``,
    forward-dated ``event_date`` rows), soonest first.

A `signals` row is a DIET row: it is NEVER converted into an InboxItem, so it
never enters the urgency-decay scorer or the materiality veto. This panel is the
only reader of the diet lane.

Buy-side ratings + estimate revisions are DISCLOSED fast-follows (no free data
path — FMP analyst is Ultimate-gated): the panel names them as scaffolded, it
does not pretend to have the data. Every signal renders through the S1 control
kit (`.p-table`/`.k-pill`/`.k-chip`/`ticker_label`) — no raw hex, guard-clean.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from calendar_clock import calendar_today
from expected_earnings import last_reported_by_ticker, upcoming_by_ticker
from signals.store import (
    SIGNAL_CONSENSUS_RATING,
    SIGNAL_GENERAL_NEWS,
    SIGNAL_MEDIA_APPEARANCE,
    SignalRow,
    load_diet_signals,
    load_forward_agenda_result,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui import living_grid as lg
from ui.controls import ticker_label

# Names on the book lead the reading lane (owner feedback 2026-07-14: "no
# priority for portfolio and eval list"). portfolio first, then evaluation,
# then everything else — a stored-field salience tier applied at the panel, so
# the diet reader stays a pure non-decaying recency sort (the guard invariant).
_BOOK_PRIORITY: dict[str, int] = {"portfolio": 0, "evaluation": 1}
_BOOK_MARKER: dict[str, tuple[str, str]] = {
    "portfolio": ("core", "Held"),
    "evaluation": ("eval", "On the evaluation list"),
}


def _load_list_types(db_path: Path) -> dict[str, str]:
    """``ticker -> list_type`` for active tracked names — used only to float the
    owner's book to the top of the reading lane and mark it. Degrades to ``{}``
    on any read error (the panel then renders in plain recency order)."""
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, list_type FROM tracked_companies WHERE archived_at IS NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(t): str(lt) for t, lt in rows if t and lt}


# The non-forward-dated reading lanes shown in the ingest stream: news-backed
# ratings (mirrored), EDGAR-fed filings (the only general_news rows that
# survive to render — see _drop_headline_news), and media appearances (written
# direct, free path). Forward-dated investor days have their own lens (the
# forward agenda).
_STREAM_TYPES = (SIGNAL_GENERAL_NEWS, SIGNAL_CONSENSUS_RATING, SIGNAL_MEDIA_APPEARANCE)


def _drop_headline_news(rows: list[SignalRow]) -> list[SignalRow]:
    """Owner ruling 2026-07-30: general-news headlines are removed from the
    diet ENTIRELY. A ``general_news`` row survives only when EDGAR-fed (it is
    a filing, rendered in the filings block); every other news row — scraped
    headline aggregation — is dropped before grouping, emptiness checks, and
    the freshness stamp, so the panel behaves as if the lane never existed."""
    return [
        r
        for r in rows
        if r.signal_type != SIGNAL_GENERAL_NEWS or (r.source_feed or "").startswith("edgar")
    ]


# Token-only scoped styles (guard-clean: every value is a token — radius via
# --radius, type via the --fs-* scale, color via palette vars / color-mix).
_PANEL_STYLE = """<style>
.diet-sec { margin-top: var(--sp-5); }
.diet-sec.first { margin-top: var(--sp-3); }
.diet-sec-h { font-size: var(--fs-title); font-weight: 600; color: var(--fg);
  margin: 0 0 var(--sp-2); }
.diet-fresh { color: var(--muted); font-size: var(--fs-caption); font-weight: 400;
  white-space: nowrap; }
.diet-when { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.diet-sig a { color: var(--fg); text-decoration: none; }
.diet-sig a:hover { color: var(--accent); text-decoration: underline; }
.diet-firm { color: var(--muted); font-size: var(--fs-caption); }
.diet-date { font-family: var(--mono); font-weight: 600; color: var(--fg);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.diet-empty { color: var(--muted); font-style: italic; padding: var(--sp-3) 0; }
/* D3 group headers inside the stream: kind + a deterministic summary. */
.diet-group-h { font-size: var(--fs-body); font-weight: 600; color: var(--fg);
  margin: var(--sp-4) 0 var(--sp-1); }
.diet-group-sum { font-weight: 400; color: var(--muted); font-size: var(--fs-caption);
  margin-left: 6px; }
.diet-scaffold { margin-top: var(--sp-4); font-size: var(--fs-caption);
  color: var(--muted); }
</style>"""

# signal_type → (display label, .k-pill tone class). Categories stay QUIET on a
# dashboard (bare .k-pill, neutral --paper fill): accent is reserved for
# interactive/selected/unread/status, not a decorative category tint. Only the
# podcasts block renders through _stream_row now, so only its type is mapped
# (an unmapped type falls back to its raw signal_type label).
_TYPE_PILL: dict[str, tuple[str, str]] = {
    SIGNAL_MEDIA_APPEARANCE: ("Podcast", ""),
}


def render_diet_panel(db_path: Path, *, today: date | None = None) -> str:
    """The Diet tab fragment: the ingest stream + the forward agenda + the
    disclosed fast-follow note. Pure read over the `signals` substrate; a
    missing or pre-0095 store renders an explicit unavailable state."""
    today = today or calendar_today()
    stream = _drop_headline_news(load_diet_signals(db_path, types=_STREAM_TYPES, limit=80))
    agenda = load_forward_agenda_result(db_path, on_or_after=today, limit=40)
    list_types = _load_list_types(db_path)
    # Stable sort (recency preserved within tier): book names float to the top.
    stream.sort(key=lambda r: _BOOK_PRIORITY.get(list_types.get(r.ticker, ""), 9))
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2 title="Pull lane — what to READ on your names, '
            "separate from the inbox's push lane (what needs action). Nothing here decays "
            "or fires an alert; a thesis breach still reaches the inbox. General-news "
            'headlines are deliberately excluded — the readouts are the reading lane.">'
            "Information diet</h2>",
            _readouts_section(db_path, list_types, today),
            _stream_section(stream, list_types),
            _agenda_section(list(agenda.rows), today, unavailable=agenda.unavailable),
            _scaffold_note(),
            "</section>",
        ]
    )


# ---------------------------------------------------------------------------
# Earnings readouts — the reading lane that replaced the news list (owner
# ruling 2026-07-30: the diet should be pre/post-ER readouts grounded in real
# data, the transcript, and the thesis — not headline aggregation). One row
# per book name (portfolio + evaluation), soonest-reporting first. Both chips
# are doorways into deterministic templates. The pre-ER artifact pre-generates
# for portfolio + owner-opted evaluation names; the post-ER artifact
# pre-generates for portfolio names only, while evaluation names require the
# explicit generate action inside the readout peek.
# ---------------------------------------------------------------------------


def _load_auto_brief_flags(db_path: Path) -> dict[str, bool]:
    """``ticker -> auto_pre_earnings_brief`` (0260) for the toggle chips.
    Degrades to ``{}`` on a pre-0260 DB — every evaluation row then renders
    the off-state toggle, which is the true state."""
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, auto_pre_earnings_brief FROM ticker_settings"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(t).upper(): bool(v) for t, v in rows if t}


def _readouts_section(db_path: Path, list_types: dict[str, str], today: date) -> str:
    head = (
        '<div class="diet-sec first"><h3 class="diet-sec-h" title="The reading lane: '
        "pre-ER prep and post-ER readouts on your book. Pre-ER briefs pre-generate in "
        "the earnings week for every held name and for evaluation names you switch on "
        "(auto-brief). Post-ER artifacts pre-generate only for held names; evaluation "
        'names generate only on explicit request.">Earnings readouts</h3>'
    )
    book = sorted(t for t, lt in list_types.items() if lt in _BOOK_PRIORITY)
    if not book:
        return (
            head + '<p class="diet-empty">No portfolio or evaluation names tracked yet.</p></div>'
        )
    upcoming: dict[str, date] = {}
    reported: dict[str, date] = {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            upcoming = upcoming_by_ticker(conn, today)
            reported = last_reported_by_ticker(conn, today)
        finally:
            conn.close()

    def _order(t: str) -> tuple[int, int, str]:
        # Soonest next ER first; names with no calendar row sink, most
        # recently reported first among them; ticker breaks ties.
        nxt = upcoming.get(t)
        if nxt is not None:
            return (0, nxt.toordinal(), t)
        last = reported.get(t)
        if last is not None:
            return (1, -last.toordinal(), t)
        return (2, 0, t)

    book.sort(key=_order)
    auto_flags = _load_auto_brief_flags(db_path)
    body = "".join(
        _readout_row(t, list_types, upcoming.get(t), reported.get(t), today, auto_flags)
        for t in book
    )
    return (
        head + '<table class="p-table"><thead><tr><th>Name</th><th>Last reported</th>'
        "<th>Next ER</th><th>Readouts</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
        f"<script>{_AUTO_BRIEF_JS}</script></div>"
    )


def _readout_row(
    t: str,
    list_types: dict[str, str],
    next_er: date | None,
    last_er: date | None,
    today: date,
    auto_flags: dict[str, bool],
) -> str:
    marker = _book_marker_html(list_types.get(t, ""))
    tq = escape(t, quote=True)
    if last_er is not None:
        ago = (today - last_er).days
        last_cell = f'{escape(last_er.isoformat())} <span class="diet-firm">({ago}d ago)</span>'
    else:
        last_cell = "—"
    if next_er is not None:
        rel = _days_until(next_er.isoformat(), today)
        next_cell = f'{escape(next_er.isoformat())} <span class="diet-firm">({rel})</span>'
    else:
        next_cell = "—"
    prep = (
        '<button type="button" class="k-chip k-chip-btn" '
        f'data-peek-url="/api/peek/earnings-prep?ticker={tq}" '
        f'data-peek-title="Earnings prep — {tq}" '
        'title="One-page pre-ER prep — serves the pre-generated brief when one exists, '
        'assembled deterministically otherwise">pre-ER prep</button>'
    )
    readout = (
        '<button type="button" class="k-chip k-chip-btn" '
        f'data-peek-url="/api/peek/earnings-readout?ticker={tq}" '
        f'data-peek-title="Post-ER readout — {tq}" '
        'title="Post-earnings readout — actuals vs what you track, the transcript, '
        'and the thesis; persisted automatically for held names and on request for evaluations">'
        "post-ER readout</button>"
    )
    # The auto-brief opt-in (0260) is an EVALUATION-name choice: held names are
    # always in the generator's scope, so their row carries no toggle.
    toggle = ""
    if list_types.get(t) == "evaluation":
        on = auto_flags.get(t, False)
        cls = "k-chip k-chip-btn diet-autobrief" + (" k-chip-ok" if on else "")
        toggle = (
            f'<button type="button" class="{cls}" data-autobrief-ticker="{tq}" '
            f'data-autobrief-on="{1 if on else 0}" '
            "title=\"Pre-generate this name's pre-ER brief in its earnings week "
            '(held names are always included)">'
            f"auto-brief {'on' if on else 'off'}</button>"
        )
    return (
        "<tr>"
        f"<td>{ticker_label(t)}{marker}</td>"
        f'<td class="diet-when">{last_cell}</td>'
        f'<td class="diet-when">{next_cell}</td>'
        f"<td>{prep} {readout}{' ' + toggle if toggle else ''}</td>"
        "</tr>"
    )


# One-click auto-brief opt-in: POSTs the existing per-ticker settings endpoint
# and reflects the new state in place (chip tone + label + data attribute) —
# the same GET-hydrated/POST-persisted shape as the bypass_budget toggle, no
# page reload, no SSE (this is a setting, not a job).
_AUTO_BRIEF_JS = """
(function () {
  if (window.__dietAutoBriefWired) return;
  window.__dietAutoBriefWired = true;
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest
      ? ev.target.closest('button[data-autobrief-ticker]') : null;
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    var next = btn.getAttribute('data-autobrief-on') !== '1';
    CCAction.busy(btn);
    fetch('/api/ticker-settings/' + encodeURIComponent(btn.getAttribute('data-autobrief-ticker')), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_pre_earnings_brief: next })
    }).then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    }).then(function () {
      btn.setAttribute('data-autobrief-on', next ? '1' : '0');
      btn.classList.toggle('k-chip-ok', next);
      // No label was stashed (busy() called without one), so release() only
      // re-enables — the receipt text below is what sticks.
      CCAction.receipt(btn, 'auto-brief ' + (next ? 'on' : 'off'));
      setTimeout(function () { CCAction.release(btn); }, 900);
    }).catch(function (err) {
      btn.textContent = 'auto-brief error — save failed'
        + (err && err.message ? (': ' + err.message) : '');
      CCAction.release(btn);
    });
  });
})();
""".strip()


# The stream is "fresh" while its newest signal is at most this old; older
# means the upstream fetch has likely stalled (wave B B3 — the feed once sat
# 13 days stale with no cue) and the header carries a .k-chip-warn instead of
# the quiet muted stamp.
_FRESH_MAX_AGE_HOURS = 48


def _freshness_line(rows: list[SignalRow]) -> str:
    """One "newest signal Nd ago" stamp for the stream header, from the rows
    already loaded (no extra query). Warn chip when the newest published_at is
    older than ``_FRESH_MAX_AGE_HOURS``; "" when nothing is parseable."""
    stamps = [r.published_at for r in rows if r.published_at]
    if not stamps:
        return ""
    try:
        newest = datetime.fromisoformat(max(stamps)[:19].replace("T", " "))
    except ValueError:
        return ""
    age = datetime.now(UTC).replace(tzinfo=None) - newest  # naive-UTC convention
    days = max(age.days, 0)
    label = f"newest signal {days}d ago"
    if age.total_seconds() > _FRESH_MAX_AGE_HOURS * 3600:
        return (
            ' <span class="k-chip k-chip-warn" title="The newest diet signal is '
            f'{days} days old — the upstream news/grades fetch may have stalled.">'
            f"{escape(label)}</span>"
        )
    return f' <span class="diet-fresh">{escape(label)}</span>'


def _stream_section(rows: list[SignalRow], list_types: dict[str, str]) -> str:
    """The ingest stream, regrouped per D3 (surface_density_jit_redesign.md,
    walkthrough #8): sell-side actions as a parsed dense table (firm / action /
    PT from→to with a deterministic per-group summary), filings as their own
    block, and podcast appearances — no general-news list (removed entirely,
    owner ruling 2026-07-30; ``rows`` arrives pre-filtered through
    :func:`_drop_headline_news`). Within every group the order stays book-first
    then newest-first (the non-decaying diet invariant is untouched — grouping
    is presentation, the reader still never decays)."""
    head = (
        '<div class="diet-sec"><h3 class="diet-sec-h" title="What happened on your '
        "names, grouped by kind — your book first within each group, newest-first. Not "
        'ranked by urgency — this is reading, not triage.">Ingest stream'
        f"{_freshness_line(rows)}</h3>"
    )
    if not rows:
        return (
            head + '<p class="diet-empty">No diet signals yet — they populate from the '
            "yfinance-grades, EDGAR, and podcast feeds.</p></div>"
        )
    ratings = [r for r in rows if r.signal_type == SIGNAL_CONSENSUS_RATING]
    filings = [r for r in rows if r.signal_type == SIGNAL_GENERAL_NEWS]
    podcasts = [r for r in rows if r.signal_type == SIGNAL_MEDIA_APPEARANCE]
    return (
        head
        + _ratings_block(ratings, list_types)
        + _filings_block(filings, list_types)
        + _podcasts_block(podcasts, list_types)
        + "</div>"
    )


# yf_grades-mirrored titles are machine-generated and structured — two fixed
# shapes: "{Firm} maintains {Rating} on {T}; PT $109 → $110" and "{Firm}
# upgrades {T} to {Rating}". Structured vendor text, so a regex parse at
# render is appropriate (the LLM-where-semantics-matter rule cuts the other
# way here: there are no semantics, only a format).
_RATING_ON_RE = re.compile(
    r"\b(?P<action>maintains|reiterates|raises|lowers)\s+"
    r"(?P<rating>[A-Za-z][A-Za-z -]{1,24}?)\s+on\b"
)
_RATING_TO_RE = re.compile(
    r"\b(?P<action>upgrades|downgrades|initiates|resumes)\s+\S+\s+to\s+"
    r"(?P<rating>[A-Za-z][A-Za-z -]{1,24}?)(?:;|,|\s+with\b|$)"
)
_PT_RE = re.compile(r"PT\s+\$(?P<a>[\d,]+(?:\.\d+)?)\s*(?:→|->)\s*\$(?P<b>[\d,]+(?:\.\d+)?)")


def _parse_rating(title: str) -> tuple[str, str, float | None, float | None]:
    """(action, rating, pt_from, pt_to) — best-effort; blanks when unparsed."""
    action, rating = "", ""
    m = _RATING_ON_RE.search(title) or _RATING_TO_RE.search(title)
    if m:
        action = m.group("action")
        rating = m.group("rating").strip()
    pt_from = pt_to = None
    pm = _PT_RE.search(title)
    if pm:
        try:
            pt_from = float(pm.group("a").replace(",", ""))
            pt_to = float(pm.group("b").replace(",", ""))
        except ValueError:
            pt_from = pt_to = None
    return action, rating, pt_from, pt_to


def _ratings_block(rows: list[SignalRow], list_types: dict[str, str]) -> str:
    if not rows:
        return ""
    parsed = [(r, *_parse_rating(r.title)) for r in rows]
    n_up = sum(1 for _, _, _, a, b in parsed if a is not None and b is not None and b > a)
    n_down = sum(1 for _, _, _, a, b in parsed if a is not None and b is not None and b < a)
    summary_bits = [f"{len(rows)} action(s) on {len({r.ticker for r in rows})} name(s)"]
    if n_up or n_down:
        summary_bits.append(f"{n_up} PT raise(s) / {n_down} cut(s)")
    body: list[str] = []
    for r, action, rating, pt_from, pt_to in parsed:
        marker = _book_marker_html(list_types.get(r.ticker, ""))
        if pt_from is not None and pt_to is not None:
            delta = (pt_to - pt_from) / pt_from * 100.0 if pt_from else 0.0
            tone = "k-num-pos" if pt_to >= pt_from else "k-num-neg"
            pt_cell = (
                f'${pt_from:,.0f} → <span class="{tone}">${pt_to:,.0f}</span> '
                f'<span class="diet-firm">({delta:+.0f}%)</span>'
            )
        else:
            pt_cell = "—"
        # The parsed read, still a doorway: linked to the story when a url
        # exists, with the full raw title on hover so nothing is lost.
        parsed_txt = (
            f"{escape(action)} <strong>{escape(rating)}</strong>" if action else escape(r.title)
        )
        action_cell = (
            f'<a href="{escape(r.url, quote=True)}" target="_blank" rel="noopener" '
            f'title="{escape(r.title)}">{parsed_txt}</a>'
            if r.url
            else f'<span title="{escape(r.title)}">{parsed_txt}</span>'
        )
        body.append(
            "<tr>"
            f'<td class="diet-when">{escape(r.published_at[:10])}</td>'
            f"<td>{ticker_label(r.ticker)}{marker}</td>"
            f'<td><span class="diet-firm">{escape(r.firm or "—")}</span></td>'
            f"<td>{action_cell}</td>"
            f'<td class="num">{pt_cell}</td>'
            "</tr>"
        )
    return (
        '<h4 class="diet-group-h">Sell-side actions '
        f'<span class="diet-group-sum">{escape(" · ".join(summary_bits))}</span></h4>'
        '<table class="p-table"><thead><tr><th>When</th><th>Name</th><th>Firm</th>'
        '<th>Action</th><th class="num">PT</th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


# Leading "KIND:" tag on an EDGAR-fed headline ("SC 13D/A: activist stake…").
_FILING_KIND_RE = re.compile(r"^([A-Z0-9][A-Z0-9 /-]{1,12}):\s*")


def _filings_block(rows: list[SignalRow], list_types: dict[str, str]) -> str:
    if not rows:
        return ""
    body: list[str] = []
    for r in rows:
        marker = _book_marker_html(list_types.get(r.ticker, ""))
        title = r.title
        kind_chip = ""
        m = _FILING_KIND_RE.match(title)
        if m:
            kind_chip = f'<span class="k-chip k-chip-mono">{escape(m.group(1))}</span> '
            title = title[m.end() :]
        link = (
            f'<a href="{escape(r.url or "", quote=True)}" target="_blank" rel="noopener">'
            f"{escape(title)}</a>"
            if r.url
            else escape(title)
        )
        body.append(
            "<tr>"
            f'<td class="diet-when">{escape(r.published_at[:10])}</td>'
            f"<td>{ticker_label(r.ticker)}{marker}</td>"
            f'<td class="diet-sig">{kind_chip}{link}</td>'
            "</tr>"
        )
    return (
        '<h4 class="diet-group-h">Filings '
        f'<span class="diet-group-sum">{len(rows)} on '
        f"{len({r.ticker for r in rows})} name(s)</span></h4>"
        '<table class="p-table"><thead><tr><th>When</th><th>Name</th><th>Filing</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _podcasts_block(rows: list[SignalRow], list_types: dict[str, str]) -> str:
    """Curated podcast appearances (``media_appearance`` — a tracked exec or
    rostered investor on an allowlisted show). Kept when the news list was
    removed: these are long-form primary conversations, not headline churn."""
    if not rows:
        return ""
    body = "".join(_stream_row(r, list_types.get(r.ticker, "")) for r in rows)
    return (
        '<h4 class="diet-group-h">Podcasts '
        f'<span class="diet-group-sum">{len(rows)} appearance(s)</span></h4>'
        + lg.grid_open()
        + lg.filter_bar(len(rows), noun="signals", placeholder="Filter by name / source / text…")
        + '<table class="p-table"><thead><tr>'
        + lg.th("When", "when", "text", num=False)
        + lg.th("Name", "name", "text", num=False)
        + lg.th("Type", "type", "text", num=False)
        + "<th>Signal</th>"
        + lg.th("Source", "source", "text", num=False)
        + f"</tr></thead><tbody>{body}</tbody></table>"
        + lg.grid_close()
    )


def _book_marker_html(list_type: str) -> str:
    marker = _BOOK_MARKER.get(list_type)
    if not marker:
        return ""
    return f' <span class="k-chip k-chip-mono" title="{escape(marker[1])}">{marker[0]}</span>'


def _linked_title(r: SignalRow) -> str:
    title = escape(r.title)
    if r.url:
        return f'<a href="{escape(r.url, quote=True)}" target="_blank" rel="noopener">{title}</a>'
    return title


def _stream_row(r: SignalRow, list_type: str = "") -> str:
    label, tone = _TYPE_PILL.get(r.signal_type, (r.signal_type, ""))
    pill_cls = f"k-pill {tone}".strip()
    type_cell = f'<span class="{pill_cls}">{escape(label)}</span>'
    title = escape(r.title)
    sig_cell = (
        f'<a href="{escape(r.url, quote=True)}" target="_blank" rel="noopener">{title}</a>'
        if r.url
        else title
    )
    firm = f'<span class="diet-firm">{escape(r.firm)}</span>' if r.firm else "—"
    marker = _BOOK_MARKER.get(list_type)
    marker_html = (
        f' <span class="k-chip k-chip-mono" title="{escape(marker[1])}">{marker[0]}</span>'
        if marker
        else ""
    )
    data = (
        lg.data_text(f"{r.ticker} {r.title} {r.firm or ''} {label} {list_type}")
        + lg.data_text_key("when", r.published_at[:10])
        + lg.data_text_key("name", r.ticker)
        + lg.data_text_key("type", label)
        + lg.data_text_key("source", r.firm or "")
    )
    return (
        f"<tr{data}>"
        f'<td class="diet-when">{escape(r.published_at[:10])}</td>'
        f"<td>{ticker_label(r.ticker)}{marker_html}</td>"
        f"<td>{type_cell}</td>"
        f'<td class="diet-sig">{sig_cell}</td>'
        f"<td>{firm}</td>"
        "</tr>"
    )


def _agenda_section(rows: list[SignalRow], today: date, *, unavailable: bool = False) -> str:
    head = (
        '<div class="diet-sec"><h3 class="diet-sec-h" title="Upcoming investor + analyst '
        'days, soonest first — extends the earnings calendar.">Forward agenda</h3>'
    )
    if unavailable:
        return (
            head + '<p class="diet-empty" role="alert" data-calendar-state="unavailable">'
            "Calendar unavailable. The event store could not be read.</p></div>"
        )
    if not rows:
        return (
            head + '<p class="diet-empty" role="status" data-calendar-state="empty">'
            "No investor days on the calendar. No upcoming events are currently stored."
            "</p></div>"
        )
    body = "".join(_agenda_row(r, today) for r in rows)
    table = (
        lg.grid_open()
        + lg.filter_bar(len(rows), noun="events", placeholder="Filter by name / event…")
        + '<table class="p-table"><thead><tr>'
        + lg.th("Date", "date", "text", num=False)
        + "<th>In</th>"
        + lg.th("Name", "name", "text", num=False)
        + "<th>Event</th>"
        + lg.th("Source", "source", "text", num=False)
        + f"</tr></thead><tbody>{body}</tbody></table>"
        + lg.grid_close()
    )
    return head + table + "</div>"


def _safe_event_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _agenda_row(r: SignalRow, today: date) -> str:
    in_days = _days_until(r.event_date, today)
    title = escape(r.title)
    event_url = _safe_event_url(r.url)
    sig_cell = (
        f'<a href="{escape(event_url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer external">{title}</a>'
        if event_url
        else f'<span class="muted">{title} · Source unavailable</span>'
    )
    firm = f'<span class="diet-firm">{escape(r.firm)}</span>' if r.firm else "—"
    data = (
        lg.data_text(f"{r.ticker} {r.title} {r.firm or ''}")
        + lg.data_text_key("date", r.event_date or "")
        + lg.data_text_key("name", r.ticker)
        + lg.data_text_key("source", r.firm or "")
    )
    return (
        f"<tr{data}>"
        f'<td class="diet-date">{escape(r.event_date or "")}</td>'
        f'<td class="diet-when">{escape(in_days)}</td>'
        f"<td>{ticker_label(r.ticker)}</td>"
        f'<td class="diet-sig">{sig_cell}</td>'
        f"<td>{firm}</td>"
        "</tr>"
    )


def _days_until(event_date: str | None, today: date) -> str:
    if not event_date:
        return "—"
    try:
        when = date.fromisoformat(event_date[:10])
    except ValueError:
        return "—"
    delta = (when - today).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"{delta}d"


def _scaffold_note() -> str:
    return (
        '<p class="diet-scaffold" title="Both need a data path this repo doesn\'t have on '
        "the free tier yet (FMP analyst estimates are Ultimate-gated) — scaffolded in the "
        'substrate, not promised here.">'
        "Coming as fast-follows: buy-side ratings (13F + ARK) and sell-side estimate revisions."
        "</p>"
    )
