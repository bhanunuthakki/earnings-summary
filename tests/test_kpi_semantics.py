from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.facts import Unit
from pipeline.kpi_semantic_scope import scoped_kpi_definitions
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    normalize_source_numeric,
    persist_kpi_semantic_context,
    semantic_admission_sql,
    unclassified_kpi_context,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _context(*, status: KpiSemanticStatus = KpiSemanticStatus.ADMITTED) -> KpiSemanticContext:
    return KpiSemanticContext(
        metric_name_as_reported="Total customers",
        reported_period_end=date(2024, 12, 31),
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        dimensions={},
        unit_scale=KpiUnitScale.MILLIONS,
        status=status,
        reason_code=None if status is KpiSemanticStatus.ADMITTED else "source_scope_ambiguous",
    )


def _semantic_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE kpi_fact_semantic_contexts (id INTEGER PRIMARY KEY,"
        "kpi_fact_id INTEGER UNIQUE,metric_name_as_reported TEXT,reported_period_end TEXT,"
        "period_role TEXT,accounting_basis TEXT,consolidation_scope TEXT,dimensions_json TEXT,"
        "unit_scale TEXT,source_row_label TEXT,source_column_header TEXT,source_value_text TEXT,"
        "status TEXT,reason_code TEXT)"
    )


def test_source_qualified_comparator_uses_a_separate_publication_lane() -> None:
    context = KpiSemanticContext.model_validate(
        {
            **_context().model_dump(),
            "period_role": KpiPeriodRole.PRIOR_YEAR_COMPARATOR,
            "publication_lane": KpiPublicationLane.COMPARATOR,
        }
    )
    assert context.status is KpiSemanticStatus.ADMITTED
    assert context.publication_lane is KpiPublicationLane.COMPARATOR


def test_admitted_context_requires_scale_and_scoped_dimensions() -> None:
    with pytest.raises(ValidationError, match="source unit scale"):
        KpiSemanticContext.model_validate(
            {
                **_context().model_dump(),
                "unit_scale": KpiUnitScale.UNKNOWN,
            }
        )


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        (KpiUnitScale.NONE, Decimal("114")),
        (KpiUnitScale.THOUSANDS, Decimal("114000")),
        (KpiUnitScale.MILLIONS, Decimal("114000000")),
        (KpiUnitScale.BILLIONS, Decimal("114000000000")),
    ],
)
def test_count_source_scale_normalizes_to_persisted_count(
    scale: KpiUnitScale, expected: Decimal
) -> None:
    assert normalize_source_numeric(Decimal("114"), unit=Unit.COUNT, unit_scale=scale) == expected


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        (KpiUnitScale.NONE, Decimal("114")),
        (KpiUnitScale.THOUSANDS, Decimal("114000")),
        (KpiUnitScale.MILLIONS, Decimal("114000000")),
        (KpiUnitScale.BILLIONS, Decimal("114000000000")),
    ],
)
def test_admitted_count_context_allows_each_source_scale(
    scale: KpiUnitScale, expected: Decimal
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, unit TEXT NOT NULL, "
        "value NUMERIC NOT NULL, source_excerpt TEXT)"
    )
    conn.execute("INSERT INTO kpi_facts VALUES (1, 'count', ?, '114 reported')", (str(expected),))
    _semantic_table(conn)
    assert (
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=_context().model_copy(update={"unit_scale": scale, "source_value_text": "114"}),
        )
        == 1
    )


def test_admitted_context_rejects_scaled_percent_against_persisted_fact() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, unit TEXT NOT NULL)")
    conn.execute("INSERT INTO kpi_facts VALUES (1, 'percent')")
    _semantic_table(conn)
    with pytest.raises(ValueError, match="persisted fact unit must match semantic unit scale"):
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=_context().model_copy(update={"unit_scale": KpiUnitScale.MILLIONS}),
        )


def test_admitted_scaled_count_rejects_unreconciled_persisted_value() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, unit TEXT NOT NULL, "
        "value NUMERIC NOT NULL, source_excerpt TEXT)"
    )
    conn.execute("INSERT INTO kpi_facts VALUES (1, 'count', 114, '114 million customers')")
    _semantic_table(conn)
    with pytest.raises(ValueError, match="do not match persisted value"):
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=_context().model_copy(update={"source_value_text": "114"}),
        )


