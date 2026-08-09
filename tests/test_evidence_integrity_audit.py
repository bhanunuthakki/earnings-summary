"""Contracts for the read-only evidence integrity auditor."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

import pytest
from alembic.config import Config

import provenance.integrity_audit as integrity_audit
from alembic import command
from execution.audit_evidence_integrity import (
    configure_read_only_audit_connection,
    main,
)
from provenance.integrity_audit import (
    FACT_CURRENT_CANDIDATE_SET_INCOMPLETE_QUERY,
    AuditOptions,
    IntegrityFinding,
    RemediationClass,
    Severity,
    append_query_finding,
    audit_connection,
    exit_code,
    fact_resolution_digest_mismatches,
)
from provenance.source_coverage import SourceCoverageLedger, SourceInventorySnapshot
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)


class _ResolutionCutoverVerifier(Protocol):
    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        resolution_snapshot_id: str,
        cutoff_at: datetime,
        observed_through: datetime,
    ) -> None: ...


def test_resolution_cutover_verifier_propagates_observed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutoff = datetime(2026, 7, 29, 0, 0)
    observed = datetime(2026, 7, 29, 2, 0)
    calls: list[tuple[str, datetime]] = []

    class _Engine:
        def __init__(self, _conn: sqlite3.Connection) -> None:
            pass

        def verify_snapshot(
            self,
            snapshot_id: str,
            cutoff_at: datetime,
            *,
            observed_through: datetime,
        ) -> None:
            assert snapshot_id == "resolution"
            assert cutoff_at == cutoff
            calls.append(("snapshot", observed_through))

    def _verify_watermark(
        _conn: sqlite3.Connection,
        *,
        resolution_snapshot_id: str,
        cutoff_at: datetime,
        observed_through: datetime,
    ) -> None:
        assert resolution_snapshot_id == "resolution"
        assert cutoff_at == cutoff
        calls.append(("watermark", observed_through))

    monkeypatch.setattr(integrity_audit, "CanonicalFactResolutionEngine", _Engine)
    monkeypatch.setattr(
        integrity_audit,
        "verify_resolution_snapshot_watermark",
        _verify_watermark,
    )

    verify_resolution_cutover = cast(
        "_ResolutionCutoverVerifier",
        getattr(integrity_audit, "_verify_resolution_cutover"),
    )
    verify_resolution_cutover(
        sqlite3.connect(":memory:"),
        resolution_snapshot_id="resolution",
        cutoff_at=cutoff,
        observed_through=observed,
    )

    assert calls == [("snapshot", observed), ("watermark", observed)]


def _database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            parent_document_id INTEGER,
            FOREIGN KEY(parent_document_id) REFERENCES documents(id)
        );
        """
    )
    conn.commit()
    conn.close()


def test_operational_audit_can_defer_expensive_global_sqlite_pragmas(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "fast-audit.db")

    class _RecordingConnection:
        def __init__(self, delegate: sqlite3.Connection) -> None:
            self.delegate = delegate
            self.statements: list[str] = []

        def execute(
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            normalized = " ".join(sql.split()).lower()
            self.statements.append(normalized)
            if normalized in {"pragma foreign_key_check", "pragma integrity_check"}:
                raise AssertionError("deep SQLite pragma was not deferred")
            return self.delegate.execute(sql, parameters)

    recording = _RecordingConnection(conn)
    summary = audit_connection(
        cast(sqlite3.Connection, recording),
        AuditOptions(deep_sqlite_checks=False),
    )

    assert summary.has_blockers is False
    assert "pragma foreign_key_check" not in recording.statements
    assert "pragma integrity_check" not in recording.statements
    conn.close()


def test_audit_cli_uses_bounded_connection_local_read_tuning() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, sql: str) -> None:
            self.statements.append(" ".join(sql.split()))

    connection = _Connection()
    configure_read_only_audit_connection(
        cast(sqlite3.Connection, connection),
        cache_mib=256,
        mmap_mib=1024,
    )

    assert connection.statements == [
        "PRAGMA query_only = ON",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA cache_size = -262144",
        "PRAGMA mmap_size = 1073741824",
    ]


