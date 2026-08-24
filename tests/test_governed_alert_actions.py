"""Deterministic lifecycle actions over stable, persisted alert evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alerts.governed_actions import (
    GovernedAlertAction,
    GovernedAlertActionError,
    GovernedAlertActionType,
    execute_governed_alert_action,
)
from compute.thesis_episode_attention import AttentionState, get_attention

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    database = migrated_db(tmp_path / "governed-actions.db", target="head")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _seed_episode(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    ticker: str = "WIX",
    semantic_digit: str = "a",
) -> None:
    connection.execute(
        "INSERT INTO thesis_evaluation_episodes "
        "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
        "semantic_input_sha256,thesis_content_sha256,ruleset_sha256,"
        "evaluator_semantic_version,result_sha256,overall_status,"
        "provenance_completeness,first_evaluated_at,last_seen_at,last_checked_at,"
        "duplicate_run_count,rule_evaluations_json,created_at) "
        "VALUES (?,?,'forward_v1','{}',?,?,?,?,?,'warn','partial',?,?,?,0,'[]',?)",
        (
            episode_id,
            ticker,
            semantic_digit * 64,
            "b" * 64,
            "c" * 64,
            "test/v1",
            "d" * 64,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )


def _seed_alert(
    connection: sqlite3.Connection,
    *,
    alert_id: int = 1,
    episode_id: str | None = None,
    trigger_kind: str = "material_news",
    signature: str = "e" * 64,
) -> None:
    connection.execute(
        "INSERT INTO alerts "
        "(id,user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha,"
        "thesis_evaluation_episode_id,review_cycle_id) "
        "VALUES (?,'bhanu','WIX',?,?,'pending','{}',?,?,?)",
        (
            alert_id,
            trigger_kind,
            NOW.isoformat(),
            signature,
            episode_id,
            "initial" if episode_id else None,
        ),
    )


def _action(
    *,
    alert_id: int = 1,
    signature: str = "e" * 64,
    kind: GovernedAlertActionType = GovernedAlertActionType.REVIEW,
    key: str = "governed-action-1",
    **overrides: object,
) -> GovernedAlertAction:
    values: dict[str, object] = {
        "idempotency_key": key,
        "actor": "owner",
        "alert_id": alert_id,
        "source_ref": f"alert:{alert_id}",
        "evidence_ref": signature,
        "action_type": kind,
        "occurred_at": NOW,
    }
    values.update(overrides)
    return GovernedAlertAction.model_validate(values)


def test_review_receipt_is_evidence_bound_and_replay_is_idempotent(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_alert(connection)

    request = _action()
    first = execute_governed_alert_action(connection, request)
    replay = execute_governed_alert_action(connection, request)

    assert replay == first
    assert first.result_state == "reviewed"
    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"
    assert (
        connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0] == 1
    )
    with pytest.raises(GovernedAlertActionError, match="evidence_ref"):
        execute_governed_alert_action(connection, _action(signature="f" * 64, key="bad-evidence"))
    assert (
        connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0] == 1
    )


def test_acknowledge_delegates_to_the_episode_attention_core(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    _seed_alert(connection, episode_id="episode-1", trigger_kind="thesis_drift")

    receipt = execute_governed_alert_action(
        connection,
        _action(
            kind=GovernedAlertActionType.ACKNOWLEDGE,
            key="acknowledge-episode-1",
            note="Reviewed against the current evidence.",
        ),
    )

    attention = get_attention(connection, "episode-1", now=NOW)
    assert receipt.result_state == "acknowledged"
    assert attention.state is AttentionState.ACKNOWLEDGED
    assert attention.next_review_at is None
    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "dismissed"
    assert connection.execute("SELECT COUNT(*) FROM thesis_ledger_entries").fetchone()[0] == 0


def test_defer_delegates_to_the_episode_attention_core(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    _seed_alert(connection, episode_id="episode-1", trigger_kind="thesis_drift")

    receipt = execute_governed_alert_action(
        connection,
        _action(
            kind=GovernedAlertActionType.DEFER,
            key="defer-episode-1",
            note="Wait for next filing.",
            defer_until=NOW + timedelta(days=14),
        ),
    )

    attention = get_attention(connection, "episode-1", now=NOW)
    assert receipt.result_state == "deferred"
    assert attention.state is AttentionState.ACKNOWLEDGED
    assert attention.next_review_at == NOW + timedelta(days=14)
    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "dismissed"
    assert connection.execute("SELECT COUNT(*) FROM thesis_ledger_entries").fetchone()[0] == 0


def test_dismiss_requires_a_reason_and_never_records_a_failed_attempt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_alert(connection)

    with pytest.raises(ValueError, match="dismiss_reason"):
        _action(kind=GovernedAlertActionType.DISMISS)
    assert (
        connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0] == 0
    )

    receipt = execute_governed_alert_action(
        connection,
        _action(
            kind=GovernedAlertActionType.DISMISS,
            key="dismiss-with-reason",
            dismiss_reason="Not material to the current thesis.",
        ),
    )
    row = connection.execute("SELECT status,dismiss_reason FROM alerts WHERE id=1").fetchone()
    assert receipt.result_state == "dismissed"
    assert tuple(row) == ("dismissed", "Not material to the current thesis.")


def test_receipt_failure_rolls_back_a_dismissal_transition(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_alert(connection)
    connection.execute(
        "CREATE TRIGGER abort_governed_alert_receipt BEFORE INSERT "
        "ON governed_alert_action_receipts BEGIN "
        "SELECT RAISE(ABORT, 'receipt write rejected'); END"
    )

    with pytest.raises(GovernedAlertActionError, match="could not append"):
        execute_governed_alert_action(
            connection,
            _action(
                kind=GovernedAlertActionType.DISMISS,
                key="receipt-failure-dismiss",
                dismiss_reason="No longer material.",
            ),
        )

    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"
    assert (
        connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0] == 0
    )


def test_receipt_failure_rolls_back_an_acknowledgement_transition(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    _seed_alert(connection, episode_id="episode-1", trigger_kind="thesis_drift")
    connection.execute(
        "CREATE TRIGGER abort_governed_alert_receipt BEFORE INSERT "
        "ON governed_alert_action_receipts BEGIN "
        "SELECT RAISE(ABORT, 'receipt write rejected'); END"
    )

    with pytest.raises(GovernedAlertActionError, match="could not append"):
        execute_governed_alert_action(
            connection,
            _action(
                kind=GovernedAlertActionType.ACKNOWLEDGE,
                key="receipt-failure-acknowledge",
            ),
        )

    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"
    assert get_attention(connection, "episode-1", now=NOW).state is AttentionState.UNREVIEWED
    assert (
        connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0] == 0
    )


def test_requires_a_caller_owned_transaction(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_alert(connection)
    connection.commit()

    with pytest.raises(GovernedAlertActionError, match="active transaction"):
        execute_governed_alert_action(connection, _action())

    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"


def test_complete_requires_a_linked_episode_and_existing_decision(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    _seed_alert(connection, episode_id="episode-1", trigger_kind="thesis_drift")
    connection.execute(
        "INSERT INTO decisions (ticker,recommendation_kind,made_at,created_at,decided_by) "
        "VALUES ('WIX','hold',?,?, 'owner')",
        (NOW.isoformat(), NOW.isoformat()),
    )
    decision_id = int(connection.execute("SELECT MAX(id) FROM decisions").fetchone()[0])

    receipt = execute_governed_alert_action(
        connection,
        _action(
            kind=GovernedAlertActionType.COMPLETE,
            key="complete-episode-1",
            decision_id=decision_id,
        ),
    )

    assert receipt.result_state == "completed"
    assert get_attention(connection, "episode-1", now=NOW).state is AttentionState.ACTED_ON
    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "dismissed"


@pytest.mark.parametrize("minutes_after", (0, 1), ids=("same_batch", "later"))
def test_supersede_accepts_a_same_ticker_same_batch_or_later_episode(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    minutes_after: int,
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-old")
    _seed_episode(connection, "episode-new", semantic_digit="b")
    if minutes_after:
        replacement_time = (NOW + timedelta(minutes=minutes_after)).isoformat()
        connection.execute(
            "UPDATE thesis_evaluation_episodes SET first_evaluated_at=?,last_seen_at=?,last_checked_at=? "
            "WHERE episode_id='episode-new'",
            (replacement_time, replacement_time, replacement_time),
        )
    _seed_alert(connection, episode_id="episode-old", trigger_kind="thesis_drift")

    receipt = execute_governed_alert_action(
        connection,
        _action(
            kind=GovernedAlertActionType.SUPERSEDE,
            key="supersede-episode-old",
            replacement_episode_id="episode-new",
        ),
    )

    old = get_attention(connection, "episode-old", now=NOW)
    assert receipt.result_state == "superseded"
    assert old.state is AttentionState.SUPERSEDED
    assert old.superseded_by_episode_id == "episode-new"


def test_rejects_double_submission_with_a_different_request_without_mutation(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_alert(connection)
    execute_governed_alert_action(connection, _action(key="same-key"))

    with pytest.raises(GovernedAlertActionError, match="idempotency"):
        execute_governed_alert_action(
            connection,
            _action(key="same-key", kind=GovernedAlertActionType.DISMISS, dismiss_reason="Wrong"),
        )
    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"


def test_rejects_thesis_only_actions_for_an_unlinked_alert_without_a_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_alert(connection)

    with pytest.raises(GovernedAlertActionError, match="thesis_drift"):
        execute_governed_alert_action(
            connection,
            _action(kind=GovernedAlertActionType.ACKNOWLEDGE, key="invalid-acknowledge"),
        )
    assert connection.execute("SELECT status FROM alerts WHERE id=1").fetchone()[0] == "pending"
    assert (
        connection.execute("SELECT COUNT(*) FROM governed_alert_action_receipts").fetchone()[0] == 0
    )
