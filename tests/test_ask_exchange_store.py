"""Durable, idempotent Ask exchange state-machine contracts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from ask.exchange_store import (
    ExchangeArtifactsV1,
    ExchangeConflictError,
    ExchangeStateError,
    PendingExchangeError,
    RevisionConflictError,
    SessionContextConflictError,
    SessionContextV1,
    StoredExchangeDataError,
    begin_exchange,
    complete_exchange,
    fail_exchange,
    get_exchange,
    get_session_context,
    hash_request_payload,
    put_session_context,
)


def _database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    name: str = "ask-exchange.db",
) -> Path:
    db_path = migrated_db(tmp_path / name, target="head")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO ask_sessions(id,scope,title,created_at,updated_at) "
            "VALUES ('session-1','portfolio','','2026-08-09T12:00:00',"
            "'2026-08-09T12:00:00')"
        )
        connection.commit()
    return db_path


def _context() -> SessionContextV1:
    return SessionContextV1(
        company_ticker="nu",
        coverage_role_at_creation="portfolio",
        lifecycle_at_creation="active",
        category="thesis",
        thesis_ref="thesis:NU:core",
        thesis_version="7",
        report_date=date(2026, 8, 8),
        origin_key="company-desk:NU",
    )


def test_boundary_models_forbid_unknown_fields_and_bound_artifact_links() -> None:
    assert _context().company_ticker == "NU"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        SessionContextV1.model_validate(
            {
                "coverage_role_at_creation": "portfolio",
                "lifecycle_at_creation": "active",
                "category": "general",
                "invented": True,
            }
        )

    with pytest.raises(ValidationError):
        ExchangeArtifactsV1(source_links=[f"source:{index}" for index in range(101)])


def test_session_context_is_one_typed_historical_snapshot_per_session(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    record = put_session_context("session-1", _context(), db_path=db_path)

    assert record.revision == 0
    assert record.context == _context()
    assert put_session_context("session-1", _context(), db_path=db_path) == record
    assert get_session_context("session-1", db_path=db_path) == record

    changed = _context().model_copy(update={"category": "kpi"})
    with pytest.raises(SessionContextConflictError):
        put_session_context("session-1", changed, db_path=db_path)


def test_session_context_round_trips_immutable_evaluation_coordinates(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    context = _context().model_copy(
        update={"evaluation_candidate_id": 17, "evaluation_instrument_type": "stock"}
    )

    put_session_context("session-1", context, db_path=db_path)

    restored = get_session_context("session-1", db_path=db_path)
    assert restored is not None
    assert restored.context.evaluation_candidate_id == 17
    assert restored.context.evaluation_instrument_type == "stock"


def test_stored_context_json_is_schema_and_hash_validated(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    put_session_context("session-1", _context(), db_path=db_path)
    invalid_json = '{"invented":true}'
    invalid_hash = hash_request_payload({"invented": True})
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE ask_session_contexts SET context_json=?,context_sha256=? "
            "WHERE session_id='session-1'",
            (invalid_json, invalid_hash),
        )
        connection.commit()

    with pytest.raises(StoredExchangeDataError, match="context is invalid"):
        get_session_context("session-1", db_path=db_path)


def test_begin_is_atomic_idempotent_and_allows_only_one_pending_exchange(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    put_session_context("session-1", _context(), db_path=db_path)
    request_hash = hash_request_payload({"message": "What changed?", "surface": "desk"})

    started = begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=request_hash,
        user_text="What changed?",
        expected_revision=0,
        db_path=db_path,
    )
    same_pending = begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=request_hash,
        user_text="What changed?",
        expected_revision=0,
        db_path=db_path,
    )

    assert started.disposition == "started"
    assert started.session_revision == 1
    assert same_pending.disposition == "pending"
    assert same_pending.exchange.user_turn_id == started.exchange.user_turn_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ask_turns WHERE session_id='session-1' AND role='user'"
        ).fetchone() == (1,)

    with pytest.raises(ExchangeConflictError, match="payload hash"):
        begin_exchange(
            session_id="session-1",
            request_id="request-1",
            payload_sha256="f" * 64,
            user_text="A different payload",
            expected_revision=1,
            db_path=db_path,
        )

    with pytest.raises(PendingExchangeError):
        begin_exchange(
            session_id="session-1",
            request_id="request-2",
            payload_sha256=hash_request_payload({"message": "Second"}),
            user_text="Second",
            expected_revision=1,
            db_path=db_path,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ask_turns WHERE session_id='session-1' AND role='user'"
        ).fetchone() == (1,)


def test_complete_writes_assistant_and_artifacts_before_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    put_session_context("session-1", _context(), db_path=db_path)
    request_hash = hash_request_payload({"message": "Show the thesis"})
    begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=request_hash,
        user_text="Show the thesis",
        expected_revision=0,
        db_path=db_path,
    )
    artifacts = ExchangeArtifactsV1(
        route="thesis",
        view_spec={"kind": "thesis_card", "ticker": "NU"},
        proposal_ref="proposal:123",
        source_links=["source:filing:NU:2026Q2"],
        fact_links=["fact:NU:revenue:2026Q2"],
    )

    completed = complete_exchange(
        request_id="request-1",
        assistant_text="The thesis remains intact.",
        artifacts=artifacts,
        expected_revision=1,
        citations=[{"source": "source:filing:NU:2026Q2"}],
        model="test-model",
        db_path=db_path,
    )

    assert completed.status == "completed"
    assert completed.session_revision == 2
    assert completed.artifacts == artifacts
    assert completed.assistant_turn_id is not None
    assert get_exchange("request-1", db_path=db_path) == completed

    replayed = begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=request_hash,
        user_text="Show the thesis",
        expected_revision=0,
        db_path=db_path,
    )
    assert replayed.disposition == "replayed"
    assert replayed.exchange == completed
    assert replayed.session_revision == 2
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT role,text FROM ask_turns WHERE session_id='session-1' ORDER BY id"
        ).fetchall() == [
            ("user", "Show the thesis"),
            ("assistant", "The thesis remains intact."),
        ]
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM ask_sessions WHERE id='session-1'")
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM ask_session_contexts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ask_exchanges").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ask_exchange_artifacts").fetchone() == (0,)


def test_complete_rejects_disagreeing_trace_identities_before_commit(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    put_session_context("session-1", _context(), db_path=db_path)
    begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=hash_request_payload({"message": "Show the thesis"}),
        user_text="Show the thesis",
        expected_revision=0,
        db_path=db_path,
    )
    artifact_trace = "ask-grounding:" + ("a" * 64)
    turn_trace = "ask-grounding:" + ("b" * 64)

    with pytest.raises(ExchangeStateError, match="trace identities disagree"):
        complete_exchange(
            request_id="request-1",
            assistant_text="The thesis remains intact.",
            artifacts=ExchangeArtifactsV1(grounding_trace_id=artifact_trace),
            grounding_trace_id=turn_trace,
            expected_revision=1,
            db_path=db_path,
        )

    pending = get_exchange("request-1", db_path=db_path)
    assert pending is not None
    assert pending.status == "pending"


def test_revision_cas_and_fail_transition_release_the_pending_slot(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
    put_session_context("session-1", _context(), db_path=db_path)
    begin_exchange(
        session_id="session-1",
        request_id="request-1",
        payload_sha256=hash_request_payload({"message": "First"}),
        user_text="First",
        expected_revision=0,
        db_path=db_path,
    )

    with pytest.raises(RevisionConflictError):
        complete_exchange(
            request_id="request-1",
            assistant_text="Stale answer",
            artifacts=ExchangeArtifactsV1(),
            expected_revision=0,
            db_path=db_path,
        )
    pending = get_exchange("request-1", db_path=db_path)
    assert pending is not None
    assert pending.status == "pending"

    failed = fail_exchange(
        request_id="request-1",
        error_code="upstream_unavailable",
        expected_revision=1,
        db_path=db_path,
    )
    assert failed.status == "failed"
    assert failed.session_revision == 2

    started = begin_exchange(
        session_id="session-1",
        request_id="request-2",
        payload_sha256=hash_request_payload({"message": "Retry"}),
        user_text="Retry",
        expected_revision=2,
        db_path=db_path,
    )
    assert started.disposition == "started"
    assert started.session_revision == 3

    with pytest.raises(ExchangeStateError):
        begin_exchange(
            session_id="session-1",
            request_id="request-1",
            payload_sha256=hash_request_payload({"message": "First"}),
            user_text="First",
            expected_revision=3,
            db_path=db_path,
        )