def test_finding_query_counts_exactly_but_materializes_only_bounded_samples() -> None:
    class _Cursor:
        def __init__(self, *, one: tuple[object, ...] | None, many: list[tuple[object, ...]]):
            self.one = one
            self.many = many

        def fetchone(self) -> tuple[object, ...] | None:
            return self.one

        def fetchall(self) -> list[tuple[object, ...]]:
            raise AssertionError("audit finding query must not materialize every violation")

        def fetchmany(self, size: int) -> list[tuple[object, ...]]:
            return self.many[:size]

    class _Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def execute(
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
        ) -> _Cursor:
            self.calls.append((sql, parameters))
            if "COUNT(*)" in sql:
                return _Cursor(one=(123_456,), many=[])
            return _Cursor(one=None, many=[("one",), ("two",), ("three",)])

    findings: list[IntegrityFinding] = []
    connection = _Connection()
    append_query_finding(
        cast(sqlite3.Connection, connection),
        findings,
        AuditOptions(sample_limit=2, deep_sqlite_checks=False),
        code="TEST_BOUNDED_FINDING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        query="SELECT value FROM violations ORDER BY value",
    )

    assert findings[0].count == 123_456
    assert findings[0].samples == ("one", "two")
    assert connection.calls[1][1] == (2,)
    assert "LIMIT ?" in connection.calls[1][0]


