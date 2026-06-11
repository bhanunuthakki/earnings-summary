"""
src/dashboard/ — HTML render layer for the Personal CIO substrate.

Renders against the ``alerts`` / ``queued_actions`` / ``thesis_ledger_entries``
/ ``position_sizing_intent`` tables (alembic 0060–0064):

  * ``feed``            — persistent chronological alert log with status badges
  * ``inbox``           — the unified deduped/ranked stream (Home rail + feed)
  * ``upcoming``        — the Home rail's compact upcoming-earnings strip
  * ``evidence_drawer`` — reusable per-alert evidence component

(The standalone morning-digest page retired 2026-06-11 — the Home rail IS
the morning view; ``/digest`` now redirects there.)

Server-side rendered only. Approve/dismiss is offered through the
``/approve`` route plus the ``execution/approve_queued_action.py`` CLI; the
HTML emits ``<a>`` links and copy-pasteable invocation lines.

The renderers consume ``src/alerts/`` and ``src/user_state/`` directly — they
do not duplicate read logic. CSS lives inline in this package (small,
self-contained) so rendered pages stay single-file.
"""

from __future__ import annotations

from dashboard._card import render_alert_card
from dashboard.evidence_drawer import render_evidence_drawer
from dashboard.feed import render_alert_feed

__all__ = [
    "render_alert_card",
    "render_alert_feed",
    "render_evidence_drawer",
]
