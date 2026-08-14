"""Lifecycle and anti-nag contracts for thesis evaluation episodes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alerts.store import dismiss_alert, fire_alert
from compute.thesis_episode_attention import (
    AttentionError,
    AttentionState,
    DeliveryStatus,
    acknowledge_episode,
    act_on_episode,
    complete_delivery,
    deliver_due_episode_alerts,
    deliver_episode_alert,
    ensure_episode_alert,
    get_attention,
    reserve_delivery,
    should_prompt,
    supersede_prior,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    database = tmp_path / "attention.db"
    migrated_db(database, target="head")
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


def test_acknowledgement_is_global_quiet_without_writing_thesis_ledger(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    connection.execute(
        "INSERT INTO alerts "
        "(user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha,"
        "thesis_evaluation_episode_id,review_cycle_id) "
        "VALUES ('bhanu','WIX','thesis_drift',?,'pending','{}',?,?,'initial')",
        (NOW.isoformat(), "1" * 64, "episode-1"),
    )
    connection.execute(
        "INSERT INTO coach_pings "
        '(class_,"key",ticker,body,status,created_at,updated_at,'
        "thesis_evaluation_episode_id,review_cycle_id) "
        "VALUES ('falsifier_breach','episode-1','WIX','review','sent',?,?,?,'initial')",
        (NOW.isoformat(), NOW.isoformat(), "episode-1"),
    )
    ledger_before = connection.execute("SELECT COUNT(*) FROM thesis_ledger_entries").fetchone()[0]

    due = NOW + timedelta(days=30)
    attention = acknowledge_episode(
        connection,
        "episode-1",
        acknowledged_at=NOW,
        note="Reviewed; no action yet.",
        next_review_at=due,
    )
    connection.commit()

    assert attention.state is AttentionState.ACKNOWLEDGED
    assert attention.actionable is False
    assert (
        should_prompt(
            connection,
            "episode-1",
            channel="telegram",
            surface="coach",
            now=NOW,
        )
        is False
    )
    assert connection.execute("SELECT status FROM alerts").fetchone()[0] == "dismissed"
    assert connection.execute("SELECT status FROM coach_pings").fetchone()[0] == "acted"
    assert (
        connection.execute("SELECT COUNT(*) FROM thesis_ledger_entries").fetchone()[0]
        == ledger_before
    )


def test_due_review_cycle_delivers_once_and_failed_send_can_retry(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    due = NOW + timedelta(days=7)
    acknowledge_episode(
        connection,
        "episode-1",
        acknowledged_at=NOW,
        next_review_at=due,
    )

    cycle_time = due + timedelta(seconds=1)
    attention = get_attention(connection, "episode-1", now=cycle_time)
    assert attention.actionable is True
    assert attention.review_cycle_id == f"review:{due.isoformat()}"
    first = reserve_delivery(
        connection,
        "episode-1",
        channel="telegram",
        surface="coach",
        reserved_at=cycle_time,
    )
    assert first is not None and first.claimed is True
    duplicate = reserve_delivery(
        connection,
        "episode-1",
        channel="telegram",
        surface="coach",
        reserved_at=cycle_time,
    )
    assert duplicate is not None and duplicate.claimed is False

    complete_delivery(
        connection,
        first.receipt_id,
        attempt_token=first.attempt_token,
        status=DeliveryStatus.FAILED,
        completed_at=cycle_time,
        failure_reason="provider unavailable",
    )
    retry = reserve_delivery(
        connection,
        "episode-1",
        channel="telegram",
        surface="coach",
        reserved_at=cycle_time + timedelta(minutes=5),
    )
    assert retry is not None and retry.claimed is True
    complete_delivery(
        connection,
        retry.receipt_id,
        attempt_token=retry.attempt_token,
        status=DeliveryStatus.DELIVERED,
        completed_at=cycle_time + timedelta(minutes=6),
        external_ref="message-1",
    )
    connection.commit()

    assert (
        should_prompt(
            connection,
            "episode-1",
            channel="telegram",
            surface="coach",
            now=cycle_time + timedelta(minutes=7),
        )
        is False
    )


def test_expired_reservation_is_reclaimed_and_old_attempt_cannot_complete(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    first = reserve_delivery(
        connection,
        "episode-1",
        channel="telegram",
        surface="coach",
        reserved_at=NOW,
        lease_for=timedelta(minutes=5),
    )
    assert first is not None and first.claimed is True
    assert (
        should_prompt(
            connection,
            "episode-1",
            channel="telegram",
            surface="coach",
            now=NOW + timedelta(minutes=4),
        )
        is False
    )
    assert (
        should_prompt(
            connection,
            "episode-1",
            channel="telegram",
            surface="coach",
            now=NOW + timedelta(minutes=6),
        )
        is True
    )

    retry = reserve_delivery(
        connection,
        "episode-1",
        channel="telegram",
        surface="coach",
        reserved_at=NOW + timedelta(minutes=6),
    )
    assert retry is not None and retry.claimed is True
    assert retry.attempt_count == 2
    assert retry.attempt_token != first.attempt_token
    with pytest.raises(AttentionError, match="stale"):
        complete_delivery(
            connection,
            retry.receipt_id,
            attempt_token=first.attempt_token,
            status=DeliveryStatus.DELIVERED,
            completed_at=NOW + timedelta(minutes=7),
        )
    complete_delivery(
        connection,
        retry.receipt_id,
        attempt_token=retry.attempt_token,
        status=DeliveryStatus.DELIVERED,
        completed_at=NOW + timedelta(minutes=7),
        external_ref="message-2",
    )


def test_due_review_scan_uses_real_delivery_receipt_and_is_once_per_cycle(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    due = NOW + timedelta(days=7)
    acknowledge_episode(connection, "episode-1", acknowledged_at=NOW, next_review_at=due)

    delivered_at = due + timedelta(seconds=1)
    assert deliver_due_episode_alerts(connection, now=delivered_at) == 1
    assert deliver_due_episode_alerts(connection, now=delivered_at + timedelta(minutes=1)) == 0
    receipt = connection.execute(
        "SELECT status,external_ref,attempt_count FROM thesis_evaluation_episode_delivery_receipts"
    ).fetchone()
    assert receipt is not None
    assert tuple(receipt) == ("delivered", "alert:1", 1)
    alert_summary = connection.execute(
        "SELECT COUNT(*),review_cycle_id FROM alerts WHERE status='pending'"
    ).fetchone()
    assert alert_summary is not None
    assert tuple(alert_summary) == (1, f"review:{due.isoformat()}")


def test_local_inbox_delivery_keeps_ok_episode_quiet(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-ok")
    connection.execute(
        "UPDATE thesis_evaluation_episodes SET overall_status='ok' WHERE episode_id='episode-ok'"
    )
    assert deliver_episode_alert(connection, "episode-ok", delivered_at=NOW) is None
    assert tuple(connection.execute("SELECT COUNT(*) FROM alerts").fetchone()) == (0,)
    assert tuple(
        connection.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_delivery_receipts"
        ).fetchone()
    ) == (0,)


def test_new_episode_supersedes_unresolved_prior_but_not_acted_episode(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-old", semantic_digit="a")
    _seed_episode(connection, "episode-acted", semantic_digit="b")
    connection.execute(
        "INSERT INTO decisions "
        "(ticker,recommendation_kind,made_at,created_at,decided_by) "
        "VALUES ('WIX','hold',?,?,'owner')",
        (NOW.isoformat(), NOW.isoformat()),
    )
    decision_id = connection.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    assert decision_id is not None
    act_on_episode(
        connection,
        "episode-acted",
        decision_id=int(decision_id),
        acted_at=NOW,
    )
    _seed_episode(connection, "episode-new", semantic_digit="c")

    assert (
        supersede_prior(
            connection,
            new_episode_id="episode-new",
            superseded_at=NOW + timedelta(minutes=1),
        )
        == 1
    )
    connection.commit()

    old = get_attention(connection, "episode-old", now=NOW)
    acted = get_attention(connection, "episode-acted", now=NOW)
    assert old.state is AttentionState.SUPERSEDED
    assert old.superseded_by_episode_id == "episode-new"
    assert acted.state is AttentionState.ACTED_ON


def test_attention_rejects_invalid_due_date_and_conflicting_completion(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")
    with pytest.raises(AttentionError, match="after"):
        acknowledge_episode(
            connection,
            "episode-1",
            acknowledged_at=NOW,
            next_review_at=NOW,
        )


def test_linked_alert_dismiss_acknowledges_episode_and_fire_is_idempotent(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = tmp_path / "linked-alert.db"
    migrated_db(database, target="head")
    with sqlite3.connect(database) as connection:
        _seed_episode(connection, "episode-1")
        connection.commit()

    first = fire_alert(
        ticker="WIX",
        trigger_kind="thesis_drift",
        fired_at=NOW,
        evidence_json='{"status":"warn"}',
        signature_sha="1" * 64,
        thesis_evaluation_episode_id="episode-1",
        review_cycle_id="initial",
        db_path=database,
    )
    duplicate = fire_alert(
        ticker="WIX",
        trigger_kind="thesis_drift",
        fired_at=NOW + timedelta(minutes=1),
        evidence_json='{"status":"warn"}',
        signature_sha="1" * 64,
        thesis_evaluation_episode_id="episode-1",
        review_cycle_id="initial",
        db_path=database,
    )
    assert duplicate.id == first.id

    dismissed = dismiss_alert(first.id, db_path=database, reason="Reviewed")

    assert dismissed.status == "dismissed"
    assert dismissed.thesis_evaluation_episode_id == "episode-1"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT attention_state,acknowledgement_note "
            "FROM thesis_evaluation_episodes WHERE episode_id='episode-1'"
        ).fetchone() == ("acknowledged", "Reviewed")


def test_episode_alert_is_one_carrier_per_actionable_review_cycle(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_episode(connection, "episode-1")

    first = ensure_episode_alert(connection, "episode-1", fired_at=NOW)
    second = ensure_episode_alert(
        connection,
        "episode-1",
        fired_at=NOW + timedelta(minutes=1),
    )

    assert first is not None and second == first
    assert tuple(
        connection.execute(
            "SELECT COUNT(*),thesis_evaluation_episode_id,review_cycle_id "
            "FROM alerts WHERE trigger_kind='thesis_drift'"
        ).fetchone()
    ) == (1, "episode-1", "initial")


def test_linked_coach_ack_delegates_but_brief_delivery_does_not(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from research.governor import mark_ping_acted, mark_pings_briefed

    database = tmp_path / "linked-coach.db"
    migrated_db(database, target="head")
    with sqlite3.connect(database) as connection:
        _seed_episode(connection, "episode-1", semantic_digit="a")
        _seed_episode(connection, "episode-2", semantic_digit="b")
        first = connection.execute(
            "INSERT INTO coach_pings "
            '(class_,"key",ticker,body,status,created_at,updated_at,'
            "thesis_evaluation_episode_id,review_cycle_id) "
            "VALUES ('falsifier_breach','episode-1','WIX','review','sent',?,?,"
            "'episode-1','initial')",
            (NOW.isoformat(), NOW.isoformat()),
        )
        second = connection.execute(
            "INSERT INTO coach_pings "
            '(class_,"key",ticker,body,status,created_at,updated_at,'
            "thesis_evaluation_episode_id,review_cycle_id) "
            "VALUES ('post_mortem','episode-2','WIX','review','routed_to_brief',?,?,"
            "'episode-2','initial')",
            (NOW.isoformat(), NOW.isoformat()),
        )
        first_id = int(first.lastrowid or 0)
        second_id = int(second.lastrowid or 0)
        connection.commit()

    assert mark_ping_acted(first_id, db_path=database) is True
    assert mark_pings_briefed([second_id], db_path=database) == 1

    with sqlite3.connect(database) as connection:
        states = dict(
            connection.execute(
                "SELECT episode_id,attention_state FROM thesis_evaluation_episodes"
            ).fetchall()
        )
    assert states == {"episode-1": "acknowledged", "episode-2": "unreviewed"}
