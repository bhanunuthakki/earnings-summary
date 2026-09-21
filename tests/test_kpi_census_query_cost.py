"""Bound census query work without a timing-sensitive performance assertion."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from compute.kpi_revision_shadow_census import (
    SnapshotEvidenceState,
    audit_kpi_revision_shadow_census,
)


@pytest.mark.parametrize("population", ["portfolio", "outside", "invalid"])
def test_unindexed_fact_population_is_scanned_without_quadratic_successor_work(
    tmp_path: Path, migrated_db: Callable[..., Path], population: str
) -> None:
    """The canonical snapshot has no supersedes index; NULLs must stay current."""
    database = migrated_db(tmp_path / "census-cost.db")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type) "
            "VALUES ('COST','Cost fixture','portfolio')"
        )
        cursor = conn.execute(
            "INSERT INTO kpi_definitions(ticker,name,unit,primary_source) "
            "VALUES ('COST','Count','count','ir_doc')"
        )
        assert cursor.lastrowid is not None
        definition_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO kpi_definitions(ticker,name,unit,primary_source) "
            "VALUES ('COST',?,'count','ir_doc')",
            ((f"Additional definition {index}",) for index in range(80)),
        )
        # Missing immutable authority is intentional: audit must retain each raw
        # row and HOLD, not hide data to meet the work budget.
        conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
        ticker = "OUTSIDE" if population == "outside" else "COST"
        period = "invalid" if population == "invalid" else "2025-12-31"
        conn.executemany(
            "INSERT INTO kpi_facts(id,ticker,period_end,fiscal_period_type,"
            "kpi_definition_id,value,unit,source_doc_id,confidence,extracted_by,supersedes_id) "
            "VALUES (?, ?, ?, 'FY', ?, '10', 'count', ?, 1.0, 'manual', ?)",
            (
                (
                    index,
                    ticker,
                    period,
                    definition_id,
                    index,
                    index - 1 if index % 100 == 0 else None,
                )
                for index in range(1, 6001)
            ),
        )
        conn.commit()
        before = conn.total_changes
        conn.execute("PRAGMA query_only=ON")
        progress_calls = 0

        def bounded_steps() -> int:
            nonlocal progress_calls
            progress_calls += 1
            return int(progress_calls > 2000)

        conn.set_progress_handler(bounded_steps, 1000)
        stamp = datetime(2026, 9, 19, tzinfo=UTC)
        try:
            result = audit_kpi_revision_shadow_census(
                conn,
                effective_at=stamp,
                known_at=stamp,
                evaluated_at=stamp,
                snapshot_evidence=SnapshotEvidenceState.unverified("synthetic_cost_fixture"),
            )
        finally:
            conn.set_progress_handler(None, 0)
        assert conn.total_changes == before
        assert result.authorizes_reader_activation is False
        expected = tuple(index for index in range(1, 6001) if index % 100 != 99)
        if population == "outside":
            assert tuple(item.fact_id for item in result.out_of_scope_facts) == expected
        elif population == "invalid":
            assert tuple(item.fact_id for item in result.invalid_in_scope_facts) == expected
        else:
            series = next(item for item in result.series if item.kpi_definition_id == definition_id)
            assert series.raw_current_fact_ids == expected
            assert len(series.fact_dispositions) == len(expected)
            assert series.canonical_current_fact_ids == ()
