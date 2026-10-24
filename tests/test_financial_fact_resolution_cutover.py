"""Investor-grade cutover contracts for financial and KPI fact observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.config import Config
from upgrade_database import upgrade_database

import db as dbmod
from alembic import command
from execution import backfill_financial_fact_resolutions as cutover_cli
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    persist_kpi_semantic_context,
)
from pipeline.restatement_detector import insert_with_restatement_detection
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.financial_fact_resolution import (
    FactCutoverRequest,
    canonical_fact_relation,
    capture_fact_row_observation,
    execute_fact_cutover,
    resolve_fact_logical_key,
    resolve_fact_row,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from provenance.observation_resolution import ObservationResolutionLedger, ResolutionRevision
from run_lock import hold_run_lock
from sqlite_runtime import SQLiteConnectionRole
from timeseries.loaders import load_financial_series

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0224_expected_document_lifecycle"
HEAD = "0225_financial_fact_resolution_cutover"
STAMP = datetime(2026, 7, 27, 12, 0, 0)


def test_kpi_observation_dimensions_use_only_current_semantic_head(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    seed_sha256 = hashlib.sha256(
        Path(__file__).read_bytes() + Path(dbmod.__file__).read_bytes()
    ).hexdigest()

    def bootstrap(path: Path) -> None:
        legacy = sqlite3.connect(path)
        legacy.row_factory = sqlite3.Row
        try:
            with patch.object(dbmod, "get_connection", return_value=legacy) as open_database:
                dbmod.init_db()
                open_database.assert_called_once_with(allow_create=True)
        finally:
            legacy.close()

    def seed_legacy(path: Path) -> None:
        migrated_db(
            path,
            archived=True,
            upgrade_from="0000_baseline",
            before_upgrade=bootstrap,
            seed_sha256=seed_sha256,
            target=PRIOR_HEAD,
        )
        legacy = sqlite3.connect(path)
        try:
            _seed_document(
                legacy, document_id=1, source_tier="sec_official", value_suffix="KPI source"
            )
            fact_id = _insert_kpi_fact(
                legacy, document_id=1, value="1000000", unit="count", kpi_definition_id=641
            )
            legacy.execute(
                "UPDATE kpi_facts SET source_excerpt='1 million customers.' WHERE id=?",
                (fact_id,),
            )
            legacy.commit()
        finally:
            legacy.close()

    database = migrated_db(
        tmp_path / "current-head-dimensions.db",
        archived=True,
        upgrade_from=PRIOR_HEAD,
        before_upgrade=seed_legacy,
        seed_sha256=seed_sha256,
    )
    backup = tmp_path / "current-head-dimensions.pre-upgrade.db"
    upgraded = upgrade_database(
        database,
        repo_root=ROOT,
        runtime_root=tmp_path,
        backup_path=backup,
        allow_isolated_db=True,
    )
    assert upgraded.status == "bridged" and upgraded.backup_path == str(backup)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        fact_id = int(conn.execute("SELECT id FROM kpi_facts").fetchone()[0])
        assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 0
        observed_at = STAMP.replace(tzinfo=UTC)
        prior = KpiSemanticContext(
            metric_name_as_reported="Customers",
            reported_period_end=date(2025, 6, 30),
            period_role=KpiPeriodRole.PRIOR_YEAR_COMPARATOR,
            publication_lane=KpiPublicationLane.COMPARATOR,
            accounting_basis=KpiAccountingBasis.GAAP,
            consolidation_scope=KpiConsolidationScope.GEOGRAPHY,
            dimensions={"country": "BR"},
            unit_scale=KpiUnitScale.MILLIONS,
            source_value_text="1",
            status=KpiSemanticStatus.ADMITTED,
        )
        first = persist_kpi_semantic_context(
            conn, kpi_fact_id=fact_id, context=prior, knowledge_at=observed_at
        )
        current = prior.model_copy(
            update={
                "metric_name_as_reported": "Total customers",
                "reported_period_end": date(2026, 6, 30),
                "period_role": KpiPeriodRole.CURRENT,
                "publication_lane": KpiPublicationLane.CURRENT_ACTUAL,
                "accounting_basis": KpiAccountingBasis.MANAGEMENT,
                "consolidation_scope": KpiConsolidationScope.CONSOLIDATED,
                "dimensions": {},
            }
        )
        second = persist_kpi_semantic_context(
            conn, kpi_fact_id=fact_id, context=current, knowledge_at=observed_at
        )
        revisions = conn.execute(
            "SELECT id,revision,supersedes_context_id,dimensions_json "
            "FROM kpi_fact_semantic_contexts WHERE kpi_fact_id=? ORDER BY revision",
            (fact_id,),
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in revisions] == [
            (first, 1, None),
            (second, 2, first),
        ]
        assert json.loads(revisions[0][3]) == {"country": "BR"}
        assert capture_fact_row_observation(
            conn, fact_table="kpi_facts", fact_row_id=fact_id, recorded_at=observed_at
        )
        result = resolve_fact_row(
            conn, fact_table="kpi_facts", fact_row_id=fact_id, knowledge_cutoff=observed_at
        )
        assert result is not None and result.resolution_status == "resolved"
        payload = conn.execute(
            "SELECT dimensions_json FROM reported_observations WHERE observation_id=?",
            (result.selected_observation_id,),
        ).fetchone()[0]
        dimensions = {item["key"]: item["value"] for item in json.loads(payload)}
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
    finally:
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
    fiscal_period_type: str = "Q2",
    currency: str = "USD",
) -> int:
    cursor = conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
        "source_doc_id, confidence, extracted_by, locator) "
        "VALUES ('ACME', ?, ?, 'revenue', ?, ?, ?, ?, 0.99, "
        "'deterministic:test', '{\"json_path\":\"[0].revenue\"}')",
        (period_end, fiscal_period_type, value, currency, unit, document_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _insert_kpi_fact(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    value: str,
    unit: str,
    supersedes_id: int | None = None,
    kpi_definition_id: int = 1,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO kpi_definitions (id, ticker, name, unit) "
        "VALUES (?, 'ACME', 'Active customers', ?)",
        (kpi_definition_id, unit),
    )
    cursor = conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
        "source_doc_id, confidence, extracted_by, locator, supersedes_id) "
        "VALUES ('ACME', '2026-06-30', 'Q2', ?, ?, ?, ?, 0.95, "
        "'deterministic:test', '{\"pdf_page\":3}', ?)",
        (kpi_definition_id, value, unit, document_id, supersedes_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


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


@pytest.mark.parametrize(
    "source_start,source_end,period_consistent",
    [
        ("2026-04-01 00:00:00", "2026-06-30 00:00:00", True),
        ("2026-04-01T09:15:00+05:30", "2026-06-30T17:45:00+05:30", True),
        ("2026-04-02T00:00:00+00:00", "2026-06-30T00:00:00+00:00", False),
        ("2026-01-01T00:00:00+00:00", "2026-06-30T00:00:00+00:00", False),
    ],
)
def test_resolution_uses_source_reporting_dates_and_keeps_duration_boundaries(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    source_start: str,
    source_end: str,
    period_consistent: bool,
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "reporting-dates.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="vendor")
        _seed_document(
            conn,
            document_id=2,
            source_tier="sec_official",
            value_suffix="issuer",
            document_period_start=datetime.fromisoformat(source_start),
            document_period_end=datetime.fromisoformat(source_end),
        )
        _insert_financial_fact(conn, document_id=1, value="100")
        issuer_id = _insert_financial_fact(conn, document_id=2, value="100", period_end=source_end)
        raw_periods = conn.execute(
            "SELECT period_start,period_end FROM reported_observations ORDER BY observation_id"
        ).fetchall()
        raw_facts = conn.execute("SELECT * FROM financial_facts ORDER BY id").fetchall()
        assert raw_facts[1]["period_end"] == source_end
        result = resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=issuer_id, knowledge_cutoff=STAMP
        )
        assert result is not None and result.candidate_count == 2
        assert result.resolution_status == (
            "resolved" if period_consistent else "unresolved_material"
        )
        checks = json.loads(
            conn.execute(
                "SELECT checks_json FROM fact_resolution_outcomes WHERE resolution_id=?",
                (result.resolution_id,),
            ).fetchone()[0]
        )
        assert checks["period_consistent"] is period_consistent
        selected = conn.execute("SELECT id FROM v_financial_facts_resolved_current").fetchall()
        assert [row[0] for row in selected] == ([issuer_id] if period_consistent else [])
        assert (
            conn.execute(
                "SELECT policy_version FROM observation_resolution_revisions WHERE resolution_id=?",
                (result.resolution_id,),
            ).fetchone()[0]
            == "investor-grade-fact-resolution@3"
        )
        assert (
            conn.execute(
                "SELECT period_start,period_end FROM reported_observations ORDER BY observation_id"
            ).fetchall()
            == raw_periods
        )
        assert conn.execute("SELECT * FROM financial_facts ORDER BY id").fetchall() == raw_facts
    finally:
        conn.close()


@pytest.mark.parametrize("boundary", ["local_date", "fiscal_type"])
def test_resolution_keeps_distinct_reporting_date_and_fiscal_keys(
    tmp_path: Path, migrated_db: Callable[..., Path], boundary: str
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "distinct-period-keys.db"))
    conn.row_factory = sqlite3.Row
    try:
        ends = (
            ("2026-06-30T23:30:00-02:00", "2026-07-01T01:30:00+00:00")
            if boundary == "local_date"
            else ("2026-06-30", "2026-06-30")
        )
        if boundary == "local_date":
            assert datetime.fromisoformat(ends[0]) == datetime.fromisoformat(ends[1])
            assert ends[0][:10] != ends[1][:10]
        logical_keys: list[str] = []
        for number, period_end in enumerate(ends, 1):
            endpoint = datetime.fromisoformat(period_end)
            _seed_document(
                conn,
                document_id=number,
                source_tier="sec_official",
                value_suffix="distinct reporting period",
                document_period_start=datetime(2026, 4, 1, tzinfo=endpoint.tzinfo),
                document_period_end=endpoint,
            )
            fact_id = _insert_financial_fact(
                conn,
                document_id=number,
                value="100",
                period_end=period_end,
                fiscal_period_type="FY" if boundary == "fiscal_type" and number == 2 else "Q2",
            )
            result = resolve_fact_row(
                conn, fact_table="financial_facts", fact_row_id=fact_id, knowledge_cutoff=STAMP
            )
            assert result is not None
            assert result.candidate_count == 1 and result.resolution_status == "resolved"
            logical_keys.append(result.logical_key)
        assert logical_keys[0] != logical_keys[1]
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_reporting_date_equivalence_does_not_override_equal_authority_dissent(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "date-authority-dissent.db"))
    conn.row_factory = sqlite3.Row
    try:
        fact_ids: list[int] = []
        for number, (end, value) in enumerate(
            [("2026-06-30", "100"), ("2026-06-30T19:00:00+03:00", "100.5")], 1
        ):
            endpoint = datetime.fromisoformat(end)
            _seed_document(
                conn,
                document_id=number,
                source_tier="sec_official",
                value_suffix="equal issuer authority",
                document_period_start=datetime(2026, 4, 1, tzinfo=endpoint.tzinfo),
                document_period_end=endpoint,
            )
            fact_ids.append(
                _insert_financial_fact(conn, document_id=number, value=value, period_end=end)
            )
        result = resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_ids[-1], knowledge_cutoff=STAMP
        )
        assert result is not None and result.candidate_count == 2
        assert result.resolution_status == "unresolved_material" and result.material_dissent
        checks = json.loads(
            conn.execute(
                "SELECT checks_json FROM fact_resolution_outcomes WHERE resolution_id=?",
                (result.resolution_id,),
            ).fetchone()[0]
        )
        assert checks["period_consistent"] is True
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_policy_v3_appends_unchanged_unresolved_outcome_and_replays_exactly(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "period-policy-history.db"))
    conn.row_factory = sqlite3.Row
    try:
        for number, (end, currency) in enumerate(
            [("2026-06-30", "USD"), ("2026-06-30T19:00:00+03:00", "BRL")], 1
        ):
            endpoint = datetime.fromisoformat(end)
            _seed_document(
                conn,
                document_id=number,
                source_tier="sec_official",
                value_suffix="historical unresolved currency",
                document_period_start=datetime(2026, 4, 1, tzinfo=endpoint.tzinfo),
                document_period_end=endpoint,
            )
            _insert_financial_fact(
                conn, document_id=number, value="100", period_end=end, currency=currency
            )
        captured = conn.execute(
            "SELECT logical_key,observation_id FROM fact_observation_revisions ORDER BY observation_id"
        ).fetchall()
        logical_key = str(captured[0][0])
        candidate_ids = tuple(str(row[1]) for row in captured)
        assert len(candidate_ids) == 2 and {row[0] for row in captured} == {logical_key}
        # Seed an immutable prior policy receipt through its public ledger. The
        # membership digest binds its exact candidate IDs, not a test resolver.
        candidate_digest = hashlib.sha256("\0".join(candidate_ids).encode()).hexdigest()
        old_checks = {
            "candidate_set_nonempty": True,
            "source_authorities_known": True,
            "currency_consistent": False,
            "fact_kind_consistent": True,
            "period_consistent": False,
            "source_tiers_known": True,
            "unit_consistent": True,
        }
        ObservationResolutionLedger(conn).persist_resolution(
            ResolutionRevision(
                resolution_id="fixture-policy-v2",
                idempotency_key="fixture-policy-v2",
                logical_key=logical_key,
                revision=1,
                candidate_observation_ids=candidate_ids,
                selected_observation_id=candidate_ids[-1],
                resolver_kind="deterministic_policy",
                policy_version="investor-grade-fact-resolution@2",
                reason="Retained synthetic v2 unresolved period/currency decision.",
                knowledge_cutoff=STAMP,
                effective_at=STAMP,
                recorded_at=STAMP,
                material_dissent=True,
            )
        )
        conn.execute(
            "INSERT INTO fact_resolution_outcomes VALUES (?,?,?,?,?)",
            (
                "fixture-policy-v2",
                "unresolved_material",
                candidate_digest,
                json.dumps(old_checks, sort_keys=True, separators=(",", ":")),
                STAMP,
            ),
        )
        old_revision = conn.execute(
            "SELECT * FROM observation_resolution_revisions WHERE resolution_id='fixture-policy-v2'"
        ).fetchone()
        old_outcome = conn.execute(
            "SELECT * FROM fact_resolution_outcomes WHERE resolution_id='fixture-policy-v2'"
        ).fetchone()
        raw_periods = conn.execute(
            "SELECT period_start,period_end FROM reported_observations ORDER BY observation_id"
        ).fetchall()
        result = resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )
        assert result.created and result.resolution_status == "unresolved_material"
        assert result.selected_observation_id == candidate_ids[-1]
        revision = conn.execute(
            "SELECT revision,policy_version,supersedes_resolution_id FROM observation_resolution_revisions "
            "WHERE resolution_id=?",
            (result.resolution_id,),
        ).fetchone()
        assert tuple(revision) == (2, "investor-grade-fact-resolution@3", "fixture-policy-v2")
        checks = json.loads(
            conn.execute(
                "SELECT checks_json FROM fact_resolution_outcomes WHERE resolution_id=?",
                (result.resolution_id,),
            ).fetchone()[0]
        )
        assert checks == {**old_checks, "period_consistent": True}
        assert (
            conn.execute(
                "SELECT * FROM observation_resolution_revisions WHERE resolution_id='fixture-policy-v2'"
            ).fetchone()
            == old_revision
        )
        assert (
            conn.execute(
                "SELECT * FROM fact_resolution_outcomes WHERE resolution_id='fixture-policy-v2'"
            ).fetchone()
            == old_outcome
        )
        tables = (
            "observation_resolution_revisions",
            "observation_resolution_candidates",
            "fact_resolution_outcomes",
        )
        retained = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in tables
        }
        replay = resolve_fact_logical_key(
            conn, logical_key=logical_key, knowledge_cutoff=STAMP, recorded_at=STAMP
        )
        assert not replay.created and replay.resolution_id == result.resolution_id
        assert {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in tables
        } == retained
        assert (
            conn.execute(
                "SELECT period_start,period_end FROM reported_observations ORDER BY observation_id"
            ).fetchall()
            == raw_periods
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 0
        )
    finally:
        conn.close()
