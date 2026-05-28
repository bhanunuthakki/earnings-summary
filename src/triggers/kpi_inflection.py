"""KPI inflection sensor — placeholder implementation.

Detects statistically significant inflections on KPIs the user has
flagged as load-bearing in ``user_kpi_registry``. The real logic
delegates to ``timeseries.detect_inflection`` over ``kpi_facts`` and
joins against the registry to gate by ``is_thesis_breaker`` /
``threshold_direction``. This stub exists to prove the Protocol shape
holds end-to-end with a concrete class; the real implementation lands
in a follow-on PR.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import ClassVar

from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)


class KpiInflectionTrigger:
    """Daily sensor over ``kpi_facts`` for user-registered KPIs."""

    kind: ClassVar[str] = "kpi_inflection"
    cadence: ClassVar[Cadence] = Cadence.DAILY

    def scan(
        self, ticker: str, db: sqlite3.Connection
    ) -> list[TriggerCandidate]:
        _ = ticker, db  # placeholder until the real impl lands
        return []

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool:
        _ = candidate, user_state
        return False

    def signature_key_evidence(
        self, candidate: TriggerCandidate
    ) -> Mapping[str, object]:
        """Placeholder: the real implementation will key on
        ``(kpi_name, inflection_period)``. Until scan returns real
        candidates this branch is unreachable; returning the full
        ``candidate.evidence`` is a safe stub that still produces a
        deterministic hash if a test ever exercises it."""
        return dict(candidate.evidence)

    def build_alert(
        self,
        candidate: TriggerCandidate,
        anchor: ThesisAnchor | None,
    ) -> AlertDraft:
        _ = candidate, anchor
        raise NotImplementedError("Implemented in PR-N11")

    def draft_actions(
        self,
        alert: AlertDraft,
        candidate: TriggerCandidate,
    ) -> list[QueuedActionDraft]:
        _ = alert, candidate
        return []
