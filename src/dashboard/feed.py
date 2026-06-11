"""Persistent inbox feed page.

Renders the full unified-inbox stream (alerts, drafts, ledger entries,
journal items, fresh synthesis sections — categorized + ranked) with
visible filter labels showing which constraints were applied. The
alert-vocabulary filters (status / trigger_kind) narrow the page back to
the alerts-only view they are defined over. Query filters are emitted as
a read-only strip — that filtering happens in the store query, not in the
rendered HTML — while the category chips filter client-side.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

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
    _render_header(body, total=len(items), limit=limit)
    _render_filter_strip(
        body,
        ticker=ticker,
        trigger_kind=trigger_kind,
        status=status,
        limit=limit,
        alerts_only=alerts_only,
    )
    body.write('<section class="dash-section dash-feed">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Stream</div>')
    body.write(f'<div class="dash-section-count">{len(items)} shown</div>')
    body.write("</div>")
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


def _render_header(body: StringIO, total: int, limit: int) -> None:
    body.write('<header class="l1-header">')
    body.write("<h1>Inbox feed</h1>")
    body.write(
        f'<div class="l1-subtitle">{total} item(s) returned '
        f"(limit {limit}). Ranked by severity x recency x position x thesis — "
        "hover a kind chip for the why. Status badge shows current state."
        "</div>"
    )
    body.write("</header>")


def _render_filter_strip(
    body: StringIO,
    *,
    ticker: str | None,
    trigger_kind: str | None,
    status: str | None,
    limit: int,
    alerts_only: bool = False,
) -> None:
    body.write('<div class="dash-filters">')
    body.write(_filter_chip("kinds", "alerts" if alerts_only else "ALL"))
    body.write(_filter_chip("ticker", ticker or "ALL"))
    body.write(_filter_chip("trigger", trigger_kind or "ALL"))
    body.write(_filter_chip("status", status or "ALL"))
    body.write(_filter_chip("limit", str(limit)))
    body.write("</div>")


def _filter_chip(label: str, value: str) -> str:
    return (
        '<div class="dash-filter-chip">'
        f'<span class="filter-label">{_esc(label)}:</span> '
        f'<span class="filter-value">{_esc(value)}</span>'
        "</div>"
    )


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
