"""Persistent inbox feed page.

Renders the full unified-inbox stream (alerts, drafts, ledger entries,
journal items, fresh synthesis sections — categorized + ranked) as ONE dense,
scannable list of uniform cards (``dashboard.inbox.render_inbox_stream``): every
kind shares one card shape, and each card is a doorway — the ticker opens that
holding in the shell, news cards link out to the source article, pending items
carry inline approve/dismiss. Deep alert detail (the evidence drawer, the
queued-action history) lives in the in-shell peek + the holding rail, not inline.

Only the filters actually narrowing the view are shown — as removable chips in
the header (the AND-composed ``status`` / ``trigger_kind`` alert-vocabulary
filters narrow the page back to the alerts-only view they're defined over). The
category chips above the stream filter client-side.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from urllib.parse import quote_plus

from dashboard._styles import CSS
from dashboard.inbox import INBOX_CSS, INBOX_JS, collect_inbox, render_inbox_stream
from identity import DEFAULT_USER_ID
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK


def render_alert_feed(
    user_id: str = DEFAULT_USER_ID,
    limit: int = 200,
    ticker: str | None = None,
    trigger_kind: str | None = None,
    status: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Return the full HTML string for the feed page.

    Filters compose with AND in the store query — the feed renders only
    what comes back. The filter strip on the rendered page mirrors the
    args so the analyst can see which slice they're looking at without
    re-checking their shell history.
    """
    # The feed is the full-page view of the unified inbox (Inbox v2): every
    # kind, categorized + ranked, with category chips. The alert-specific
    # filters (trigger_kind / status) narrow it back to the alerts-only view
    # those filters are defined over.
    alerts_only = bool(trigger_kind or status)
    items = collect_inbox(
        db_path,
        user_id=user_id,
        ticker=ticker,
        status=status,
        trigger_kind=trigger_kind,
        kinds=("alert",) if alerts_only else ("alert", "draft", "ledger", "note", "synthesis"),
        limit=limit,
    )

    body = StringIO()
    body.write('<div class="l1-shell">')
    _render_header(
        body,
        total=len(items),
        active=_active_filters(ticker=ticker, trigger_kind=trigger_kind, status=status),
    )
    # One band only: the category chips (rendered by the stream) head the list —
    # no separate "Stream / N shown" title band over them (design_language §6.1).
    body.write('<section class="dash-section dash-feed">')
    body.write(
        render_inbox_stream(
            items,
            db_path=db_path,
            surface="feed",
            show_filters=True,
            empty_text="No items match the current filters.",
        )
    )
    body.write("</section>")
    _render_footer(body)
    body.write("</div>")
    return _document(body.getvalue())


# ----------------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------------


def _render_header(
    body: StringIO, total: int, active: list[tuple[str, str, str]]
) -> None:
    body.write('<header class="l1-header">')
    body.write("<h1>Inbox feed</h1>")
    body.write(
        f'<div class="l1-subtitle">{total} shown · ranked by what matters now — '
        "newest and most material first. Filter the stream by category below, or "
        "hover a card to expand it.</div>"
    )
    # Only the filters actually narrowing the view are shown — each a removable
    # chip linking back to the feed without that one constraint. Nothing
    # filtered → no chrome band at all (no "ALL · ALL · ALL" noise).
    if active:
        body.write('<div class="dash-filters">')
        for label, value, remove_href in active:
            body.write(
                f'<a class="k-chip k-chip-btn" href="{_esc(remove_href)}" '
                'title="Remove this filter">'
                f"<span>{_esc(label)}:</span> "
                f'<span class="filter-value">{_esc(value)}</span> '
                "<span>✕</span></a>"
            )
        body.write("</div>")
    body.write("</header>")


def _active_filters(
    *, ticker: str | None, trigger_kind: str | None, status: str | None
) -> list[tuple[str, str, str]]:
    """The server-side filters currently narrowing the feed, as
    ``(label, value, remove_href)`` — each chip links back to the feed with that
    one constraint dropped (the others preserved). Empty when nothing is
    filtered, so the header renders no filter band."""
    active = [
        ("ticker", "ticker", ticker),
        ("trigger", "trigger_kind", trigger_kind),
        ("status", "status", status),
    ]
    active = [(label, param, value) for label, param, value in active if value]
    chips: list[tuple[str, str, str]] = []
    for label, param, value in active:
        others = [(p, v) for (_lbl, p, v) in active if p != param]
        qs = "&".join(f"{p}={quote_plus(str(v))}" for p, v in others if v)
        chips.append((label, str(value), "/feed" + (f"?{qs}" if qs else "")))
    return chips


def _render_footer(body: StringIO) -> None:
    generated_at = datetime.now(UTC).replace(tzinfo=None)
    body.write('<div class="l1-footer">')
    body.write("<span>Inbox feed</span>")
    body.write(stamp_html(generated_at, prefix="generated "))
    body.write("</div>")


# ----------------------------------------------------------------------------
# Document shell
# ----------------------------------------------------------------------------


def _document(body: str) -> str:
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · inbox feed</title>
{FAVICON_LINK}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap" rel="stylesheet">
<style>{CSS}
{INBOX_CSS}</style>
</head>
<body>
{body}
<script>{INBOX_JS}</script>
</body>
</html>
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
