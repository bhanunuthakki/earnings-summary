"""Immutable, accession-scoped SEC CompanyFacts evidence capture."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from execution import fetch_sec_xbrl as fetch_sec_xbrl_execution
from pipeline import restatement_detector, sec_xbrl
from pipeline.sec_xbrl import FetchedCompanyFacts, ingest_for_ticker
from provenance.financial_fact_resolution import FactTable
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.sec_companyfacts_capture import (
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


class _AdvancingClock:
    def __init__(self, step_ns: int = 1_000_000) -> None:
        self._now = 0
        self._step_ns = step_ns

    def __call__(self) -> int:
        current = self._now
        self._now += self._step_ns
        return current

    def advance(self, duration_ns: int) -> None:
        self._now += duration_ns


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
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0231_legacy_document_evidence_bindings")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_snapshot_document(
    conn: sqlite3.Connection,
    body: bytes,
    *,
    document_id: int,
    fetched_at: datetime = STAMP,
) -> None:
    digest = hashlib.sha256(body).hexdigest()
    database_path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    snapshot_path = (database_path.parent / "snapshots" / digest[:2] / f"{digest}.json").resolve()
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_url, source_quality_tier, "
        "accession_number, filing_date) "
        "VALUES (?, 'ACME', 'sec_xbrl', 'sec_companyfacts_snapshot', ?, ?, ?, "
        "'ok', ?, ?, 'sec_official', NULL, NULL)",
        (
            document_id,
            str(snapshot_path),
            digest,
            fetched_at,
            len(body),
            SOURCE_URL,
        ),
    )


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
    tmp_path: Path,
    body: bytes,
    *,
    snapshot_document_id: int,
    retrieved_at: datetime = STAMP,
) -> SecCompanyFactsCaptureRequest:
    return SecCompanyFactsCaptureRequest(
        ticker="ACME",
        normalized_cik=CIK,
        issuer_id=ISSUER_ID,
        source_url=SOURCE_URL,
        raw_body=body,
        payload=parse_companyfacts_body(body, expected_cik=CIK),
        snapshot_document_id=snapshot_document_id,
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
        _seed_snapshot_document(conn, raw_body, document_id=1)
        first = capture_sec_companyfacts(
            conn,
            _request(tmp_path, raw_body, snapshot_document_id=1),
        )
        conn.commit()

        assert first.document_version_created is True
        assert first.bindings_created == 1
        assert first.bindings_unchanged == 0
        digest = hashlib.sha256(raw_body).hexdigest()
        blob = conn.execute(
            "SELECT sha256, byte_size, storage_uri FROM evidence_content_blobs"
        ).fetchone()
        assert blob is not None
        assert tuple(blob[:2]) == (digest, len(raw_body))
        stored_path = Path(url2pathname(urlparse(str(blob[2])).path))
        assert stored_path.read_bytes() == raw_body
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions"
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
            == 0
        )
        snapshot = conn.execute(
            "SELECT doc_type, sha256, accession_number FROM documents"
        ).fetchone()
        assert snapshot is not None
        assert tuple(snapshot) == ("sec_companyfacts_snapshot", digest, None)

        binding_before = tuple(
            conn.execute(
                "SELECT binding_revision_id, idempotency_key, effective_at, "
                "knowledge_at, recorded_at "
                "FROM legacy_document_evidence_binding_revisions"
            ).fetchone()
        )
        replay = capture_sec_companyfacts(
            conn,
            _request(
                tmp_path,
                raw_body,
                snapshot_document_id=1,
                retrieved_at=STAMP + timedelta(hours=1),
            ),
        )
        conn.commit()
        assert replay.document_version_created is False
        assert replay.bindings_created == 0
        assert replay.bindings_unchanged == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions"
            ).fetchone()[0]
            == 1
        )
        binding_after = tuple(
            conn.execute(
                "SELECT binding_revision_id, idempotency_key, effective_at, "
                "knowledge_at, recorded_at "
                "FROM legacy_document_evidence_binding_revisions"
            ).fetchone()
        )
        assert binding_after == binding_before
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence_document_observation_links").fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_changed_response_creates_a_new_aggregate_snapshot_without_mutation(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        first_body = _body()
        second_body = _body(second_value=201)
        _seed_snapshot_document(conn, first_body, document_id=1)
        first = capture_sec_companyfacts(
            conn,
            _request(tmp_path, first_body, snapshot_document_id=1),
        )
        conn.commit()
        _seed_snapshot_document(
            conn,
            second_body,
            document_id=2,
            fetched_at=STAMP + timedelta(hours=1),
        )
        second = capture_sec_companyfacts(
            conn,
            _request(
                tmp_path,
                second_body,
                snapshot_document_id=2,
                retrieved_at=STAMP + timedelta(hours=1),
            ),
        )
        conn.commit()

        assert first.bindings_created == 1
        assert second.document_version_created is True
        assert second.bindings_created == 1
        assert second.bindings_unchanged == 0
        bindings = conn.execute(
            "SELECT legacy_document_id, revision, scope_content_sha256 "
            "FROM legacy_document_evidence_binding_revisions "
            "ORDER BY legacy_document_id"
        ).fetchall()
        assert [(int(row[0]), int(row[1])) for row in bindings] == [(1, 1), (2, 1)]
        assert [str(row[2]) for row in bindings] == [
            hashlib.sha256(first_body).hexdigest(),
            hashlib.sha256(second_body).hexdigest(),
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE node_kind = 'section'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def _seed_issuer_identity(conn: sqlite3.Connection) -> None:
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
    _seed_issuer_identity(conn)
    return conn


def test_current_schema_companyfacts_fact_admission_is_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: object,
) -> None:
    database_path = tmp_path / "current-companyfacts-ingest.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
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
        receipts: list[sec_xbrl.SecIngestTimingReceipt] = []
        stats = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            timing_sink=receipts.append,
            monotonic_ns=_AdvancingClock(),
        )

        assert stats.facts_inserted == 2
        assert receipts == [stats.timing]
        assert stats.timing is not None
        assert stats.timing.outcome == "success"
        assert stats.timing.failed_phase is None
        assert stats.timing.total_ms > 0
        phase_values = stats.timing.phases.model_dump()
        assert all(value > 0 for value in phase_values.values())
        receipt_payload = stats.timing.model_dump(mode="json")

        def _keys(value: object) -> set[str]:
            found: set[str] = set()
            if isinstance(value, dict):
                mapping = cast("dict[object, object]", value)
                for key, item in mapping.items():
                    found.add(str(key))
                    found.update(_keys(item))
            if isinstance(value, list):
                for item in cast("list[object]", value):
                    found.update(_keys(item))
            return found

        receipt_keys = _keys(receipt_payload)
        for forbidden in (
            "source_url",
            "cik",
            "raw_body",
            "file_path",
            "digest",
            "accession",
            "locator",
            "value",
        ):
            assert forbidden not in receipt_keys
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0018_add_transcript_acquisition_receipts"
        )
        snapshot_documents = conn.execute(
            "SELECT id, doc_type, accession_number, sha256 FROM documents"
        ).fetchall()
        assert len(snapshot_documents) == 1
        assert snapshot_documents[0][1] == "sec_companyfacts_snapshot"
        assert snapshot_documents[0][2] is None
        assert snapshot_documents[0][3] == hashlib.sha256(raw_body).hexdigest()
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_legacy_fact_evidence_matches_accepted_current"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_fact_observation_match_proofs_current_valid"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 2
        )
        admitted = conn.execute(
            "SELECT fact.id, fact.value, fact.source_doc_id, fact.locator, "
            "match.fact_row_id, match.match_revision_id, match.relocated_locator_json, "
            "match.matched_entry_sha256, proof.fact_row_id, proof.match_revision_id, "
            "proof.observation_id, link.fact_row_id, link.observation_id "
            "FROM financial_facts AS fact "
            "JOIN v_legacy_fact_evidence_matches_accepted_current AS match "
            "ON match.fact_table = 'financial_facts' AND match.fact_row_id = fact.id "
            "JOIN v_fact_observation_match_proofs_current_valid AS proof "
            "ON proof.match_revision_id = match.match_revision_id "
            "JOIN fact_observation_revisions AS link "
            "ON link.fact_table = proof.fact_table AND link.fact_row_id = proof.fact_row_id "
            "AND link.fact_revision = proof.fact_revision "
            "ORDER BY fact.value"
        ).fetchall()
        parsed = parse_companyfacts_body(raw_body, expected_cik=CIK)
        entries = parsed.facts["us-gaap"]["Revenues"].units["USD"]
        expected = (
            (100, ACCESSION_ONE, 0, "2025-12-31"),
            (200, ACCESSION_TWO, 1, "2026-06-30"),
        )
        assert len(admitted) == len(expected)
        for row, (value, accession, entry_index, period_end) in zip(
            admitted, expected, strict=True
        ):
            expected_path = f"facts.us-gaap.Revenues.units.USD[{entry_index}]"
            assert int(row[1]) == value
            assert int(row[2]) == int(snapshot_documents[0][0])
            assert json.loads(str(row[3])) == {
                "accession_number": accession,
                "kind": "fmp_json_table",
                "json_path": expected_path,
                "locator_version": 2,
                "table_cell": {
                    "cell_value_as_extracted": str(value),
                    "column_header": period_end,
                    "json_path": expected_path,
                    "row_label": "Revenues",
                    "table_title": "us-gaap",
                },
                "verbatim_snippet": str(value),
            }
            assert json.loads(str(row[6])) == {
                "accession_number": accession,
                "concept": "Revenues",
                "entry_index": entry_index,
                "json_path": expected_path,
                "namespace": "us-gaap",
                "unit": "USD",
            }
            entry_bytes = json.dumps(
                entries[entry_index].model_dump(mode="json", exclude_none=False),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            assert row[7] == hashlib.sha256(entry_bytes).hexdigest()
            assert int(row[0]) == int(row[4]) == int(row[8]) == int(row[11])
            assert row[5] == row[9]
            assert row[10] == row[12]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_current_schema_companyfacts_replay_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_db: object,
) -> None:
    database_path = tmp_path / "current-companyfacts-replay.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) "
        "VALUES ('ACME', 'Acme', 'evaluation')"
    )
    raw_body = _body()
    responses = iter(
        (
            FetchedCompanyFacts(
                source_url=SOURCE_URL,
                raw_body=raw_body,
                observed_at=STAMP - timedelta(seconds=1),
                retrieved_at=STAMP,
            ),
            FetchedCompanyFacts(
                source_url=SOURCE_URL,
                raw_body=raw_body,
                observed_at=STAMP + timedelta(hours=1) - timedelta(seconds=1),
                retrieved_at=STAMP + timedelta(hours=1),
            ),
        )
    )
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return next(responses)

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    try:
        first = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        immutable_chain_tables = (
            "documents",
            "legacy_document_evidence_binding_revisions",
            "financial_facts",
            "legacy_fact_evidence_match_revisions",
            "reported_observations",
            "fact_observation_revisions",
            "fact_observation_match_proofs",
            "observation_resolution_revisions",
        )
        before = {
            table: [
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            ]
            for table in immutable_chain_tables
        }
        source_observations_before = conn.execute(
            "SELECT COUNT(*) FROM evidence_source_observations"
        ).fetchone()[0]
        retrieval_links_before = conn.execute(
            "SELECT COUNT(*) FROM evidence_document_observation_links"
        ).fetchone()[0]
        conn.execute("UPDATE tracked_companies SET brief_dirty = 0 WHERE ticker = 'ACME'")
        conn.commit()
        second = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        after = {
            table: [
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            ]
            for table in immutable_chain_tables
        }
        assert first.accessions_inserted == 2
        assert first.facts_inserted == 2
        assert second.accessions_inserted == 0
        assert second.facts_inserted == 0
        assert before == after
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0]
            == source_observations_before + 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence_document_observation_links").fetchone()[0]
            == retrieval_links_before + 1
        )
        capsys.readouterr()

        def _unexpected_invalidation(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("exact replay must not invalidate fact consumers")

        monkeypatch.setattr(
            fetch_sec_xbrl_execution,
            "flag_silent_staleness",
            _unexpected_invalidation,
        )
        monkeypatch.setattr(
            fetch_sec_xbrl_execution,
            "mark_artifacts_dirty_for_fact_change",
            _unexpected_invalidation,
        )
        assert fetch_sec_xbrl_execution.handle_silent_staleness(
            conn,
            ticker="ACME",
            stats=second,
            db_path=str(database_path),
        ) == (False, 0)
        assert (
            conn.execute(
                "SELECT brief_dirty FROM tracked_companies WHERE ticker = 'ACME'"
            ).fetchone()[0]
            == 0
        )
        assert capsys.readouterr().err == ""
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_current_schema_companyfacts_match_failure_rolls_back_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: object,
) -> None:
    database_path = tmp_path / "current-companyfacts-rollback.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
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

    def _reject(*_args: object, **_kwargs: object) -> object:
        raise ValueError("forced exact-match rejection")

    monkeypatch.setattr(
        "provenance.sec_companyfacts_fact_matcher.match_companyfacts_fact_row",
        _reject,
    )
    try:
        receipts: list[sec_xbrl.SecIngestTimingReceipt] = []
        with pytest.raises(ValueError, match="forced exact-match rejection"):
            ingest_for_ticker(
                conn,
                ticker="ACME",
                project_root=tmp_path,
                timing_sink=receipts.append,
                monotonic_ns=_AdvancingClock(),
            )
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.outcome == "failed"
        assert receipt.failed_phase == "fact_admission"
        assert receipt.phases.http_fetch_ms > 0
        assert receipt.phases.commit_ms == 0
        assert "forced exact-match rejection" not in receipt.model_dump_json()
        for table in (
            "documents",
            "financial_facts",
            "legacy_fact_evidence_match_revisions",
            "reported_observations",
            "fact_observation_revisions",
            "fact_observation_match_proofs",
            "observation_resolution_revisions",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_sec_ingest_timing_receipt_rejects_negative_and_unknown_fields() -> None:
    durations = {
        "http_fetch_ms": 1.0,
        "payload_parse_ms": 1.0,
        "snapshot_registration_ms": 1.0,
        "accession_mapping_ms": 1.0,
        "document_evidence_capture_ms": 1.0,
        "tag_selection_ms": 1.0,
        "fact_persistence_restatement_ms": 1.0,
        "evidence_capture_resolution_ms": 1.0,
        "commit_ms": 1.0,
        "latest_cache_publish_ms": 1.0,
    }
    with pytest.raises(ValidationError):
        sec_xbrl.SecIngestPhaseDurations.model_validate({**durations, "http_fetch_ms": -1.0})
    with pytest.raises(ValidationError):
        sec_xbrl.SecIngestPhaseDurations.model_validate({**durations, "unexpected_ms": 1.0})
    valid_phases = sec_xbrl.SecIngestPhaseDurations.model_validate(durations)
    with pytest.raises(ValidationError, match="failed_phase"):
        sec_xbrl.SecIngestTimingReceipt(
            ticker="ACME",
            outcome="failed",
            failed_phase=None,
            total_ms=10,
            phases=valid_phases,
        )


def test_companyfacts_selection_failure_keeps_elapsed_timing_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: object,
) -> None:
    database_path = tmp_path / "selection-failure.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
    payload = json.loads(_body())
    entries = payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    entries[1].update(entries[0])
    entries[1]["val"] = 999
    raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return FetchedCompanyFacts(
            source_url=SOURCE_URL,
            raw_body=raw_body,
            observed_at=STAMP - timedelta(seconds=1),
            retrieved_at=STAMP,
        )

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    receipts: list[sec_xbrl.SecIngestTimingReceipt] = []
    try:
        with pytest.raises(ValueError, match="identical SEC chronology"):
            ingest_for_ticker(
                conn,
                ticker="ACME",
                project_root=tmp_path,
                timing_sink=receipts.append,
                monotonic_ns=_AdvancingClock(),
            )
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.outcome == "failed"
        assert receipt.failed_phase == "fact_admission"
        assert receipt.phases.tag_selection_ms > 0
        assert receipt.phases.commit_ms == 0
        for table in (
            "documents",
            "financial_facts",
            "legacy_document_evidence_binding_revisions",
            "reported_observations",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_current_schema_companyfacts_post_match_observation_proof_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: object,
) -> None:
    database_path = tmp_path / "current-companyfacts-observation-proof-rollback.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
    fetched = FetchedCompanyFacts(
        source_url=SOURCE_URL,
        raw_body=_body(),
        observed_at=STAMP - timedelta(seconds=1),
        retrieved_at=STAMP,
    )
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return fetched

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    capture = sec_xbrl.capture_fact_row_observation

    def _capture_then_reject(
        connection: sqlite3.Connection,
        *,
        fact_table: FactTable,
        fact_row_id: int,
        recorded_at: datetime,
    ) -> bool:
        capture(
            connection,
            fact_table=fact_table,
            fact_row_id=fact_row_id,
            recorded_at=recorded_at,
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM legacy_fact_evidence_match_revisions").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_match_proofs").fetchone()[0] == 1
        raise RuntimeError("forced post-match observation/proof failure")

    monkeypatch.setattr(sec_xbrl, "capture_fact_row_observation", _capture_then_reject)
    try:
        with pytest.raises(RuntimeError, match="forced post-match observation/proof failure"):
            ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        for table in (
            "documents",
            "financial_facts",
            "legacy_fact_evidence_match_revisions",
            "reported_observations",
            "fact_observation_revisions",
            "fact_observation_match_proofs",
            "observation_resolution_revisions",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_current_schema_companyfacts_resolution_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: object,
) -> None:
    database_path = tmp_path / "current-companyfacts-resolution-rollback.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
    fetched = FetchedCompanyFacts(
        source_url=SOURCE_URL,
        raw_body=_body(),
        observed_at=STAMP - timedelta(seconds=1),
        retrieved_at=STAMP,
    )
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return fetched

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)

    def _reject_resolution(*_args: object, **_kwargs: object) -> None:
        assert (
            conn.execute("SELECT COUNT(*) FROM legacy_fact_evidence_match_revisions").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_match_proofs").fetchone()[0] == 1
        raise RuntimeError("forced resolution failure")

    monkeypatch.setattr(restatement_detector, "resolve_fact_row", _reject_resolution)
    try:
        with pytest.raises(RuntimeError, match="forced resolution failure"):
            ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        for table in (
            "documents",
            "financial_facts",
            "legacy_fact_evidence_match_revisions",
            "reported_observations",
            "fact_observation_revisions",
            "fact_observation_match_proofs",
            "observation_resolution_revisions",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_current_schema_companyfacts_amendment_preserves_chronology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: object,
) -> None:
    database_path = tmp_path / "current-companyfacts-amendment.db"
    assert callable(migrated_db)
    migrated_db(database_path, target="head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_issuer_identity(conn)
    original_body = _body()
    amended_payload = json.loads(original_body)
    amended_entry = amended_payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"][1]
    amended_entry["val"] = 201
    amended_entry["form"] = "10-Q/A"
    amended_entry["filed"] = "2026-07-25"
    amended_entry["accn"] = "0000000001-26-000003"
    amended_body = json.dumps(
        amended_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    responses = iter(
        (
            FetchedCompanyFacts(
                source_url=SOURCE_URL,
                raw_body=original_body,
                observed_at=STAMP - timedelta(seconds=1),
                retrieved_at=STAMP,
            ),
            FetchedCompanyFacts(
                source_url=SOURCE_URL,
                raw_body=amended_body,
                observed_at=STAMP + timedelta(hours=1) - timedelta(seconds=1),
                retrieved_at=STAMP + timedelta(hours=1),
            ),
        )
    )
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return next(responses)

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    try:
        ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        clock = _AdvancingClock()
        record_restatement = sec_xbrl.record_restatement_observation

        def _slow_restatement_observation(
            connection: sqlite3.Connection,
            *,
            fact_table: str,
            superseded_id: int,
            new_value: object,
            user_id: str = "bhanu",
            observed_at: str | None = None,
        ) -> int | None:
            clock.advance(50_000_000)
            return record_restatement(
                connection,
                fact_table=fact_table,
                superseded_id=superseded_id,
                new_value=new_value,
                user_id=user_id,
                observed_at=observed_at,
            )

        monkeypatch.setattr(
            sec_xbrl,
            "record_restatement_observation",
            _slow_restatement_observation,
        )
        second = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            monotonic_ns=clock,
        )
        assert second.timing is not None
        assert second.timing.phases.fact_persistence_restatement_ms >= 50

        rows = conn.execute(
            "SELECT fact.id, fact.value, fact.supersedes_id, fact.locator, "
            "match.matched_entry_sha256 "
            "FROM financial_facts AS fact "
            "JOIN v_legacy_fact_evidence_matches_accepted_current AS match "
            "ON match.fact_table='financial_facts' AND match.fact_row_id=fact.id "
            "WHERE fact.period_end='2026-06-30 00:00:00.000000' "
            "OR substr(fact.period_end, 1, 10)='2026-06-30' "
            "ORDER BY fact.id"
        ).fetchall()
        assert len(rows) == 2
        assert [int(row[1]) for row in rows] == [200, 201]
        assert rows[1][2] == rows[0][0]
        assert json.loads(str(rows[0][3]))["accession_number"] == ACCESSION_TWO
        assert json.loads(str(rows[1][3]))["accession_number"] == "0000000001-26-000003"
        assert rows[0][4] != rows[1][4]
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 4
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


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
        documents = conn.execute(
            "SELECT doc_type, accession_number, sha256, file_path FROM documents"
        ).fetchall()
        assert len(documents) == 1
        assert tuple(documents[0][:3]) == (
            "sec_companyfacts_snapshot",
            None,
            hashlib.sha256(raw_body).hexdigest(),
        )
        stored_path = Path(str(documents[0][3]))
        assert stored_path.exists()
        assert hashlib.sha256(stored_path.read_bytes()).hexdigest() == documents[0][2]
        facts = conn.execute(
            "SELECT fact.value, fact.source_doc_id, fact.locator, "
            "observation.evidence_node_id, node.node_kind "
            "FROM financial_facts AS fact "
            "JOIN fact_observation_revisions AS link "
            "ON link.fact_table = 'financial_facts' AND link.fact_row_id = fact.id "
            "JOIN reported_observations AS observation USING (observation_id) "
            "JOIN evidence_nodes AS node ON node.node_id = observation.evidence_node_id "
            "ORDER BY fact.value"
        ).fetchall()
        assert [(int(row[0]), int(row[1]), str(row[4])) for row in facts] == [
            (100, 1, "document"),
            (200, 1, "document"),
        ]
        locators = {int(row[0]): json.loads(str(row[2])) for row in facts}
        assert locators[100]["accession_number"] == ACCESSION_ONE
        assert locators[100]["json_path"] == "facts.us-gaap.Revenues.units.USD[0]"
        assert locators[200]["accession_number"] == ACCESSION_TWO
        assert locators[200]["json_path"] == "facts.us-gaap.Revenues.units.USD[1]"
        assert "/snapshots/" in str(stored_path).replace("\\", "/")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
