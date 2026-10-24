"""Investor-grade cutover contracts for financial and KPI fact observations."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import pytest
from alembic.config import Config

from alembic import command
from compute.fmp_derived_kpis import (
    _fetch_full_kpi_series,  # pyright: ignore[reportPrivateUsage] -- exact producer seam
)
from execution import backfill_financial_fact_resolutions as cutover_cli
from models.facts import Currency, Unit
from pipeline.kpi_definition_revisions import (
    KpiCurrencyDisposition,
    KpiDefinitionPeriodKind,
    KpiStockFlowBehavior,
    KpiUnitFamily,
    persist_kpi_definition_revision,
)
from pipeline.kpi_semantics import current_kpi_semantic_context, persist_kpi_semantic_context
from pipeline.queries import open_db
from pipeline.restatement_detector import insert_with_restatement_detection
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.financial_fact_resolution import (
    FactCutoverRequest,
    HistoricalFactAuthorityUnavailableError,
    _dimensions,  # pyright: ignore[reportPrivateUsage] -- current-head seam under test
    canonical_fact_relation,
    canonical_fact_selections_as_known,
    execute_fact_cutover,
    resolve_fact_logical_key,
    resolve_fact_row,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from run_lock import hold_run_lock
from sqlite_runtime import (
    SQLiteConnectionRole,
    fact_capture_numeric_matches,
    register_sqlite_integrity_functions,
)
from tests.fixtures.kpi_revision_setup import definition_fixture
from tests.kpi_semantic_support import admit_all_kpi_facts
from timeseries.loaders import load_financial_series

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0224_expected_document_lifecycle"
HEAD = "0225_financial_fact_resolution_cutover"
STAMP = datetime(2026, 7, 27, 12, 0, 0)


def test_capture_numeric_policy_matches_sqlite_trigger_text_without_rounding() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        register_sqlite_integrity_functions(conn)
        value = 0.00263774618976614
        captured = str(conn.execute("SELECT CAST(? AS TEXT)", (value,)).fetchone()[0])
        next_value = math.nextafter(value, math.inf)
        assert captured == "0.0026377461897661401"
        assert captured != str(value)
        assert fact_capture_numeric_matches("0231-trigger-v1", captured, value, captured)
        assert (
            conn.execute(
                "SELECT fact_capture_numeric_matches(?,?,?,CAST(? AS TEXT))",
                ("0231-trigger-v1", captured, value, value),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT fact_capture_numeric_matches(?,?,?,CAST(? AS TEXT))",
                ("0231-trigger-v1", captured, next_value, next_value),
            ).fetchone()[0]
            == 0
        )
        assert not fact_capture_numeric_matches("manual-v1", "1.00000000000000001", 1.0, "1")
    finally:
        conn.close()


def test_nested_canonical_reader_keeps_active_managed_cursor(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "nested-capture-reader.db")
    conn = open_db(path)
    try:

        def capture_test(value: int) -> int:
            return value

        conn.create_function("capture_test", 1, capture_test)
        cursor = conn.execute("SELECT capture_test(1) UNION ALL SELECT capture_test(2)")
        assert cursor.fetchone()[0] == 1
        assert canonical_fact_relation(conn, "kpi_facts").selection_mode == "resolved_view"
        assert cursor.fetchone()[0] == 2
    finally:
        conn.close()


def test_kpi_observation_dimensions_use_only_current_semantic_head() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY,kpi_definition_id INTEGER);
        CREATE TABLE kpi_fact_semantic_contexts (
            id INTEGER PRIMARY KEY,kpi_fact_id INTEGER,revision INTEGER,
            supersedes_context_id INTEGER,metric_name_as_reported TEXT,
            period_role TEXT,publication_lane TEXT,accounting_basis TEXT,
            consolidation_scope TEXT,dimensions_json TEXT,unit_scale TEXT,status TEXT
        );
        INSERT INTO kpi_facts VALUES (7,641);
        INSERT INTO kpi_fact_semantic_contexts VALUES
          (10,7,1,NULL,'Customers','prior_year_comparator','comparator','gaap',
           'geography','{"country":"BR"}','actual','admitted'),
          (11,7,2,10,'Total customers','current','current_actual','management',
           'consolidated','{}','millions','admitted');
        """
    )
    row = conn.execute("SELECT * FROM kpi_facts WHERE id=7").fetchone()
    dimensions = {item.key: item.value for item in _dimensions(conn, "kpi_facts", row)}
    assert dimensions == {
        "accounting_basis": "management",
        "consolidation_scope": "consolidated",
        "kpi_definition_id": "641",
        "metric_name_as_reported": "Total customers",
        "period_role": "current",
        "publication_lane": "current_actual",
        "semantic_status": "admitted",
        "unit_scale": "millions",
    }
    conn.close()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _create_legacy_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start DATETIME,
            period_end DATETIME,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            fetched_at DATETIME NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER,
            source_quality_tier TEXT NOT NULL,
            accession_number TEXT,
            filing_date TEXT
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            confidence REAL NOT NULL DEFAULT 1.0,
            extracted_by TEXT,
            supersedes_id INTEGER,
            locator TEXT
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            confidence REAL NOT NULL DEFAULT 1.0,
            extracted_by TEXT,
            supersedes_id INTEGER,
            locator TEXT,
            source_excerpt TEXT,
            computed_from TEXT,
            formula_id INTEGER,
            formula_version INTEGER
        );
        """
    )


def _database(tmp_path: Path, *, migrate: bool = True) -> tuple[Path, sqlite3.Connection]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "fact-cutover.db"
    conn = sqlite3.connect(path)
    _create_legacy_schema(conn)
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, PRIOR_HEAD if not migrate else HEAD)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return path, conn


def _seed_document(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    source_tier: str,
    value_suffix: str,
    source_type: str | None = None,
    source_clock: datetime = STAMP,
    document_period_start: datetime | None = datetime(2026, 4, 1),
    document_period_end: datetime | None = datetime(2026, 6, 30),
) -> None:
    blob_sha = hashlib.sha256(f"document-{document_id}".encode()).hexdigest()
    config_sha = hashlib.sha256(b"config").hexdigest()
    output_sha = hashlib.sha256(f"output-{document_id}".encode()).hexdigest()
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, period_start, period_end, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, source_url, source_quality_tier, "
        "accession_number, filing_date) "
        "VALUES (?, 'ACME', ?, 'sec_10q', ?, ?, ?, ?, ?, 'ok', 42, ?, ?, ?, ?)",
        (
            document_id,
            source_type or ("sec_xbrl" if source_tier == "sec_official" else "fmp"),
            document_period_start,
            document_period_end,
            f"data/document-{document_id}.json",
            blob_sha,
            STAMP,
            f"https://example.test/{document_id}",
            source_tier,
            f"0000000000-26-{document_id:06d}",
            "2026-07-20",
        ),
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=42,
            media_type="application/json",
            storage_uri=f"file:///evidence/{document_id}.json",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"source-{document_id}",
            idempotency_key=f"source-{document_id}",
            source_kind="sec_filing",
            source_url=f"https://example.test/{document_id}",
            blob_sha256=blob_sha,
            source_published_at=source_clock,
            filing_at=source_clock,
            accepted_at=source_clock,
            observed_at=source_clock,
            retrieved_at=source_clock,
            retrieval_config_sha256=config_sha,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=f"document-version-{document_id}",
            document_key=f"ACME:2026Q2:{document_id}",
            version_sequence=1,
            observation_id=f"source-{document_id}",
            blob_sha256=blob_sha,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="10-Q",
            form_type="10-Q",
            accession_number=f"0000000000-26-{document_id:06d}",
            exhibit_id=None,
            period_start=document_period_start,
            period_end=document_period_end,
            as_of_at=document_period_end,
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=document_id,
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=f"run-{document_id}",
            idempotency_key=f"run-{document_id}",
            document_version_id=f"document-version-{document_id}",
            input_sha256=blob_sha,
            extractor_name="test-parser",
            extractor_config_sha256=config_sha,
            extractor_code_version="test@1",
            output_sha256=output_sha,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id=f"node-document-{document_id}",
            evidence_key=f"document-{document_id}",
            revision=1,
            extraction_run_id=f"run-{document_id}",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text=f"Document {document_id} {value_suffix}",
            locator=None,
            recorded_at=STAMP,
        )
    )


def _insert_financial_fact(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    value: str,
    unit: str = "actual",
    period_end: str = "2026-06-30",
    line_item: str = "revenue",
) -> int:
    cursor = conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
        "source_doc_id, confidence, extracted_by, locator) "
        "VALUES ('ACME', ?, 'Q2', ?, ?, 'USD', ?, ?, 0.99, "
        "'deterministic:test', '{\"json_path\":\"[0].revenue\"}')",
        (period_end, line_item, value, unit, document_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _insert_kpi_fact(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    value: str,
    unit: str,
    currency: str | None = None,
    supersedes_id: int | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO kpi_definitions (id, ticker, name, unit) "
        "VALUES (1, 'ACME', 'Active customers', ?)",
        (unit,),
    )
    if currency is None:
        cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "source_doc_id, confidence, extracted_by, locator, supersedes_id) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 1, ?, ?, ?, 0.95, "
            "'deterministic:test', '{\"pdf_page\":3}', ?)",
            (value, unit, document_id, supersedes_id),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, currency, "
            "source_doc_id, confidence, extracted_by, locator, supersedes_id) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 1, ?, ?, ?, ?, 0.95, "
            "'deterministic:test', '{\"pdf_page\":3}', ?)",
            (value, unit, currency, document_id, supersedes_id),
        )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_transform_series_carries_exact_selected_legacy_identity(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "typed-series.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_kpi_fact(conn, document_id=1, value="114.2", unit="millions")
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (706,'ACME','Active customers','millions','sec_xbrl')"
        )
        conn.execute("UPDATE kpi_facts SET kpi_definition_id=706 WHERE id=?", (fact_id,))
        admit_all_kpi_facts(conn)
        assert (
            resolve_fact_row(
                conn, fact_table="kpi_facts", fact_row_id=fact_id, knowledge_cutoff=STAMP
            )
            is not None
        )
        points = _fetch_full_kpi_series(conn, "ACME", "Active customers", Unit.MILLIONS)
        assert len(points) == 1 and points[0].selected_input is not None
        selected = points[0].selected_input
        link = conn.execute(
            "SELECT fact_revision,observation_id FROM fact_observation_revisions "
            "WHERE fact_table='kpi_facts' AND fact_row_id=? "
            "ORDER BY fact_revision DESC LIMIT 1",
            (fact_id,),
        ).fetchone()
        assert link is not None
        assert selected.fact_table == "kpi_facts"
        assert selected.fact_id == fact_id
        assert selected.fact_revision == link["fact_revision"]
        assert selected.observation_id == link["observation_id"]
        assert selected.doc_id == 1
        assert selected.document_version_id == "document-version-1"
        assert selected.resolution_id is not None
        assert selected.resolution_revision == 1
    finally:
        conn.close()


def test_targeted_resolution_preview_is_read_only(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_kpi_fact(conn, document_id=1, value="114.2", unit="millions")

        preview = resolve_fact_row(
            conn,
            fact_table="kpi_facts",
            fact_row_id=fact_id,
            knowledge_cutoff=STAMP,
            persist=False,
        )

        assert preview is not None
        assert preview.resolution_status == "resolved"
        assert preview.selected_observation_id == f"kpi_facts:{fact_id}:r1"
        assert (
            conn.execute("SELECT COUNT(*) FROM observation_resolution_revisions").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM fact_resolution_outcomes").fetchone()[0] == 0
    finally:
        conn.close()


def test_resolution_keeps_predecessor_until_successor_is_available(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    successor_clock = datetime(2026, 7, 28, 12, 0, 0)
    try:
        _seed_document(
            conn,
            document_id=1,
            source_tier="sec_official",
            value_suffix="original",
            source_clock=STAMP,
        )
        predecessor_id = _insert_kpi_fact(conn, document_id=1, value="100", unit="millions")
        _seed_document(
            conn,
            document_id=2,
            source_tier="sec_official",
            value_suffix="correction",
            source_clock=successor_clock,
        )
        successor_id = _insert_kpi_fact(
            conn,
            document_id=2,
            value="101",
            unit="millions",
            supersedes_id=predecessor_id,
        )
        # Recreate a legacy timestamp representation without weakening the
        # production append-only schema: this disposable fixture is never
        # persisted beyond the test.
        conn.execute("DROP TRIGGER trg_reported_observations_append_only")
        conn.execute(
            "UPDATE reported_observations SET available_at=REPLACE(available_at,' ','T') "
            "WHERE observation_id=?",
            (f"kpi_facts:{successor_id}:r1",),
        )
        stored_successor_clock = str(
            conn.execute(
                "SELECT observation.available_at "
                "FROM fact_observation_revisions AS revision "
                "JOIN reported_observations AS observation USING (observation_id) "
                "WHERE revision.fact_table='kpi_facts' AND revision.fact_row_id=?",
                (successor_id,),
            ).fetchone()[0]
        )
        assert "T" in stored_successor_clock

        before = resolve_fact_row(
            conn,
            fact_table="kpi_facts",
            fact_row_id=predecessor_id,
            knowledge_cutoff=STAMP,
            persist=False,
        )
        after = resolve_fact_row(
            conn,
            fact_table="kpi_facts",
            fact_row_id=successor_id,
            knowledge_cutoff=successor_clock,
            persist=False,
        )

        assert before is not None
        assert before.selected_observation_id == f"kpi_facts:{predecessor_id}:r1"
        assert after is not None
        assert after.selected_observation_id == f"kpi_facts:{successor_id}:r1"
    finally:
        conn.close()


def test_targeted_resolution_cli_previews_then_applies_one_fact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = migrated_db(tmp_path / "targeted-resolution.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP.isoformat()),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_kpi_fact(conn, document_id=1, value="114.2", unit="millions")
        conn.commit()
    finally:
        conn.close()
    args = [
        "--db",
        str(db_path),
        "--fact-table",
        "kpi_facts",
        "--fact-row-id",
        str(fact_id),
        "--knowledge-cutoff",
        STAMP.isoformat(),
    ]

    assert cutover_cli.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["resolution_status"] == "resolved"
    check = sqlite3.connect(db_path)
    try:
        assert (
            check.execute("SELECT COUNT(*) FROM observation_resolution_revisions").fetchone()[0]
            == 0
        )
    finally:
        check.close()

    lock_held = False
    actual_hold_run_lock = cutover_cli.hold_run_lock
    actual_connect_sqlite = cutover_cli.connect_sqlite

    @contextmanager
    def tracked_target_lock(path: Path, *, owner: str) -> Generator[object, None, None]:
        nonlocal lock_held
        assert path == db_path
        with actual_hold_run_lock(path, owner=owner, timeout_s=0) as lock:
            lock_held = True
            try:
                yield lock
            finally:
                lock_held = False

    def checked_connect(
        path: Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool = False,
    ) -> sqlite3.Connection:
        if role is SQLiteConnectionRole.WRITER:
            assert lock_held, "writer connection opened before exact database lock"
        return actual_connect_sqlite(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(cutover_cli, "hold_run_lock", tracked_target_lock)
    monkeypatch.setattr(cutover_cli, "connect_sqlite", checked_connect)
    decoy_db = tmp_path / "decoy.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(decoy_db))
    with hold_run_lock(decoy_db, owner="decoy-environment-lock", timeout_s=0):
        assert cutover_cli.main([*args, "--apply"]) == 0
        applied = json.loads(capsys.readouterr().out)
    assert applied["resolution_status"] == "resolved"
    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT id FROM v_kpi_facts_resolved_current WHERE id=?", (fact_id,)
        ).fetchone() == (fact_id,)
    finally:
        check.close()


def test_targeted_resolution_rejects_predecessor_and_commits_exact_successor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "targeted-exact-row.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP.isoformat()),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="old")
        predecessor_id = _insert_kpi_fact(conn, document_id=1, value="100", unit="millions")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="new")
        successor_id = _insert_kpi_fact(
            conn,
            document_id=2,
            value="101",
            unit="millions",
            supersedes_id=predecessor_id,
        )
        conn.commit()
    finally:
        conn.close()

    base_args = [
        "--db",
        str(db_path),
        "--fact-table",
        "kpi_facts",
        "--knowledge-cutoff",
        STAMP.isoformat(),
        "--apply",
    ]
    with pytest.raises(RuntimeError, match="is not the exact resolved observation"):
        cutover_cli.main([*base_args, "--fact-row-id", str(predecessor_id)])

    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM observation_resolution_revisions"
        ).fetchone() == (0,)
        assert check.execute("SELECT COUNT(*) FROM fact_resolution_outcomes").fetchone() == (0,)
    finally:
        check.close()

    assert cutover_cli.main([*base_args, "--fact-row-id", str(successor_id)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["selected_observation_id"] == f"kpi_facts:{successor_id}:r1"
    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT id FROM v_kpi_facts_resolved_current WHERE id=?", (successor_id,)
        ).fetchone() == (successor_id,)
    finally:
        check.close()


def test_migration_captures_every_insert_and_semantic_update_and_blocks_delete(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_financial_fact(conn, document_id=1, value="100")

        first = conn.execute(
            "SELECT link.fact_revision, observation.numeric_value, link.source_document_id "
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id)"
        ).fetchone()
        assert first is not None
        assert tuple(first) == (1, "100", 1)

        conn.execute(
            "UPDATE financial_facts SET value = '101', confidence = 0.98 WHERE id = ?",
            (fact_id,),
        )
        revisions = conn.execute(
            "SELECT link.fact_revision, observation.numeric_value "
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id) "
            "ORDER BY link.fact_revision"
        ).fetchall()
        assert [tuple(row) for row in revisions] == [(1, "100"), (2, "101")]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM financial_facts WHERE id = ?", (fact_id,))
    finally:
        conn.close()


def test_resolution_preserves_complete_candidates_and_selects_higher_source_tier(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
        _insert_financial_fact(conn, document_id=1, value="98")
        sec_fact_id = _insert_financial_fact(conn, document_id=2, value="100")
        logical_key = str(
            conn.execute("SELECT logical_key FROM fact_observation_revisions LIMIT 1").fetchone()[0]
        )

        result = resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )

        assert result.candidate_count == 2
        assert result.resolution_status == "resolved"
        assert result.material_dissent is True
        candidates = conn.execute(
            "SELECT COUNT(*) FROM observation_resolution_candidates WHERE resolution_id = ?",
            (result.resolution_id,),
        ).fetchone()
        assert candidates is not None and candidates[0] == 2
        selected = conn.execute(
            "SELECT id, source_doc_id FROM v_financial_facts_resolved_current"
        ).fetchall()
        assert [tuple(row) for row in selected] == [(sec_fact_id, 2)]
    finally:
        conn.close()


def test_resolution_prefers_issuer_over_vendor_within_the_same_source_tier(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(
            conn,
            document_id=1,
            source_tier="fmp_normalized",
            source_type="ir_doc",
            value_suffix="issuer release",
        )
        _seed_document(
            conn,
            document_id=2,
            source_tier="fmp_normalized",
            source_type="fmp",
            value_suffix="newer vendor normalization",
            source_clock=datetime(2026, 7, 28, 12, 0, 0),
        )
        issuer_fact_id = _insert_financial_fact(conn, document_id=1, value="100")
        _insert_financial_fact(conn, document_id=2, value="100.5")
        logical_key = str(
            conn.execute("SELECT logical_key FROM fact_observation_revisions LIMIT 1").fetchone()[0]
        )

        result = resolve_fact_logical_key(
            conn,
            logical_key=logical_key,
            knowledge_cutoff=datetime(2026, 7, 29, 12, 0, 0),
            recorded_at=datetime(2026, 7, 29, 12, 0, 0),
        )

        assert result.resolution_status == "resolved"
        selected = conn.execute(
            "SELECT id, source_doc_id FROM v_financial_facts_resolved_current"
        ).fetchall()
        assert [tuple(row) for row in selected] == [(issuer_fact_id, 1)]
    finally:
        conn.close()


def test_resolution_fails_closed_on_any_top_authority_value_dissent(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(
            conn,
            document_id=1,
            source_tier="fmp_normalized",
            source_type="ir_doc",
            value_suffix="issuer release one",
        )
        _seed_document(
            conn,
            document_id=2,
            source_tier="fmp_normalized",
            source_type="ir_doc",
            value_suffix="issuer release two",
        )
        _insert_financial_fact(conn, document_id=1, value="100")
        _insert_financial_fact(conn, document_id=2, value="100.5")
        logical_key = str(
            conn.execute("SELECT logical_key FROM fact_observation_revisions LIMIT 1").fetchone()[0]
        )

        result = resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )

        assert result.resolution_status == "unresolved_material"
        assert result.material_dissent is True
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_unresolved_same_tier_material_conflict_fails_closed_without_deleting_candidates(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC one")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC two")
        _insert_financial_fact(conn, document_id=1, value="100")
        _insert_financial_fact(conn, document_id=2, value="130")
        logical_key = str(
            conn.execute("SELECT logical_key FROM fact_observation_revisions LIMIT 1").fetchone()[0]
        )

        result = resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )

        assert result.resolution_status == "unresolved_material"
        assert result.material_dissent is True
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM observation_resolution_candidates").fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_kpi_unit_conflict_is_evidence_anchored_and_fails_closed(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="KPI count")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="KPI millions")
        _insert_kpi_fact(conn, document_id=1, value="1000000", unit="count")
        _insert_kpi_fact(conn, document_id=2, value="1", unit="millions")
        link = conn.execute(
            "SELECT logical_key, observation.dimensions_json, observation.evidence_node_id "
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id) "
            "WHERE link.fact_table = 'kpi_facts' ORDER BY link.fact_row_id LIMIT 1"
        ).fetchone()
        assert link is not None
        assert str(link[1]) == '[{"key":"kpi_definition_id","value":"1"}]'
        assert str(link[2]) == "node-document-1"

        result = resolve_fact_logical_key(
            conn,
            logical_key=str(link[0]),
            knowledge_cutoff=STAMP,
            recorded_at=STAMP,
        )

        assert result.candidate_count == 2
        assert result.resolution_status == "unresolved_material"
        assert conn.execute("SELECT COUNT(*) FROM v_kpi_facts_resolved_current").fetchone()[0] == 0
        checks = conn.execute(
            "SELECT checks_json FROM fact_resolution_outcomes WHERE resolution_id = ?",
            (result.resolution_id,),
        ).fetchone()
        assert checks is not None
        assert json.loads(str(checks[0]))["unit_consistent"] is False
    finally:
        conn.close()


def test_backfill_is_dry_run_by_default_checkpointed_and_idempotent(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        # Suppress the capture trigger to model rows that predate migration 0225.
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        checkpoint = tmp_path / "fact-cutover-state.json"

        dry_run = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=False,
                batch_size=10,
                checkpoint_path=checkpoint,
                knowledge_cutoff=STAMP,
            ),
        )
        assert dry_run.rows_planned == 1
        assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 0
        assert not checkpoint.exists()

        applied = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=checkpoint,
                knowledge_cutoff=STAMP,
            ),
        )
        assert applied.rows_captured == 1
        assert applied.resolutions_created == 1
        assert checkpoint.exists()
        replay = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=checkpoint,
                knowledge_cutoff=STAMP,
            ),
        )
        assert replay.rows_captured == 0
        assert replay.resolutions_created == 0
    finally:
        conn.close()


def test_backfill_dry_run_reports_missing_evidence_without_writes(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        blob_sha = hashlib.sha256(b"legacy-document").hexdigest()
        conn.execute(
            "INSERT INTO documents "
            "(id, ticker, source_type, doc_type, period_start, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url, "
            "source_quality_tier, accession_number, filing_date) "
            "VALUES (1, 'ACME', 'sec_xbrl', 'sec_10q', '2026-04-01', '2026-06-30', "
            "'data/legacy-document.json', ?, ?, 'ok', 42, "
            "'https://example.test/legacy', 'sec_official', "
            "'0000000000-26-000001', '2026-07-20')",
            (blob_sha, STAMP),
        )
        # Model a fact captured before the evidence-ledger and fact-cutover triggers.
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        checkpoint = tmp_path / "fact-cutover-missing-evidence-state.json"

        dry_run = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=False,
                batch_size=10,
                checkpoint_path=checkpoint,
                knowledge_cutoff=STAMP,
            ),
        )

        assert dry_run.rows_considered == 1
        assert dry_run.rows_planned == 1
        assert dry_run.rows_quarantined == 1
        assert dry_run.finding_counts == {"missing_evidence_document_anchor": 1}
        assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 0
        assert not checkpoint.exists()
    finally:
        conn.close()


def test_backfill_retries_quarantine_after_processing_later_ready_rows(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        missing_sha = hashlib.sha256(b"missing-evidence").hexdigest()
        conn.execute(
            "INSERT INTO documents "
            "(id, ticker, source_type, doc_type, period_start, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url, "
            "source_quality_tier, accession_number, filing_date) "
            "VALUES (1, 'ACME', 'sec_xbrl', 'sec_10q', '2026-04-01', '2026-06-30', "
            "'data/missing-evidence.json', ?, ?, 'ok', 42, "
            "'https://example.test/missing', 'sec_official', "
            "'0000000000-26-000001', '2026-07-20')",
            (missing_sha, STAMP),
        )
        _seed_document(
            conn,
            document_id=2,
            source_tier="sec_official",
            value_suffix="ready",
        )
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="100")
        _insert_financial_fact(conn, document_id=2, value="200")
        conn.commit()
        checkpoint = tmp_path / "fact-cutover-quarantine-retry-state.json"
        request = FactCutoverRequest(
            apply=True,
            batch_size=1,
            checkpoint_path=checkpoint,
            knowledge_cutoff=STAMP,
        )

        first = execute_fact_cutover(conn, request)
        second = execute_fact_cutover(conn, request)
        third = execute_fact_cutover(conn, request)

        assert first.rows_quarantined == 1
        assert first.checkpoint_complete is False
        assert second.rows_captured == 1
        assert second.checkpoint_complete is False
        assert third.rows_quarantined == 1
        assert third.rows_considered == 1
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_backfill_accepts_aware_cutoff_for_naive_legacy_clocks(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()

        applied = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=tmp_path / "fact-cutover-aware-clock-state.json",
                knowledge_cutoff=STAMP.replace(tzinfo=UTC),
            ),
        )

        assert applied.rows_captured == 1
        assert applied.rows_quarantined == 0
        assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 1
    finally:
        conn.close()


def test_backfill_does_not_apply_a_newer_document_period_to_comparative_fact(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(
            conn,
            document_id=1,
            value="100",
            period_end="2025-12-31",
        )
        conn.commit()

        applied = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=tmp_path / "fact-cutover-comparative-state.json",
                knowledge_cutoff=STAMP,
            ),
        )

        assert applied.rows_captured == 1
        assert applied.rows_quarantined == 0
        period = conn.execute(
            "SELECT period_start, period_end FROM reported_observations"
        ).fetchone()
        assert period is not None
        assert str(period[0]) == "2025-12-31 00:00:00"
        assert str(period[1]) == "2025-12-31 00:00:00"
    finally:
        conn.close()


def test_resolution_orders_mixed_naive_and_aware_candidate_clocks(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(
            conn,
            document_id=1,
            source_tier="sec_official",
            value_suffix="naive",
        )
        _seed_document(
            conn,
            document_id=2,
            source_tier="fmp_normalized",
            value_suffix="aware",
            source_clock=STAMP.replace(tzinfo=UTC),
        )
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="100")
        _insert_financial_fact(conn, document_id=2, value="99")
        conn.commit()

        applied = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=tmp_path / "fact-cutover-mixed-clock-state.json",
                knowledge_cutoff=STAMP.replace(tzinfo=UTC),
            ),
        )

        assert applied.rows_captured == 2
        assert applied.resolutions_created == 1
        assert applied.rows_quarantined == 0
    finally:
        conn.close()


def test_backfill_uses_fact_end_when_document_period_is_unknown(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(
            conn,
            document_id=1,
            source_tier="sec_official",
            value_suffix="unknown-period",
            document_period_start=None,
            document_period_end=None,
        )
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()

        applied = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=tmp_path / "fact-cutover-unknown-period-state.json",
                knowledge_cutoff=STAMP,
            ),
        )

        assert applied.rows_captured == 1
        period = conn.execute(
            "SELECT period_start, period_end FROM reported_observations"
        ).fetchone()
        assert period is not None
        assert str(period[0]) == str(period[1])
    finally:
        conn.close()


def test_canonical_relation_uses_legacy_only_before_0225(tmp_path: Path) -> None:
    _, legacy_conn = _database(tmp_path / "legacy", migrate=False)
    try:
        assert canonical_fact_relation(legacy_conn, "financial_facts").selection_mode == (
            "legacy_pre_cutover"
        )
    finally:
        legacy_conn.close()
    _, current_conn = _database(tmp_path / "current")
    try:
        relation = canonical_fact_relation(current_conn, "financial_facts")
        assert relation.sql == "v_financial_facts_resolved_current"
        assert relation.selection_mode == "resolved_view"
    finally:
        current_conn.close()


def test_derived_relation_requires_exact_current_v1_input_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "derived-input-selection.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="CHILD")
        child_id = _insert_financial_fact(conn, document_id=2, value="100")
        assert (
            resolve_fact_row(
                conn, fact_table="financial_facts", fact_row_id=child_id, knowledge_cutoff=STAMP
            )
            is not None
        )
        child_link = conn.execute(
            "SELECT fact_revision,observation_id FROM fact_observation_revisions "
            "WHERE fact_table='financial_facts' AND fact_row_id=?",
            (child_id,),
        ).fetchone()
        child_resolution = conn.execute(
            "SELECT resolution_id,revision FROM v_observation_resolution_current "
            "WHERE selected_observation_id=?",
            (child_link["observation_id"],),
        ).fetchone()
        assert child_resolution is not None
        input_ref = {
            "ref": "legacy_canonical_fact",
            "fact_table": "financial_facts",
            "fact_id": child_id,
            "fact_revision": child_link["fact_revision"],
            "observation_id": child_link["observation_id"],
            "resolution_id": child_resolution["resolution_id"],
            "resolution_revision": child_resolution["revision"],
            "doc_id": 2,
            "document_version_id": "document-version-2",
            "item": "revenue",
        }
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (706,'ACME','Revenue ratio','percent','sec_xbrl')"
        )
        cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
            "source_doc_id,confidence,extracted_by,computed_from,formula_id,formula_version) "
            "VALUES ('ACME','2026-06-30','Q2',706,50,'percent',1,0.95,"
            "'fmp_derived',?,1,1)",
            (json.dumps({"inputs": [input_ref]}),),
        )
        assert cursor.lastrowid is not None
        derived_id = int(cursor.lastrowid)
        admit_all_kpi_facts(conn)
        assert (
            resolve_fact_row(
                conn, fact_table="kpi_facts", fact_row_id=derived_id, knowledge_cutoff=STAMP
            )
            is not None
        )
        relation = canonical_fact_relation(conn, "kpi_facts")
        selected = conn.execute(f"SELECT id FROM {relation.sql}").fetchall()
        assert [int(row[0]) for row in selected] == [derived_id]

        def selected_derived() -> tuple[list[int], list[int]]:
            current = [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")]
            historical = [
                item.fact_row_id
                for item in canonical_fact_selections_as_known(
                    conn,
                    fact_table="kpi_facts",
                    effective_at=STAMP.replace(tzinfo=UTC),
                    known_at=datetime(2027, 1, 1, tzinfo=UTC),
                )
            ]
            return current, historical

        assert selected_derived() == ([derived_id], [derived_id])
        conn.execute("SAVEPOINT numeric_tamper")
        conn.execute("DROP TRIGGER trg_financial_facts_observation_update")
        conn.execute("UPDATE financial_facts SET value=101 WHERE id=?", (child_id,))
        assert selected_derived() == ([], [])
        conn.execute("ROLLBACK TO numeric_tamper")
        conn.execute("RELEASE numeric_tamper")
        assert selected_derived() == ([derived_id], [derived_id])
        conn.execute("SAVEPOINT locator_tamper")
        conn.execute("DROP TRIGGER trg_financial_facts_observation_update")
        conn.execute(
            "UPDATE financial_facts SET locator=? WHERE id=?",
            (json.dumps({"page": 99}), child_id),
        )
        assert selected_derived() == ([], [])
        conn.execute("ROLLBACK TO locator_tamper")
        conn.execute("RELEASE locator_tamper")
        assert selected_derived() == ([derived_id], [derived_id])
        # An isolated corrupt-capture fixture proves Decimal parity. The real
        # observation table remains append-only under its installed trigger.
        conn.execute("SAVEPOINT capture_tamper")
        conn.execute("DROP TRIGGER trg_reported_observations_append_only")
        conn.execute(
            "UPDATE reported_observations SET method_version='manual-v1',"
            "numeric_value='100.00000000000000001' WHERE observation_id=?",
            (child_link["observation_id"],),
        )
        assert selected_derived() == ([], [])
        with pytest.raises(HistoricalFactAuthorityUnavailableError):
            canonical_fact_selections_as_known(
                conn,
                fact_table="financial_facts",
                effective_at=STAMP.replace(tzinfo=UTC),
                known_at=datetime(2027, 1, 1, tzinfo=UTC),
            )
        conn.execute("ROLLBACK TO capture_tamper")
        conn.execute("RELEASE capture_tamper")
        assert selected_derived() == ([derived_id], [derived_id])
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (707,'ACME','Invalid ratio','percent','sec_xbrl')"
        )
        invalid_cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
            "source_doc_id,confidence,extracted_by,computed_from,formula_id,formula_version) "
            "VALUES ('ACME','2026-06-30','Q2',707,50,'percent',1,0.95,"
            "'fmp_derived',?,1,1)",
            (json.dumps({"inputs": [{**input_ref, "resolution_revision": 999}]}),),
        )
        assert invalid_cursor.lastrowid is not None
        invalid_id = int(invalid_cursor.lastrowid)
        admit_all_kpi_facts(conn)
        assert (
            resolve_fact_row(
                conn, fact_table="kpi_facts", fact_row_id=invalid_id, knowledge_cutoff=STAMP
            )
            is not None
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [
            derived_id
        ]
        historical = canonical_fact_selections_as_known(
            conn,
            fact_table="kpi_facts",
            effective_at=STAMP.replace(tzinfo=UTC),
            known_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        assert [selection.fact_row_id for selection in historical] == [derived_id]
        assert (
            canonical_fact_selections_as_known(
                conn,
                fact_table="kpi_facts",
                effective_at=STAMP.replace(tzinfo=UTC),
                known_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
            == ()
        )
        quarantine_at = datetime(2027, 1, 2, tzinfo=UTC)
        conn.execute(
            "INSERT INTO document_semantic_disposition_revisions "
            "(assessment_id,idempotency_key,document_version_id,revision,semantic_status,"
            "reason_code,reason_details_json,decision_kind,reviewer_identity,policy_name,"
            "policy_version,policy_config_sha256,effective_at,knowledge_at,recorded_at,"
            "supersedes_assessment_id,material_dissent) "
            "VALUES ('child-quarantine','child-quarantine','document-version-2',1,"
            "'quarantined','source_review','{\"reason\":\"test\"}',"
            "'human','reviewer','source-test','v1',?,?,?,?,NULL,0)",
            ("a" * 64, quarantine_at, quarantine_at, quarantine_at),
        )
        assert conn.execute(f"SELECT id FROM {relation.sql}").fetchall() == []
        before_quarantine = canonical_fact_selections_as_known(
            conn,
            fact_table="kpi_facts",
            effective_at=STAMP.replace(tzinfo=UTC),
            known_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        assert [selection.fact_row_id for selection in before_quarantine] == [derived_id]
        assert (
            canonical_fact_selections_as_known(
                conn,
                fact_table="kpi_facts",
                effective_at=STAMP.replace(tzinfo=UTC),
                known_at=datetime(2027, 1, 3, tzinfo=UTC),
            )
            == ()
        )
        release_at = datetime(2027, 1, 4, tzinfo=UTC)
        conn.execute(
            "INSERT INTO document_semantic_disposition_revisions "
            "(assessment_id,idempotency_key,document_version_id,revision,semantic_status,"
            "reason_code,reason_details_json,decision_kind,reviewer_identity,policy_name,"
            "policy_version,policy_config_sha256,effective_at,knowledge_at,recorded_at,"
            "supersedes_assessment_id,material_dissent) "
            "VALUES ('child-release','child-release','document-version-2',2,"
            "'required','source_review','{\"reason\":\"test\"}',"
            "'human','reviewer','source-test','v1',?,?,?,?,'child-quarantine',0)",
            ("a" * 64, release_at, release_at, release_at),
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [
            derived_id
        ]
        after_release = canonical_fact_selections_as_known(
            conn,
            fact_table="kpi_facts",
            effective_at=STAMP.replace(tzinfo=UTC),
            known_at=datetime(2027, 1, 5, tzinfo=UTC),
        )
        assert [selection.fact_row_id for selection in after_release] == [derived_id]
        _seed_document(
            conn,
            document_id=4,
            source_tier="sec_official",
            value_suffix="RESTATEMENT",
            source_clock=datetime(2027, 1, 6, tzinfo=UTC),
        )
        replacement = conn.execute(
            "INSERT INTO financial_facts "
            "(ticker,period_end,fiscal_period_type,line_item,value,currency,unit,"
            "source_doc_id,confidence,extracted_by,locator,supersedes_id) "
            "VALUES ('ACME','2026-06-30','Q2','revenue',105,'USD','actual',4,0.99,"
            "'deterministic:test','{\"json_path\":\"[0].revenue\"}',?)",
            (child_id,),
        )
        assert replacement.lastrowid is not None
        assert (
            resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=int(replacement.lastrowid),
                knowledge_cutoff=datetime(2027, 1, 7, tzinfo=UTC),
            )
            is not None
        )
        assert conn.execute(f"SELECT id FROM {relation.sql}").fetchall() == []
        assert [
            selection.fact_row_id
            for selection in canonical_fact_selections_as_known(
                conn,
                fact_table="kpi_facts",
                effective_at=STAMP.replace(tzinfo=UTC),
                known_at=datetime(2027, 1, 5, tzinfo=UTC),
            )
        ] == [derived_id]
    finally:
        conn.close()


@pytest.mark.parametrize("monetary", [False, True])
def test_typed_kpi_child_requires_source_bound_definition_revision(
    tmp_path: Path, migrated_db: Callable[..., Path], monetary: bool
) -> None:
    path = migrated_db(tmp_path / f"derived-kpi-definition-{monetary}.db")
    conn = open_db(path)
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        conn.execute(
            "INSERT INTO reporting_entities "
            "(reporting_entity_id,idempotency_key,issuer_id,reporting_entity_kind,"
            "display_name,created_at) VALUES "
            "('entity-acme','entity-acme','issuer-acme','legal_registrant','ACME',?)",
            (STAMP,),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="OUTPUT")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="CHILD")
        _seed_document(conn, document_id=3, source_tier="sec_official", value_suffix="DEFINITION")
        locator = EvidenceLocator(page_number=3)
        EvidenceLedger(conn).persist(
            EvidenceNode(
                node_id="definition-node-3",
                evidence_key="definition-node-3",
                revision=1,
                extraction_run_id="run-3",
                parent_node_id="node-document-3",
                supersedes_node_id=None,
                node_kind="passage",
                text="Active customers",
                locator=locator,
                recorded_at=STAMP,
            )
        )
        child_unit = "actual" if monetary else "count"
        child_currency = "USD" if monetary else None
        child_id = _insert_kpi_fact(
            conn,
            document_id=2,
            value="100",
            unit=child_unit,
            currency=child_currency,
        )
        conn.execute(
            "UPDATE kpi_facts SET source_excerpt='Active customers: 100' WHERE id=?",
            (child_id,),
        )
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (706,'ACME','Active customers',?,'sec_xbrl')",
            (child_unit,),
        )
        conn.execute("UPDATE kpi_facts SET kpi_definition_id=706 WHERE id=?", (child_id,))
        admit_all_kpi_facts(conn)
        assert current_kpi_semantic_context(conn, kpi_fact_id=child_id) is not None
        revision = definition_fixture(
            kpi_definition_revision_id="definition-acme-customers-r1",
            idempotency_key="definition-acme-customers-r1",
            kpi_definition_id=706,
            reporting_entity_id="entity-acme",
            reported_label="Active customers",
            reported_definition_text="Active customers",
            period_kind=KpiDefinitionPeriodKind.INSTANT,
            stock_flow_behavior=KpiStockFlowBehavior.STOCK,
            unit_family=KpiUnitFamily.CURRENCY if monetary else KpiUnitFamily.COUNT,
            unit_key=Unit.ACTUAL if monetary else Unit.COUNT,
            currency_disposition=(
                KpiCurrencyDisposition.EXPLICIT
                if monetary
                else KpiCurrencyDisposition.NOT_APPLICABLE
            ),
            currency=Currency.USD if monetary else None,
            source_document_version_id="document-version-3",
            source_evidence_node_id="definition-node-3",
            source_locator=json.loads(locator.canonical_json),
            effective_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        persist_kpi_definition_revision(conn, revision)
        original_context = current_kpi_semantic_context(conn, kpi_fact_id=child_id)
        assert original_context is not None
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=child_id,
            context=original_context.context,
            knowledge_at=datetime(2026, 10, 24, tzinfo=UTC),
            kpi_definition_revision_id=revision.kpi_definition_revision_id,
        )
        child_result = resolve_fact_row(
            conn,
            fact_table="kpi_facts",
            fact_row_id=child_id,
            knowledge_cutoff=datetime(2026, 10, 24, tzinfo=UTC),
        )
        assert child_result is not None
        assert child_result.selected_observation_id is not None, child_result
        selected_concept = conn.execute(
            "SELECT concept_key FROM reported_observations WHERE observation_id=?",
            (child_result.selected_observation_id,),
        ).fetchone()
        assert selected_concept is not None
        assert selected_concept[0] == "kpi_definition:706"
        link = conn.execute(
            "SELECT fact_revision,observation_id FROM fact_observation_revisions "
            "WHERE fact_table='kpi_facts' AND fact_row_id=? "
            "ORDER BY fact_revision DESC LIMIT 1",
            (child_id,),
        ).fetchone()
        resolution = conn.execute(
            "SELECT resolution_id,revision FROM v_observation_resolution_current "
            "WHERE selected_observation_id=?",
            (link["observation_id"],),
        ).fetchone()
        assert resolution is not None
        typed = {
            "ref": "legacy_canonical_fact",
            "fact_table": "kpi_facts",
            "fact_id": child_id,
            "fact_revision": link["fact_revision"],
            "observation_id": link["observation_id"],
            "resolution_id": resolution["resolution_id"],
            "resolution_revision": resolution["revision"],
            "doc_id": 2,
            "document_version_id": "document-version-2",
            "item": "Active customers",
        }
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (707,'ACME','Customer ratio','percent','sec_xbrl')"
        )
        cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
            "source_doc_id,confidence,extracted_by,computed_from,formula_id,formula_version) "
            "VALUES ('ACME','2026-06-30','Q2',707,50,'percent',1,0.95,"
            "'fmp_derived',?,1,1)",
            (json.dumps({"inputs": [typed]}),),
        )
        assert cursor.lastrowid is not None
        output_id = int(cursor.lastrowid)
        admit_all_kpi_facts(conn)
        assert (
            resolve_fact_row(
                conn,
                fact_table="kpi_facts",
                fact_row_id=output_id,
                knowledge_cutoff=datetime(2026, 10, 24, tzinfo=UTC),
            )
            is not None
        )
        relation = canonical_fact_relation(conn, "kpi_facts")
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [
            child_id,
            output_id,
        ]
        captured_dimensions = conn.execute(
            "SELECT dimensions_json FROM reported_observations WHERE observation_id=?",
            (child_result.selected_observation_id,),
        ).fetchone()
        assert captured_dimensions is not None
        assert json.loads(str(captured_dimensions[0])) == [
            {"key": "kpi_definition_id", "value": "706"}
        ]
        before_change = datetime(2026, 10, 25, tzinfo=UTC)
        if monetary:
            captured_currency = conn.execute(
                "SELECT currency FROM reported_observations WHERE observation_id=?",
                (child_result.selected_observation_id,),
            ).fetchone()
            assert captured_currency is not None
            assert captured_currency[0] == "USD"
            assert [
                selection.fact_row_id
                for selection in canonical_fact_selections_as_known(
                    conn,
                    fact_table="kpi_facts",
                    effective_at=STAMP.replace(tzinfo=UTC),
                    known_at=before_change,
                )
            ] == [child_id, output_id]
        conn.execute("DROP TRIGGER trg_reported_observations_append_only")
        if monetary:
            # Fault injection: a persisted observation cannot be changed in production.
            conn.execute(
                "UPDATE reported_observations SET currency='EUR' WHERE observation_id=?",
                (child_result.selected_observation_id,),
            )
            assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [
                child_id
            ]
            assert [
                selection.fact_row_id
                for selection in canonical_fact_selections_as_known(
                    conn,
                    fact_table="kpi_facts",
                    effective_at=STAMP.replace(tzinfo=UTC),
                    known_at=before_change,
                )
            ] == [child_id]
            conn.execute(
                "UPDATE reported_observations SET currency='USD' WHERE observation_id=?",
                (child_result.selected_observation_id,),
            )
        conn.execute(
            "UPDATE reported_observations SET dimensions_json=? WHERE observation_id=?",
            (
                json.dumps(
                    [
                        {"key": "kpi_definition_id", "value": "706"},
                        {"key": "semantic_status", "value": "admitted"},
                    ]
                ),
                child_result.selected_observation_id,
            ),
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [child_id]
        child_row = conn.execute("SELECT * FROM kpi_facts WHERE id=?", (child_id,)).fetchone()
        assert child_row is not None
        full_dimensions = [
            dimension.model_dump()
            for dimension in _dimensions(
                conn,
                "kpi_facts",
                child_row,
            )
        ]
        conn.execute(
            "UPDATE reported_observations SET dimensions_json=? WHERE observation_id=?",
            (json.dumps(full_dimensions), child_result.selected_observation_id),
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [
            child_id,
            output_id,
        ]
        conn.execute(
            "UPDATE reported_observations SET dimensions_json=? WHERE observation_id=?",
            (captured_dimensions[0], child_result.selected_observation_id),
        )
        assert [
            selection.fact_row_id
            for selection in canonical_fact_selections_as_known(
                conn,
                fact_table="kpi_facts",
                effective_at=STAMP.replace(tzinfo=UTC),
                known_at=before_change,
            )
        ] == [child_id, output_id]
        quarantine_at = datetime(2026, 11, 1, tzinfo=UTC)
        conn.execute(
            "INSERT INTO document_semantic_disposition_revisions "
            "(assessment_id,idempotency_key,document_version_id,revision,semantic_status,"
            "reason_code,reason_details_json,decision_kind,reviewer_identity,policy_name,"
            "policy_version,policy_config_sha256,effective_at,knowledge_at,recorded_at,"
            "supersedes_assessment_id,material_dissent) "
            "VALUES ('definition-quarantine','definition-quarantine','document-version-3',1,"
            "'quarantined','source_review','{\"reason\":\"test\"}',"
            "'human','reviewer','source-test','v1',?,?,?,?,NULL,0)",
            ("a" * 64, quarantine_at, quarantine_at, quarantine_at),
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [child_id]
        assert [
            selection.fact_row_id
            for selection in canonical_fact_selections_as_known(
                conn,
                fact_table="kpi_facts",
                effective_at=STAMP.replace(tzinfo=UTC),
                known_at=before_change,
            )
        ] == [child_id, output_id]
        release_at = datetime(2026, 11, 3, tzinfo=UTC)
        conn.execute(
            "INSERT INTO document_semantic_disposition_revisions "
            "(assessment_id,idempotency_key,document_version_id,revision,semantic_status,"
            "reason_code,reason_details_json,decision_kind,reviewer_identity,policy_name,"
            "policy_version,policy_config_sha256,effective_at,knowledge_at,recorded_at,"
            "supersedes_assessment_id,material_dissent) "
            "VALUES ('definition-release','definition-release','document-version-3',2,"
            "'required','source_review','{\"reason\":\"test\"}',"
            "'human','reviewer','source-test','v1',?,?,?,?,'definition-quarantine',0)",
            ("a" * 64, release_at, release_at, release_at),
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [
            child_id,
            output_id,
        ]
        current_context = current_kpi_semantic_context(conn, kpi_fact_id=child_id)
        assert current_context is not None
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=child_id,
            context=current_context.context,
            knowledge_at=datetime(2026, 11, 4, tzinfo=UTC),
            kpi_definition_revision_id=None,
        )
        assert [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")] == [child_id]
    finally:
        conn.close()


def test_derived_reader_batches_163_selected_outputs_with_ledger_history(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "derived-163-read.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="OUTPUT")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="CHILD")
        _seed_document(conn, document_id=3, source_tier="fmp_normalized", value_suffix="COMPETITOR")
        for ordinal in range(163):
            line_item = f"component_{ordinal}"
            child_id = _insert_financial_fact(
                conn,
                document_id=2,
                value=str(100 + ordinal),
                line_item=line_item,
            )
            child_result = resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=child_id,
                knowledge_cutoff=STAMP,
            )
            assert child_result is not None
            assert child_result.selected_observation_id is not None
            if ordinal % 10 == 0:
                competitor_id = _insert_financial_fact(
                    conn,
                    document_id=3,
                    value=str(101 + ordinal),
                    line_item=line_item,
                )
                assert (
                    resolve_fact_row(
                        conn,
                        fact_table="financial_facts",
                        fact_row_id=competitor_id,
                        knowledge_cutoff=STAMP,
                    )
                    is not None
                )
            link = conn.execute(
                "SELECT fact_revision,observation_id FROM fact_observation_revisions "
                "WHERE fact_table='financial_facts' AND fact_row_id=? "
                "ORDER BY fact_revision DESC LIMIT 1",
                (child_id,),
            ).fetchone()
            resolution = conn.execute(
                "SELECT resolution_id,revision FROM v_observation_resolution_current "
                "WHERE selected_observation_id=?",
                (link["observation_id"],),
            ).fetchone()
            assert resolution is not None
            ref = {
                "ref": "legacy_canonical_fact",
                "fact_table": "financial_facts",
                "fact_id": child_id,
                "fact_revision": link["fact_revision"],
                "observation_id": link["observation_id"],
                "resolution_id": resolution["resolution_id"],
                "resolution_revision": resolution["revision"],
                "doc_id": 2,
                "document_version_id": "document-version-2",
                "item": line_item,
            }
            definition_id = 1000 + ordinal
            conn.execute(
                "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
                "VALUES (?,'ACME',?,'percent','sec_xbrl')",
                (definition_id, f"Derived metric {ordinal}"),
            )
            conn.execute(
                "INSERT INTO kpi_facts "
                "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
                "source_doc_id,confidence,extracted_by,computed_from,formula_id,formula_version) "
                "VALUES ('ACME','2026-06-30','Q2',?,50,'percent',1,0.95,"
                "'fmp_derived',?,1,1)",
                (definition_id, json.dumps({"inputs": [ref]})),
            )
        admit_all_kpi_facts(conn)
        output_rows = conn.execute("SELECT id FROM kpi_facts ORDER BY id").fetchall()
        assert len(output_rows) == 163
        for output_row in output_rows:
            assert (
                resolve_fact_row(
                    conn,
                    fact_table="kpi_facts",
                    fact_row_id=int(output_row[0]),
                    knowledge_cutoff=datetime(2027, 1, 1, tzinfo=UTC),
                )
                is not None
            )
        traced: list[str] = []
        conn.set_trace_callback(traced.append)
        current_start = perf_counter()
        relation = canonical_fact_relation(conn, "kpi_facts")
        current = [int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}")]
        current_elapsed = perf_counter() - current_start
        historical_start = perf_counter()
        historical = canonical_fact_selections_as_known(
            conn,
            fact_table="kpi_facts",
            effective_at=STAMP.replace(tzinfo=UTC),
            known_at=datetime(2027, 1, 2, tzinfo=UTC),
        )
        historical_elapsed = perf_counter() - historical_start
        conn.set_trace_callback(None)
        assert len(current) == 163
        assert len(historical) == 163
        assert len(traced) <= 30
        print(
            f"derived-reader-163 current={current_elapsed:.3f}s "
            f"as_known={historical_elapsed:.3f}s statements={len(traced)}"
        )
    finally:
        conn.close()


def test_derived_reader_accepts_32_observations_and_rejects_33(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "derived-depth.db")
    conn = open_db(path)
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="OUTPUT")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="CHILD")
        _seed_document(conn, document_id=3, source_tier="sec_official", value_suffix="DEFINITION")
        conn.execute(
            "INSERT INTO reporting_entities "
            "(reporting_entity_id,idempotency_key,issuer_id,reporting_entity_kind,"
            "display_name,created_at) VALUES "
            "('entity-acme','entity-acme','issuer-acme','legal_registrant','ACME',?)",
            (STAMP,),
        )
        locator = EvidenceLocator(page_number=3)
        EvidenceLedger(conn).persist(
            EvidenceNode(
                node_id="depth-definition-node",
                evidence_key="depth-definition-node",
                revision=1,
                extraction_run_id="run-3",
                parent_node_id="node-document-3",
                supersedes_node_id=None,
                node_kind="passage",
                text="Derived metric definition",
                locator=locator,
                recorded_at=STAMP,
            )
        )
        leaf_id = _insert_financial_fact(conn, document_id=2, value="100")
        assert (
            resolve_fact_row(
                conn, fact_table="financial_facts", fact_row_id=leaf_id, knowledge_cutoff=STAMP
            )
            is not None
        )
        prior_table = "financial_facts"
        prior_id = leaf_id
        prior_item = "revenue"
        prior_doc = 2
        root_ids: list[int] = []
        for level in range(1, 33):
            link = conn.execute(
                "SELECT fact_revision,observation_id FROM fact_observation_revisions "
                "WHERE fact_table=? AND fact_row_id=? ORDER BY fact_revision DESC LIMIT 1",
                (prior_table, prior_id),
            ).fetchone()
            resolution = conn.execute(
                "SELECT resolution_id,revision FROM v_observation_resolution_current "
                "WHERE selected_observation_id=?",
                (link["observation_id"],),
            ).fetchone()
            assert resolution is not None
            ref = {
                "ref": "legacy_canonical_fact",
                "fact_table": prior_table,
                "fact_id": prior_id,
                "fact_revision": link["fact_revision"],
                "observation_id": link["observation_id"],
                "resolution_id": resolution["resolution_id"],
                "resolution_revision": resolution["revision"],
                "doc_id": prior_doc,
                "document_version_id": f"document-version-{prior_doc}",
                "item": prior_item,
            }
            definition_id = 2000 + level
            label = f"Derived metric {level}"
            conn.execute(
                "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
                "VALUES (?,'ACME',?,'percent','sec_xbrl')",
                (definition_id, label),
            )
            cursor = conn.execute(
                "INSERT INTO kpi_facts "
                "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
                "source_doc_id,confidence,extracted_by,computed_from,formula_id,formula_version) "
                "VALUES ('ACME','2026-06-30','Q2',?,50,'percent',1,0.95,"
                "'fmp_derived',?,1,1)",
                (definition_id, json.dumps({"inputs": [ref]})),
            )
            assert cursor.lastrowid is not None
            current_id = int(cursor.lastrowid)
            admit_all_kpi_facts(conn)
            definition = definition_fixture(
                kpi_definition_revision_id=f"depth-definition-{level}",
                idempotency_key=f"depth-definition-{level}",
                kpi_definition_id=definition_id,
                reporting_entity_id="entity-acme",
                reported_label=label,
                reported_definition_text="Derived metric definition",
                period_kind=KpiDefinitionPeriodKind.DURATION,
                stock_flow_behavior=KpiStockFlowBehavior.RATIO,
                unit_family=KpiUnitFamily.PERCENTAGE,
                unit_key=Unit.PERCENT,
                currency_disposition=KpiCurrencyDisposition.NOT_APPLICABLE,
                currency=None,
                source_document_version_id="document-version-3",
                source_evidence_node_id="depth-definition-node",
                source_locator=json.loads(locator.canonical_json),
                effective_at=datetime(2026, 4, 1, tzinfo=UTC),
            )
            persist_kpi_definition_revision(conn, definition)
            context = current_kpi_semantic_context(conn, kpi_fact_id=current_id)
            assert context is not None
            persist_kpi_semantic_context(
                conn,
                kpi_fact_id=current_id,
                context=context.context,
                knowledge_at=datetime(2026, 10, 24, tzinfo=UTC),
                kpi_definition_revision_id=definition.kpi_definition_revision_id,
            )
            assert (
                resolve_fact_row(
                    conn,
                    fact_table="kpi_facts",
                    fact_row_id=current_id,
                    knowledge_cutoff=datetime(2026, 10, 24, tzinfo=UTC),
                )
                is not None
            )
            root_ids.append(current_id)
            prior_table = "kpi_facts"
            prior_id = current_id
            prior_item = label
            prior_doc = 1
        relation = canonical_fact_relation(conn, "kpi_facts")
        accepted = {
            int(row[0])
            for row in conn.execute(
                f"SELECT id FROM {relation.sql} WHERE id IN (?,?)", root_ids[-2:]
            )
        }
        assert accepted == {root_ids[-2]}
    finally:
        conn.close()


def test_derived_reader_accepts_500_unique_observations_and_rejects_501(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "derived-width.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="OUTPUT")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="CHILD")
        inputs: list[dict[str, object]] = []
        for ordinal in range(500):
            line_item = f"wide_component_{ordinal}"
            child_id = _insert_financial_fact(conn, document_id=2, value="100", line_item=line_item)
            assert (
                resolve_fact_row(
                    conn,
                    fact_table="financial_facts",
                    fact_row_id=child_id,
                    knowledge_cutoff=STAMP,
                )
                is not None
            )
            link = conn.execute(
                "SELECT fact_revision,observation_id FROM fact_observation_revisions "
                "WHERE fact_table='financial_facts' AND fact_row_id=? "
                "ORDER BY fact_revision DESC LIMIT 1",
                (child_id,),
            ).fetchone()
            resolution = conn.execute(
                "SELECT resolution_id,revision FROM v_observation_resolution_current "
                "WHERE selected_observation_id=?",
                (link["observation_id"],),
            ).fetchone()
            assert resolution is not None
            inputs.append(
                {
                    "ref": "legacy_canonical_fact",
                    "fact_table": "financial_facts",
                    "fact_id": child_id,
                    "fact_revision": link["fact_revision"],
                    "observation_id": link["observation_id"],
                    "resolution_id": resolution["resolution_id"],
                    "resolution_revision": resolution["revision"],
                    "doc_id": 2,
                    "document_version_id": "document-version-2",
                    "item": line_item,
                }
            )
        output_ids: list[int] = []
        for count in (499, 500):
            definition_id = 3000 + count
            conn.execute(
                "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
                "VALUES (?,'ACME',?,'percent','sec_xbrl')",
                (definition_id, f"Wide derived {count}"),
            )
            cursor = conn.execute(
                "INSERT INTO kpi_facts "
                "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
                "source_doc_id,confidence,extracted_by,computed_from,formula_id,formula_version) "
                "VALUES ('ACME','2026-06-30','Q2',?,50,'percent',1,0.95,"
                "'fmp_derived',?,1,1)",
                (definition_id, json.dumps({"inputs": inputs[:count]})),
            )
            assert cursor.lastrowid is not None
            output_ids.append(int(cursor.lastrowid))
        admit_all_kpi_facts(conn)
        for output_id in output_ids:
            assert (
                resolve_fact_row(
                    conn,
                    fact_table="kpi_facts",
                    fact_row_id=output_id,
                    knowledge_cutoff=datetime(2027, 1, 1, tzinfo=UTC),
                )
                is not None
            )
        relation = canonical_fact_relation(conn, "kpi_facts")
        assert [
            int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql} ORDER BY id")
        ] == [output_ids[0]]
    finally:
        conn.close()


def test_canonical_timeseries_loader_reads_selected_resolution_and_fails_closed(
    tmp_path: Path,
) -> None:
    path, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
        _insert_financial_fact(conn, document_id=1, value="98")
        _insert_financial_fact(conn, document_id=2, value="100")
        logical_key = str(
            conn.execute("SELECT logical_key FROM fact_observation_revisions LIMIT 1").fetchone()[0]
        )
        resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )
        conn.commit()
        selected = load_financial_series("ACME", "revenue", db_path=path, period_types=("Q2",))
        assert [float(item.value) for item in selected] == [100.0]

        _seed_document(conn, document_id=3, source_tier="sec_official", value_suffix="SEC conflict")
        _insert_financial_fact(conn, document_id=3, value="130")
        resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )
        conn.commit()
        assert load_financial_series("ACME", "revenue", db_path=path, period_types=("Q2",)) == []
    finally:
        conn.close()


def test_canonical_writer_resolves_immediately_after_database_capture(tmp_path: Path) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id, _ = insert_with_restatement_detection(
            conn,
            ticker="ACME",
            period_end=datetime(2026, 6, 30),
            fiscal_period_type="Q2",
            line_item="revenue",
            value=Decimal("100"),
            currency="USD",
            unit="actual",
            source_doc_id=1,
            confidence=0.99,
            extracted_by="deterministic:test",
            locator='{"json_path":"[0].revenue"}',
        )
        assert fact_id is not None
        selected = conn.execute("SELECT id FROM v_financial_facts_resolved_current").fetchall()
        assert [int(row[0]) for row in selected] == [fact_id]
    finally:
        conn.close()


def test_0225_migration_round_trip_preserves_legacy_fact_rows(tmp_path: Path) -> None:
    path, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
    finally:
        conn.close()

    command.downgrade(_config(path), PRIOR_HEAD)
    downgraded = sqlite3.connect(path)
    try:
        assert downgraded.execute("SELECT id, value FROM financial_facts").fetchone() == (
            fact_id,
            100,
        )
        names = {
            str(row[0])
            for row in downgraded.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view', 'trigger')"
            )
        }
        assert "fact_observation_revisions" not in names
        assert "fact_resolution_outcomes" not in names
        assert "v_financial_facts_resolved_current" not in names
        assert "trg_financial_facts_observation_insert" not in names
        assert downgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        downgraded.close()


def test_integrity_auditor_flags_unbridged_fact_state_and_missing_capture_guard(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    try:
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC one")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC two")
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        orphan_id = _insert_financial_fact(conn, document_id=1, value="100")
        summary = audit_connection(conn, AuditOptions())
        by_code = {finding.code: finding for finding in summary.findings}
        assert by_code["FINANCIAL_FACTS_OBSERVATION_MISSING"].samples == (
            f"financial_facts:{orphan_id}",
        )
        assert "FINANCIAL_FACTS_CAPTURE_TRIGGER_MISSING" in by_code
    finally:
        conn.close()