def test_unclassified_producer_context_is_explicitly_quarantined() -> None:
    context = unclassified_kpi_context(
        metric_name_as_reported="Legacy KPI",
        reported_period_end=date(2025, 3, 31),
    )
    assert context.status is KpiSemanticStatus.LEGACY_UNKNOWN
    assert context.period_role is KpiPeriodRole.UNKNOWN
    assert context.unit_scale is KpiUnitScale.UNKNOWN
    assert context.reason_code == "producer_missing_semantic_context"
    with pytest.raises(ValidationError, match="require dimensions"):
        KpiSemanticContext.model_validate(
            {
                **_context().model_dump(),
                "consolidation_scope": KpiConsolidationScope.GEOGRAPHY,
                "dimensions": {},
            }
        )


def test_context_is_immutable_on_conflicting_replay() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, unit TEXT NOT NULL)")
    conn.execute("INSERT INTO kpi_facts VALUES (1, 'millions')")
    _semantic_table(conn)
    persist_kpi_semantic_context(conn, kpi_fact_id=1, context=_context())
    persist_kpi_semantic_context(conn, kpi_fact_id=1, context=_context())
    with pytest.raises(ValueError, match="conflicts"):
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=_context().model_copy(
                update={
                    "status": KpiSemanticStatus.QUARANTINED,
                    "reason_code": "source_scope_ambiguous",
                }
            ),
        )


def test_admission_sql_excludes_explicit_quarantine_but_keeps_legacy() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, unit TEXT NOT NULL)")
    _semantic_table(conn)
    conn.executemany("INSERT INTO kpi_facts(id,unit) VALUES (?, 'millions')", [(1,), (2,), (3,)])
    persist_kpi_semantic_context(conn, kpi_fact_id=2, context=_context())
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=3,
        context=KpiSemanticContext(
            metric_name_as_reported="Customers",
            reported_period_end=date(2023, 12, 31),
            period_role=KpiPeriodRole.PRIOR_YEAR_COMPARATOR,
            publication_lane=KpiPublicationLane.COMPARATOR,
            accounting_basis=KpiAccountingBasis.MANAGEMENT,
            consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
            unit_scale=KpiUnitScale.MILLIONS,
            status=KpiSemanticStatus.QUARANTINED,
            reason_code="period_role_prior_year_comparator",
        ),
    )
    join, where = semantic_admission_sql(conn)
    rows = conn.execute(f"SELECT kf.id FROM kpi_facts kf {join} WHERE {where} ORDER BY kf.id")
    assert [int(row[0]) for row in rows] == [1, 2]


def test_scope_is_report_union_facts_metrics_for_portfolio_only(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tracked_companies(ticker TEXT,list_type TEXT,user_id TEXT,archived_at TEXT);"
        "CREATE TABLE kpi_definitions(id INTEGER PRIMARY KEY,ticker TEXT,name TEXT);"
        "CREATE TABLE kpi_facts(id INTEGER PRIMARY KEY,kpi_definition_id INTEGER,"
        "source_doc_id INTEGER,unit TEXT NOT NULL);"
    )
    _semantic_table(conn)
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?,?,'default',NULL)",
        [("NU", "portfolio"), ("NOW", "watchlist")],
    )
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?)",
        [(1, "NU", "Total customers"), (2, "NU", "NIM"), (3, "NOW", "cRPO")],
    )
    conn.execute("INSERT INTO kpi_facts VALUES (1,1,7,'millions')")
    persist_kpi_semantic_context(conn, kpi_fact_id=1, context=_context())
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps({"chart_priorities": ["NIM"]}))

    rows = scoped_kpi_definitions(conn, repo_root=tmp_path)
    assert [(row.ticker, row.name, row.reasons) for row in rows] == [
        ("NU", "NIM", ("report",)),
        ("NU", "Total customers", ("facts_metrics",)),
    ]
    assert rows[0].fact_count == 0
    assert rows[0].missing_context_count == 0
    assert rows[1].fact_count == 1
    assert rows[1].missing_context_count == 0


