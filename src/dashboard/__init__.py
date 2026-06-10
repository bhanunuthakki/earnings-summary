"""
src/dashboard/ — static HTML render layer for the Personal CIO substrate.

Renders three surfaces against the ``alerts`` / ``queued_actions`` /
``thesis_ledger_entries`` / ``position_sizing_intent`` tables (alembic
0060–0064):

  * ``digest``         — daily morning digest, "what fired in the last 24h"
  * ``feed``           — persistent chronological alert log with status badges
  * ``evidence_drawer`` — reusable per-alert evidence component, default-expanded

Server-side rendered only. The approve/dismiss interaction is offered through
the ``execution/approve_queued_action.py`` CLI; the HTML emits ``<a>`` links
plus copy-pasteable invocation lines so the dashboard remains a static
artifact while we wait for a real server (much later).

The renderers consume ``src/alerts/`` and ``src/user_state/`` directly — they
do not duplicate read logic. CSS lives inline in this package (small,
self-contained) so the deliverable HTML files are single-file and openable
straight from the filesystem.
"""

from __future__ import annotations

from dashboard._card import render_alert_card
from dashboard.digest import render_morning_digest
from dashboard.evidence_drawer import render_evidence_drawer
from dashboard.feed import render_alert_feed

__all__ = [
    "render_alert_card",
    "render_alert_feed",
    "render_evidence_drawer",
    "render_morning_digest",
]
