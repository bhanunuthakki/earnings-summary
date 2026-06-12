"""src/triggers/ — Personal CIO trigger framework.

Public surface:

    base            — Protocol contract + draft dataclasses
                      (Trigger, Cadence, TriggerCandidate, AlertDraft,
                       QueuedActionDraft, UserStateContext, ThesisAnchor)
    kpi_inflection  — KpiInflectionTrigger (full impl as of PR-N11)
    earnings_tone   — EarningsToneTrigger (full impl as of PR-N8)
    saydo_due       — SayDoDueTrigger (full impl as of PR-N12)
    material_news   — MaterialNewsTrigger (full impl as of PR-N15)
    registry        — ENABLED_TRIGGERS / ALL_TRIGGERS lists consumed by
                      the morning driver (PR-N9)

The morning driver walks ``ENABLED_TRIGGERS``, instantiating each class
and running the lifecycle ``scan → should_fire → build_alert →
draft_actions``. The CRUD layer in ``alerts.store`` persists the drafts.
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
from triggers.decision_condition import DecisionConditionTrigger
from triggers.earnings_tone import EarningsToneTrigger
from triggers.kpi_inflection import KpiInflectionTrigger
from triggers.material_news import MaterialNewsTrigger
from triggers.registry import ALL_TRIGGERS, ENABLED_TRIGGERS
from triggers.saydo_due import SayDoDueTrigger

__all__ = [
    "ALL_TRIGGERS",
    "ENABLED_TRIGGERS",
    "AlertDraft",
    "Cadence",
    "DecisionConditionTrigger",
    "EarningsToneTrigger",
    "KpiInflectionTrigger",
    "MaterialNewsTrigger",
    "QueuedActionDraft",
    "SayDoDueTrigger",
    "ThesisAnchor",
    "Trigger",
    "TriggerCandidate",
    "UserStateContext",
]
