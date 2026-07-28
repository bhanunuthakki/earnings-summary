"""Information-diet panel for the command-center shell — the PULL lane.

The inverse product of the inbox: where the inbox is a decaying PUSH lane
("what needs your action — your thesis may be breaking"), this is a
non-decaying PULL lane ("what a diligent analyst should ingest"). It reads the
typed `signals` substrate (alembic 0095) directly and renders two lenses over
it:

  * the INGEST STREAM — recent sell-side rating changes (``consensus_rating``,
    routed free from the yf_grades feed) and general news (``general_news``),
    newest first. NON-decaying: a story does not lose its place because the
    clock moved (design_language "Diet-vs-alert").
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

from signals.store import (
    SIGNAL_CONSENSUS_RATING,
    SIGNAL_GENERAL_NEWS,
    SIGNAL_MEDIA_APPEARANCE,
    SignalRow,
    load_diet_signals,
    load_forward_agenda,
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
# ratings + news (mirrored) plus media appearances (written direct, free path).
# Forward-dated investor days have their own lens (the forward agenda).
_STREAM_TYPES = (SIGNAL_GENERAL_NEWS, SIGNAL_CONSENSUS_RATING, SIGNAL_MEDIA_APPEARANCE)

# Token-only scoped styles (guard-clean: every value is a token — radius via
# --radius, type via the --fs-* scale, color via palette vars / color-mix).
_PANEL_STYLE = """<style>
.diet-sec { margin-top: var(--sp-5); }
.diet-sec.first { margin-top: var(--sp-3); }
.diet-sec-h { font-size: var(--fs-title); font-weight: 600; color: var(--fg);
  margin: 0 0 var(--sp-2); }
.diet-sec-sub { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 var(--sp-3); }
.diet-fresh { color: var(--muted); font-size: var(--fs-caption); white-space: nowrap; }
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
.diet-scaffold { margin-top: var(--sp-5); padding: var(--sp-3) var(--sp-4);
  background: var(--paper); border: 1px solid var(--border); border-radius: var(--radius);
  font-size: var(--fs-caption); color: var(--muted); line-height: 1.55; }
</style>"""

# signal_type → (display label, .k-pill tone class). Categories stay QUIET on a
# dashboard (bare .k-pill, neutral --paper fill): accent is reserved for
# interactive/selected/unread/status, not a decorative category tint.
_TYPE_PILL: dict[str, tuple[str, str]] = {
    SIGNAL_CONSENSUS_RATING: ("Rating", ""),
    "general_news": ("News", ""),
    "investor_day": ("Investor day", ""),
    SIGNAL_MEDIA_APPEARANCE: ("Podcast", ""),
}


def render_diet_panel(db_path: Path) -> str:
    """The Diet tab fragment: the ingest stream + the forward agenda + the
    disclosed fast-follow note. Pure read over the `signals` substrate; degrades
    to a quiet empty state on a pre-0095 DB (no `signals` table)."""
    today = datetime.now(UTC).date()
    stream = load_diet_signals(db_path, types=_STREAM_TYPES, limit=80)
    agenda = load_forward_agenda(db_path, on_or_after=today, limit=40)
    list_types = _load_list_types(db_path)
    # Stable sort (recency preserved within tier): book names float to the top.
    stream.sort(key=lambda r: _BOOK_PRIORITY.get(list_types.get(r.ticker, ""), 9))
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Information diet</h2>',
            '<p class="sub">The pull lane — what to <strong>read</strong> on your names, '
            "kept separate from the inbox's push lane (what needs your <strong>action</strong>). "
            "Sell-side ratings and news here never decay or fire an alert; a thesis breach "
            "still reaches the inbox.</p>",
            _stream_section(stream, list_types),
            _agenda_section(agenda, today),
            _scaffold_note(),
            "</section>",
        ]
    )


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
    block, and the remaining news reading list — instead of one undifferentiated
    chronological mix. Within every group the order stays book-first then
    newest-first (the non-decaying diet invariant is untouched — grouping is
    presentation, the reader still never decays)."""
    head = (
        '<div class="diet-sec first"><h3 class="diet-sec-h">Ingest stream</h3>'
        '<p class="diet-sec-sub">What happened on your names, grouped by kind — '
        "<strong>your book first</strong> within each group, newest-first. "
        "Not ranked by urgency — this is reading, not triage."
        f"{_freshness_line(rows)}</p>"
    )
    if not rows:
        return (
            head + '<p class="diet-empty">No diet signals yet — they populate from the '
            "news + yfinance-grades feeds.</p></div>"
        )
    ratings = [r for r in rows if r.signal_type == SIGNAL_CONSENSUS_RATING]
    filings = [
        r
        for r in rows
        if r.signal_type == SIGNAL_GENERAL_NEWS and (r.source_feed or "").startswith("edgar")
    ]
    grouped_ids = {id(r) for r in ratings} | {id(r) for r in filings}
    news = [r for r in rows if id(r) not in grouped_ids]
    return (
        head
        + _ratings_block(ratings, list_types)
        + _filings_block(filings, list_types)
        + _news_block(news, list_types)
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


def _news_block(rows: list[SignalRow], list_types: dict[str, str]) -> str:
    if not rows:
        return ""
    body = "".join(_stream_row(r, list_types.get(r.ticker, "")) for r in rows)
    return (
        '<h4 class="diet-group-h">News &amp; podcasts '
        f'<span class="diet-group-sum">{len(rows)} item(s)</span></h4>'
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


def _agenda_section(rows: list[SignalRow], today: date) -> str:
    head = (
        '<div class="diet-sec"><h3 class="diet-sec-h">Forward agenda</h3>'
        '<p class="diet-sec-sub">Upcoming investor + analyst days, soonest first — '
        "queryable event rows, not prose. Extends the earnings calendar.</p>"
    )
    if not rows:
        return (
            head + '<p class="diet-empty">No investor days on the calendar — these land '
            "as the IR-events feed records them.</p></div>"
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


def _agenda_row(r: SignalRow, today: date) -> str:
    in_days = _days_until(r.event_date, today)
    title = escape(r.title)
    sig_cell = (
        f'<a href="{escape(r.url, quote=True)}" target="_blank" rel="noopener">{title}</a>'
        if r.url
        else title
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
        '<div class="diet-scaffold">'
        "<strong>Coming as fast-follows:</strong> buy-side ratings (the 13F + ARK layer) "
        "and sell-side estimate / model revisions. Both need a data path this repo doesn't "
        "have on the free tier yet (FMP analyst estimates are Ultimate-gated), so they're "
        "scaffolded in the substrate but not promised here. Post-event takeaway summaries of "
        "the investor days above arrive with the summarization pass."
        "</div>"
    )