def test_candidate_completeness_uses_exact_symmetric_current_set() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE fact_observation_revisions (
            fact_table TEXT NOT NULL,
            fact_row_id INTEGER NOT NULL,
            fact_revision INTEGER NOT NULL,
            observation_id TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            PRIMARY KEY (fact_table, fact_row_id, fact_revision)
        );
        CREATE TABLE observation_resolution_revisions (
            resolution_id TEXT PRIMARY KEY,
            logical_key TEXT NOT NULL,
            revision INTEGER NOT NULL
        );
        CREATE TABLE observation_resolution_candidates (
            resolution_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            PRIMARY KEY (resolution_id, observation_id)
        );
        CREATE TABLE fact_resolution_outcomes (
            resolution_id TEXT PRIMARY KEY
        );

        INSERT INTO fact_observation_revisions VALUES
            ('financial_facts', 1, 1, 'complete-old', 'complete'),
            ('financial_facts', 1, 2, 'complete-new', 'complete'),
            ('financial_facts', 2, 1, 'missing-member', 'missing'),
            ('financial_facts', 3, 1, 'current-only', 'revised');

        INSERT INTO observation_resolution_revisions VALUES
            ('resolution-complete', 'complete', 1),
            ('resolution-missing', 'missing', 1),
            ('resolution-extra', 'extra', 1),
            ('resolution-revised-old', 'revised', 1),
            ('resolution-revised-new', 'revised', 2);

        INSERT INTO fact_resolution_outcomes VALUES
            ('resolution-complete'),
            ('resolution-missing'),
            ('resolution-extra'),
            ('resolution-revised-old'),
            ('resolution-revised-new');

        INSERT INTO observation_resolution_candidates VALUES
            ('resolution-complete', 'complete-new'),
            ('resolution-extra', 'orphan-candidate'),
            ('resolution-revised-old', 'stale-only'),
            ('resolution-revised-new', 'current-only');

        CREATE VIEW v_observation_resolution_current AS
        SELECT revision.*
        FROM observation_resolution_revisions AS revision
        WHERE NOT EXISTS (
            SELECT 1
            FROM observation_resolution_revisions AS newer
            WHERE newer.logical_key = revision.logical_key
              AND newer.revision > revision.revision
        );
        """
    )
    try:
        rows = conn.execute(FACT_CURRENT_CANDIDATE_SET_INCOMPLETE_QUERY).fetchall()
    finally:
        conn.close()

    assert rows == [("extra",), ("missing",)]


def test_detects_dangling_document_parent_without_mutating_database(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    _database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO documents (id, parent_document_id) VALUES (7, 99)")
    conn.commit()
    conn.close()

    before = db_path.read_bytes()
    read_only = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        summary = audit_connection(read_only, AuditOptions())
    finally:
        read_only.close()

    finding = next(item for item in summary.findings if item.code == "DOCUMENT_PARENT_DANGLING")
    assert finding.count == 1
    assert finding.remediation == "manual"
    assert db_path.read_bytes() == before


def test_reports_absent_additive_schema_as_advisory_and_empty_legacy_schema_as_clean(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "clean.db"
    _database(db_path)
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    codes = {item.code for item in summary.findings}
    assert "EVIDENCE_LEDGER_SCHEMA_ABSENT" in codes
    assert "DOCUMENT_PARENT_DANGLING" not in codes
    assert not summary.has_blockers


def test_fact_resolution_digest_audit_streams_one_candidate_query() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE fact_resolution_outcomes (
            resolution_id TEXT PRIMARY KEY,
            candidate_set_sha256 TEXT NOT NULL
        );
        CREATE TABLE observation_resolution_candidates (
            resolution_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            PRIMARY KEY (resolution_id, observation_id)
        );
        """
    )
    expected = hashlib.sha256(b"observation-1\0observation-2").hexdigest()
    conn.executemany(
        "INSERT INTO fact_resolution_outcomes VALUES (?, ?)",
        (("resolution-1", expected), ("resolution-2", "0" * 64)),
    )
    conn.executemany(
        "INSERT INTO observation_resolution_candidates VALUES (?, ?)",
        (
            ("resolution-1", "observation-1"),
            ("resolution-1", "observation-2"),
        ),
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        count, samples = fact_resolution_digest_mismatches(conn, sample_limit=5)
    finally:
        conn.close()

    candidate_queries = [
        statement
        for statement in statements
        if "FROM fact_resolution_outcomes AS outcome" in statement
        and "observation_resolution_candidates AS candidate" in statement
    ]
    assert len(candidate_queries) == 1
    assert count == 1
    assert samples == ("resolution-2",)


def test_fact_resolution_digest_audit_reads_in_bounded_batches() -> None:
    expected = hashlib.sha256(b"observation-1\0observation-2").hexdigest()

    class _BatchedCursor:
        def __init__(self) -> None:
            self.batches: list[list[tuple[object, ...]]] = [
                [("resolution-1", expected, "observation-1")],
                [
                    ("resolution-1", expected, "observation-2"),
                    ("resolution-2", "0" * 64, None),
                ],
                [],
            ]
            self.sizes: list[int] = []

        def __iter__(self) -> object:
            raise AssertionError("digest audit must fetch rows in bounded batches")

        def fetchmany(self, size: int) -> list[tuple[object, ...]]:
            self.sizes.append(size)
            return self.batches.pop(0)

    class _Connection:
        def __init__(self) -> None:
            self.cursor = _BatchedCursor()
            self.calls = 0

        def execute(self, _sql: str) -> _BatchedCursor:
            self.calls += 1
            return self.cursor

    connection = _Connection()
    count, samples = fact_resolution_digest_mismatches(
        cast(sqlite3.Connection, connection),
        sample_limit=5,
    )

    assert connection.calls == 1
    assert connection.cursor.sizes == [8192, 8192, 8192]
    assert count == 1
    assert samples == ("resolution-2",)


def test_verifies_blob_bytes_with_a_bounded_budget(tmp_path: Path) -> None:
    payload = b"trusted immutable source bytes"
    payload_path = tmp_path / "source.txt"
    payload_path.write_bytes(payload)
    db_path = tmp_path / "blobs.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE evidence_content_blobs (sha256 TEXT PRIMARY KEY, byte_size INTEGER, storage_uri TEXT)"
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?, ?, ?)",
        (hashlib.sha256(payload).hexdigest(), len(payload), payload_path.as_uri()),
    )
    conn.commit()
    conn.close()

    read_only = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        summary = audit_connection(
            read_only,
            AuditOptions(verify_bytes=True, repo_root=tmp_path, max_verify_bytes=1),
        )
    finally:
        read_only.close()

    finding = next(item for item in summary.findings if item.code == "BLOB_BYTE_BUDGET_EXHAUSTED")
    assert finding.count == 1


def test_verifies_a_file_uri_inside_the_explicit_repo_root(tmp_path: Path) -> None:
    payload = b"trusted immutable source bytes"
    payload_path = tmp_path / "source.txt"
    payload_path.write_bytes(payload)
    db_path = tmp_path / "verified.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE evidence_content_blobs (sha256 TEXT PRIMARY KEY, byte_size INTEGER, storage_uri TEXT)"
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?, ?, ?)",
        (hashlib.sha256(payload).hexdigest(), len(payload), payload_path.as_uri()),
    )
    conn.commit()
    conn.close()

    read_only = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        summary = audit_connection(
            read_only,
            AuditOptions(verify_bytes=True, repo_root=tmp_path, max_verify_bytes=1024),
        )
    finally:
        read_only.close()

    assert not any(item.code.startswith("BLOB_") for item in summary.findings)


