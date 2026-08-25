"""Read-only, presentation-safe projection of Operations attention findings.

The durable action writer remains the sole lifecycle mutator.  This module
only reads the migration-owned projection table and makes elapsed suppression
truthful at the observation time supplied by the caller.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from operations.attention import (
    AttentionHealth,
    AttentionLifecycle,
    AttentionSeverity,
    EvidenceIdentity,
    EvidenceKind,
    FindingKind,
    OperationsAttentionFinding,
)
from operations.attention_store import OperatorAction

AttentionAvailability = Literal["available", "empty", "unavailable"]
AttentionTone = Literal["ok", "warn", "bad"]
_SEVERITY_TONES: dict[AttentionSeverity, AttentionTone] = {
    AttentionSeverity.INFO: "ok",
    AttentionSeverity.WARNING: "warn",
    AttentionSeverity.CRITICAL: "bad",
}
_HEALTH_TONES: dict[AttentionHealth, AttentionTone] = {
    AttentionHealth.HEALTHY: "ok",
    AttentionHealth.DEGRADED: "warn",
    AttentionHealth.INVALID: "bad",
    AttentionHealth.UNAVAILABLE: "bad",
}
_MAX_PANEL_FINDINGS = 200


class _SafeView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AttentionActionView(_SafeView):
    action: Literal["acknowledge", "snooze", "resolve"]
    label: str = Field(min_length=1, max_length=32)


class AttentionFindingView(_SafeView):
    """Only bounded evidence identity and lifecycle facts may reach markup."""

    finding_id: str = Field(pattern=r"operations-attention:[0-9a-f]{64}")
    owner: str = Field(pattern=r"[A-Za-z0-9_.:-]{1,128}")
    kind: str = Field(pattern=r"[a-z_]{1,64}")
    severity: Literal["info", "warning", "critical"]
    severity_tone: AttentionTone
    health: Literal["healthy", "degraded", "invalid", "unavailable"]
    health_tone: AttentionTone
    lifecycle: Literal["open", "acknowledged", "snoozed", "resolved", "superseded"]
    lifecycle_detail: str = Field(min_length=1, max_length=64)
    evidence_version: str = Field(pattern=r"[A-Za-z0-9_.:-]{1,128}")
    evidence_reference: str = Field(pattern=r"[A-Za-z0-9_.:-]{1,256}")
    evidence_fingerprint_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    evidence_reference_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    opened_label: str = Field(min_length=1, max_length=32)
    updated_at: datetime
    updated_label: str = Field(min_length=1, max_length=32)
    effective_label: str = Field(min_length=1, max_length=32)
    actionable: bool
    actions: tuple[AttentionActionView, ...]


class AttentionPanelView(_SafeView):
    state: AttentionAvailability
    message: str = Field(min_length=1, max_length=160)
    observed_label: str = Field(min_length=1, max_length=32)
    findings: tuple[AttentionFindingView, ...] = ()


def _as_datetime(value: object, *, field: str, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise ValueError(f"stored {field} is missing")
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"stored {field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"stored {field} must be timezone-aware")
    return parsed


def _stored_finding(row: sqlite3.Row) -> tuple[OperationsAttentionFinding, datetime]:
    evidence = EvidenceIdentity(
        kind=EvidenceKind(str(row["evidence_kind"])),
        fingerprint_sha256=str(row["evidence_fingerprint_sha256"]),
        version=str(row["evidence_version"]),
        reference=str(row["evidence_reference"]),
        reference_sha256=str(row["evidence_reference_sha256"]),
    )
    opened_at = _as_datetime(row["opened_at"], field="opened_at", required=True)
    assert opened_at is not None
    finding = OperationsAttentionFinding(
        finding_id=str(row["finding_id"]),
        owner=str(row["owner"]),
        kind=FindingKind(str(row["kind"])),
        severity=AttentionSeverity(str(row["severity"])),
        health=AttentionHealth(str(row["health"])),
        evidence=evidence,
        lifecycle=AttentionLifecycle(str(row["lifecycle"])),
        opened_at=opened_at,
        acknowledged_at=_as_datetime(row["acknowledged_at"], field="acknowledged_at"),
        acknowledged_until=_as_datetime(row["acknowledged_until"], field="acknowledged_until"),
        snoozed_until=_as_datetime(row["snoozed_until"], field="snoozed_until"),
        resolved_at=_as_datetime(row["resolved_at"], field="resolved_at"),
        superseded_by_finding_id=(
            None
            if row["superseded_by_finding_id"] is None
            else str(row["superseded_by_finding_id"])
        ),
    )
    updated_at = _as_datetime(row["updated_at"], field="updated_at", required=True)
    assert updated_at is not None
    return finding, updated_at


def _timestamp_label(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _safe_evidence_reference(value: str) -> str:
    """Reject filesystem-looking references before they can cross into HTML."""

    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or ".." in posix.parts
        or ".." in windows.parts
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("stored evidence reference is not safe for presentation")
    return value


def _severity_tone(severity: AttentionSeverity) -> AttentionTone:
    return _SEVERITY_TONES[severity]


def _health_tone(health: AttentionHealth) -> AttentionTone:
    return _HEALTH_TONES[health]


def _effective_detail(finding: OperationsAttentionFinding, *, observed_at: datetime) -> str:
    effective = finding.attention_lifecycle(at=observed_at)
    if (
        effective is AttentionLifecycle.OPEN
        and finding.lifecycle is AttentionLifecycle.ACKNOWLEDGED
    ):
        return "Acknowledgement expired; acknowledged policy remains"
    if effective is AttentionLifecycle.OPEN and finding.lifecycle is AttentionLifecycle.SNOOZED:
        return "Snooze expired; snoozed policy remains"
    return effective.value.replace("_", " ").title()


def _allowed_actions(finding: OperationsAttentionFinding) -> tuple[AttentionActionView, ...]:
    """Mirror the writer's closed transition and suppression policy exactly."""

    suppression_allowed = (
        finding.severity is not AttentionSeverity.CRITICAL
        and finding.health
        not in {
            AttentionHealth.INVALID,
            AttentionHealth.UNAVAILABLE,
        }
    )
    allowed: list[OperatorAction] = []
    if finding.lifecycle is AttentionLifecycle.OPEN:
        if suppression_allowed:
            allowed.extend((OperatorAction.ACKNOWLEDGE, OperatorAction.SNOOZE))
        if finding.health is AttentionHealth.HEALTHY:
            allowed.append(OperatorAction.RESOLVE)
    elif finding.lifecycle is AttentionLifecycle.ACKNOWLEDGED:
        if suppression_allowed:
            allowed.append(OperatorAction.SNOOZE)
        if finding.health is AttentionHealth.HEALTHY:
            allowed.append(OperatorAction.RESOLVE)
    elif finding.lifecycle is AttentionLifecycle.SNOOZED:
        if suppression_allowed:
            allowed.append(OperatorAction.ACKNOWLEDGE)
        if finding.health is AttentionHealth.HEALTHY:
            allowed.append(OperatorAction.RESOLVE)
    labels = {
        OperatorAction.ACKNOWLEDGE: "Acknowledge",
        OperatorAction.SNOOZE: "Snooze",
        OperatorAction.RESOLVE: "Resolve",
    }
    return tuple(
        AttentionActionView(action=action.value, label=labels[action]) for action in allowed
    )


