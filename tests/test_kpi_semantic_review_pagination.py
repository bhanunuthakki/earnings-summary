from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

import pipeline.kpi_semantic_review as review_module
from pipeline.kpi_semantic_review import (
    KpiSemanticReviewBatch,
    build_kpi_semantic_review_batch,
)
from pipeline.kpi_semantic_scope import ScopedKpiDefinition

OBSERVED_AT = datetime(2026, 8, 31, 12, tzinfo=UTC)
PAGE_SIZE = 1_000


def _database(fact_count: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tracked_companies(
            ticker TEXT,list_type TEXT,user_id TEXT,archived_at TEXT
        );
        INSERT INTO tracked_companies VALUES ('NU','portfolio','owner',NULL);
        CREATE TABLE kpi_definitions(
            id INTEGER PRIMARY KEY,ticker TEXT,name TEXT,unit TEXT
        );
        INSERT INTO kpi_definitions VALUES (1,'NU','Total customers','count');
        CREATE TABLE kpi_facts(
            id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
            kpi_definition_id INTEGER,value TEXT,unit TEXT,source_doc_id INTEGER
        );
        CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts;
        """
    )
    conn.executemany(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?,?,?,NULL)",
        (
            (
                fact_id,
                "NU",
                f"2024-12-{((fact_id - 1) % 28) + 1:02d}",
                "Q4",
                1,
                str(fact_id),
                "count",
            )
            for fact_id in range(fact_count, 0, -1)
        ),
    )
    return conn


def _scope(
    _conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str,
) -> tuple[ScopedKpiDefinition, ...]:
    del repo_root
    assert user_id == "owner"
    return (
        ScopedKpiDefinition(
            ticker="NU",
            kpi_definition_id=1,
            name="Total customers",
            reasons=("facts_metrics",),
            fact_count=1,
            admitted_context_count=0,
            quarantined_context_count=0,
            legacy_unknown_context_count=0,
            missing_context_count=1,
            current_actual_count=0,
            comparator_count=0,
            guidance_target_count=0,
            management_explanation_count=0,
            analyst_question_count=0,
        ),
    )


def _page(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    after_fact_id: int,
    limit: int = PAGE_SIZE,
) -> KpiSemanticReviewBatch:
    return build_kpi_semantic_review_batch(
        conn,
        repo_root=tmp_path,
        user_id="owner",
        ticker="NU",
        limit=limit,
        observed_at=OBSERVED_AT,
        after_fact_id=after_fact_id,
    )


@pytest.mark.parametrize("fact_count", (0, 1, 999, 1_000, 1_001))
def test_keyset_page_boundaries(
    fact_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_module, "scoped_kpi_definitions", _scope)
    conn = _database(fact_count)
    try:
        batch = _page(conn, tmp_path, after_fact_id=0)
    finally:
        conn.close()

    expected_count = min(fact_count, PAGE_SIZE)
    assert [item.fact_id for item in batch.items] == list(range(1, expected_count + 1))
    assert batch.total_items == expected_count
    assert batch.truncated is (fact_count > PAGE_SIZE)
    assert batch.schema_version == "kpi_semantic_review.v3"


def test_keyset_multi_page_has_strict_progress_without_gaps_or_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_module, "scoped_kpi_definitions", _scope)
    fact_count = 2_501
    conn = _database(fact_count)
    cursor = 0
    seen: list[int] = []
    page_sizes: list[int] = []
    try:
        while True:
            batch = _page(conn, tmp_path, after_fact_id=cursor)
            page_ids = [item.fact_id for item in batch.items]
            assert page_ids
            assert page_ids == sorted(page_ids)
            assert page_ids[0] > cursor
            assert all(current > prior for prior, current in pairwise(page_ids))
            seen.extend(page_ids)
            page_sizes.append(len(page_ids))
            if not batch.truncated:
                break
            next_cursor = page_ids[-1]
            assert next_cursor > cursor
            cursor = next_cursor
    finally:
        conn.close()

    assert page_sizes == [1_000, 1_000, 501]
    assert seen == list(range(1, fact_count + 1))
    assert len(seen) == len(set(seen))


def test_keyset_cursor_rejects_negative_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_module, "scoped_kpi_definitions", _scope)
    conn = _database(1)
    try:
        with pytest.raises(ValueError, match="after_fact_id must be non-negative"):
            _page(conn, tmp_path, after_fact_id=-1)
    finally:
        conn.close()