def test_verifies_file_uris_across_multiple_explicit_content_roots(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    additional_root = tmp_path / "captured-blobs"
    outside_root = tmp_path / "not-authorized"
    for root in (repo_root, additional_root, outside_root):
        root.mkdir()
    payloads = (
        (repo_root / "legacy.txt", b"legacy evidence"),
        (additional_root / "captured.txt", b"captured evidence"),
        (outside_root / "outside.txt", b"outside evidence"),
    )
    for path, payload in payloads:
        path.write_bytes(payload)
    db_path = tmp_path / "multiple-roots.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE evidence_content_blobs "
        "(sha256 TEXT PRIMARY KEY, byte_size INTEGER, storage_uri TEXT)"
    )
    conn.executemany(
        "INSERT INTO evidence_content_blobs VALUES (?, ?, ?)",
        (
            (hashlib.sha256(payload).hexdigest(), len(payload), path.as_uri())
            for path, payload in payloads
        ),
    )
    conn.commit()
    conn.close()

    read_only = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        summary = audit_connection(
            read_only,
            AuditOptions(
                verify_bytes=True,
                repo_root=repo_root,
                content_roots=(additional_root,),
                max_verify_bytes=1024,
            ),
        )
    finally:
        read_only.close()

    unsafe = next(
        finding for finding in summary.findings if finding.code == "BLOB_STORAGE_URI_UNSAFE"
    )
    assert unsafe.count == 1
    assert unsafe.samples == (hashlib.sha256(b"outside evidence").hexdigest(),)
    assert not any(
        finding.code in {"BLOB_BYTES_MISSING", "BLOB_BYTES_HASH_OR_SIZE_MISMATCH"}
        for finding in summary.findings
    )


def test_strict_exit_is_nonzero_only_for_blockers() -> None:
    assert exit_code(has_blockers=False, strict=True) == 0
    assert exit_code(has_blockers=True, strict=False) == 0
    assert exit_code(has_blockers=True, strict=True) == 2


def test_cli_strict_mode_emits_json_summary_and_fails_for_blockers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "strict.db"
    _database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO documents (id, parent_document_id) VALUES (1, 2)")
    conn.commit()
    conn.close()

    assert main(["--db-path", str(db_path), "--strict"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["has_blockers"] is True
    assert '"event": "evidence_integrity_audit_finished"' in captured.err


def test_summary_is_closed_json_schema() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        summary = audit_connection(conn, AuditOptions(sample_limit=1))
    finally:
        conn.close()
    payload = json.loads(summary.model_dump_json())
    assert payload["schema_version"] == "evidence-integrity-audit/v1"


def test_partial_ocr_governance_schema_is_a_hard_stop(tmp_path: Path) -> None:
    db_path = tmp_path / "partial-ocr.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ocr_document_assessments (assessment_id TEXT PRIMARY KEY)")
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    finding = next(
        item for item in summary.findings if item.code == "OCR_GOVERNANCE_SCHEMA_PARTIAL"
    )
    assert finding.count == 3
    assert finding.remediation == "hard-stop"


def test_integrity_auditor_is_directly_executable_from_repo_root() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "execution" / "audit_evidence_integrity.py"), "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--db-path" in result.stdout
    assert "--content-root" in result.stdout


def test_accepts_empty_complete_evidence_search_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "complete.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0215_observation_resolution_ledger")
    command.upgrade(config, "0216_search_corpus_foundation")

    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    assert not summary.has_blockers


def test_blocks_uninitialized_coverage_and_search_for_existing_evidence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "uninitialized-search.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0219_source_coverage_ledger")

    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, "
        "blob_sha256, issuer_id, ticker, document_type, form_type, accession_number, "
        "exhibit_id, period_start, period_end, as_of_at, language, "
        "replaces_document_version_id, legacy_document_id, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "document-1",
            "ACME:10-K:2025",
            1,
            "observation-1",
            "a" * 64,
            "issuer-acme",
            "ACME",
            "10-K",
            "10-K",
            "0001",
            None,
            None,
            stamp,
            stamp,
            "en",
            None,
            None,
            stamp,
        ),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    codes = {finding.code for finding in summary.findings}
    assert "SOURCE_COVERAGE_UNINITIALIZED" in codes
    assert "SEARCH_CORPUS_NOT_BUILT" in codes


