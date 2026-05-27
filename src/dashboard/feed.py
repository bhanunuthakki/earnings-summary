"""Persistent chronological alert log.

Renders a flat list of alerts (newest first) across all statuses, with
visible filter labels showing which constraints were applied. Filters
are emitted as a read-only filter strip — the actual filtering happens
in the store query, not in the rendered HTML, so re-filtering means
re-running the CLI with different args.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from alerts import AlertRow, list_alerts, list_queued_actions_for_alert
from dashboard._card import render_alert_card
from dashboard._styles import CSS


def render_alert_feed(
    user_id: str = "bhanu",
    limit: int = 200,
    ticker: str | None = None,
    trigger_kind: str | None = None,
    status: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Return the full HTML string for the alert feed.

    Filters compose with AND in the store query — the feed renders only
    what comes back. The filter strip on the rendered page mirrors the
    args so the analyst can see which slice they're looking at without
    re-checking their shell history.
    """
    alerts_all = list_alerts(
        user_id=user_id,
        ticker=ticker,
        status=status,
        limit=limit,
        db_path=db_path,
    )
    alerts = (
        alerts_all
        if trigger_kind is None
        else [a for a in alerts_all if a.trigger_kind == trigger_kind]
    )

    body = StringIO()
    body.write('<div class="l1-shell">')
    _render_header(body, total=len(alerts), limit=limit)
    _render_filter_strip(
        body,
        ticker=ticker,
        trigger_kind=trigger_kind,
        status=status,
        limit=limit,
    )
    _render_alert_list(body, alerts, db_path)
    _render_footer(body)
    body.write("</div>")
    return _document(body.getvalue())


# ----------------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------------


def _render_header(body: StringIO, total: int, limit: int) -> None:
    body.write('<header class="l1-header">')
    body.write("<h1>Alert feed</h1>")
    body.write(
        f'<div class="l1-subtitle">{total} alert(s) returned '
        f"(limit {limit}). Newest first. Status badge shows current state."
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
) -> None:
    body.write('<div class="dash-filters">')
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


def _render_alert_list(
    body: StringIO,
    alerts: list[AlertRow],
    db_path: Path | None,
) -> None:
    body.write('<section class="dash-section dash-feed">')
    body.write('<div class="dash-section-header">')
    body.write('<div class="dash-section-title">Alerts</div>')
    body.write(f'<div class="dash-section-count">{len(alerts)} shown</div>')
    body.write("</div>")

    if not alerts:
        body.write(
            '<div class="empty-state">No alerts match the current filters.</div>'
        )
        body.write("</section>")
        return

    for alert in alerts:
        actions = list_queued_actions_for_alert(alert.id, db_path=db_path)
        render_alert_card(body, alert, actions=actions, show_status_badge=True)
    body.write("</section>")


def _render_footer(body: StringIO) -> None:
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    body.write('<div class="l1-footer">')
    body.write("<span>Alert feed</span>")
    body.write(f"<span>generated {_esc(generated_at)}</span>")
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
<title>Alert feed</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