def test_scope_counts_only_current_fact_heads(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tracked_companies(ticker TEXT,list_type TEXT,user_id TEXT,archived_at TEXT);"
        "CREATE TABLE kpi_definitions(id INTEGER PRIMARY KEY,ticker TEXT,name TEXT);"
        "CREATE TABLE kpi_facts(id INTEGER PRIMARY KEY,kpi_definition_id INTEGER,"
        "source_doc_id INTEGER,unit TEXT NOT NULL,supersedes_id INTEGER);"
        "CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts WHERE id=2;"
    )
    _semantic_table(conn)
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio','default',NULL)")
    conn.execute("INSERT INTO kpi_definitions VALUES (1,'NU','Total customers')")
    conn.executemany(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?)",
        [
            (1, 1, 7, "millions", None),
            (2, 1, 8, "millions", 1),
        ],
    )
    persist_kpi_semantic_context(conn, kpi_fact_id=2, context=_context())

    rows = scoped_kpi_definitions(conn, repo_root=tmp_path)

    assert len(rows) == 1
    assert rows[0].fact_count == 1
    assert rows[0].admitted_context_count == 1
    assert rows[0].missing_context_count == 0


def test_semantic_audit_requires_explicit_database_path() -> None:
    from execution.audit_kpi_semantics import build_parser

    actions = {item.dest: item for item in build_parser()._actions}
    assert actions["db"].required is True
    assert actions["user_id"].required is True


def test_semantic_audit_gate_fails_closed_on_empty_owner_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import audit_kpi_semantics as audit
    from sqlite_runtime import SQLiteConnectionRole

    db_path = tmp_path / "empty-owner.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE tracked_companies(ticker TEXT,list_type TEXT,user_id TEXT,archived_at TEXT);"
        "CREATE TABLE kpi_definitions(id INTEGER PRIMARY KEY,ticker TEXT,name TEXT);"
    )
    conn.close()

    roles: list[SQLiteConnectionRole] = []
    actual_connect = audit.connect_sqlite

    def checked_connect(path: Path, *, role: SQLiteConnectionRole) -> sqlite3.Connection:
        roles.append(role)
        return actual_connect(path, role=role)

    monkeypatch.setattr(audit, "connect_sqlite", checked_connect)
    assert audit.main(["--db", str(db_path), "--user-id", "bhanu", "--gate"]) == 2
    assert roles == [SQLiteConnectionRole.READ_ONLY]
    captured = capsys.readouterr()
    event = json.loads(captured.err)
    summary = json.loads(captured.out)
    assert event["gate_blocked"] is True
    assert event["empty_scope"] is True
    assert summary["gate_blocked"] is True
    assert summary["empty_scope"] is True


def test_active_migration_head_installs_append_only_semantic_context(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "portfolio.db")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kpi_fact_semantic_contexts'"
        ).fetchone()
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_kpi_fact_semantic_contexts_%'"
            )
        }
        assert triggers == {
            "trg_kpi_fact_semantic_contexts_predecessor",
            "trg_kpi_fact_semantic_contexts_no_update",
            "trg_kpi_fact_semantic_contexts_no_delete",
        }
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
        }
        assert {
            "revision",
            "supersedes_context_id",
            "publication_lane",
            "reviewed_by",
            "knowledge_at",
        } <= columns

        first = persist_kpi_semantic_context(
            conn,
            kpi_fact_id=999,
            context=unclassified_kpi_context(
                metric_name_as_reported="Total customers",
                reported_period_end=date(2024, 12, 31),
            ),
            reviewed_by="pipeline",
        )
        with pytest.raises(ValueError, match="requires an existing fact"):
            persist_kpi_semantic_context(
                conn,
                kpi_fact_id=999,
                context=_context(),
                reviewed_by="source_review:owner",
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM kpi_fact_semantic_contexts WHERE kpi_fact_id=999"
            ).fetchone()[0]
            == 1
        )
        assert first is not None
    finally:
        conn.close()