def test_blocks_unbound_legacy_and_new_inventory_issuer_identities(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "unbound-issuer.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0227_issuer_reporting_registry")

    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, "
        "blob_sha256, issuer_id, ticker, document_type, form_type, accession_number, "
        "exhibit_id, period_start, period_end, as_of_at, language, "
        "replaces_document_version_id, legacy_document_id, recorded_at) "
        "VALUES ('document-legacy','legacy',1,'missing-observation',?,'legacy-ticker:ACME',"
        "'ACME','10-K','10-K','0001',NULL,NULL,?,?,'en',NULL,NULL,?)",
        ("a" * 64, stamp, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshots "
        "(snapshot_id,idempotency_key,inventory_key,revision,issuer_id,ticker,source_kind,"
        "source_url,source_observation_id,outcome,authoritative,retrieval_config_sha256,"
        "collector_code_version,started_at,completed_at,recorded_at,supersedes_snapshot_id) "
        "VALUES ('inventory','inventory','unbound:sec',1,'unbound-issuer','ACME',"
        "'sec_submissions','https://data.sec.gov/submissions/CIK0000123456.json',"
        "'missing-observation','succeeded',1,?,'fixture@1',?,?,?,NULL)",
        ("b" * 64, stamp, stamp, stamp),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    codes = {finding.code for finding in summary.findings}
    assert "EVIDENCE_ISSUER_BINDING_MISSING" in codes
    assert "SOURCE_INVENTORY_ISSUER_NOT_CANONICAL" in codes
    assert "IDENTITY_REGISTRY_UNINITIALIZED" in codes


def test_blocks_unresolved_identity_and_listing_conflicts(tmp_path: Path) -> None:
    db_path = tmp_path / "identity-conflict.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0227_issuer_reporting_registry")

    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    conn.execute(
        "INSERT INTO issuer_identifier_resolution_outcomes "
        "(resolution_id,idempotency_key,resolution_key,revision,outcome,"
        "selected_assertion_id,candidate_digest_sha256,policy_name,policy_version,"
        "policy_config_sha256,reason_code,reason_details_json,material_dissent,"
        "effective_at,knowledge_at,recorded_at,supersedes_resolution_id) "
        "VALUES ('issuer-unresolved','issuer-unresolved','sec_cik:0000123456',1,"
        "'unresolved',NULL,?,'policy','1',?,'conflicting_authorities','{}',1,?,?,?,NULL)",
        ("a" * 64, "b" * 64, stamp, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO security_listing_resolution_outcomes "
        "(resolution_id,idempotency_key,resolution_key,revision,outcome,"
        "selected_assertion_id,candidate_digest_sha256,policy_name,policy_version,"
        "policy_config_sha256,reason_code,reason_details_json,material_dissent,"
        "effective_at,knowledge_at,recorded_at,supersedes_resolution_id) "
        "VALUES ('listing-unresolved','listing-unresolved','listing:XNAS:ACME',1,"
        "'unresolved',NULL,?,'policy','1',?,'conflicting_securities','{}',1,?,?,?,NULL)",
        ("c" * 64, "d" * 64, stamp, stamp, stamp),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    codes = {finding.code for finding in summary.findings}
    assert "ISSUER_IDENTIFIER_UNRESOLVED" in codes
    assert "SECURITY_LISTING_UNRESOLVED" in codes


def test_recomputes_source_inventory_component_digest(tmp_path: Path) -> None:
    db_path = tmp_path / "inventory-digest.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0220_source_inventory_seals")

    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="snapshot-failed",
            idempotency_key="snapshot-failed",
            inventory_key="issuer:ir",
            revision=1,
            issuer_id="issuer",
            ticker="ACME",
            source_kind="ir_crawl",
            source_url="https://ir.example.test",
            outcome="failed",
            authoritative=False,
            retrieval_config_sha256="a" * 64,
            collector_code_version="inventory@1",
            started_at=stamp,
            completed_at=stamp,
            recorded_at=stamp,
        )
    )
    component = InventoryComponent(
        component_id="component-failed",
        idempotency_key="component-failed",
        snapshot_id="snapshot-failed",
        component_key="primary",
        component_kind="primary",
        source_url="https://ir.example.test",
        outcome="failed",
        required=True,
        failure_reason="http_503",
        ordinal=0,
        recorded_at=stamp,
    )
    store = SourceInventorySealStore(conn)
    store.persist(component)
    store.persist(
        InventorySeal(
            snapshot_id="snapshot-failed",
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="incomplete",
            sealed_at=stamp,
        )
    )
    conn.execute("DROP TRIGGER trg_source_inventory_snapshot_seals_append_only")
    conn.execute(
        "UPDATE source_inventory_snapshot_seals SET component_digest_sha256 = ?",
        ("f" * 64,),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    finding = next(
        item for item in summary.findings if item.code == "SOURCE_INVENTORY_SEAL_DIGEST_MISMATCH"
    )
    assert finding.count == 1
    assert finding.samples == ("snapshot-failed",)


def _search_database(tmp_path: Path) -> Path:
    db_path = tmp_path / "search-audit.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0215_observation_resolution_ledger")
    command.upgrade(config, "0216_search_corpus_foundation")
    return db_path


