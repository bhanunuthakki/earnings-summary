"""Typed storage contract for semantic thesis-evaluation episodes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from compute.thesis_evaluation_episodes import (
    AcceptedObservationInput,
    EpisodeCheckInput,
    EpisodeIdempotencyConflictError,
    EpisodeNondeterminismError,
    EpisodeSeverity,
    ForwardSemanticInput,
    ProvenanceCompleteness,
    SemanticRuleInput,
    episode_history_relation,
    record_forward_episode,
)


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    database = tmp_path / "episodes.db"
    migrated_db(database, target="head")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _semantic_input(*, observed_value: str = "11.0") -> ForwardSemanticInput:
    return ForwardSemanticInput(
        ticker="wix",
        thesis_content_sha256="a" * 64,
        ruleset_version="wix-break-rules/v3",
        evaluator_semantic_version="thesis-evaluator/v2",
        hard_rules=(
            SemanticRuleInput(
                rule_id="growth-floor",
                definition={
                    "kpi_name": "Bookings growth",
                    "comparator": "lt",
                    "threshold": "12",
                    "unit": "percent",
                },
            ),
        ),
        soft_rules=(),
        accepted_observations=(
            AcceptedObservationInput(
                metric_identity="Bookings growth",
                period_end="2026-06-30",
                observed_value=observed_value,
                accepted_value=observed_value,
                unit="percent",
                currency=None,
                material_source_semantics=("earnings-release", "accepted-current"),
                restatement_semantics="original",
            ),
        ),
    )


def _seed_run(connection: sqlite3.Connection, run_id: str) -> None:
    connection.execute(
        "INSERT INTO ingestion_runs "
        "(run_id,started_at,directive,ticker_scope,status) VALUES (?,?,?,?,?)",
        (run_id, "2026-08-14T09:00:00+00:00", "test", '["WIX"]', "ok"),
    )


def _seed_raw(connection: sqlite3.Connection, *, run_id: str, severity: EpisodeSeverity) -> int:
    cursor = connection.execute(
        "INSERT INTO thesis_evaluations "
        "(ticker,evaluated_at,overall_status,rule_evaluations_json,run_id) "
        "VALUES ('WIX','2026-08-14T09:00:00+00:00',?,'[]',?)",
        (severity.value, run_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _check(
    *, run_id: str, severity: EpisodeSeverity, raw_evaluation_id: int | None = None
) -> EpisodeCheckInput:
    return EpisodeCheckInput(
        run_id=run_id,
        checked_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        evidence_as_of=datetime(2026, 6, 30, 23, 59, tzinfo=UTC),
        severity=severity,
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        rule_evaluations=(
            {
                "rule_id": "growth-floor",
                "status": severity.value,
                "observations": [{"period_end": "2026-06-30", "value": "11.0"}],
            },
        ),
        soft_rule_results=(),
        raw_evaluation_id=raw_evaluation_id,
    )


def test_forward_semantic_input_is_order_stable() -> None:
    first = _semantic_input()
    second = first.model_copy(
        update={
            "hard_rules": tuple(reversed(first.hard_rules)),
            "accepted_observations": tuple(reversed(first.accepted_observations)),
        }
    )
    assert first.ruleset_sha256 == second.ruleset_sha256
    assert first.semantic_input_sha256 == second.semantic_input_sha256


def test_new_run_same_semantics_updates_episode_without_another_episode(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    semantic = _semantic_input()
    _seed_run(connection, "run-wix-1")
    raw_id = _seed_raw(connection, run_id="run-wix-1", severity=EpisodeSeverity.WARN)

    first = record_forward_episode(
        connection,
        semantic=semantic,
        check=_check(
            run_id="run-wix-1",
            severity=EpisodeSeverity.WARN,
            raw_evaluation_id=raw_id,
        ),
    )
    _seed_run(connection, "run-wix-2")
    second_check = _check(run_id="run-wix-2", severity=EpisodeSeverity.WARN).model_copy(
        update={"checked_at": datetime(2026, 8, 15, 9, 0, tzinfo=UTC)}
    )
    second = record_forward_episode(connection, semantic=semantic, check=second_check)
    connection.commit()

    assert first.created is True and first.deduplicated is False
    assert second.created is False and second.deduplicated is True
    assert first.episode_id == second.episode_id
    assert connection.execute("SELECT COUNT(*) FROM thesis_evaluation_episodes").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()[0]
        == 2
    )
    episode = connection.execute(
        "SELECT duplicate_run_count,last_checked_at FROM thesis_evaluation_episodes"
    ).fetchone()
    assert tuple(episode) == (1, "2026-08-15T09:00:00+00:00")


def test_exact_run_replay_is_idempotent_and_does_not_increment_counts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    semantic = _semantic_input()
    _seed_run(connection, "run-wix-1")
    raw_id = _seed_raw(connection, run_id="run-wix-1", severity=EpisodeSeverity.WARN)
    check = _check(
        run_id="run-wix-1",
        severity=EpisodeSeverity.WARN,
        raw_evaluation_id=raw_id,
    )
    first = record_forward_episode(connection, semantic=semantic, check=check)
    replay = record_forward_episode(connection, semantic=semantic, check=check)

    assert replay == first.model_copy(update={"created": False, "replayed": True})
    count = connection.execute(
        "SELECT duplicate_run_count FROM thesis_evaluation_episodes"
    ).fetchone()
    assert tuple(count) == (0,)


def test_run_id_reuse_with_changed_semantics_fails_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_run(connection, "run-wix-1")
    raw_id = _seed_raw(connection, run_id="run-wix-1", severity=EpisodeSeverity.WARN)
    record_forward_episode(
        connection,
        semantic=_semantic_input(),
        check=_check(
            run_id="run-wix-1",
            severity=EpisodeSeverity.WARN,
            raw_evaluation_id=raw_id,
        ),
    )

    with pytest.raises(EpisodeIdempotencyConflictError, match="run_id"):
        record_forward_episode(
            connection,
            semantic=_semantic_input(observed_value="8.0"),
            check=_check(run_id="run-wix-1", severity=EpisodeSeverity.WARN),
        )


def test_same_semantic_input_with_different_severity_is_nondeterminism(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    connection = _database(tmp_path, migrated_db)
    semantic = _semantic_input()
    _seed_run(connection, "run-wix-1")
    raw_id = _seed_raw(connection, run_id="run-wix-1", severity=EpisodeSeverity.WARN)
    record_forward_episode(
        connection,
        semantic=semantic,
        check=_check(
            run_id="run-wix-1",
            severity=EpisodeSeverity.WARN,
            raw_evaluation_id=raw_id,
        ),
    )
    _seed_run(connection, "run-wix-2")

    with pytest.raises(EpisodeNondeterminismError, match="severity"):
        record_forward_episode(
            connection,
            semantic=semantic,
            check=_check(run_id="run-wix-2", severity=EpisodeSeverity.BREACH),
        )


def test_unresolved_is_persisted_and_partial_provenance_is_explicit(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _database(tmp_path, migrated_db)
    _seed_run(connection, "run-wix-unresolved")
    raw_id = _seed_raw(
        connection,
        run_id="run-wix-unresolved",
        severity=EpisodeSeverity.UNRESOLVED,
    )
    check = _check(
        run_id="run-wix-unresolved",
        severity=EpisodeSeverity.UNRESOLVED,
        raw_evaluation_id=raw_id,
    ).model_copy(update={"provenance_completeness": ProvenanceCompleteness.PARTIAL})
    result = record_forward_episode(connection, semantic=_semantic_input(), check=check)

    row = connection.execute(
        "SELECT overall_status,provenance_completeness "
        "FROM thesis_evaluation_episodes "
        "WHERE episode_id=?",
        (result.episode_id,),
    ).fetchone()
    assert tuple(row) == ("unresolved", "partial")
    assert episode_history_relation(connection) == "v_thesis_evaluation_history"


def test_compatibility_helper_falls_back_to_raw_relation() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE thesis_evaluations "
        "(id INTEGER PRIMARY KEY,ticker TEXT,evaluated_at TEXT,overall_status TEXT,"
        "rule_evaluations_json TEXT,run_id TEXT,soft_rule_results_json TEXT)"
    )
    assert episode_history_relation(connection) == "thesis_evaluations"
