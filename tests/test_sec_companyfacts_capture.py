"""Immutable, accession-scoped SEC CompanyFacts evidence capture."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from pipeline import sec_xbrl
from pipeline.sec_xbrl import FetchedCompanyFacts, ingest_for_ticker
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.sec_companyfacts_capture import (
    CompanyFactsAccessionDocument,
    SecCompanyFactsCaptureRequest,
    capture_sec_companyfacts,
    parse_companyfacts_body,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
CIK = "0000000001"
ISSUER_ID = "issuer-acme"
SOURCE_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
ACCESSION_ONE = "0000000001-26-000001"
ACCESSION_TWO = "0000000001-26-000002"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "companyfacts-capture.db"
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
        """
    )
    for document_id, accession in ((1, ACCESSION_ONE), (2, ACCESSION_TWO)):
        conn.execute(
            "INSERT INTO documents "
            "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
            "fetch_status, raw_bytes_size, source_url, source_quality_tier, "
            "accession_number, filing_date) "
            "VALUES (?, 'ACME', 'sec_xbrl', 'sec_10q', ?, ?, ?, 'ok', 0, ?, "
            "'sec_official', ?, '2026-07-20')",
            (
                document_id,
                f"sec-companyfacts://ACME/{accession}",
                hashlib.sha256(accession.encode()).hexdigest(),
                STAMP,
                SOURCE_URL,
                accession,
            ),
        )
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0231_legacy_document_evidence_bindings")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _body(*, second_value: int = 200) -> bytes:
    payload = {
        "cik": 1,
        "entityName": "ACME Corporation",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue recognized.",
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 100,
                                "accn": ACCESSION_ONE,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-01",
                                "frame": "CY2025",
                            },
                            {
                                "start": "2026-04-01",
                                "end": "2026-06-30",
                                "val": second_value,
                                "accn": ACCESSION_TWO,
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-07-20",
                                "frame": "CY2026Q2",
                            },
                        ]
                    },
                }
            }
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def test_contract_accepts_explicit_null_taxonomy_metadata_but_not_missing_fields() -> None:
    decoded = json.loads(_body())
    concept = decoded["facts"]["us-gaap"]["Revenues"]
    concept["label"] = None
    concept["description"] = None
    body_with_nulls = json.dumps(decoded, separators=(",", ":")).encode()

    parsed = parse_companyfacts_body(body_with_nulls, expected_cik=CIK)

    assert parsed.facts["us-gaap"]["Revenues"].label is None
    del concept["label"]
    missing_label = json.dumps(decoded, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="closed schema"):
        parse_companyfacts_body(missing_label, expected_cik=CIK)


def _request(
    tmp_path: Path, body: bytes, *, retrieved_at: datetime = STAMP
) -> SecCompanyFactsCaptureRequest:
    return SecCompanyFactsCaptureRequest(
        ticker="ACME",
        normalized_cik=CIK,
        issuer_id=ISSUER_ID,
        source_url=SOURCE_URL,
        raw_body=body,
        payload=parse_companyfacts_body(body, expected_cik=CIK),
        accession_documents=(
            CompanyFactsAccessionDocument(
                accession_number=ACCESSION_ONE,
                legacy_document_id=1,
            ),
            CompanyFactsAccessionDocument(
                accession_number=ACCESSION_TWO,
                legacy_document_id=2,
            ),
        ),
        blob_root=tmp_path / "snapshots",
        observed_at=retrieved_at - timedelta(seconds=1),
        retrieved_at=retrieved_at,
    )