def _semantic_search_database(tmp_path: Path) -> Path:
    db_path = _search_database(tmp_path)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(config, "0231_legacy_document_evidence_bindings")
    command.upgrade(config, "0232_document_semantic_dispositions")
    return db_path


def _insert_evidence_document(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    stamp: datetime,
) -> None:
    suffix = hashlib.sha256(document_version_id.encode()).hexdigest()
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?, ?, ?, ?, ?)",
        (suffix, 1, "image/jpeg", f"file:///evidence/{document_version_id}.jpg", stamp),
    )
    observation_id = f"observation-{document_version_id}"
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, "
        "source_published_at, filing_at, accepted_at, observed_at, retrieved_at, "
        "retrieval_config_sha256, collector_code_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            observation_id,
            observation_id,
            "sec_filing",
            f"https://www.sec.gov/{document_version_id}",
            suffix,
            None,
            None,
            None,
            stamp,
            stamp,
            "a" * 64,
            "collector@1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, accession_number, exhibit_id, "
        "period_start, period_end, as_of_at, language, replaces_document_version_id, "
        "legacy_document_id, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_version_id,
            document_version_id,
            1,
            observation_id,
            suffix,
            "issuer-acme",
            "ACME",
            "filing_attachment",
            "10-K",
            document_version_id,
            None,
            None,
            stamp,
            stamp,
            "en",
            None,
            None,
            stamp,
        ),
    )


def _insert_semantic_disposition(
    conn: sqlite3.Connection,
    *,
    assessment_id: str,
    document_version_id: str,
    semantic_status: str,
    decision_kind: str,
    reviewer_identity: str | None,
    stamp: datetime,
) -> None:
    conn.execute(
        "INSERT INTO document_semantic_disposition_revisions "
        "(assessment_id, idempotency_key, document_version_id, revision, semantic_status, "
        "reason_code, reason_details_json, decision_kind, reviewer_identity, policy_name, "
        "policy_version, policy_config_sha256, effective_at, knowledge_at, recorded_at, "
        "supersedes_assessment_id, material_dissent) "
        "VALUES (?, ?, ?, 1, ?, 'reviewed_content', '{\"basis\":\"review\"}', ?, ?, "
        "'semantic-review', '1', ?, ?, ?, ?, NULL, 0)",
        (
            assessment_id,
            assessment_id,
            document_version_id,
            semantic_status,
            decision_kind,
            reviewer_identity,
            "b" * 64,
            stamp,
            stamp,
            stamp,
        ),
    )


