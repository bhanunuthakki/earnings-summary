"""Evaluator-to-episode integration contracts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from compute.thesis_evaluator import evaluate_ticker_thesis, persist_verdict
from compute.thesis_history import fetch_history, streak_summary


def _open_database(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    database = tmp_path / "episode-integration.db"
    migrated_db(database, target="head")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _seed_run(connection: sqlite3.Connection, run_id: str, started_at: str) -> None:
    connection.execute(
        "INSERT INTO ingestion_runs "
        "(run_id,started_at,directive,ticker_scope,status) VALUES (?,?,?,?,?)",
        (run_id, started_at, "test_episode_integration", '["ZZZ"]', "ok"),
    )
    connection.commit()


def test_identical_evaluator_checks_append_receipts_not_owner_history(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    connection = _open_database(tmp_path, migrated_db)
    holdings = tmp_path / "holdings"
    holdings.mkdir()
    (holdings / "ZZZ.json").write_text(
        json.dumps(
            {
                "ticker": "ZZZ",
                "thesis": "Owner thesis remains unchanged.",
                "key_driver": "Durable unit economics",
                "break_rules": [],
                "business_model_rules": [],
                "break_rules_soft": [],
            }
        ),
        encoding="utf-8",
    )

    first_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    second_at = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _seed_run(connection, "episode-run-1", first_at.isoformat())
    first = evaluate_ticker_thesis(connection, ticker="ZZZ", holdings_dir=holdings)
    persist_verdict(
        connection,
        replace(first, evaluated_at=first_at),
        run_id="episode-run-1",
        holdings_dir=holdings,
    )

    _seed_run(connection, "episode-run-2", second_at.isoformat())
    second = evaluate_ticker_thesis(connection, ticker="ZZZ", holdings_dir=holdings)
    persist_verdict(
        connection,
        replace(second, evaluated_at=second_at),
        run_id="episode-run-2",
        holdings_dir=holdings,
    )

    assert (
        connection.execute("SELECT COUNT(*) FROM thesis_evaluations WHERE ticker='ZZZ'").fetchone()[
            0
        ]
        == 1
    )
    episode = connection.execute(
        "SELECT duplicate_run_count,provenance_completeness "
        "FROM thesis_evaluation_episodes WHERE ticker='ZZZ'"
    ).fetchone()
    assert tuple(episode) == (1, "partial")
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts WHERE ticker='ZZZ'"
        ).fetchone()[0]
        == 2
    )
    # An OK episode is retained analytically but does not create a nag card.
    assert tuple(
        connection.execute(
            "SELECT COUNT(*) FROM alerts WHERE thesis_evaluation_episode_id IN "
            "(SELECT episode_id FROM thesis_evaluation_episodes WHERE ticker='ZZZ')"
        ).fetchone()
    ) == (0,)

    history = fetch_history(connection, "ZZZ")
    assert len(history) == 1
    assert history[0].evaluated_at == first_at
    assert history[0].checked_at == second_at
    summary = streak_summary(connection, "ZZZ")
    assert summary is not None
    assert summary.total_evaluations == 1
    assert summary.last_evaluated_at == second_at
