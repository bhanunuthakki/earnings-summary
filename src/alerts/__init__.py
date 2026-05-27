"""
src/alerts/ — row-CRUD for the fired-alert log and its attached queued actions.

The pair (``alerts`` + ``queued_actions``) is the substrate the trigger
framework writes to whenever a KPI inflection, earnings-tone shift, SayDo
verdict, thesis-anchor drift, or material-news event happens. The dashboard
reads from these tables to render the alert feed and to power the
approve/dismiss / apply/cancel buttons.

The store module exposes:
  * fire_alert, find_by_signature, list_pending_alerts, list_alerts,
    approve_alert, dismiss_alert
  * queue_action, list_queued_actions_for_alert, list_pending_actions,
    apply_action, cancel_action
  * compute_signature_sha — deterministic re-fire dedup hash, called by
    sensors before they fire_alert.

Sensors themselves ship in PR-N2 (stub framework) and PR-N8+ (per-trigger
real implementations); this module is the row-CRUD substrate they consume.
"""

from __future__ import annotations

from alerts.store import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_PENDING,
    ALERT_STATUS_APPROVED,
    ALERT_STATUS_DISMISSED,
    ALERT_STATUS_EXPIRED,
    ALERT_STATUS_PENDING,
    AlertRow,
    QueuedActionRow,
    apply_action,
    approve_alert,
    cancel_action,
    compute_signature_sha,
    dismiss_alert,
    find_by_signature,
    fire_alert,
    list_alerts,
    list_pending_actions,
    list_pending_alerts,
    list_queued_actions_for_alert,
    queue_action,
)

__all__ = [
    "ACTION_STATUS_APPLIED",
    "ACTION_STATUS_CANCELLED",
    "ACTION_STATUS_PENDING",
    "ALERT_STATUS_APPROVED",
    "ALERT_STATUS_DISMISSED",
    "ALERT_STATUS_EXPIRED",
    "ALERT_STATUS_PENDING",
    "AlertRow",
    "QueuedActionRow",
    "apply_action",
    "approve_alert",
    "cancel_action",
    "compute_signature_sha",
    "dismiss_alert",
    "find_by_signature",
    "fire_alert",
    "list_alerts",
    "list_pending_actions",
    "list_pending_alerts",
    "list_queued_actions_for_alert",
    "queue_action",
]
