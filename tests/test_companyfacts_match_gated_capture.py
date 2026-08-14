"""CompanyFacts facts must be matched before observation capture."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from compute.metrics_engine.inputs import CanonicalConcept
from compute.metrics_engine.io import (
    _unsealed_lineage_reason,  # pyright: ignore[reportPrivateUsage]
)
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
    execute_fact_cutover,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from provenance.legacy_fact_evidence_match import (
    CanonicalJSONObject,
    CompanyFactsCandidateManifestV1,
    CompanyFactsCandidateV1,
    CompanyFactsRelocatedLocator,
    FinancialFactPayloadV1,
    LegacyFactEvidenceMatchLedger,
    LegacyFactEvidenceMatchRevision,
    OriginalFactLocator,
)

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "0236_fact_observation_match_proofs"
HEAD = "0237_companyfacts_match_gated_capture"
STAMP = datetime(2026, 7, 27, 12, 0, 0)
CONFIG_SHA = hashlib.sha256(b"companyfacts-gate-test-config").hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _create_predecessor_database(tmp_path: Path) -> Path:
    path = tmp_path / "companyfacts-match-gate.db"
    conn = sqlite3.connect(path)
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
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, PREDECESSOR)
    return path


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _trigger_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (name,),
    ).fetchone()
    assert row is not None
    return str(row["sql"])


def _seed_document(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    source_kind: str,
    direct_bridge: bool,
) -> tuple[str, str]:
    suffix = f"{document_id}-{source_kind}"
    accession = f"0000000001-26-{document_id:06d}"
    body = f'{{"source":"{source_kind}","document":{document_id}}}'.encode()
    digest = hashlib.sha256(body).hexdigest()
    output_sha = hashlib.sha256(b"document-node:" + body).hexdigest()
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, period_start, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, source_url, "
        "source_quality_tier, accession_number, filing_date) "
        "VALUES (?, 'ACME', ?, 'sec_10q', '2026-04-01', '2026-06-30', ?, ?, ?, "
        "'ok', ?, ?, 'sec_official', ?, '2026-07-20')",
        (
            document_id,
            source_kind,
            f"evidence://{suffix}",
            hashlib.sha256(f"legacy:{suffix}".encode()).hexdigest(),
            STAMP,
            len(body),
            f"https://example.test/{suffix}",
            accession,
        ),
    )

    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="application/json",
            storage_uri=f"file:///evidence/{digest}.json",
            recorded_at=STAMP,
        )
    )
    observation_id = f"source-{suffix}"
    ledger.persist(
        SourceObservation(
            observation_id=observation_id,
            idempotency_key=observation_id,
            source_kind=source_kind,
            source_url=f"https://example.test/{suffix}",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="companyfacts-gate-test@1",
        )
    )
    version_id = f"version-{suffix}"
    ledger.persist(
        DocumentVersion(
            document_version_id=version_id,
            document_key=f"issuer-acme:{suffix}",
            version_sequence=1,
            observation_id=observation_id,
            blob_sha256=digest,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type=(
                "companyfacts_snapshot" if source_kind == "sec_companyfacts" else "sec_filing"
            ),
            form_type=("SEC-COMPANYFACTS" if source_kind == "sec_companyfacts" else "10-Q"),
            accession_number=None if source_kind == "sec_companyfacts" else accession,
            exhibit_id=None,
            period_start=datetime(2026, 4, 1),
            period_end=datetime(2026, 6, 30),
            as_of_at=STAMP,
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=document_id if direct_bridge else None,
            recorded_at=STAMP,
        )
    )
    run_id = f"run-{suffix}"
    ledger.persist(
        ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=run_id,
            document_version_id=version_id,
            input_sha256=digest,
            extractor_name="companyfacts-gate-test",
            extractor_config_sha256=CONFIG_SHA,
            extractor_code_version="companyfacts-gate-test@1",
            output_sha256=output_sha,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    node_id = f"node-{suffix}"
    ledger.persist(
        EvidenceNode(
            node_id=node_id,
            evidence_key=f"{version_id}:document",
            revision=1,
            extraction_run_id=run_id,
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text=f"Evidence for {suffix}",
            locator=None,
            recorded_at=STAMP,
        )
    )
    if not direct_bridge:
        locator_json = f'{{"accession_number":"{accession}"}}'
        conn.execute(
            "INSERT INTO legacy_document_evidence_binding_revisions "
            "(binding_revision_id, idempotency_key, legacy_document_id, revision, "
            "document_version_id, evidence_node_id, scope_locator_json, "
            "scope_locator_sha256, scope_content_sha256, effective_at, knowledge_at, "
            "recorded_at, supersedes_binding_revision_id) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                f"binding-{suffix}",
                f"binding-{suffix}",
                document_id,
                version_id,
                node_id,
                locator_json,
                hashlib.sha256(locator_json.encode()).hexdigest(),
                hashlib.sha256(f"scope:{suffix}".encode()).hexdigest(),
                STAMP,
                STAMP,
                STAMP,
            ),
        )
    return version_id, node_id


def _insert_fact(conn: sqlite3.Connection, table: str, document_id: int) -> int:
    if table == "financial_facts":
        cursor = conn.execute(
            "INSERT INTO financial_facts "
            "(ticker, period_end, fiscal_period_type, line_item, value, currency, "
            "unit, source_doc_id, confidence, extracted_by, locator) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 'revenue', 100, 'USD', "
            "'actual', ?, 0.99, 'test', '{\"cell\":\"revenue\"}')",
            (document_id,),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "source_doc_id, confidence, extracted_by, locator) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 1, 100, 'percent', ?, 0.99, "
            "'test', '{\"cell\":\"growth\"}')",
            (document_id,),
        )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _observation_count(
    conn: sqlite3.Connection,
    *,
    table: str,
    fact_id: int,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM fact_observation_revisions WHERE fact_table = ? AND fact_row_id = ?",
        (table, fact_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("table", ["financial_facts", "kpi_facts"])
def test_companyfacts_capture_is_gated_and_downgrade_restores_predecessor(
    tmp_path: Path,
    table: str,
) -> None:
    path = _create_predecessor_database(tmp_path)
    conn = _connection(path)
    delete_trigger = f"trg_{table}_observation_delete"
    capture_trigger = f"trg_{table}_observation_insert"
    delete_sql_before = _trigger_sql(conn, delete_trigger)
    capture_sql_before = _trigger_sql(conn, capture_trigger)
    conn.close()

    command.upgrade(_config(path), HEAD)
    conn = _connection(path)
    try:
        assert _trigger_sql(conn, delete_trigger) == delete_sql_before
        assert _trigger_sql(conn, capture_trigger) != capture_sql_before
        conn.execute("INSERT INTO kpi_definitions VALUES (1, 'ACME', 'Growth', 'percent')")
        _seed_document(
            conn,
            document_id=1,
            source_kind="sec_filing",
            direct_bridge=True,
        )
        _seed_document(
            conn,
            document_id=2,
            source_kind="sec_companyfacts",
            direct_bridge=False,
        )

        ordinary_fact_id = _insert_fact(conn, table, 1)
        assert _observation_count(conn, table=table, fact_id=ordinary_fact_id) == 1
        conn.execute(f"UPDATE {table} SET value = 101 WHERE id = ?", (ordinary_fact_id,))
        assert _observation_count(conn, table=table, fact_id=ordinary_fact_id) == 2

        companyfacts_fact_id = _insert_fact(conn, table, 2)
        assert _observation_count(conn, table=table, fact_id=companyfacts_fact_id) == 0
        conn.execute(
            f"UPDATE {table} SET value = 101 WHERE id = ?",
            (companyfacts_fact_id,),
        )
        assert _observation_count(conn, table=table, fact_id=companyfacts_fact_id) == 0
        conn.commit()
    finally:
        conn.close()

    command.downgrade(_config(path), PREDECESSOR)
    conn = _connection(path)
    try:
        assert _trigger_sql(conn, delete_trigger) == delete_sql_before
        assert _trigger_sql(conn, capture_trigger) == capture_sql_before
        conn.execute(
            f"UPDATE {table} SET value = 102 WHERE id = ?",
            (companyfacts_fact_id,),
        )
        assert _observation_count(conn, table=table, fact_id=companyfacts_fact_id) == 1
    finally:
        conn.close()


def test_upgrade_fails_closed_on_unexpected_predecessor_trigger_shape(
    tmp_path: Path,
) -> None:
    path = _create_predecessor_database(tmp_path)
    conn = _connection(path)
    try:
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        conn.execute(
            "CREATE TRIGGER trg_financial_facts_observation_insert "
            "AFTER INSERT ON financial_facts BEGIN SELECT 1; END"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        RuntimeError,
        match=(
            "trg_financial_facts_observation_insert does not match "
            "the expected 0231 predecessor body"
        ),
    ):
        command.upgrade(_config(path), HEAD)

    conn = _connection(path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None
        assert tuple(row) == (PREDECESSOR,)
    finally:
        conn.close()


def test_staged_companyfacts_fact_requires_match_and_captures_proof_atomically(
    tmp_path: Path,
) -> None:
    path = _create_predecessor_database(tmp_path)
    command.upgrade(_config(path), HEAD)
    conn = _connection(path)
    try:
        IssuerRegistry(conn).persist(
            IssuerEntity(
                issuer_id="issuer-acme",
                idempotency_key="issuer-acme",
                entity_kind="operating_company",
                created_at=STAMP,
            )
        )
        _, node_id = _seed_document(
            conn,
            document_id=2,
            source_kind="sec_companyfacts",
            direct_bridge=False,
        )
        fact_id = _insert_fact(conn, "financial_facts", 2)
        conn.commit()
        checkpoint = tmp_path / "companyfacts-proof-cutover.json"
        request = FactCutoverRequest(
            apply=False,
            batch_size=10,
            checkpoint_path=checkpoint,
            knowledge_cutoff=STAMP,
        )

        blocked = execute_fact_cutover(conn, request)
        assert blocked.rows_quarantined == 1
        assert blocked.finding_counts == {"companyfacts_fact_evidence_match_required": 1}

        binding = conn.execute(
            "SELECT binding_revision_id, revision, scope_content_sha256 "
            "FROM v_legacy_document_evidence_bindings_current "
            "WHERE legacy_document_id = 2"
        ).fetchone()
        assert binding is not None
        original_locator = OriginalFactLocator(root={"cell": "revenue"})
        relocated = CompanyFactsRelocatedLocator(
            accession_number="0000000001-26-000002",
            namespace="us-gaap",
            concept="Revenues",
            unit="USD",
            entry_index=0,
            json_path="facts.us-gaap.Revenues.units.USD[0]",
        )
        entry_sha = hashlib.sha256(b"matched-companyfacts-entry").hexdigest()
        match = LegacyFactEvidenceMatchRevision(
            match_revision_id="match-companyfacts-fact-1",
            idempotency_key="match-companyfacts-fact-1",
            fact_table="financial_facts",
            fact_row_id=fact_id,
            issuer_id="issuer-acme",
            revision=1,
            fact_payload=FinancialFactPayloadV1(
                schema_version="financial_fact_payload.v1",
                fact_table="financial_facts",
                fact_row_id=fact_id,
                ticker="ACME",
                period_end="2026-06-30",
                fiscal_period_type="Q2",
                line_item="revenue",
                value="100",
                currency="USD",
                unit="actual",
                source_doc_id=2,
                extracted_by="test",
                locator=original_locator,
            ),
            original_locator=original_locator,
            relocated_locator=relocated,
            legacy_binding_revision_id=str(binding[0]),
            legacy_binding_revision=int(binding[1]),
            binding_scope_content_sha256=str(binding[2]),
            evidence_node_id=node_id,
            matched_entry_sha256=entry_sha,
            candidate_manifest=CompanyFactsCandidateManifestV1(
                schema_version="companyfacts_candidate_manifest.v1",
                candidates=(
                    CompanyFactsCandidateV1(
                        entry_sha256=entry_sha,
                        relocated_locator=relocated,
                    ),
                ),
            ),
            matched_candidate_count=1,
            issuer_check="pass",
            context_check="pass",
            unit_check="pass",
            sign_check="pass",
            fiscal_period_check="pass",
            value_check="pass",
            matcher_name="test-companyfacts-matcher",
            matcher_version="1",
            matcher_config_sha256=CONFIG_SHA,
            outcome="accepted",
            reason_code="unique_relocated_match",
            reason_details=CanonicalJSONObject(root={}),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_match_revision_id=None,
        )
        LegacyFactEvidenceMatchLedger(conn).persist(match)
        conn.commit()

        applied = execute_fact_cutover(
            conn,
            request.model_copy(update={"apply": True}),
        )
        assert applied.rows_captured == 1
        assert applied.rows_quarantined == 0
        proof = conn.execute(
            "SELECT proof.match_revision_id, observation.method_version "
            "FROM v_fact_observation_match_proofs_current_valid AS proof "
            "JOIN reported_observations AS observation USING (observation_id)"
        ).fetchone()
        assert proof is not None
        assert tuple(proof) == (
            "match-companyfacts-fact-1",
            "0236-companyfacts-match-v1",
        )
        # Metrics admission must read the canonical evidence source kind, not
        # trust the legacy documents.source_type compatibility label.
        conn.execute("UPDATE documents SET source_type = 'legacy_cache' WHERE id = 2")
        assert (
            _unsealed_lineage_reason(
                conn,
                [(CanonicalConcept.REVENUE, datetime(2026, 6, 30), 2)],
            )
            == "companyfacts_input_requires_derivation_seal"
        )
        audit = audit_connection(
            conn,
            AuditOptions(deep_sqlite_checks=False, sample_limit=10),
        )
        codes = {finding.code for finding in audit.findings}
        assert "COMPANYFACTS_CAPTURE_GATE_MISSING" not in codes
        assert "COMPANYFACTS_CURRENT_OBSERVATION_MATCH_PROOF_MISSING" not in codes
        assert "FACT_MATCH_JSON_DIGEST_MISMATCH" not in codes
        assert "FACT_MATCH_JSON_NONCANONICAL" not in codes
    finally:
        conn.close()
