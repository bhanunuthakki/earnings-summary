"""Exact proof bridge from fact observations to accepted evidence matches."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
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
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from provenance.legacy_fact_evidence_match import (
    CanonicalJSONObject,
    CompanyFactsCandidateManifestV1,
    CompanyFactsCandidateV1,
    CompanyFactsRelocatedLocator,
    FinancialFactPayloadV1,
    LegacyFactEvidenceMatchLedger,
    LegacyFactEvidenceMatchRevision,
    MatchOutcome,
    OriginalFactLocator,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0236_fact_observation_match_proofs"
STAMP = datetime(2026, 7, 27, 20, 0, 0)
SHA = hashlib.sha256(b"test").hexdigest()
ACCESSION = "0000000001-26-000001"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "fact-observation-match-proofs.db"
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
    command.stamp(_config(path), "0213_decision_draft_provider_id")
    command.upgrade(_config(path), HEAD)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return path, conn


def _bind(conn: sqlite3.Connection, revision: int) -> str:
    scope_sha = hashlib.sha256(f"scope-{revision}".encode()).hexdigest()
    locator_json = f'{{"accession_number":"{ACCESSION}"}}'
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions "
        "(binding_revision_id, idempotency_key, legacy_document_id, revision, "
        "document_version_id, evidence_node_id, scope_locator_json, "
        "scope_locator_sha256, scope_content_sha256, effective_at, knowledge_at, "
        "recorded_at, supersedes_binding_revision_id) "
        "VALUES (?, ?, 1, ?, 'snapshot-acme', 'node-acme', ?, ?, ?, ?, ?, ?, ?)",
        (
            f"binding-{revision}",
            f"binding-{revision}",
            revision,
            locator_json,
            hashlib.sha256(locator_json.encode()).hexdigest(),
            scope_sha,
            STAMP,
            STAMP,
            STAMP,
            None if revision == 1 else f"binding-{revision - 1}",
        ),
    )
    return scope_sha


def _insert_fact(conn: sqlite3.Connection, *, value: int, locator_index: int) -> int:
    cursor = conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, "
        "unit, source_doc_id, confidence, extracted_by, locator) "
        "VALUES ('ACME', '2026-06-30', 'Q2', 'revenue', ?, 'USD', "
        "'actual', 1, 0.99, 'sec_xbrl', ?)",
        (
            value,
            f'{{"json_path":"facts.us-gaap.Revenues.units.USD[{locator_index}]"}}',
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _seed_subject(
    conn: sqlite3.Connection,
) -> tuple[int, str, str, str]:
    IssuerRegistry(conn).persist(
        IssuerEntity(
            issuer_id="issuer-acme",
            idempotency_key="issuer-acme",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_url, source_quality_tier, "
        "accession_number, filing_date) "
        "VALUES (1, 'ACME', 'sec_xbrl', 'sec_10q', ?, ?, ?, 'ok', 100, ?, "
        "'sec_official', ?, '2026-07-20')",
        (
            f"sec-companyfacts://ACME/{ACCESSION}",
            hashlib.sha256(ACCESSION.encode()).hexdigest(),
            STAMP,
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            ACCESSION,
        ),
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=SHA,
            byte_size=2,
            media_type="application/json",
            storage_uri=f"file:///snapshots/{SHA}.json",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="source-acme",
            idempotency_key="source-acme",
            source_kind="sec_companyfacts",
            source_url=("https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"),
            blob_sha256=SHA,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=SHA,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="snapshot-acme",
            document_key="issuer-acme:sec-companyfacts",
            version_sequence=1,
            observation_id="source-acme",
            blob_sha256=SHA,
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
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="run-acme",
            idempotency_key="run-acme",
            document_version_id="snapshot-acme",
            input_sha256=SHA,
            extractor_name="test",
            extractor_config_sha256=SHA,
            extractor_code_version="test@1",
            output_sha256=SHA,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id="node-acme",
            evidence_key="issuer-acme:companyfacts:accession",
            revision=1,
            extraction_run_id="run-acme",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="section",
            text="CompanyFacts accession scope",
            locator=None,
            recorded_at=STAMP,
        )
    )
    scope_sha = _bind(conn, 1)
    fact_id = _insert_fact(conn, value=100, locator_index=0)
    link = conn.execute(
        "SELECT observation_id FROM fact_observation_revisions "
        "WHERE fact_table = 'financial_facts' AND fact_row_id = ? "
        "AND fact_revision = 1",
        (fact_id,),
    ).fetchone()
    assert link is not None
    conn.commit()
    return fact_id, str(link[0]), "binding-1", scope_sha


def _match(
    fact_id: int,
    binding_id: str,
    binding_revision: int,
    scope_sha: str,
    *,
    match_revision: int = 1,
    match_id: str = "match-one",
    outcome: MatchOutcome = "accepted",
    supersedes: str | None = None,
    value: int = 100,
    locator_index: int = 0,
) -> LegacyFactEvidenceMatchRevision:
    accepted = outcome == "accepted"
    original_locator = OriginalFactLocator(
        root={"json_path": f"facts.us-gaap.Revenues.units.USD[{locator_index}]"}
    )
    relocated_locator = CompanyFactsRelocatedLocator(
        accession_number=ACCESSION,
        namespace="us-gaap",
        concept="Revenues",
        unit="USD",
        entry_index=locator_index,
        json_path=f"facts.us-gaap.Revenues.units.USD[{locator_index}]",
    )
    matched_entry_sha = hashlib.sha256(f"matched-entry-{locator_index}".encode()).hexdigest()
    return LegacyFactEvidenceMatchRevision(
        match_revision_id=match_id,
        idempotency_key=match_id,
        fact_table="financial_facts",
        fact_row_id=fact_id,
        issuer_id="issuer-acme",
        revision=match_revision,
        fact_payload=FinancialFactPayloadV1(
            schema_version="financial_fact_payload.v1",
            fact_table="financial_facts",
            fact_row_id=fact_id,
            ticker="ACME",
            period_end="2026-06-30",
            fiscal_period_type="Q2",
            line_item="revenue",
            value=str(value),
            currency="USD",
            unit="actual",
            source_doc_id=1,
            extracted_by="sec_xbrl",
            locator=original_locator,
        ),
        original_locator=original_locator,
        relocated_locator=relocated_locator if accepted else None,
        legacy_binding_revision_id=binding_id,
        legacy_binding_revision=binding_revision,
        binding_scope_content_sha256=scope_sha,
        evidence_node_id="node-acme",
        matched_entry_sha256=matched_entry_sha if accepted else None,
        candidate_manifest=CompanyFactsCandidateManifestV1(
            schema_version="companyfacts_candidate_manifest.v1",
            candidates=(
                CompanyFactsCandidateV1(
                    entry_sha256=matched_entry_sha,
                    relocated_locator=relocated_locator,
                ),
            ),
        ),
        matched_candidate_count=1 if accepted else 0,
        issuer_check="pass",
        context_check="pass",
        unit_check="pass",
        sign_check="pass",
        fiscal_period_check="pass",
        value_check="pass" if accepted else "fail",
        matcher_name="deterministic-companyfacts-relocator",
        matcher_version="1",
        matcher_config_sha256=SHA,
        outcome=outcome,
        reason_code="exact_companyfacts_entry" if accepted else "no_exact_match",
        reason_details=CanonicalJSONObject(root={}),
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        supersedes_match_revision_id=supersedes,
    )


def _proof(
    fact_id: int,
    observation_id: str,
    match_id: str,
    *,
    proof_id: str = "proof-one",
    idempotency_key: str = "proof-one",
    fact_revision: int = 1,
    effective_at: datetime = STAMP,
    knowledge_at: datetime = STAMP,
    recorded_at: datetime = STAMP,
) -> dict[str, object]:
    return {
        "proof_id": proof_id,
        "idempotency_key": idempotency_key,
        "observation_id": observation_id,
        "match_revision_id": match_id,
        "fact_table": "financial_facts",
        "fact_row_id": fact_id,
        "fact_revision": fact_revision,
        "effective_at": effective_at,
        "knowledge_at": knowledge_at,
        "recorded_at": recorded_at,
    }


def _persist_proof(conn: sqlite3.Connection, proof: dict[str, object]) -> None:
    columns = tuple(proof)
    conn.execute(
        "INSERT INTO fact_observation_match_proofs "
        f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(proof[column] for column in columns),
    )


def test_valid_proof_is_exactly_replayable_and_append_only(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, observation_id, binding_id, scope_sha = _seed_subject(conn)
    LegacyFactEvidenceMatchLedger(conn).persist(_match(fact_id, binding_id, 1, scope_sha))
    proof = _proof(fact_id, observation_id, "match-one")
    try:
        _persist_proof(conn, proof)
        _persist_proof(conn, proof)
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_match_proofs").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT proof_id FROM v_fact_observation_match_proofs_current_valid"
            ).fetchone()[0]
            == "proof-one"
        )

        divergent = dict(proof)
        divergent["recorded_at"] = STAMP + timedelta(seconds=1)
        with pytest.raises(sqlite3.IntegrityError, match="idempotency conflict"):
            _persist_proof(conn, divergent)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE fact_observation_match_proofs "
                "SET recorded_at = ? WHERE proof_id = 'proof-one'",
                (STAMP + timedelta(seconds=1),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM fact_observation_match_proofs WHERE proof_id = 'proof-one'")
    finally:
        conn.close()


def test_stale_binding_invalidates_proof_and_new_match_restores_it(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, observation_id, binding_id, scope_sha = _seed_subject(conn)
    match_ledger = LegacyFactEvidenceMatchLedger(conn)
    match_ledger.persist(_match(fact_id, binding_id, 1, scope_sha))
    _persist_proof(conn, _proof(fact_id, observation_id, "match-one"))
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_fact_observation_match_proofs_current_valid"
            ).fetchone()[0]
            == 1
        )

        scope_two = _bind(conn, 2)
        # A byte-for-byte replay remains a no-op after the historical proof
        # has become stale; only a divergent new proof must revalidate.
        _persist_proof(conn, _proof(fact_id, observation_id, "match-one"))
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_fact_observation_match_proofs_current_valid"
            ).fetchone()[0]
            == 0
        )

        match_ledger.persist(
            _match(
                fact_id,
                "binding-2",
                2,
                scope_two,
                match_revision=2,
                match_id="match-two",
                supersedes="match-one",
            )
        )
        _persist_proof(
            conn,
            _proof(
                fact_id,
                observation_id,
                "match-two",
                proof_id="proof-two",
                idempotency_key="proof-two",
            ),
        )
        valid = conn.execute(
            "SELECT proof_id, match_revision_id FROM v_fact_observation_match_proofs_current_valid"
        ).fetchall()
        assert [tuple(row) for row in valid] == [("proof-two", "match-two")]
    finally:
        conn.close()


def test_unaccepted_match_and_mismatched_observation_are_rejected(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, _, binding_id, scope_sha = _seed_subject(conn)
    match_ledger = LegacyFactEvidenceMatchLedger(conn)
    match_ledger.persist(_match(fact_id, binding_id, 1, scope_sha))
    second_fact_id = _insert_fact(conn, value=101, locator_index=1)
    second_observation = conn.execute(
        "SELECT observation_id FROM fact_observation_revisions "
        "WHERE fact_table = 'financial_facts' AND fact_row_id = ?",
        (second_fact_id,),
    ).fetchone()
    assert second_observation is not None
    match_ledger.persist(
        _match(
            second_fact_id,
            binding_id,
            1,
            scope_sha,
            match_id="match-retryable",
            outcome="retryable",
            value=101,
            locator_index=1,
        )
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="exact fact observation"):
            _persist_proof(
                conn,
                _proof(fact_id, str(second_observation[0]), "match-one"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="accepted match"):
            _persist_proof(
                conn,
                _proof(
                    second_fact_id,
                    str(second_observation[0]),
                    "match-retryable",
                    proof_id="proof-retryable",
                    idempotency_key="proof-retryable",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="fact row"):
            _persist_proof(
                conn,
                _proof(
                    second_fact_id,
                    str(second_observation[0]),
                    "match-one",
                    proof_id="proof-wrong-match",
                    idempotency_key="proof-wrong-match",
                ),
            )
    finally:
        conn.close()


def test_proof_clocks_cannot_precede_sources_or_each_other(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, observation_id, binding_id, scope_sha = _seed_subject(conn)
    LegacyFactEvidenceMatchLedger(conn).persist(_match(fact_id, binding_id, 1, scope_sha))
    earlier = STAMP - timedelta(seconds=1)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="knowledge clock"):
            _persist_proof(
                conn,
                _proof(
                    fact_id,
                    observation_id,
                    "match-one",
                    effective_at=earlier,
                    knowledge_at=earlier,
                    recorded_at=STAMP,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="clocks"):
            _persist_proof(
                conn,
                _proof(
                    fact_id,
                    observation_id,
                    "match-one",
                    proof_id="proof-bad-recorded",
                    idempotency_key="proof-bad-recorded",
                    recorded_at=earlier,
                ),
            )
    finally:
        conn.close()


def test_migration_chain_is_reversible(tmp_path: Path) -> None:
    path, conn = _database(tmp_path)
    try:
        objects = {
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name LIKE '%fact_observation_match_proof%'"
            )
        }
        assert ("table", "fact_observation_match_proofs") in objects
        assert (
            "view",
            "v_fact_observation_match_proofs_current_valid",
        ) in objects
    finally:
        conn.close()

    command.downgrade(_config(path), "0235_legacy_fact_evidence_matches")
    downgraded = sqlite3.connect(path)
    try:
        assert (
            downgraded.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'fact_observation_match_proofs'"
            ).fetchone()
            is None
        )
    finally:
        downgraded.close()
    command.upgrade(_config(path), HEAD)
