from __future__ import annotations

import json
import sqlite3
import sys
import unicodedata
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from identity import DEFAULT_USER_ID
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
        "INSERT INTO tracked_companies VALUES (?,?,?,NULL)",
        [("NU", "portfolio", DEFAULT_USER_ID), ("NOW", "watchlist", DEFAULT_USER_ID)],
    )
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?)",
        [(1, "NU", "Total customers"), (2, "NU", "NIM"), (3, "NOW", "cRPO")],
    )
    conn.execute("INSERT INTO kpi_facts VALUES (1,1,7,'millions')")
    persist_kpi_semantic_context(conn, kpi_fact_id=1, context=_context())
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps({"ticker": "NU", "chart_priorities": ["NIM"]}))

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
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('NU','portfolio',?,NULL)",
        (DEFAULT_USER_ID,),
    )
    conn.execute("INSERT INTO kpi_definitions VALUES (1,'NU','Total customers')")
    conn.executemany(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?)",
        [
            (1, 1, 7, "millions", None),
            (2, 1, 8, "millions", 1),
        ],
    )
    persist_kpi_semantic_context(conn, kpi_fact_id=2, context=_context())
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text('{"ticker":"NU"}', encoding="utf-8")

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


def test_active_migration_head_installs_append_only_report_reference_dispositions(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "portfolio-reference-dispositions.db")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='report_kpi_reference_resolution_revisions'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='kpi_semantic_disposition_commits'"
        ).fetchone()
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_report_kpi_reference_resolution_%'"
            )
        }
        assert triggers == {
            "trg_report_kpi_reference_resolution_predecessor",
            "trg_report_kpi_reference_resolution_no_update",
            "trg_report_kpi_reference_resolution_no_delete",
        }
        insert_sql = (
            "INSERT INTO report_kpi_reference_resolution_revisions "
            "(user_id,ticker,source_path,json_pointer,reference_kind,requested_label,"
            "reference_content_sha256,status,kpi_definition_id,reason_code,revision,"
            "supersedes_resolution_id,reviewed_by,knowledge_at) "
            "VALUES ('owner','NU','micro_thesis/holdings/NU.json','/tier_1_kpis/0/name',"
            "'tier_1_kpi','Unmapped',?, ?, ?, 'no_matching_reported_definition',1,NULL,"
            "'source-review:owner','2026-08-30T12:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert_sql, ("a" * 64, "resolved", 1))
        conn.execute(
            insert_sql.replace("/tier_1_kpis/0/name", "/tier_1_kpis/1/name"),
            ("a" * 64, "retired", None),
        )
        conn.execute(insert_sql, ("a" * 64, "unresolved", None))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE report_kpi_reference_resolution_revisions SET reason_code='changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM report_kpi_reference_resolution_revisions")
        conn.execute(
            "INSERT INTO kpi_semantic_disposition_commits VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                1,
                1,
                1,
                1,
                "2026-08-30T12:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE kpi_semantic_disposition_commits SET fact_disposition_count=2")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM kpi_semantic_disposition_commits")
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("token", "quote", "valid"),
    [
        ("60", "Revenue 60,801", False),
        ("801", "Revenue 60,801", False),
        ("60,801", "Revenue 60,801", True),
        ("12", "Loss (12)", False),
        ("(12)", "Loss (12)", True),
        ("12", "Loss -12", False),
        ("12", "Loss \u221212", False),
        ("12", "Loss - 12", False),
        ("12", "Count (12 million shares)", False),
        ("12", "Count 12, compared with 10", True),
        ("-12", "Loss -12", True),
        ("1", "Revenue 1.23", False),
        ("23", "Revenue 1.23", False),
        ("1.23", "Revenue $1.23", True),
        ("10", "Outlook $10-20", True),
        ("20", "Outlook $10-20", True),
        ("$20", "Outlook $10-20", False),
        ("3", "Outlook 3-5%", True),
        ("5%", "Outlook 3-5%", True),
        ("5", "Outlook 3-5%", False),
    ],
)
def test_complete_source_numeric_token(token: str, quote: str, valid: bool) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote) is valid