def _view_for(
    finding: OperationsAttentionFinding, *, observed_at: datetime, updated_at: datetime
) -> AttentionFindingView:
    effective = finding.attention_lifecycle(at=observed_at)
    actions = _allowed_actions(finding)
    return AttentionFindingView(
        finding_id=finding.finding_id,
        owner=finding.owner,
        kind=finding.kind.value,
        severity=finding.severity.value,
        severity_tone=_severity_tone(finding.severity),
        health=finding.health.value,
        health_tone=_health_tone(finding.health),
        lifecycle=effective.value,
        lifecycle_detail=_effective_detail(finding, observed_at=observed_at),
        evidence_version=finding.evidence.version,
        evidence_reference=_safe_evidence_reference(finding.evidence.reference),
        evidence_fingerprint_sha256=finding.evidence.fingerprint_sha256,
        evidence_reference_sha256=finding.evidence.reference_sha256,
        opened_label=_timestamp_label(finding.opened_at),
        updated_at=updated_at,
        updated_label=_timestamp_label(updated_at),
        effective_label=_timestamp_label(observed_at),
        actionable=effective is AttentionLifecycle.OPEN,
        actions=actions,
    )


def _sort_key(item: AttentionFindingView) -> tuple[int, int, float, str]:
    severity_rank = {"critical": 0, "warning": 1, "info": 2}[item.severity]
    return (
        0 if item.actionable else 1,
        severity_rank,
        -item.updated_at.timestamp(),
        item.finding_id,
    )


def build_attention_panel_view(
    conn: sqlite3.Connection, *, observed_at: datetime
) -> AttentionPanelView:
    """Read the durable projection once and degrade without exposing DB details."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_label = _timestamp_label(observed_at)
    try:
        original_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,
                       evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,
                       acknowledged_at,acknowledged_until,snoozed_until,resolved_at,superseded_by_finding_id,
                       updated_at
                FROM operations_attention_findings
                WHERE lifecycle IN ('open', 'acknowledged', 'snoozed')
                ORDER BY
                    CASE
                        WHEN lifecycle = 'open' THEN 0
                        WHEN lifecycle = 'acknowledged' AND acknowledged_until <= ? THEN 0
                        WHEN lifecycle = 'snoozed' AND snoozed_until <= ? THEN 0
                        ELSE 1
                    END,
                    CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                    updated_at DESC,
                    finding_id
                LIMIT ?
                """,
                (observed_at.isoformat(), observed_at.isoformat(), _MAX_PANEL_FINDINGS),
            ).fetchall()
        finally:
            conn.row_factory = original_factory
        findings = tuple(
            sorted(
                (
                    _view_for(finding, observed_at=observed_at, updated_at=updated_at)
                    for finding, updated_at in (_stored_finding(row) for row in rows)
                ),
                key=_sort_key,
            )
        )
    except (sqlite3.Error, ValueError, TypeError):
        return AttentionPanelView(
            state="unavailable",
            message="Attention findings are unavailable.",
            observed_label=observed_label,
        )
    if not findings:
        return AttentionPanelView(
            state="empty",
            message="No attention findings are recorded.",
            observed_label=observed_label,
        )
    return AttentionPanelView(
        state="available",
        message="Attention findings are ordered by actionability, severity, and latest update.",
        observed_label=observed_label,
        findings=findings,
    )


__all__ = [
    "AttentionActionView",
    "AttentionFindingView",
    "AttentionPanelView",
    "build_attention_panel_view",
]