def test_semantic_audit_blocks_unauthorized_exclusion_and_missing_review(
    tmp_path: Path,
) -> None:
    db_path = _semantic_search_database(tmp_path)
    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    _insert_evidence_document(conn, document_version_id="document-automated", stamp=stamp)
    _insert_evidence_document(conn, document_version_id="document-unnamed", stamp=stamp)
    _insert_evidence_document(conn, document_version_id="document-unreviewed", stamp=stamp)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    _insert_semantic_disposition(
        conn,
        assessment_id="semantic-automated",
        document_version_id="document-automated",
        semantic_status="not_required",
        decision_kind="deterministic",
        reviewer_identity=None,
        stamp=stamp,
    )
    _insert_semantic_disposition(
        conn,
        assessment_id="semantic-unnamed",
        document_version_id="document-unnamed",
        semantic_status="not_required",
        decision_kind="human",
        reviewer_identity="   ",
        stamp=stamp,
    )
    failed_blob = hashlib.sha256(b"document-unreviewed").hexdigest()
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
        "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
        "started_at, completed_at, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "failed-unreviewed",
            "failed-unreviewed",
            "document-unreviewed",
            failed_blob,
            "fulltext-evidence-backfill",
            "c" * 64,
            "extractor@1",
            "d" * 64,
            stamp,
            stamp,
            "failed",
        ),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    unauthorized = next(
        finding
        for finding in summary.findings
        if finding.code == "SEMANTIC_NOT_REQUIRED_UNAUTHORIZED"
    )
    assert unauthorized.count == 2
    assert unauthorized.samples == ("semantic-automated", "semantic-unnamed")
    invalid = next(
        finding
        for finding in summary.findings
        if finding.code == "SEMANTIC_DISPOSITION_INVALID_RECORD"
    )
    assert invalid.count == 1
    assert invalid.samples == ("semantic-unnamed",)
    missing = next(
        finding
        for finding in summary.findings
        if finding.code == "SEMANTIC_DISPOSITION_MISSING_AFTER_FAILED_EXTRACTION"
    )
    assert missing.count == 1
    assert missing.samples == ("document-unreviewed",)


def test_semantic_audit_detects_current_corpus_contradiction(
    tmp_path: Path,
) -> None:
    db_path = _semantic_search_database(tmp_path)
    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    _insert_evidence_document(conn, document_version_id="document-review", stamp=stamp)
    _insert_semantic_disposition(
        conn,
        assessment_id="semantic-review",
        document_version_id="document-review",
        semantic_status="review_required",
        decision_kind="deterministic",
        reviewer_identity=None,
        stamp=stamp,
    )
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "manifest-review",
            "manifest-review",
            "all-company-reports",
            1,
            "a" * 64,
            "selector@1",
            None,
            None,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "membership-review",
            "manifest-review",
            "document-review",
            "document-review",
            "included",
            "coverage:captured",
            stamp,
        ),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    contradiction = next(
        finding
        for finding in summary.findings
        if finding.code == "SEMANTIC_DISPOSITION_CORPUS_CONTRADICTION"
    )
    assert contradiction.count == 1
    assert contradiction.samples == (
        "manifest-review|document-review|included|review_required|coverage:captured",
    )


def test_human_not_required_membership_is_not_reported_as_unchunked(
    tmp_path: Path,
) -> None:
    db_path = _semantic_search_database(tmp_path)
    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 27)
    _insert_evidence_document(conn, document_version_id="document-nonsemantic", stamp=stamp)
    _insert_semantic_disposition(
        conn,
        assessment_id="semantic-human",
        document_version_id="document-nonsemantic",
        semantic_status="not_required",
        decision_kind="human",
        reviewer_identity="analyst@example.test",
        stamp=stamp,
    )
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "manifest-nonsemantic",
            "manifest-nonsemantic",
            "all-company-reports",
            1,
            "a" * 64,
            "selector@1",
            None,
            None,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "membership-nonsemantic",
            "manifest-nonsemantic",
            "document-nonsemantic",
            "document-nonsemantic",
            "included",
            "semantic:not_required:semantic-human",
            stamp,
        ),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    codes = {finding.code for finding in summary.findings}
    assert "SEMANTIC_DISPOSITION_CORPUS_CONTRADICTION" not in codes
    assert "SEARCH_INCLUDED_DOCUMENT_UNCHUNKED" not in codes


def test_detects_unsealed_search_manifest(tmp_path: Path) -> None:
    db_path = _search_database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "manifest-unsealed",
            "manifest-unsealed",
            "all-company-reports",
            1,
            "a" * 64,
            "selector@1",
            None,
            None,
            datetime(2026, 7, 26),
        ),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    finding = next(item for item in summary.findings if item.code == "SEARCH_MANIFEST_UNSEALED")
    assert finding.count == 1


