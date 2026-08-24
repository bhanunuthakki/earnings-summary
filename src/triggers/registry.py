"""Trigger registry — enumerates which sensor classes the morning driver runs.

``ENABLED_TRIGGERS`` is the production set: only triggers whose full lifecycle
(``scan → should_fire → build_alert → draft_actions``) is implemented end-to-end
appear here. The morning driver iterates this list by default.

``ALL_TRIGGERS`` is the unfiltered set including stubs and deliberately-disabled
sensors; useful for tests and for the ``--triggers`` CLI flag which can opt into
a trigger that isn't in the default set (e.g. to confirm the driver tolerates a
stub's ``NotImplementedError`` from ``build_alert``, or to run ``saydo_due``
on demand).

``SayDoDueTrigger`` is intentionally NOT in ``ENABLED_TRIGGERS`` (owner call,
2026-07-02): the say/do verdict it would surface is already recorded
deterministically by the matcher (``execution/match_commitments.py`` →
``management_commitments.outcome`` + the ``saydo_historical_metrics`` ledger,
shown in each ticker's Say/Do panel and consumed by the mgmt-credibility lens),
so a per-verdict feed alert + owner-approval draft was redundant noise. The
class stays fully wired in ``ALL_TRIGGERS`` — re-add it to ``ENABLED_TRIGGERS``
(or pass ``--triggers saydo_due``) to bring the feed cards back.

Adding a new trigger: import its class, append to ``ALL_TRIGGERS`` (and to
``ENABLED_TRIGGERS`` once its lifecycle is real). Order is purely cosmetic —
the driver doesn't depend on it.
"""

from __future__ import annotations

from triggers.base import StatefulTrigger, Trigger
from triggers.decision_condition import DecisionConditionTrigger
from triggers.earnings_tone import EarningsToneTrigger
from triggers.kpi_inflection import KpiInflectionTrigger
from triggers.material_news import MaterialNewsTrigger
from triggers.price_action import PriceActionTrigger
from triggers.restatement import RestatementTrigger
from triggers.saydo_due import SayDoDueTrigger

ENABLED_TRIGGERS: list[type[Trigger]] = [
    EarningsToneTrigger,
    KpiInflectionTrigger,
    # SayDoDueTrigger — disabled by owner call (see module docstring); the
    # say/do verdict is auto-tracked by the matcher, so no feed alert is drafted.
    MaterialNewsTrigger,
    DecisionConditionTrigger,
    RestatementTrigger,
]

ALL_TRIGGERS: list[type[Trigger] | type[StatefulTrigger]] = [
    EarningsToneTrigger,
    KpiInflectionTrigger,
    SayDoDueTrigger,
    MaterialNewsTrigger,
    DecisionConditionTrigger,
    RestatementTrigger,
    # Stateful and intentionally dark: requires an explicitly armed BHA-85
    # checkpoint ladder and is available only through --triggers price_action.
    PriceActionTrigger,
]
