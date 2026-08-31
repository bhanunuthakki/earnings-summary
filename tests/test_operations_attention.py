"""Domain and schema contract for governed Operations attention findings."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from operations.attention import (
    AttentionAction,
    AttentionHealth,
    AttentionLifecycle,
    AttentionReason,
    AttentionReasonCode,
    AttentionSeverity,
    EvidenceIdentity,
    EvidenceKind,
    FindingKind,
    OperationsAttentionFinding,
    apply_attention_action,
    derive_finding_id,
    reconcile_material_evidence,
)
from operations.models import OperationsRegistry, OperationsSnapshot

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0035_add_report_kpi_reference_resolution_states"
NOW = datetime(2026, 8, 24, 18, 0, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _evidence(*, fingerprint: str = "a" * 64, version: str = "v1") -> EvidenceIdentity:
    return EvidenceIdentity(
        kind=EvidenceKind.RUNTIME_RECEIPT,
        fingerprint_sha256=fingerprint,
        version=version,
        reference="operations.runtime.pair.latest.json",
        reference_sha256="b" * 64,
    )


def _finding(*, evidence: EvidenceIdentity | None = None) -> OperationsAttentionFinding:
    return OperationsAttentionFinding.open(
        owner="scheduler.collect_operations_runtime_observations",
        kind=FindingKind.RUNTIME_HEALTH,
        severity=AttentionSeverity.WARNING,
        health=AttentionHealth.DEGRADED,
        evidence=evidence or _evidence(),
        opened_at=NOW,
    )


def _reason(code: AttentionReasonCode = AttentionReasonCode.EVIDENCE_REVIEWED) -> AttentionReason:
    return AttentionReason(
        code=code,
        reference_sha256="e" * 64,
    )


def test_finding_identity_is_stable_and_ignores_display_text_and_snapshot_time() -> None:
    evidence = _evidence()

    expected = derive_finding_id(
        owner="scheduler.collect_operations_runtime_observations",
        kind=FindingKind.RUNTIME_HEALTH,
        evidence=evidence,
    )
    assert expected == _finding().finding_id
    assert (
        expected
        == "operations-attention:dd092c4e3044926cefbd8d500e299e3e14ee2c33ab44e995a348c422c0caa7f2"
    )
    assert expected.startswith("operations-attention:")
    assert len(expected) == len("operations-attention:") + 64
    assert (
        derive_finding_id(
            owner="scheduler.collect_operations_runtime_observations",
            kind=FindingKind.RUNTIME_HEALTH,
            evidence=EvidenceIdentity(
                kind=EvidenceKind.RUNTIME_RECEIPT,
                fingerprint_sha256="a" * 64,
                version="v1",
                reference="different-safe-label",
                reference_sha256="c" * 64,
            ),
        )
        == expected
    )


def test_acknowledge_and_snooze_preserve_health_and_expiry_reopens_attention() -> None:
    finding = _finding()
    acknowledged = apply_attention_action(
        finding,
        AttentionAction.ACKNOWLEDGE,
        at=NOW,
        reason=_reason(),
        acknowledge_until=NOW + timedelta(hours=1),
    )
    assert acknowledged.lifecycle is AttentionLifecycle.ACKNOWLEDGED
    assert acknowledged.health is AttentionHealth.DEGRADED
    assert not acknowledged.requires_attention(at=NOW)
    assert acknowledged.requires_attention(at=NOW + timedelta(hours=1))
    assert acknowledged.attention_lifecycle(at=NOW + timedelta(hours=1)) is AttentionLifecycle.OPEN
    reopened_acknowledgement = apply_attention_action(
        acknowledged,
        AttentionAction.EXPIRE_ACKNOWLEDGEMENT,
        at=NOW + timedelta(hours=1),
    )
    assert reopened_acknowledgement.lifecycle is AttentionLifecycle.OPEN

    snoozed = apply_attention_action(
        finding,
        AttentionAction.SNOOZE,
        at=NOW,
        reason=_reason(AttentionReasonCode.INVESTIGATION_IN_PROGRESS),
        snooze_until=NOW + timedelta(hours=1),
    )
    assert snoozed.lifecycle is AttentionLifecycle.SNOOZED
    assert not snoozed.requires_attention(at=NOW + timedelta(minutes=59))
    assert snoozed.requires_attention(at=NOW + timedelta(hours=1))
    assert snoozed.attention_lifecycle(at=NOW + timedelta(hours=1)) is AttentionLifecycle.OPEN


def test_temporary_suppression_requires_a_bounded_reason_and_future_expiry() -> None:
    finding = _finding()

    with pytest.raises(ValueError, match="acknowledge requires a bounded reason"):
        apply_attention_action(
            finding,
            AttentionAction.ACKNOWLEDGE,
            at=NOW,
            acknowledge_until=NOW + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="acknowledge requires acknowledge_until"):
        apply_attention_action(
            finding,
            AttentionAction.ACKNOWLEDGE,
            at=NOW,
            reason=_reason(),
        )
    with pytest.raises(ValueError, match="snooze requires a bounded reason"):
        apply_attention_action(
            finding,
            AttentionAction.SNOOZE,
            at=NOW,
            snooze_until=NOW + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="snooze_until must be after at"):
        apply_attention_action(
            finding,
            AttentionAction.SNOOZE,
            at=NOW,
            reason=_reason(AttentionReasonCode.INVESTIGATION_IN_PROGRESS),
            snooze_until=NOW,
        )
    with pytest.raises(ValueError, match="reason code is not allowed for snooze"):
        apply_attention_action(
            finding,
            AttentionAction.SNOOZE,
            at=NOW,
            reason=_reason(),
            snooze_until=NOW + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="cannot predate finding opened_at"):
        apply_attention_action(
            finding,
            AttentionAction.ACKNOWLEDGE,
            at=NOW - timedelta(seconds=1),
            reason=_reason(),
            acknowledge_until=NOW + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    ("severity", "health"),
    [
        (AttentionSeverity.CRITICAL, AttentionHealth.DEGRADED),
        (AttentionSeverity.WARNING, AttentionHealth.INVALID),
        (AttentionSeverity.WARNING, AttentionHealth.UNAVAILABLE),
    ],
)
def test_critical_invalid_or_unavailable_findings_cannot_be_silently_suppressed(
    severity: AttentionSeverity, health: AttentionHealth
) -> None:
    finding = OperationsAttentionFinding.open(
        owner="scheduler.collect_operations_runtime_observations",
        kind=FindingKind.RUNTIME_HEALTH,
        severity=severity,
        health=health,
        evidence=_evidence(),
        opened_at=NOW,
    )

    with pytest.raises(ValueError, match="cannot be acknowledged or snoozed"):
        apply_attention_action(finding, AttentionAction.ACKNOWLEDGE, at=NOW)
    with pytest.raises(ValueError, match="cannot be acknowledged or snoozed"):
        apply_attention_action(
            finding,
            AttentionAction.SNOOZE,
            at=NOW,
            reason=_reason(AttentionReasonCode.INVESTIGATION_IN_PROGRESS),
            snooze_until=NOW + timedelta(days=1),
        )
    with pytest.raises(ValueError, match="require healthy evidence"):
        apply_attention_action(finding, AttentionAction.RESOLVE, at=NOW)


def test_closed_lifecycle_transition_matrix_rejects_terminal_rewrites_and_stale_timestamps() -> (
    None
):
    resolved = OperationsAttentionFinding.open(
        owner="scheduler.collect_operations_runtime_observations",
        kind=FindingKind.RUNTIME_HEALTH,
        severity=AttentionSeverity.INFO,
        health=AttentionHealth.HEALTHY,
        evidence=_evidence(),
        opened_at=NOW,
    )
    resolved = apply_attention_action(resolved, AttentionAction.RESOLVE, at=NOW)

    with pytest.raises(ValueError, match="not allowed from resolved"):
        apply_attention_action(
            resolved,
            AttentionAction.ACKNOWLEDGE,
            at=NOW,
            reason=_reason(),
            acknowledge_until=NOW + timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="only resolved findings may retain resolved_at"):
        OperationsAttentionFinding(
            finding_id=derive_finding_id(
                owner="scheduler.collect_operations_runtime_observations",
                kind=FindingKind.RUNTIME_HEALTH,
                evidence=_evidence(fingerprint="f" * 64),
            ),
            owner="scheduler.collect_operations_runtime_observations",
            kind=FindingKind.RUNTIME_HEALTH,
            severity=AttentionSeverity.INFO,
            health=AttentionHealth.HEALTHY,
            evidence=_evidence(fingerprint="f" * 64),
            lifecycle=AttentionLifecycle.OPEN,
            opened_at=NOW,
            resolved_at=NOW,
        )


def test_material_evidence_change_supersedes_acknowledgement_and_reopens_successor() -> None:
    acknowledged = apply_attention_action(
        _finding(),
        AttentionAction.ACKNOWLEDGE,
        at=NOW,
        reason=_reason(),
        acknowledge_until=NOW + timedelta(hours=1),
    )
    outcome = reconcile_material_evidence(
        acknowledged,
        evidence=_evidence(fingerprint="d" * 64, version="v2"),
        observed_at=NOW + timedelta(minutes=1),
    )

    assert outcome.superseded.lifecycle is AttentionLifecycle.SUPERSEDED
    assert outcome.successor.lifecycle is AttentionLifecycle.OPEN
    assert outcome.superseded.superseded_by_finding_id == outcome.successor.finding_id
    assert outcome.successor.requires_attention(at=NOW + timedelta(minutes=1))
    with pytest.raises(ValueError, match="cannot reconcile terminal superseded"):
        reconcile_material_evidence(
            outcome.superseded,
            evidence=_evidence(fingerprint="f" * 64, version="v3"),
            observed_at=NOW + timedelta(minutes=2),
        )
    with pytest.raises(ValueError, match="cannot predate finding opened_at"):
        reconcile_material_evidence(
            acknowledged,
            evidence=_evidence(fingerprint="e" * 64, version="v3"),
            observed_at=NOW - timedelta(seconds=1),
        )
    resolved = apply_attention_action(
        OperationsAttentionFinding.open(
            owner="scheduler.collect_operations_runtime_observations",
            kind=FindingKind.RUNTIME_HEALTH,
            severity=AttentionSeverity.INFO,
            health=AttentionHealth.HEALTHY,
            evidence=_evidence(fingerprint="1" * 64),
            opened_at=NOW,
        ),
        AttentionAction.RESOLVE,
        at=NOW,
    )
    with pytest.raises(ValueError, match="cannot reconcile terminal resolved"):
        reconcile_material_evidence(
            resolved,
            evidence=_evidence(fingerprint="2" * 64, version="v2"),
            observed_at=NOW + timedelta(minutes=2),
        )


def test_attention_persistence_slice_is_a_deliberate_surface_exclusion() -> None:
    """No Operations projection changes until bounded evidence and actions are wired end-to-end.

    This is the explicit BHA-88 slice-one surface disposition: the existing
    registry and snapshot contracts remain the sole live Operations surfaces;
    this domain/persistence substrate neither claims health nor exposes a
    mutating control.
    """

    assert "attention_findings" not in OperationsRegistry.model_fields
    assert "attention_findings" not in OperationsSnapshot.model_fields


def test_migration_creates_append_only_bounded_attention_receipts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "attention.db")
    config = _config(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "operations_attention_findings",
            "operations_attention_lifecycle_events",
            "operations_attention_action_receipts",
            "operations_attention_repair_references",
        } <= tables
        connection.execute(
            "INSERT INTO operations_attention_findings "
            "(finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,"
            "evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "operations-attention:" + "a" * 64,
                "scheduler.collect_operations_runtime_observations",
                "runtime_health",
                "runtime_receipt",
                "a" * 64,
                "v1",
                "operations.runtime.pair.latest.json",
                "b" * 64,
                "warning",
                "degraded",
                "open",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO operations_attention_lifecycle_events "
            "(event_id,finding_id,event_kind,from_lifecycle,to_lifecycle,evidence_fingerprint_sha256,"
            "occurred_at,receipt_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (
                "operations-attention-event:" + "c" * 64,
                "operations-attention:" + "a" * 64,
                "acknowledge",
                "open",
                "acknowledged",
                "a" * 64,
                NOW.isoformat(),
                "d" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be suppressed"):
            connection.execute(
                "UPDATE operations_attention_findings "
                "SET severity='critical', lifecycle='acknowledged' "
                "WHERE finding_id=?",
                ("operations-attention:" + "a" * 64,),
            )
        connection.execute(
            "INSERT INTO operations_attention_action_receipts "
            "(receipt_id,idempotency_key_sha256,finding_id,lifecycle_event_id,actor,action,"
            "result_lifecycle,occurred_at,request_sha256,reason_code,reason_reference_sha256,"
            "acknowledged_until,snoozed_until,result_state,failure_code,failure_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "operations-attention-action:" + "e" * 64,
                "f" * 64,
                "operations-attention:" + "a" * 64,
                "operations-attention-event:" + "c" * 64,
                "owner.bhanu",
                "acknowledge",
                "acknowledged",
                NOW.isoformat(),
                "0" * 64,
                "evidence_reviewed",
                "1" * 64,
                (NOW + timedelta(hours=1)).isoformat(),
                None,
                "applied",
                None,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO operations_attention_action_receipts "
            "(receipt_id,idempotency_key_sha256,finding_id,lifecycle_event_id,actor,action,"
            "result_lifecycle,occurred_at,request_sha256,reason_code,reason_reference_sha256,"
            "acknowledged_until,snoozed_until,result_state,failure_code,failure_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "operations-attention-action:" + "2" * 64,
                "3" * 64,
                "operations-attention:" + "a" * 64,
                None,
                "owner.bhanu",
                "snooze",
                "snoozed",
                NOW.isoformat(),
                "4" * 64,
                None,
                None,
                None,
                None,
                "rejected",
                "invalid_expiry",
                "6" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="canonical identity is immutable"):
            connection.execute(
                "UPDATE operations_attention_findings SET owner='scheduler.changed' "
                "WHERE finding_id=?",
                ("operations-attention:" + "a" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="canonical identity is immutable"):
            connection.execute(
                "UPDATE operations_attention_findings SET opened_at=? WHERE finding_id=?",
                ((NOW + timedelta(seconds=1)).isoformat(), "operations-attention:" + "a" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError, match="canonical identity is immutable"):
            connection.execute(
                "UPDATE operations_attention_findings SET finding_id=? WHERE finding_id=?",
                (
                    "operations-attention:" + "7" * 64,
                    "operations-attention:" + "a" * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="predates finding"):
            connection.execute(
                "INSERT INTO operations_attention_lifecycle_events "
                "(event_id,finding_id,event_kind,from_lifecycle,to_lifecycle,evidence_fingerprint_sha256,"
                "occurred_at,receipt_sha256) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "operations-attention-event:" + "7" * 64,
                    "operations-attention:" + "a" * 64,
                    "detected",
                    None,
                    "open",
                    "a" * 64,
                    (NOW - timedelta(seconds=1)).isoformat(),
                    "8" * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="fingerprint does not match"):
            connection.execute(
                "INSERT INTO operations_attention_lifecycle_events "
                "(event_id,finding_id,event_kind,from_lifecycle,to_lifecycle,evidence_fingerprint_sha256,"
                "occurred_at,receipt_sha256) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "operations-attention-event:" + "9" * 64,
                    "operations-attention:" + "a" * 64,
                    "resolve",
                    "open",
                    "resolved",
                    "9" * 64,
                    NOW.isoformat(),
                    "8" * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="source lifecycle does not match"):
            connection.execute(
                "INSERT INTO operations_attention_lifecycle_events "
                "(event_id,finding_id,event_kind,from_lifecycle,to_lifecycle,evidence_fingerprint_sha256,"
                "occurred_at,receipt_sha256) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "operations-attention-event:" + "8" * 64,
                    "operations-attention:" + "a" * 64,
                    "resolve",
                    "acknowledged",
                    "resolved",
                    "a" * 64,
                    NOW.isoformat(),
                    "7" * 64,
                ),
            )
        connection.execute(
            "INSERT INTO operations_attention_findings "
            "(finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,"
            "evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "operations-attention:" + "6" * 64,
                "scheduler.critical",
                "runtime_health",
                "runtime_receipt",
                "6" * 64,
                "v1",
                "operations.runtime.pair.latest.json",
                "b" * 64,
                "critical",
                "degraded",
                "open",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO operations_attention_action_receipts "
            "(receipt_id,idempotency_key_sha256,finding_id,lifecycle_event_id,actor,action,"
            "result_lifecycle,occurred_at,request_sha256,reason_code,reason_reference_sha256,"
            "acknowledged_until,snoozed_until,result_state,failure_code,failure_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "operations-attention-action:" + "5" * 64,
                "4" * 64,
                "operations-attention:" + "6" * 64,
                None,
                "owner.bhanu",
                "acknowledge",
                "acknowledged",
                NOW.isoformat(),
                "3" * 64,
                None,
                None,
                None,
                None,
                "rejected",
                "prohibited_suppression",
                "2" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO operations_attention_findings "
            "(finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,"
            "evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "operations-attention:" + "9" * 64,
                "scheduler.other",
                "runtime_health",
                "runtime_receipt",
                "9" * 64,
                "v1",
                "operations.runtime.pair.latest.json",
                "b" * 64,
                "info",
                "healthy",
                "open",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO operations_attention_lifecycle_events "
            "(event_id,finding_id,event_kind,from_lifecycle,to_lifecycle,evidence_fingerprint_sha256,"
            "occurred_at,receipt_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (
                "operations-attention-event:" + "a" * 64,
                "operations-attention:" + "9" * 64,
                "acknowledge",
                "open",
                "acknowledged",
                "9" * 64,
                NOW.isoformat(),
                "b" * 64,
            ),
        )
        connection.execute(
            "UPDATE operations_attention_findings "
            "SET lifecycle='resolved', resolved_at=? WHERE finding_id=?",
            (NOW.isoformat(), "operations-attention:" + "9" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="transition is not allowed"):
            connection.execute(
                "UPDATE operations_attention_findings "
                "SET lifecycle='acknowledged', acknowledged_at=?, acknowledged_until=?, resolved_at=NULL "
                "WHERE finding_id=?",
                (
                    NOW.isoformat(),
                    (NOW + timedelta(hours=1)).isoformat(),
                    "operations-attention:" + "9" * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="event does not match"):
            connection.execute(
                "INSERT INTO operations_attention_action_receipts "
                "(receipt_id,idempotency_key_sha256,finding_id,lifecycle_event_id,actor,action,"
                "result_lifecycle,occurred_at,request_sha256,reason_code,reason_reference_sha256,"
                "acknowledged_until,snoozed_until,result_state,failure_code,failure_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "operations-attention-action:" + "b" * 64,
                    "c" * 64,
                    "operations-attention:" + "a" * 64,
                    "operations-attention-event:" + "a" * 64,
                    "owner.bhanu",
                    "acknowledge",
                    "acknowledged",
                    NOW.isoformat(),
                    "d" * 64,
                    "evidence_reviewed",
                    "e" * 64,
                    (NOW + timedelta(hours=1)).isoformat(),
                    None,
                    "applied",
                    None,
                    None,
                ),
            )
        connection.execute(
            "INSERT INTO operations_attention_repair_references "
            "(repair_reference_id,finding_id,reference_kind,reference_label,reference_sha256,recorded_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "operations-attention-repair:" + "1" * 64,
                "operations-attention:" + "a" * 64,
                "repair_receipt",
                "repair.receipt.v1",
                "2" * 64,
                NOW.isoformat(),
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE operations_attention_lifecycle_events SET to_lifecycle='resolved'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM operations_attention_action_receipts")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE operations_attention_repair_references SET reference_label='changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM operations_attention_findings")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO operations_attention_findings "
                "(finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,"
                "evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "operations-attention:" + "e" * 64,
                    "scheduler.collect_operations_runtime_observations",
                    "runtime_health",
                    "runtime_receipt",
                    "not-a-hash",
                    "v1",
                    "operations.runtime.pair.latest.json",
                    "b" * 64,
                    "warning",
                    "degraded",
                    "open",
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )

    command.downgrade(config, "0023_add_price_action_sensor_state")
    with sqlite3.connect(path) as connection:
        tables_after_downgrade = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "operations_attention_findings",
            "operations_attention_lifecycle_events",
            "operations_attention_action_receipts",
            "operations_attention_repair_references",
        }.isdisjoint(tables_after_downgrade)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0023_add_price_action_sensor_state",
        )