@pytest.mark.parametrize(
    ("quote", "token", "valid"),
    [
        ("Outlook 10 -20", "20", True),
        ("Outlook 10 -20", "-20", False),
        ("Outlook 10 - 20", "20", True),
        ("Outlook 10 - 20", "- 20", False),
        ("Outlook 3 -5%", "5%", True),
        ("Outlook 3 -5%", "-5%", False),
        ("Outlook $10 - $20", "$20", True),
        ("Outlook $10 - $20", "20", True),
        ("Outlook $10 - $20", "- $20", False),
        ("Outlook 1,000.25 -2,000.75", "2,000.75", True),
        ("Outlook 1,000.25 -2,000.75", "-2,000.75", False),
        ("Outlook -10 - -5", "-5", True),
        ("Outlook -10 - -5", "5", False),
        ("Outlook (10) - (5)", "(5)", True),
        ("Outlook (10) - (5)", "5", False),
        ("Loss -20", "-20", True),
        ("Loss -20", "20", False),
        ("Q2 -20", "-20", True),
        ("Q2 -20", "20", False),
        ("Item X10 -20", "-20", True),
        ("Item X10 -20", "20", False),
    ],
)
def test_spaced_range_and_standalone_negative_tokens(quote: str, token: str, valid: bool) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote) is valid


@pytest.mark.parametrize(
    ("quote", "token", "valid"),
    [
        ("Revenue -$ 20", "20", False),
        ("Revenue -$ 20", "-$ 20", True),
        ("Revenue - $20", "$20", False),
        ("Revenue - $20", "- $20", True),
        ("Revenue $- 20", "20", False),
        ("Revenue $- 20", "$- 20", True),
        ("Revenue $- 20", "- 20", True),
        ("Revenue ($ 20)", "20", False),
        ("Revenue ($ 20)", "($ 20)", True),
        ("Revenue ($20)%", "($20)%", True),
        ("Revenue ($20)%", "20", False),
        ("Revenue \u2212 $ 20", "20", False),
        ("Revenue $ \u2212 20", "20", False),
        ("Revenue \u2212 $ 20", "\u2212 $ 20", False),
        ("Revenue 1e3", "1", False),
        ("Revenue 1e3", "3", False),
        ("Revenue 1e3", "1e3", False),
        ("Revenue 1E-3", "1", False),
        ("Revenue 1E-3", "-3", False),
        ("Revenue 1e+3", "+3", False),
        ("Revenue 1e--3", "3", False),
        ("Revenue 1.2.3", "1.2", False),
        ("Revenue 1.2.3", "3", False),
        ("Revenue 1,23", "1", False),
        ("Revenue 1,23", "23", False),
        ("Revenue 1,,234", "234", False),
        ("Revenue 20%%", "20", False),
        ("Count (12 million shares)", "12", False),
        ("Count 12, compared with 10", "12", True),
        ("Outlook $10 - $20", "20", True),
        ("Outlook 10 - 20", "20", True),
        ("Outlook -10 - -5", "-5", True),
        ("Outlook (10) - (5)", "(5)", True),
        ("Outlook 1,000.25 -2,000.75", "2,000.75", True),
        ("Q2 -20", "-20", True),
        ("Q2 -20", "20", False),
        ("2Q -20", "-20", True),
        ("2Q -20", "20", False),
        ("Revenue 1e3 then 20", "20", True),
    ],
)
def test_closed_scan_numeric_envelopes(quote: str, token: str, valid: bool) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote) is valid


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("-$ 20", "-20"),
        ("- $20", "-20"),
        ("$- 20", "-20"),
        ("($ 20)", "-20"),
        ("($20)%", "-20"),
        ("(20%)", "-20"),
        ("+ $ 1,000.25", "1000.25"),
        (".5", "0.5"),
        ("20 %", "20"),
    ],
)
def test_closed_parse_supported_envelopes(token: str, expected: str) -> None:
    from pipeline.kpi_semantics import parse_source_numeric

    assert parse_source_numeric(token) == Decimal(expected)


