"""src/triggers/ — Personal CIO trigger framework.

Public surface:

    base            — Protocol contract + draft dataclasses
                      (Trigger, Cadence, TriggerCandidate, AlertDraft,
                       QueuedActionDraft, UserStateContext, ThesisAnchor)
    kpi_inflection  — KpiInflectionTrigger (stub; real impl in follow-on PR)

The morning driver (also a follow-on PR) instantiates each registered
trigger class and walks the lifecycle ``scan → should_fire →
build_alert → draft_actions``. The CRUD layer (separate PR) persists
the produced drafts.
"""

from __future__ import annotations

from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    Trigger,
    TriggerCandidate,
    UserStateContext,
)
from triggers.kpi_inflection import KpiInflectionTrigger

__all__ = [
    "AlertDraft",
    "Cadence",
    "KpiInflectionTrigger",
    "QueuedActionDraft",
    "ThesisAnchor",
    "Trigger",
    "TriggerCandidate",
    "UserStateContext",
]
