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
from ui.controls import ticker_label

# The non-forward-dated reading lanes shown in the ingest stream: news-backed
# ratings + news (mirrored) plus media appearances (written direct, free path).
# Forward-dated investor days have their own lens (the forward agenda).
_STREAM_TYPES = (SIGNAL_GENERAL_NEWS, SIGNAL_CONSENSUS_RATING, SIGNAL_MEDIA_APPEARANCE)

# Token-only scoped styles (guard-clean: every value is a token — radius via
# --radius, type via the --fs-* scale, color via palette vars / color-mix).
_PANEL_STYLE = """<style>
.diet-sec { margin-top: var(--sp-5); }
.diet-sec.first { margin-top: var(--sp-3); }
.diet-sec-h { font-size: var(--fs-section); font-weight: 600; color: var(--fg);
  margin: 0 0 var(--sp-2); }
.diet-sec-sub { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 var(--sp-3); }
.diet-when { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.diet-sig a { color: var(--fg); text-decoration: none; }
.diet-sig a:hover { color: var(--accent); text-decoration: underline; }
.diet-firm { color: var(--muted); font-size: var(--fs-caption); }
.diet-date { font-family: var(--mono); font-weight: 600; color: var(--fg);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.diet-empty { color: var(--muted); font-style: italic; padding: var(--sp-3) 0; }
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
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Information diet</h2>',
            '<p class="sub">The pull lane — what to <strong>read</strong> on your names, '
            "kept separate from the inbox's push lane (what needs your <strong>action</strong>). "
            "Sell-side ratings and news here never decay or fire an alert; a thesis breach "
            "still reaches the inbox.</p>",
            _stream_section(stream),
            _agenda_section(agenda, today),
            _scaffold_note(),
            "</section>",
        ]
    )


def _stream_section(rows: list[SignalRow]) -> str:
    head = (
        '<div class="diet-sec first"><h3 class="diet-sec-h">Ingest stream</h3>'
        '<p class="diet-sec-sub">Recent sell-side ratings + news on tracked names, '
        "newest first. Not ranked by urgency — this is reading, not triage.</p>"
    )
    if not rows:
        return (
            head + '<p class="diet-empty">No diet signals yet — they populate from the '
            "news + yfinance-grades feeds.</p></div>"
        )
    body = "".join(_stream_row(r) for r in rows)
    table = (
        '<table class="p-table"><thead><tr>'
        "<th>When</th><th>Name</th><th>Type</th><th>Signal</th><th>Source</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
    )
    return head + table + "</div>"


def _stream_row(r: SignalRow) -> str:
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
    return (
        "<tr>"
        f'<td class="diet-when">{escape(r.published_at[:10])}</td>'
        f"<td>{ticker_label(r.ticker)}</td>"
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
        '<table class="p-table"><thead><tr>'
        "<th>Date</th><th>In</th><th>Name</th><th>Event</th><th>Source</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
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
    return (
        "<tr>"
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