@pytest.mark.parametrize(
    "token",
    [
        "1e3",
        "1E-3",
        "1e--3",
        "1,23",
        "1.2.3",
        "20%%",
        "--20",
        "$$20",
        "(-20)",
        "$ (20)",
        "(12 million shares)",
        "\u2212 $20",
    ],
)
def test_closed_parse_unsupported_envelopes(token: str) -> None:
    from pipeline.kpi_semantics import parse_source_numeric

    with pytest.raises(ValueError, match="not numeric"):
        parse_source_numeric(token)


@pytest.mark.parametrize(
    "sign",
    [
        char
        for char in map(chr, range(sys.maxunicode + 1))
        if char not in "+-"
        and (
            unicodedata.category(char) == "Pd"
            or (unicodedata.category(char) == "Sm" and "MINUS" in unicodedata.name(char, ""))
            or unicodedata.normalize("NFKC", char) in {"+", "-"}
        )
    ],
)
def test_closed_boundary_unicode_sign_catalog(sign: str) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    for quote in (f"Revenue {sign}20", f"Revenue {sign} $20", f'Revenue: "{sign}20"'):
        assert not source_numeric_token_occurs("20", quote), repr(quote)
    assert not source_numeric_token_occurs("1", f"Revenue 1{sign}20")


@pytest.mark.parametrize(
    "control",
    [
        char
        for char in map(chr, range(sys.maxunicode + 1))
        if unicodedata.category(char) == "Cf"
        or (unicodedata.category(char) == "Cc" and not char.isspace())
    ],
)
def test_closed_boundary_format_control_catalog(control: str) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    for quote in (f"Revenue {control}-20", f"Revenue -{control}20", f"Revenue 20{control}"):
        assert not source_numeric_token_occurs("20", quote), repr(quote)
    assert not source_numeric_token_occurs("2", f"Revenue 2{control}0")
    assert not source_numeric_token_occurs("0", f"Revenue 2{control}0")


@pytest.mark.parametrize(
    ("quote", "token", "valid"),
    [
        ("Revenue ..20", ".20", False),
        ("Revenue ,20", "20", False),
        ("Revenue ,,20", "20", False),
        ("Revenue .,.20", ".20", False),
        ("Revenue 1..20", "1", False),
        ("Revenue .20", ".20", True),
        ('"Revenue": 20', "20", True),
        ("'Revenue';20", "20", True),
        ("Revenue, 20", "20", True),
        ("Revenue. 20", "20", True),
        ("Revenue =20", "20", True),
        ("Revenue [Q2]: -20", "-20", True),
        ("Revenue [20]", "20", False),
        ("Revenue {20}", "20", False),
        ("Revenue -:20", "20", False),
        ("Q2.20 -20", ".20", False),
        ("Revenue - $20", "- $20", True),
        ("Outlook $10 - $20", "20", True),
        ("Outlook -10 - -5", "-5", True),
        ("Outlook (10) - (5)", "(5)", True),
    ],
)
def test_closed_boundary_delimiters(quote: str, token: str, valid: bool) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote) is valid


@pytest.mark.parametrize(
    ("quote", "token"),
    [
        ("tax rate between 15-17%, an increase", "17%"),
        ("prior outlook of 13-16%.", "16%"),
    ],
)
def test_trial8_percent_range_upper_endpoint_before_prose_punctuation(
    quote: str, token: str
) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote)


@pytest.mark.parametrize(
    ("quote", "token", "valid"),
    [
        ("Rate 17%, another value", "17%", True),
        ("Rate 17%.", "17%", True),
        ("Loss (17), another value", "(17)", True),
        ("Loss (17).", "(17)", True),
        ("Loss (17) %, another value", "(17) %", True),
        ("Loss -$ 17%, another value", "-$ 17%", True),
        ("Rate 17%, 20%.", "20%", True),
        ("Rate 17%, -20%.", "-20%", True),
        ("Rate 17%, -20%.", "20%", False),
        ("Rate 17%..", "17%", False),
        ("Rate 17%,,", "17%", False),
        ("Rate 17%.20", "17%", False),
        ("Rate 17%.20", ".20", False),
        ("Rate 17%,20", "17%", False),
        ("Rate 17%,20", "20", False),
        ("Rate ,17%.", "17%", False),
        ("Rate ..17%.", ".17%", False),
        ("Rate 1,23%.", "23%", False),
        ("Rate 1e3%.", "3%", False),
        ("Rate \u221217%.", "17%", False),
        ("Rate \ufe6317%.", "17%", False),
        ("Rate -\u200b17%.", "17%", False),
        ("Rate 17\u200b%.", "17%", False),
        ("Rate (17)%.", "17%", False),
        ("Rate (17)%.", "(17)%", True),
        ("Rate (17%%).", "17%", False),
        ("Rate (17%%).", "(17%%)", False),
    ],
)
def test_numeric_envelope_prose_end_preserves_closed_start_and_sign(
    quote: str, token: str, valid: bool
) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote) is valid


