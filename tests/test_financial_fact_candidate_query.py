"""Candidate lookup cost and temporal parity on an isolated synthetic ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from provenance import financial_fact_resolution

# Inspect the private query seam without expanding the production interface.
load_complete_candidates = getattr(financial_fact_resolution, "_load_complete_candidates")

CUTOFF = datetime(2026, 10, 3)


def candidate_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, supersedes_id INTEGER);
        CREATE INDEX ix_0270_financial_facts_supersedes_id
          ON financial_facts(supersedes_id);
        CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, supersedes_id INTEGER);
        CREATE INDEX ix_kpi_facts_supersedes_id ON kpi_facts(supersedes_id);
        CREATE TABLE documents (id INTEGER PRIMARY KEY, filing_date TEXT,
          fetched_at TEXT, source_type TEXT);
        INSERT INTO documents VALUES (1,'2026-09-01','2026-09-01','sec_xbrl');
        CREATE TABLE reported_observations (observation_id TEXT PRIMARY KEY,
          numeric_value TEXT, currency TEXT, unit TEXT, period_start TEXT,
          period_end TEXT, fiscal_period_type TEXT, available_at TEXT);
        CREATE TABLE fact_observation_revisions (fact_table TEXT, fact_row_id INTEGER,
          fact_revision INTEGER, observation_id TEXT UNIQUE, logical_key TEXT,
          source_document_id INTEGER, source_tier TEXT,
          PRIMARY KEY(fact_table, fact_row_id, fact_revision));
        CREATE INDEX ix_fact_observation_logical_revision
          ON fact_observation_revisions(logical_key,fact_table,fact_row_id,fact_revision);
        """
    )
    return conn


def capture_candidate(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    *,
    revision: int = 1,
    available_at: str = "2026-09-01",
    logical_key: str = "target",
) -> str:
    observation_id = f"{table}:{row_id}:r{revision}"
    conn.execute(
        "INSERT INTO reported_observations VALUES (?, '100', 'USD', 'dollars', "
        "'2026-04-01','2026-06-30','Q2',?)",
        (observation_id, available_at),
    )
    conn.execute(
        "INSERT INTO fact_observation_revisions VALUES (?,?,?,?,?,1,'sec_official')",
        (table, row_id, revision, observation_id, logical_key),
    )
    return observation_id


@pytest.mark.parametrize("table", ["financial_facts", "kpi_facts"])
def test_candidate_lookup_does_not_walk_unrelated_observation_ledger(table: str) -> None:
    conn = candidate_database()
    try:
        conn.execute(f"INSERT INTO {table} VALUES (1,NULL)")
        expected_id = capture_candidate(conn, table, 1)
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 0

        def measure() -> int:
            nonlocal steps
            steps = 0
            conn.set_progress_handler(progress, 1)
            try:
                candidates = load_complete_candidates(conn, "target", CUTOFF)
            finally:
                conn.set_progress_handler(None, 0)
            assert tuple(candidate.observation_id for candidate in candidates) == (expected_id,)
            return steps

        small = measure()
        for row_id in range(2, 5002):
            conn.execute(f"INSERT INTO {table} VALUES (?,NULL)", (row_id,))
            capture_candidate(conn, table, row_id, logical_key=f"unrelated:{row_id}")
        large = measure()
        assert large < 500
        assert large <= small * 2
    finally:
        conn.close()


@pytest.mark.parametrize("table", ["financial_facts", "kpi_facts"])
def test_candidate_successor_latest_revision_and_availability_parity(table: str) -> None:
    conn = candidate_database()
    try:

        def candidate_ids(cutoff: datetime = CUTOFF) -> list[str]:
            statements: list[str] = []
            unchanged = conn.total_changes
            conn.set_trace_callback(statements.append)
            try:
                candidates = load_complete_candidates(conn, "target", cutoff)
            finally:
                conn.set_trace_callback(None)
            sql = statements[0]
            assert (
                conn.execute(sql).fetchall()
                == conn.execute(sql.replace("CROSS JOIN", "JOIN")).fetchall()
            )
            assert conn.total_changes == unchanged
            return [item.observation_id for item in candidates]

        conn.execute(f"INSERT INTO {table} VALUES (1,NULL)")
        predecessor = capture_candidate(conn, table, 1)
        # An uncaptured successor cannot suppress captured evidence.
        conn.execute(f"INSERT INTO {table} VALUES (2,1)")
        assert candidate_ids() == [predecessor]
        successor = capture_candidate(conn, table, 2)
        assert candidate_ids() == [successor]
        latest = capture_candidate(conn, table, 2, revision=2, available_at="2026-10-04")
        # Current-head eligibility is retained. Older successor revisions are not
        # substituted when the latest captured revision is beyond the cutoff.
        assert candidate_ids() == [predecessor]
        assert candidate_ids(datetime(2026, 10, 4)) == [latest]
        # Independent evidence remains in the complete, ordered candidate set.
        conn.execute(f"INSERT INTO {table} VALUES (3,NULL),(4,NULL)")
        independent = capture_candidate(conn, table, 3)
        capture_candidate(conn, table, 4, available_at="2026-10-04")
        assert candidate_ids() == [predecessor, independent]
    finally:
        conn.close()
