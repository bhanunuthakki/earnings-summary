"""Transactional persistence contract for the Operations attention action core."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import operations.attention_store as attention_store
from operations.attention import (
    AttentionReason,
    AttentionReasonCode,
    EvidenceIdentity,
    EvidenceKind,
    FindingKind,
    derive_finding_id,
)
from operations.attention_store import (
    ActionFailureCode,
    ActionResultState,
    OperatorAction,
    OperatorActionRequest,
    execute_operator_action,
)
from sqlite_runtime import SQLiteConnectionRole

NOW = datetime(2026, 8, 24, 19, 0, tzinfo=UTC)
FINGERPRINT = "b" * 64
FINDING_ID = derive_finding_id(
    owner="scheduler.collect_operations_runtime_observations",
    kind=FindingKind.RUNTIME_HEALTH,
    evidence=EvidenceIdentity(
        kind=EvidenceKind.RUNTIME_RECEIPT,
        fingerprint_sha256=FINGERPRINT,
        version="v1",
        reference="operations.runtime.pair.latest.json",
        reference_sha256="c" * 64,
    ),
)


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    path = migrated_db(tmp_path / "attention-actions.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO operations_attention_findings(
                finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,
                evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                FINDING_ID,
                "scheduler.collect_operations_runtime_observations",
                "runtime_health",
                "runtime_receipt",
                FINGERPRINT,
                "v1",
                "operations.runtime.pair.latest.json",
                "c" * 64,
                "warning",
                "degraded",
                "open",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    return path


def _request(
    *,
    action: OperatorAction = OperatorAction.ACKNOWLEDGE,
    key: str = "operator-action-1",
    fingerprint: str = FINGERPRINT,
    at: datetime = NOW + timedelta(minutes=1),
) -> OperatorActionRequest:
    return OperatorActionRequest(
        finding_id=FINDING_ID,
        action=action,
        evidence_fingerprint_sha256=fingerprint,
        idempotency_key=key,
        occurred_at=at,
        reason=(
            AttentionReason(
                code=(
                    AttentionReasonCode.EVIDENCE_REVIEWED
                    if action is OperatorAction.ACKNOWLEDGE
                    else AttentionReasonCode.INVESTIGATION_IN_PROGRESS
                ),
                reference_sha256="d" * 64,
            )
            if action in {OperatorAction.ACKNOWLEDGE, OperatorAction.SNOOZE}
            else None
        ),
        acknowledge_until=(at + timedelta(hours=1))
        if action is OperatorAction.ACKNOWLEDGE
        else None,
        snooze_until=(at + timedelta(hours=1)) if action is OperatorAction.SNOOZE else None,
    )


def test_applies_one_action_with_atomic_projection_event_and_safe_receipt(db_path: Path) -> None:
    request = _request()
    assert "operator-action-1" not in repr(request)
    receipt = execute_operator_action(request, actor="owner.bhanu", db_path=db_path)

    assert receipt.result_state is ActionResultState.APPLIED
    assert receipt.durable
    assert receipt.lifecycle_event_id is not None
    assert receipt.failure_code is None
    with sqlite3.connect(db_path) as conn:
        finding = conn.execute(
            "SELECT lifecycle,acknowledged_at,acknowledged_until FROM operations_attention_findings"
        ).fetchone()
        assert finding is not None
        assert finding[0] == "acknowledged"
        assert finding[1] == (NOW + timedelta(minutes=1)).isoformat()
        assert finding[2] == (NOW + timedelta(hours=1, minutes=1)).isoformat()
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_lifecycle_events"
        ).fetchone() == (1,)
        persisted = conn.execute(
            "SELECT actor,idempotency_key_sha256,request_sha256,result_state FROM "
            "operations_attention_action_receipts"
        ).fetchone()
        assert persisted is not None
        assert persisted[0] == "owner.bhanu"
        assert persisted[1] == receipt.idempotency_key_sha256
        assert persisted[2] == receipt.request_sha256
        assert persisted[3] == "applied"
        dumped = " ".join(str(value) for value in persisted)
        assert "operator-action-1" not in dumped


def test_exact_replay_returns_original_and_same_key_change_conflicts(db_path: Path) -> None:
    original = execute_operator_action(_request(), actor="owner.bhanu", db_path=db_path)
    replay = execute_operator_action(_request(), actor="owner.bhanu", db_path=db_path)
    conflict = execute_operator_action(
        _request(key="operator-action-1", at=NOW + timedelta(minutes=2)),
        actor="owner.bhanu",
        db_path=db_path,
    )

    assert replay.result_state is ActionResultState.REPLAYED
    assert replay.receipt_id == original.receipt_id
    assert conflict.result_state is ActionResultState.CONFLICT
    assert conflict.failure_code is ActionFailureCode.CONFLICT
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_action_receipts"
        ).fetchone() == (1,)


def test_rejects_distinct_key_action_that_predates_persisted_projection(db_path: Path) -> None:
    acknowledged_at = NOW + timedelta(minutes=3)
    acknowledged = execute_operator_action(
        _request(key="later-acknowledgement", at=acknowledged_at),
        actor="owner.bhanu",
        db_path=db_path,
    )
    predating_snooze = execute_operator_action(
        _request(
            action=OperatorAction.SNOOZE,
            key="earlier-snooze",
            at=NOW + timedelta(minutes=2),
        ),
        actor="owner.bhanu",
        db_path=db_path,
    )

    assert acknowledged.result_state is ActionResultState.APPLIED
    assert predating_snooze.result_state is ActionResultState.REJECTED
    assert predating_snooze.failure_code is ActionFailureCode.CONFLICT
    assert predating_snooze.durable
    with sqlite3.connect(db_path) as conn:
        projection = conn.execute(
            "SELECT lifecycle,updated_at FROM operations_attention_findings WHERE finding_id=?",
            (FINDING_ID,),
        ).fetchone()
        assert projection == ("acknowledged", acknowledged_at.isoformat())
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_lifecycle_events"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT result_state,failure_code FROM operations_attention_action_receipts "
            "WHERE idempotency_key_sha256=?",
            (predating_snooze.idempotency_key_sha256,),
        ).fetchone() == ("rejected", "conflict")


def test_applies_snooze_then_healthy_resolve(db_path: Path) -> None:
    snoozed_at = NOW + timedelta(minutes=1)
    snoozed = execute_operator_action(
        _request(action=OperatorAction.SNOOZE, key="snooze", at=snoozed_at),
        actor="owner.bhanu",
        db_path=db_path,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE operations_attention_findings SET health='healthy', updated_at=? WHERE finding_id=?",
            ((NOW + timedelta(minutes=2)).isoformat(), FINDING_ID),
        )
    resolved_at = NOW + timedelta(minutes=3)
    resolved = execute_operator_action(
        _request(action=OperatorAction.RESOLVE, key="resolve", at=resolved_at),
        actor="owner.bhanu",
        db_path=db_path,
    )

    assert snoozed.result_state is ActionResultState.APPLIED
    assert resolved.result_state is ActionResultState.APPLIED
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT lifecycle,resolved_at,updated_at FROM operations_attention_findings "
            "WHERE finding_id=?",
            (FINDING_ID,),
        ).fetchone() == ("resolved", resolved_at.isoformat(), resolved_at.isoformat())
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_lifecycle_events"
        ).fetchone() == (2,)


def test_stale_evidence_and_prohibited_suppression_are_durable_rejections(db_path: Path) -> None:
    stale = execute_operator_action(
        _request(key="stale", fingerprint="e" * 64), actor="owner.bhanu", db_path=db_path
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE operations_attention_findings SET severity='critical' WHERE finding_id=?",
            (FINDING_ID,),
        )
    prohibited = execute_operator_action(
        _request(key="critical"), actor="owner.bhanu", db_path=db_path
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE operations_attention_findings SET severity='warning', health='invalid' "
            "WHERE finding_id=?",
            (FINDING_ID,),
        )
    unhealthy_resolve = execute_operator_action(
        _request(action=OperatorAction.RESOLVE, key="unhealthy-resolve"),
        actor="owner.bhanu",
        db_path=db_path,
    )

    assert stale.result_state is ActionResultState.REJECTED
    assert stale.failure_code is ActionFailureCode.CONFLICT
    assert stale.durable
    assert prohibited.result_state is ActionResultState.REJECTED
    assert prohibited.failure_code is ActionFailureCode.PROHIBITED_SUPPRESSION
    assert unhealthy_resolve.result_state is ActionResultState.REJECTED
    assert unhealthy_resolve.failure_code is ActionFailureCode.INVALID_TRANSITION
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT lifecycle FROM operations_attention_findings").fetchone() == (
            "open",
        )
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_lifecycle_events"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_action_receipts"
        ).fetchone() == (3,)


def test_operator_registry_rejects_reconciler_only_verbs() -> None:
    assert {action.value for action in attention_store.OPERATOR_ACTIONS} == {
        "acknowledge",
        "snooze",
        "resolve",
    }
    with pytest.raises(ValueError):
        OperatorAction("detected")


def test_failure_after_projection_update_rolls_back_every_mutation(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_receipt(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise sqlite3.OperationalError("injected receipt failure")

    monkeypatch.setattr(attention_store, "_insert_receipt", fail_receipt)
    with pytest.raises(sqlite3.OperationalError, match="injected receipt failure"):
        execute_operator_action(_request(), actor="owner.bhanu", db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT lifecycle FROM operations_attention_findings").fetchone() == (
            "open",
        )
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_lifecycle_events"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_action_receipts"
        ).fetchone() == (0,)


def test_writer_uses_schema_preflight_and_unknown_finding_fails_closed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = attention_store.connect_sqlite
    seen: list[tuple[object, object]] = []

    def tracked_connect(
        path: Path | str,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        seen.append((role, schema_preflight))
        return real_connect(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(attention_store, "connect_sqlite", tracked_connect)
    unknown = OperatorActionRequest(
        finding_id="operations-attention:" + "f" * 64,
        action=OperatorAction.RESOLVE,
        evidence_fingerprint_sha256=FINGERPRINT,
        idempotency_key="unknown-finding",
        occurred_at=NOW + timedelta(minutes=1),
    )
    receipt = execute_operator_action(unknown, actor="owner.bhanu", db_path=db_path)

    assert seen == [(attention_store.SQLiteConnectionRole.WRITER, True)]
    assert receipt.result_state is ActionResultState.REJECTED
    assert not receipt.durable
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_action_receipts"
        ).fetchone() == (0,)


def test_concurrent_same_request_serializes_to_one_apply_and_one_replay(db_path: Path) -> None:
    barrier = threading.Barrier(2)
    results: list[ActionResultState] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                execute_operator_action(
                    _request(key="parallel"), actor="owner.bhanu", db_path=db_path
                ).result_state
            )
        except BaseException as error:  # pragma: no cover - failure is asserted below
            failures.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not failures
    assert sorted(results) == [ActionResultState.APPLIED, ActionResultState.REPLAYED]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_action_receipts"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_lifecycle_events"
        ).fetchone() == (1,)