@pytest.mark.parametrize(
    ("quote", "token", "valid"),
    [
        ("Repurchases  —  (10,167)  —  (22,921)", "(10,167)", True),
        ("Proceeds  24,910  —  24,910  —", "24,910", True),
        ("Exchange  (7)  131  —  243", "131", True),
        ("—  20", "20", True),
        ("20  —", "20", True),
        ("20\t—\t30", "20", False),
        ("20\n—\n30", "20", False),
        ("Repurchases  —  (10,167)", "10,167", False),
        ("Proceeds  —  24,910", "0", False),
        ("Proceeds  —24,910", "24,910", False),
        ("Proceeds  24,910—", "24,910", False),
        ("Outlook 10 — 20", "10", False),
        ("Outlook 10—20", "20", False),
        ("Loss  \u2212  20", "20", False),
        ("Loss  \u2013  20", "20", False),
        ("Loss  —  1,23", "23", False),
        ("Loss  —  1e3", "3", False),
    ],
)
def test_table_empty_em_dash_cell_preserves_neighboring_tokens(
    quote: str, token: str, valid: bool
) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote) is valid


@pytest.mark.parametrize(
    ("label", "quote", "token"),
    [
        (
            "GAAP revenue year-over-year change %",
            "GAAP revenue year-over-year change %  28 %  30 %",
            "28 %",
        ),
        (
            "Revenue excluding foreign exchange effect year-over-year change \n%",
            "Revenue excluding foreign exchange effect year-over-year change \n%  27 %  28 %",
            "27 %",
        ),
        (
            "GAAP advertising revenue year-over-year change %",
            "GAAP advertising revenue year-over-year change %  27 %  30 %",
            "27 %",
        ),
        (
            "Advertising revenue excluding foreign exchange effect year-over-\nyear change %",
            "Advertising revenue excluding foreign exchange effect year-over-\nyear change %  26 %  27 %",
            "26 %",
        ),
    ],
)
def test_actual_trial11_percent_row_label_is_separate_from_first_cell(
    label: str, quote: str, token: str
) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote, row_label=label)


@pytest.mark.parametrize(
    ("quote", "label", "token", "valid"),
    [
        ("Growth %  28 %  30 %", "Growth %", "28 %", True),
        ("Growth %  28 %  30 %", None, "28 %", False),
        ("Growth %  28 %  30 %", "Other %", "28 %", False),
        ("@Growth %  28 %", "Growth %", "28 %", False),
        ("@Growth %  28 %", "@Growth %", "28 %", False),
        ("Growth 1e3 %  28 %", "Growth 1e3 %", "28 %", False),
        ("Growth 1,23 %  28 %", "Growth 1,23 %", "28 %", False),
        ("%  28 %", "%", "28 %", False),
        ("Growth % 28 %", "Growth %", "28 %", False),
        ("Growth %28 %", "Growth %", "28 %", False),
        ("Growth %%  28 %", "Growth %%", "28 %", False),
        ("Growth -%  28 %", "Growth -%", "28 %", False),
        ("Growth \u2212%  28 %", "Growth \u2212%", "28 %", False),
        ("Growth %  2\u200b8 %", "Growth %", "28 %", False),
        ("Growth %  0 %", "Growth %", "0 %", True),
        ("Growth %  28 %", "Growth %", "0", False),
    ],
)
def test_percent_row_label_boundary_requires_exact_safe_label_and_table_gap(
    quote: str, label: str | None, token: str, valid: bool
) -> None:
    from pipeline.kpi_semantics import source_numeric_token_occurs

    assert source_numeric_token_occurs(token, quote, row_label=label) is valid
