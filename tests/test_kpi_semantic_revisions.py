from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
    semantic_admission_sql,
    unclassified_kpi_context,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_facts(
            id INTEGER PRIMARY KEY,
            value TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'millions'
        );
        CREATE TABLE kpi_fact_semantic_contexts(
            id INTEGER PRIMARY KEY,
            kpi_fact_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            supersedes_context_id INTEGER UNIQUE,
            metric_name_as_reported TEXT NOT NULL,
            reported_period_end TEXT,
            period_role TEXT NOT NULL,
            publication_lane TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,
            consolidation_scope TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            unit_scale TEXT NOT NULL,
            source_row_label TEXT,
            source_column_header TEXT,
            status TEXT NOT NULL,
            reason_code TEXT,
            reviewed_by TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            UNIQUE(kpi_fact_id, revision)
        );
        """
    )
    return conn


def _qualified(
    *,
    lane: KpiPublicationLane = KpiPublicationLane.CURRENT_ACTUAL,
    role: KpiPeriodRole = KpiPeriodRole.CURRENT,
) -> KpiSemanticContext:
    return KpiSemanticContext(
        metric_name_as_reported="Total customers",
        reported_period_end=date(2024, 12, 31),
        period_role=role,
        publication_lane=lane,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        dimensions={},
        unit_scale=KpiUnitScale.MILLIONS,
        status=KpiSemanticStatus.ADMITTED,
    )


def test_semantic_context_can_mature_without_rewriting_the_fact() -> None:
    conn = _db()
    conn.execute("INSERT INTO kpi_facts(id,value) VALUES (1,'95')")
    observed_at = datetime(2026, 8, 27, 18, tzinfo=UTC)

    first = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=1,
        context=unclassified_kpi_context(
            metric_name_as_reported="Total customers",
            reported_period_end=date(2024, 12, 31),
        ),
        reviewed_by="pipeline",
        knowledge_at=observed_at,
    )
    second = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=1,
        context=_qualified(),
        reviewed_by="source_review:owner",
        knowledge_at=observed_at,
    )

    rows = conn.execute(
        "SELECT id,revision,supersedes_context_id,status FROM "
        "kpi_fact_semantic_contexts ORDER BY revision"
    ).fetchall()
    assert [(row["revision"], row["status"]) for row in rows] == [
        (1, "legacy_unknown"),
        (2, "admitted"),
    ]
    assert rows[0]["id"] == first
    assert rows[1]["id"] == second
    assert rows[1]["supersedes_context_id"] == first
    assert conn.execute("SELECT value FROM kpi_facts WHERE id=1").fetchone()[0] == "95"
    assert current_kpi_semantic_context(conn, kpi_fact_id=1).revision == 2


def test_semantic_context_identical_replay_is_idempotent() -> None:
    conn = _db()
    conn.execute("INSERT INTO kpi_facts(id,value) VALUES (1,'95')")
    observed_at = datetime(2026, 8, 27, 18, tzinfo=UTC)
    first = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=1,
        context=_qualified(),
        reviewed_by="source_review:owner",
        knowledge_at=observed_at,
    )
    replay = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=1,
        context=_qualified(),
        reviewed_by="source_review:owner",
        knowledge_at=observed_at,
    )
    assert replay == first
    assert conn.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0] == 1


def test_qualified_noncurrent_lane_is_valid_but_not_a_current_series_value() -> None:
    conn = _db()
    conn.executemany("INSERT INTO kpi_facts(id,value) VALUES (?,?)", [(1, "95"), (2, "120")])
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=1,
        context=_qualified(),
        reviewed_by="source_review:owner",
    )
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=2,
        context=_qualified(
            lane=KpiPublicationLane.GUIDANCE_TARGET,
            role=KpiPeriodRole.GUIDANCE,
        ),
        reviewed_by="source_review:owner",
    )
    join, where = semantic_admission_sql(conn)
    rows = conn.execute(f"SELECT kf.id FROM kpi_facts kf {join} WHERE {where} ORDER BY kf.id")
    assert [row[0] for row in rows] == [1]


def test_fail_closed_reader_excludes_missing_and_legacy_unknown() -> None:
    conn = _db()
    conn.executemany("INSERT INTO kpi_facts(id,value) VALUES (?,?)", [(1, "95"), (2, "120")])
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=2,
        context=unclassified_kpi_context(
            metric_name_as_reported="Total customers",
            reported_period_end=date(2024, 12, 31),
        ),
        reviewed_by="pipeline",
    )
    join, where = semantic_admission_sql(conn, fail_closed=True)
    rows = conn.execute(f"SELECT kf.id FROM kpi_facts kf {join} WHERE {where}")
    assert list(rows) == []