def test_capture_persists_exact_bytes_shared_snapshot_and_exact_replay(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    raw_body = _body()
    try:
        first = capture_sec_companyfacts(conn, _request(tmp_path, raw_body))
        conn.commit()

        assert first.document_version_created is True
        assert first.bindings_created == 2
        assert first.bindings_unchanged == 0
        digest = hashlib.sha256(raw_body).hexdigest()
        blob = conn.execute(
            "SELECT sha256, byte_size, storage_uri FROM evidence_content_blobs"
        ).fetchone()
        assert blob is not None
        assert tuple(blob[:2]) == (digest, len(raw_body))
        stored_path = Path(str(blob[2]).removeprefix("file:///"))
        assert stored_path.read_bytes() == raw_body
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT document_version_id) "
                "FROM legacy_document_evidence_binding_revisions"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE node_kind = 'document'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE node_kind = 'section'"
            ).fetchone()[0]
            == 2
        )

        replay = capture_sec_companyfacts(conn, _request(tmp_path, raw_body))
        conn.commit()
        assert replay.document_version_created is False
        assert replay.bindings_created == 0
        assert replay.bindings_unchanged == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_changed_snapshot_rebinds_only_changed_accession_scope(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    try:
        first = capture_sec_companyfacts(conn, _request(tmp_path, _body()))
        conn.commit()
        second = capture_sec_companyfacts(
            conn,
            _request(
                tmp_path,
                _body(second_value=201),
                retrieved_at=STAMP + timedelta(hours=1),
            ),
        )
        conn.commit()

        assert first.bindings_created == 2
        assert second.document_version_created is True
        assert second.bindings_created == 1
        assert second.bindings_unchanged == 1
        counts = conn.execute(
            "SELECT legacy_document_id, COUNT(*) "
            "FROM legacy_document_evidence_binding_revisions "
            "GROUP BY legacy_document_id ORDER BY legacy_document_id"
        ).fetchall()
        assert [tuple(row) for row in counts] == [(1, 1), (2, 2)]
        current = conn.execute(
            "SELECT legacy_document_id, revision, document_version_id "
            "FROM v_legacy_document_evidence_bindings_current "
            "ORDER BY legacy_document_id"
        ).fetchall()
        assert int(current[0][1]) == 1
        assert int(current[1][1]) == 2
        assert str(current[0][2]) != str(current[1][2])
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE node_kind = 'section'"
            ).fetchone()[0]
            == 3
        )
    finally:
        conn.close()


def _ingest_database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "companyfacts-ingest.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        CREATE UNIQUE INDEX uq_financial_facts_provenance
        ON financial_facts (
            ticker, period_end, fiscal_period_type, line_item, source_doc_id
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
    command.upgrade(config, "0231_legacy_document_evidence_bindings")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id=ISSUER_ID,
            idempotency_key="issuer-acme",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    assertion = IdentifierAssertion(
        assertion_id="assertion-acme-cik",
        idempotency_key="assertion-acme-cik",
        issuer_id=ISSUER_ID,
        identifier_type="sec_cik",
        identifier_value=CIK,
        normalized_value=CIK,
        authority="manual",
        source_observation_id=None,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="resolution-acme-cik",
            idempotency_key="resolution-acme-cik",
            resolution_key=f"sec_cik:{CIK}",
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="test",
            policy_version="1",
            policy_config_sha256=hashlib.sha256(b"test-policy").hexdigest(),
            material_dissent=False,
            reason_code="manual_test_identity",
            reason_details=(("source", "test"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_resolution_id=None,
        )
    )
    conn.commit()
    return conn


def test_ingest_captures_evidence_before_post_cutover_fact_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ingest_database(tmp_path)
    raw_body = _body()
    fetched = FetchedCompanyFacts(
        source_url=SOURCE_URL,
        raw_body=raw_body,
        observed_at=STAMP - timedelta(seconds=1),
        retrieved_at=STAMP,
    )
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return fetched

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    try:
        stats = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)

        assert stats.accessions_inserted == 2
        assert stats.facts_inserted == 2
        facts = conn.execute(
            "SELECT fact.value, observation.evidence_node_id, node.node_kind "
            "FROM financial_facts AS fact "
            "JOIN fact_observation_revisions AS link "
            "ON link.fact_table = 'financial_facts' AND link.fact_row_id = fact.id "
            "JOIN reported_observations AS observation USING (observation_id) "
            "JOIN evidence_nodes AS node ON node.node_id = observation.evidence_node_id "
            "ORDER BY fact.value"
        ).fetchall()
        assert [(int(row[0]), str(row[2])) for row in facts] == [
            (100, "section"),
            (200, "section"),
        ]
        assert all(str(row[1]).startswith("sec-companyfacts-accession-node:") for row in facts)
        document_paths = conn.execute(
            "SELECT DISTINCT file_path FROM documents ORDER BY file_path"
        ).fetchall()
        assert len(document_paths) == 2
        assert all("/snapshots/" in str(row[0]).replace("\\", "/") for row in document_paths)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
