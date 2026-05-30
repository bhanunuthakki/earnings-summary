"""Trigger registry — enumerates which sensor classes the morning driver runs.

``ENABLED_TRIGGERS`` is the production set: only triggers whose full lifecycle
(``scan → should_fire → build_alert → draft_actions``) is implemented end-to-end
appear here. The morning driver iterates this list by default.

``ALL_TRIGGERS`` is the unfiltered set including stubs; useful for tests and
for the ``--triggers`` CLI flag which can opt into a stub trigger to confirm
the driver tolerates its ``NotImplementedError`` from ``build_alert``.

Adding a new trigger: import its class, append to ``ALL_TRIGGERS`` (and to
``ENABLED_TRIGGERS`` once its lifecycle is real). Order is purely cosmetic —
the driver doesn't depend on it.
"""

from __future__ import annotations

from triggers.base import Trigger
from triggers.earnings_tone import EarningsToneTrigger
from triggers.kpi_inflection import KpiInflectionTrigger
from triggers.material_news import MaterialNewsTrigger
from triggers.saydo_due import SayDoDueTrigger

ENABLED_TRIGGERS: list[type[Trigger]] = [
    EarningsToneTrigger,
    KpiInflectionTrigger,
    SayDoDueTrigger,
    MaterialNewsTrigger,
]

ALL_TRIGGERS: list[type[Trigger]] = [
    EarningsToneTrigger,
    KpiInflectionTrigger,
    SayDoDueTrigger,
    MaterialNewsTrigger,
]
