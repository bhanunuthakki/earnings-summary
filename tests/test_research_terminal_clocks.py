"""Terminal uniqueness uses exact instants, including fractional seconds."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta, timezone
from pathlib import Path

import pytest

import provenance.population_research_snapshots as population
import provenance.research_snapshot as snapshots
from provenance.research_snapshot import ResearchSnapshotRequest
from sqlite_runtime import register_sqlite_integrity_functions
from tests import test_research_snapshot as fixtures
from tests.test_research_snapshot import T1


@pytest.fixture
def terminal_db(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[sqlite3.Connection, ResearchSnapshotRequest]]:
    conn = sqlite3.connect(migrated_db(tmp_path / "terminal.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(conn)
    try:
        getattr(fixtures, "_insert_foundation")(conn)
        getattr(fixtures, "_insert_resolution_publication_mapping")(conn)
        request = getattr(fixtures, "_research_request")(
            getattr(fixtures, "_processing_snapshot")(conn)
        )
        yield conn, request
    finally:
        conn.close()


def _seal(
    conn: sqlite3.Connection, request: ResearchSnapshotRequest, label: str, micros: int
) -> None:
    retained = request.model_copy(
        update={
            "research_snapshot_id": label,
            "idempotency_key": label,
            "recorded_at": T1 + timedelta(microseconds=micros),
        }
    )
    getattr(snapshots, "_build_research_snapshot_with_verifier")(
        conn, retained, verifier=getattr(fixtures, "_SealedDoubleVerifier")()
    )


def test_distinct_fractional_observation_clocks_do_not_conflict(
    terminal_db: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> None:
    conn, request = terminal_db
    _seal(conn, request, "earlier", 100000)
    query = request.model_copy(update={"recorded_at": T1 + timedelta(microseconds=900000)})
    getattr(population, "_require_unambiguous_terminal")(conn, query)


def test_exact_collision_is_found_after_distinct_fractional_candidate(
    terminal_db: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> None:
    conn, request = terminal_db
    _seal(conn, request, "earlier", 100000)
    _seal(conn, request, "same", 900000)
    offset = timezone(timedelta(hours=-7))
    query = request.model_copy(
        update={
            "cutoff_at": T1.astimezone(offset),
            "recorded_at": (T1 + timedelta(microseconds=900000)).astimezone(offset),
        }
    )
    with pytest.raises(population.ResearchSnapshotPlanError, match="terminal_scope_conflict"):
        getattr(population, "_require_unambiguous_terminal")(conn, query)
