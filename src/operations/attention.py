"""Closed, evidence-led lifecycle semantics for Operations attention findings.

This module intentionally carries no display copy, raw snapshot, command, or
operator identity text.  Those are presentation or action-boundary concerns.
It owns the deterministic identity and lifecycle rules that later persistence,
action, and UI layers must share.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9_.:/-]{1,256}\Z")
_FINDING_ID = re.compile(r"operations-attention:[0-9a-f]{64}\Z")


class AttentionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AttentionHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class AttentionLifecycle(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class AttentionAction(StrEnum):
    DETECT = "detected"
    ACKNOWLEDGE = "acknowledge"
    SNOOZE = "snooze"
    EXPIRE_ACKNOWLEDGEMENT = "expire_acknowledgement"
    EXPIRE_SNOOZE = "expire_snooze"
    RESOLVE = "resolve"
    SUPERSEDE = "supersede"


class FindingKind(StrEnum):
    RUNTIME_HEALTH = "runtime_health"
    SCHEMA_COMPATIBILITY = "schema_compatibility"
    DATA_INTEGRITY = "data_integrity"
    REPAIR_REQUIRED = "repair_required"


class EvidenceKind(StrEnum):
    RUNTIME_RECEIPT = "runtime_receipt"
    SCHEMA_OBSERVATION = "schema_observation"
    INTEGRITY_AUDIT = "integrity_audit"
    REPAIR_RECEIPT = "repair_receipt"


class AttentionReasonCode(StrEnum):
    EVIDENCE_REVIEWED = "evidence_reviewed"
    FOLLOW_UP_SCHEDULED = "follow_up_scheduled"
    INVESTIGATION_IN_PROGRESS = "investigation_in_progress"
    MAINTENANCE_WINDOW = "maintenance_window"


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _require_label(value: str, name: str) -> str:
    normalized = value.strip()
    if _LABEL.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a bounded canonical label")
    return normalized


def _require_sha256(value: str, name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceIdentity:
    """A bounded, redacted pointer to the evidence supporting one finding."""

    kind: EvidenceKind
    fingerprint_sha256: str
    version: str
    reference: str
    reference_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceKind(self.kind))
        _require_sha256(self.fingerprint_sha256, "fingerprint_sha256")
        object.__setattr__(self, "version", _require_label(self.version, "version"))
        normalized_reference = self.reference.strip()
        if _REFERENCE.fullmatch(normalized_reference) is None:
            raise ValueError("reference must be a bounded redacted reference")
        object.__setattr__(self, "reference", normalized_reference)
        _require_sha256(self.reference_sha256, "reference_sha256")


@dataclass(frozen=True, slots=True)
class AttentionReason:
    """A bounded, auditable reason for temporarily suppressing a finding."""

    code: AttentionReasonCode
    reference_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", AttentionReasonCode(self.code))
        _require_sha256(self.reference_sha256, "reference_sha256")


def derive_finding_id(*, owner: str, kind: FindingKind, evidence: EvidenceIdentity) -> str:
    """Build the stable identity from owner, finding kind, fingerprint and version only.

    A display string, evidence reference/hash, and observation timestamp are
    intentionally excluded: none should create a new finding identity.
    """

    canonical = json.dumps(
        {
            "evidence_fingerprint_sha256": _require_sha256(
                evidence.fingerprint_sha256, "fingerprint_sha256"
            ),
            "evidence_version": _require_label(evidence.version, "version"),
            "finding_kind": FindingKind(kind).value,
            "owner": _require_label(owner, "owner"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "operations-attention:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationsAttentionFinding:
    """Current lifecycle projection; immutable receipts explain every mutation."""

    finding_id: str
    owner: str
    kind: FindingKind
    severity: AttentionSeverity
    health: AttentionHealth
    evidence: EvidenceIdentity
    lifecycle: AttentionLifecycle
    opened_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_until: datetime | None = None
    snoozed_until: datetime | None = None
    resolved_at: datetime | None = None
    superseded_by_finding_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _require_label(self.owner, "owner"))
        object.__setattr__(self, "kind", FindingKind(self.kind))
        object.__setattr__(self, "severity", AttentionSeverity(self.severity))
        object.__setattr__(self, "health", AttentionHealth(self.health))
        object.__setattr__(self, "lifecycle", AttentionLifecycle(self.lifecycle))
        expected = derive_finding_id(owner=self.owner, kind=self.kind, evidence=self.evidence)
        if self.finding_id != expected or _FINDING_ID.fullmatch(self.finding_id) is None:
            raise ValueError("finding_id must match its canonical attention identity")
        _require_aware(self.opened_at, "opened_at")
        for name, value in (
            ("acknowledged_at", self.acknowledged_at),
            ("acknowledged_until", self.acknowledged_until),
            ("snoozed_until", self.snoozed_until),
            ("resolved_at", self.resolved_at),
        ):
            if value is not None:
                _require_aware(value, name)
        if self.lifecycle is AttentionLifecycle.ACKNOWLEDGED:
            if self.acknowledged_at is None or self.acknowledged_until is None:
                raise ValueError(
                    "acknowledged findings require acknowledged_at and acknowledged_until"
                )
            if self.acknowledged_until <= self.acknowledged_at:
                raise ValueError("acknowledged_until must be after acknowledged_at")
        elif self.acknowledged_at is not None or self.acknowledged_until is not None:
            raise ValueError("only acknowledged findings may retain acknowledgement timestamps")
        if self.lifecycle is AttentionLifecycle.SNOOZED:
            if self.snoozed_until is None:
                raise ValueError("snoozed findings require snoozed_until")
        elif self.snoozed_until is not None:
            raise ValueError("only snoozed findings may have snoozed_until")
        if self.lifecycle is AttentionLifecycle.RESOLVED:
            if self.resolved_at is None:
                raise ValueError("resolved findings require resolved_at")
        elif self.resolved_at is not None:
            raise ValueError("only resolved findings may retain resolved_at")
        if self.lifecycle is AttentionLifecycle.SUPERSEDED:
            if self.superseded_by_finding_id is None:
                raise ValueError("superseded findings require a successor identity")
            if _FINDING_ID.fullmatch(self.superseded_by_finding_id) is None:
                raise ValueError("superseded_by_finding_id must be canonical")
        elif self.superseded_by_finding_id is not None:
            raise ValueError("only superseded findings may have a successor identity")

    @classmethod
    def open(
        cls,
        *,
        owner: str,
        kind: FindingKind,
        severity: AttentionSeverity,
        health: AttentionHealth,
        evidence: EvidenceIdentity,
        opened_at: datetime,
    ) -> OperationsAttentionFinding:
        return cls(
            finding_id=derive_finding_id(owner=owner, kind=kind, evidence=evidence),
            owner=_require_label(owner, "owner"),
            kind=FindingKind(kind),
            severity=AttentionSeverity(severity),
            health=AttentionHealth(health),
            evidence=evidence,
            lifecycle=AttentionLifecycle.OPEN,
            opened_at=_require_aware(opened_at, "opened_at"),
        )

    def attention_lifecycle(self, *, at: datetime) -> AttentionLifecycle:
        """Project elapsed snooze state without hiding the underlying health."""

        now = _require_aware(at, "at")
        if (
            self.lifecycle is AttentionLifecycle.ACKNOWLEDGED
            and self.acknowledged_until is not None
            and now >= self.acknowledged_until
        ):
            return AttentionLifecycle.OPEN
        if (
            self.lifecycle is AttentionLifecycle.SNOOZED
            and self.snoozed_until is not None
            and now >= self.snoozed_until
        ):
            return AttentionLifecycle.OPEN
        return self.lifecycle

    def requires_attention(self, *, at: datetime) -> bool:
        return self.attention_lifecycle(at=at) is AttentionLifecycle.OPEN


def _suppression_is_prohibited(finding: OperationsAttentionFinding) -> bool:
    return finding.severity is AttentionSeverity.CRITICAL or finding.health in {
        AttentionHealth.INVALID,
        AttentionHealth.UNAVAILABLE,
    }


_ALLOWED_TRANSITIONS: dict[AttentionLifecycle, frozenset[AttentionAction]] = {
    AttentionLifecycle.OPEN: frozenset(
        {AttentionAction.ACKNOWLEDGE, AttentionAction.SNOOZE, AttentionAction.RESOLVE}
    ),
    AttentionLifecycle.ACKNOWLEDGED: frozenset(
        {
            AttentionAction.SNOOZE,
            AttentionAction.EXPIRE_ACKNOWLEDGEMENT,
            AttentionAction.RESOLVE,
        }
    ),
    AttentionLifecycle.SNOOZED: frozenset(
        {AttentionAction.ACKNOWLEDGE, AttentionAction.EXPIRE_SNOOZE, AttentionAction.RESOLVE}
    ),
    AttentionLifecycle.RESOLVED: frozenset(),
    AttentionLifecycle.SUPERSEDED: frozenset(),
}

_ALLOWED_SUPPRESSION_REASONS: dict[AttentionAction, frozenset[AttentionReasonCode]] = {
    AttentionAction.ACKNOWLEDGE: frozenset({AttentionReasonCode.EVIDENCE_REVIEWED}),
    AttentionAction.SNOOZE: frozenset(
        {
            AttentionReasonCode.FOLLOW_UP_SCHEDULED,
            AttentionReasonCode.INVESTIGATION_IN_PROGRESS,
            AttentionReasonCode.MAINTENANCE_WINDOW,
        }
    ),
}


def apply_attention_action(
    finding: OperationsAttentionFinding,
    action: AttentionAction,
    *,
    at: datetime,
    reason: AttentionReason | None = None,
    acknowledge_until: datetime | None = None,
    snooze_until: datetime | None = None,
) -> OperationsAttentionFinding:
    """Apply the closed non-execution lifecycle actions to a finding projection."""

    occurred_at = _require_aware(at, "at")
    if occurred_at < finding.opened_at:
        raise ValueError("action time cannot predate finding opened_at")
    selected = AttentionAction(action)
    allowed = _ALLOWED_TRANSITIONS[finding.lifecycle]
    if selected not in allowed:
        raise ValueError(f"{selected.value} is not allowed from {finding.lifecycle.value}")
    if selected in {
        AttentionAction.ACKNOWLEDGE,
        AttentionAction.SNOOZE,
    } and _suppression_is_prohibited(finding):
        raise ValueError(
            "critical, invalid, or unavailable findings cannot be acknowledged or snoozed"
        )
    if selected is AttentionAction.ACKNOWLEDGE:
        if reason is None:
            raise ValueError("acknowledge requires a bounded reason")
        if reason.code not in _ALLOWED_SUPPRESSION_REASONS[selected]:
            raise ValueError("reason code is not allowed for acknowledge")
        if acknowledge_until is None:
            raise ValueError("acknowledge requires acknowledge_until")
        until = _require_aware(acknowledge_until, "acknowledge_until")
        if until <= occurred_at:
            raise ValueError("acknowledge_until must be after at")
        return replace(
            finding,
            lifecycle=AttentionLifecycle.ACKNOWLEDGED,
            acknowledged_at=occurred_at,
            acknowledged_until=until,
            snoozed_until=None,
        )
    if selected is AttentionAction.SNOOZE:
        if reason is None:
            raise ValueError("snooze requires a bounded reason")
        if reason.code not in _ALLOWED_SUPPRESSION_REASONS[selected]:
            raise ValueError("reason code is not allowed for snooze")
        if snooze_until is None:
            raise ValueError("snooze requires snooze_until")
        until = _require_aware(snooze_until, "snooze_until")
        if until <= occurred_at:
            raise ValueError("snooze_until must be after at")
        return replace(
            finding,
            lifecycle=AttentionLifecycle.SNOOZED,
            acknowledged_at=None,
            acknowledged_until=None,
            snoozed_until=until,
        )
    if selected is AttentionAction.EXPIRE_ACKNOWLEDGEMENT:
        if finding.acknowledged_until is None:
            raise ValueError("acknowledged finding lacks acknowledged_until")
        if occurred_at < finding.acknowledged_until:
            raise ValueError("acknowledgement has not expired")
        return replace(
            finding,
            lifecycle=AttentionLifecycle.OPEN,
            acknowledged_at=None,
            acknowledged_until=None,
        )
    if selected is AttentionAction.EXPIRE_SNOOZE:
        if finding.lifecycle is not AttentionLifecycle.SNOOZED or finding.snoozed_until is None:
            raise ValueError("only a snoozed finding can expire")
        if occurred_at < finding.snoozed_until:
            raise ValueError("snooze has not expired")
        return replace(finding, lifecycle=AttentionLifecycle.OPEN, snoozed_until=None)
    if selected is AttentionAction.RESOLVE:
        if finding.health is not AttentionHealth.HEALTHY:
            raise ValueError("findings require healthy evidence before they can be resolved")
        return replace(
            finding,
            lifecycle=AttentionLifecycle.RESOLVED,
            resolved_at=occurred_at,
            acknowledged_at=None,
            acknowledged_until=None,
            snoozed_until=None,
        )
    raise ValueError(f"{selected.value} is not an in-place attention action")


@dataclass(frozen=True, slots=True)
class EvidenceReconciliation:
    superseded: OperationsAttentionFinding
    successor: OperationsAttentionFinding


def reconcile_material_evidence(
    finding: OperationsAttentionFinding,
    *,
    evidence: EvidenceIdentity,
    observed_at: datetime,
) -> EvidenceReconciliation:
    """Supersede an old projection and reopen attention for materially new evidence."""

    _require_aware(observed_at, "observed_at")
    if observed_at < finding.opened_at:
        raise ValueError("evidence observation cannot predate finding opened_at")
    if finding.lifecycle in {AttentionLifecycle.RESOLVED, AttentionLifecycle.SUPERSEDED}:
        raise ValueError(f"cannot reconcile terminal {finding.lifecycle.value} finding")
    if (
        finding.evidence.fingerprint_sha256 == evidence.fingerprint_sha256
        and finding.evidence.version == evidence.version
    ):
        raise ValueError("evidence fingerprint/version has not materially changed")
    successor = OperationsAttentionFinding.open(
        owner=finding.owner,
        kind=finding.kind,
        severity=finding.severity,
        health=finding.health,
        evidence=evidence,
        opened_at=observed_at,
    )
    superseded = replace(
        finding,
        lifecycle=AttentionLifecycle.SUPERSEDED,
        acknowledged_at=None,
        acknowledged_until=None,
        snoozed_until=None,
        resolved_at=None,
        superseded_by_finding_id=successor.finding_id,
    )
    return EvidenceReconciliation(superseded=superseded, successor=successor)
