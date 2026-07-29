"""Revisioned bridges from legacy source documents to canonical evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.financial_fact_resolution import FactCutoverRequest, execute_fact_cutover
from provenance.integrity_audit import AuditOptions, audit_connection

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0231_legacy_document_evidence_bindings"
STAMP = datetime(2026, 7, 27, 12, 0, 0)
CONFIG_SHA = hashlib.sha256(b"config").hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "legacy-evidence-bindings.db"
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
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_legacy_document(conn: sqlite3.Connection, document_id: int, accession: str) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, "
        "raw_bytes_size, source_url, source_quality_tier, accession_number, filing_date) "
        "VALUES (?, 'ACME', 'sec_xbrl', 'sec_10q', ?, ?, ?, 'ok', 0, ?, "
        "'sec_official', ?, '2026-07-20')",
        (
            document_id,
            f"sec-companyfacts://ACME/{accession}",
            hashlib.sha256(accession.encode()).hexdigest(),
            STAMP,
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            accession,
        ),
    )


def _seed_snapshot(
    conn: sqlite3.Connection,
    *,
    suffix: str,
    body: bytes,
    storage_path: Path | None = None,
) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    output_sha = hashlib.sha256(b"document-node:" + body).hexdigest()
    if storage_path is not None:
        storage_path.write_bytes(body)
    storage_uri = (
        storage_path.as_uri() if storage_path is not None else f"file:///snapshots/{digest}.json"
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="application/json",
            storage_uri=storage_uri,
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"source-{suffix}",
            idempotency_key=f"source-{suffix}",
            source_kind="sec_companyfacts",
            source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="sec-companyfacts@test",
        )
    )
    document_version_id = f"snapshot-{suffix}"
    ledger.persist(
        DocumentVersion(
            document_version_id=document_version_id,
            document_key="issuer-acme:sec-companyfacts",
            version_sequence=1 if suffix == "one" else 2,
            observation_id=f"source-{suffix}",
            blob_sha256=digest,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="companyfacts_snapshot",
            form_type="SEC-COMPANYFACTS",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            period_end=None,
            as_of_at=STAMP,
            language="en",
            replaces_document_version_id=None if suffix == "one" else "snapshot-one",
            legacy_document_id=None,
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=f"run-{suffix}",
            idempotency_key=f"run-{suffix}",
            document_version_id=document_version_id,
            input_sha256=digest,
            extractor_name="sec-companyfacts-document-anchor",
            extractor_config_sha256=CONFIG_SHA,
            extractor_code_version="test@1",
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
            evidence_key=f"{document_version_id}:document",
            revision=1,
            extraction_run_id=f"run-{suffix}",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text=f"SEC CompanyFacts snapshot {suffix}",
            locator=None,
            recorded_at=STAMP,
        )
    )
    return document_version_id, node_id


def _bind(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    accession: str,
    revision: int,
    document_version_id: str,
    node_id: str,
) -> None:
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions "
        "(binding_revision_id, idempotency_key, legacy_document_id, revision, "
        "document_version_id, evidence_node_id, scope_locator_json, "
        "scope_locator_sha256, scope_content_sha256, effective_at, knowledge_at, recorded_at, "
        "supersedes_binding_revision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"binding-{document_id}-{revision}",
            f"binding-{document_id}-{revision}",
            document_id,
            revision,
            document_version_id,
            node_id,
            f'{{"accession_number":"{accession}"}}',
            hashlib.sha256(f'{{"accession_number":"{accession}"}}'.encode()).hexdigest(),
            hashlib.sha256(f"scope:{accession}:{revision}".encode()).hexdigest(),
            STAMP,
            STAMP,
            STAMP,
            None if revision == 1 else f"binding-{document_id}-{revision - 1}",
        ),
    )


def test_many_legacy_accessions_share_snapshot_and_corrections_use_new_binding(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        first_accession = "0000000001-26-000001"
        second_accession = "0000000001-26-000002"
        _seed_legacy_document(conn, 1, first_accession)
        _seed_legacy_document(conn, 2, second_accession)
        first_version, first_node = _seed_snapshot(conn, suffix="one", body=b'{"facts":1}')
        _bind(
            conn,
            document_id=1,
            accession=first_accession,
            revision=1,
            document_version_id=first_version,
            node_id=first_node,
        )
        _bind(
            conn,
            document_id=2,
            accession=second_accession,
            revision=1,
            document_version_id=first_version,
            node_id=first_node,
        )

        cursor = conn.execute(
            "INSERT INTO financial_facts "
            "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
            "source_doc_id, confidence, extracted_by, locator) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 'revenue', 100, 'USD', 'actual', "
            "1, 0.99, 'sec_xbrl', "
            '\'{"json_path":"facts.us-gaap.Revenues.units.USD[0]"}\')'
        )
        assert cursor.lastrowid is not None
        fact_id = int(cursor.lastrowid)
        first_observation = conn.execute(
            "SELECT observation.evidence_node_id "
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id) "
            "WHERE link.fact_row_id = ? AND link.fact_revision = 1",
            (fact_id,),
        ).fetchone()
        assert first_observation is not None
        assert tuple(first_observation) == (first_node,)

        second_version, second_node = _seed_snapshot(conn, suffix="two", body=b'{"facts":2}')
        _bind(
            conn,
            document_id=1,
            accession=first_accession,
            revision=2,
            document_version_id=second_version,
            node_id=second_node,
        )
        conn.execute("UPDATE financial_facts SET value = 101 WHERE id = ?", (fact_id,))

        revisions = conn.execute(
            "SELECT link.fact_revision, observation.numeric_value, "
            "observation.evidence_node_id "
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id) "
            "WHERE link.fact_row_id = ? ORDER BY link.fact_revision",
            (fact_id,),
        ).fetchall()
        assert [tuple(row) for row in revisions] == [
            (1, "100", first_node),
            (2, "101", second_node),
        ]
        shared_bindings = conn.execute(
            "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions "
            "WHERE document_version_id = ?",
            (first_version,),
        ).fetchone()
        assert shared_bindings is not None
        assert tuple(shared_bindings) == (2,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("fact_value", "json_path", "expected_captured", "expected_reason"),
    [
        ("100", "facts.us-gaap.Revenues.units.USD[0]", 1, None),
        ("101", "facts.us-gaap.Revenues.units.USD[0]", 0, "companyfacts_locator_unverified"),
        ("-100", "facts.us-gaap.Revenues.units.USD[0]", 0, "companyfacts_locator_unverified"),
        ("100", "facts.us-gaap.Revenues.units.USD[1]", 0, "companyfacts_locator_unverified"),
    ],
)
def test_legacy_fact_backfill_resolves_only_verified_accession_cells(
    tmp_path: Path,
    fact_value: str,
    json_path: str,
    expected_captured: int,
    expected_reason: str | None,
) -> None:
    conn = _database(tmp_path)
    try:
        accession = "0000000001-26-000001"
        _seed_legacy_document(conn, 1, accession)
        body = (
            b'{"facts":{"us-gaap":{"Revenues":{"units":{"USD":['
            b'{"accn":"0000000001-26-000001","end":"2026-06-30","val":100}'
            b"]}}}}}"
        )
        version_id, node_id = _seed_snapshot(
            conn,
            suffix="one",
            body=body,
            storage_path=tmp_path / "companyfacts.json",
        )
        _bind(
            conn,
            document_id=1,
            accession=accession,
            revision=1,
            document_version_id=version_id,
            node_id=node_id,
        )
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        cursor = conn.execute(
            "INSERT INTO financial_facts "
            "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
            "source_doc_id, confidence, extracted_by, locator) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 'revenue', ?, 'USD', 'actual', "
            "1, 0.99, 'sec_xbrl', ?)",
            (fact_value, json.dumps({"json_path": json_path})),
        )
        assert cursor.lastrowid is not None
        conn.commit()

        result = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=tmp_path / "binding-cutover-state.json",
                knowledge_cutoff=STAMP,
            ),
        )

        assert result.rows_captured == expected_captured
        assert result.rows_quarantined == 1 - expected_captured
        assert result.finding_counts == ({} if expected_reason is None else {expected_reason: 1})
        captured = conn.execute(
            "SELECT observation.evidence_node_id "
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id) "
            "WHERE link.fact_table = 'financial_facts' AND link.fact_row_id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        if expected_captured:
            assert captured is not None
            assert tuple(captured) == (node_id,)
            audit = audit_connection(conn, AuditOptions())
            mismatch = next(
                (
                    finding
                    for finding in audit.findings
                    if finding.code == "FACT_OBSERVATION_EVIDENCE_DOCUMENT_MISMATCH"
                ),
                None,
            )
            assert mismatch is None
        else:
            assert captured is None
    finally:
        conn.close()


def test_companyfacts_bound_derived_kpi_requires_input_observation_lineage(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        accession = "0000000001-26-000001"
        _seed_legacy_document(conn, 1, accession)
        body = (
            b'{"facts":{"us-gaap":{"Revenues":{"units":{"USD":['
            b'{"accn":"0000000001-26-000001","end":"2026-06-30","val":100}'
            b"]}}}}}"
        )
        version_id, node_id = _seed_snapshot(
            conn,
            suffix="one",
            body=body,
            storage_path=tmp_path / "companyfacts-kpi.json",
        )
        _bind(
            conn,
            document_id=1,
            accession=accession,
            revision=1,
            document_version_id=version_id,
            node_id=node_id,
        )
        conn.execute("INSERT INTO kpi_definitions VALUES (1, 'ACME', 'Growth', 'percent')")
        conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
        conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "source_doc_id, confidence, extracted_by, locator, computed_from, "
            "formula_id, formula_version) "
            "VALUES ('ACME', '2026-06-30', 'Q2', 1, 100, 'percent', 1, 0.9, "
            "'metrics_engine', ?, '[{\"fact_id\":1}]', 1, 1)",
            (json.dumps({"json_path": "facts.us-gaap.Revenues.units.USD[0]"}),),
        )
        conn.commit()

        result = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=tmp_path / "derived-kpi-cutover-state.json",
                knowledge_cutoff=STAMP,
            ),
        )

        assert result.rows_captured == 0
        assert result.rows_quarantined == 1
        assert result.finding_counts == {"companyfacts_derived_fact_requires_input_lineage": 1}
    finally:
        conn.close()


def test_binding_chain_is_append_only_and_node_must_belong_to_document(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        accession = "0000000001-26-000001"
        _seed_legacy_document(conn, 1, accession)
        first_version, first_node = _seed_snapshot(conn, suffix="one", body=b'{"facts":1}')
        second_version, second_node = _seed_snapshot(conn, suffix="two", body=b'{"facts":2}')
        _bind(
            conn,
            document_id=1,
            accession=accession,
            revision=1,
            document_version_id=first_version,
            node_id=first_node,
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE legacy_document_evidence_binding_revisions "
                "SET evidence_node_id = ? WHERE legacy_document_id = 1",
                (second_node,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="same document version"):
            _bind(
                conn,
                document_id=1,
                accession=accession,
                revision=2,
                document_version_id=second_version,
                node_id=first_node,
            )
    finally:
        conn.close()