def test_detects_corrupt_search_manifest_seal_digest(tmp_path: Path) -> None:
    db_path = _search_database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "manifest-corrupt",
            "manifest-corrupt",
            "all-company-reports",
            1,
            "a" * 64,
            "selector@1",
            None,
            None,
            datetime(2026, 7, 26),
        ),
    )
    empty_digest = hashlib.sha256(b"[]").hexdigest()
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals VALUES (?, ?, ?, ?, ?)",
        ("manifest-corrupt", 0, empty_digest, "complete", datetime(2026, 7, 26)),
    )
    conn.execute("DROP TRIGGER trg_search_corpus_manifest_seals_append_only")
    conn.execute(
        "UPDATE search_corpus_manifest_seals SET membership_digest_sha256 = ? "
        "WHERE manifest_id = 'manifest-corrupt'",
        ("f" * 64,),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    finding = next(
        item for item in summary.findings if item.code == "SEARCH_MANIFEST_SEAL_MISMATCH"
    )
    assert finding.count == 1


def test_detects_successful_search_index_with_omitted_chunk_membership(tmp_path: Path) -> None:
    db_path = _search_database(tmp_path)
    conn = sqlite3.connect(db_path)
    stamp = datetime(2026, 7, 26)
    content = "Revenue increased 20%."
    content_sha = hashlib.sha256(content.encode()).hexdigest()
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?, ?, ?, ?, ?)",
        ("a" * 64, 10, "text/plain", "file:///evidence/acme.txt", stamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, "
        "source_published_at, filing_at, accepted_at, observed_at, retrieved_at, "
        "retrieval_config_sha256, collector_code_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "observation-1",
            "observation-1",
            "sec_filing",
            "https://www.sec.gov/acme",
            "a" * 64,
            None,
            None,
            None,
            stamp,
            stamp,
            "b" * 64,
            "collector@1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, accession_number, exhibit_id, "
        "period_start, period_end, as_of_at, language, replaces_document_version_id, "
        "legacy_document_id, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "document-1",
            "ACME:10-K:2025",
            1,
            "observation-1",
            "a" * 64,
            "issuer-acme",
            "ACME",
            "10-K",
            "10-K",
            "0001",
            None,
            None,
            stamp,
            stamp,
            "en",
            None,
            None,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
        "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
        "started_at, completed_at, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-1",
            "run-1",
            "document-1",
            "a" * 64,
            "parser",
            "c" * 64,
            "parser@1",
            "d" * 64,
            stamp,
            stamp,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, "
        "supersedes_node_id, node_kind, text, locator_json, locator_sha256, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "node-1",
            "node-1",
            1,
            "run-1",
            None,
            None,
            "passage",
            content,
            "{}",
            hashlib.sha256(b"{}").hexdigest(),
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "manifest-1",
            "manifest-1",
            "all-company-reports",
            1,
            "e" * 64,
            "selector@1",
            None,
            None,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "membership-1",
            "manifest-1",
            "ACME:10-K:2025",
            "document-1",
            "included",
            "current",
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO search_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "chunk-1",
            "chunk-1",
            "manifest-1",
            "node-1",
            "node-1:0",
            1,
            content,
            content_sha,
            0,
            len(content),
            "f" * 64,
            "chunker@1",
            stamp,
            stamp,
        ),
    )
    membership_payload = [["membership-1", "ACME:10-K:2025", "document-1", "included", "current"]]
    membership_digest = hashlib.sha256(
        json.dumps(membership_payload, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals VALUES (?, ?, ?, ?, ?)",
        ("manifest-1", 1, membership_digest, "complete", stamp),
    )
    conn.execute(
        "INSERT INTO search_index_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "index-1",
            "index-1",
            "all-company-reports:lexical",
            1,
            "manifest-1",
            "vector",
            "1" * 64,
            "indexer@1",
            "succeeded",
            None,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    try:
        summary = audit_connection(conn, AuditOptions())
        conn.execute(
            "INSERT INTO search_index_memberships VALUES (?, ?, ?, ?, ?)",
            ("index-1", "chunk-1", "included", None, stamp),
        )
        conn.commit()
        artifact_summary = audit_connection(conn, AuditOptions())
    finally:
        conn.close()

    finding = next(item for item in summary.findings if item.code == "SEARCH_INDEX_MEMBERSHIP_GAP")
    assert finding.count == 1
    assert finding.samples == ("index-1|chunk-1|NULL",)
    artifact_finding = next(
        item for item in artifact_summary.findings if item.code == "SEARCH_VECTOR_ARTIFACT_GAP"
    )
    assert artifact_finding.samples == ("index-1|chunk-1",)
