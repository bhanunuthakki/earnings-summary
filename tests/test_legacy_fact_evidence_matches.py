"""Append-only legacy fact-to-evidence match schema and typed ledger."""

from __future__ import annotations

import hashlib
import json
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
    KpiFactPayloadV1,
    LegacyFactEvidenceMatchLedger,
    LegacyFactEvidenceMatchRevision,
    OriginalFactLocator,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0235_legacy_fact_evidence_matches"
STAMP = datetime(2026, 7, 27, 20, 0, 0)
SHA = hashlib.sha256(b"test").hexdigest()
ACCESSION = "0000000001-26-000001"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "legacy-fact-evidence-match.db"
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
    return path, conn


def _seed_subject(conn: sqlite3.Connection) -> tuple[int, str, str]:
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
    scope_sha = _bind(conn, revision=1, node_id="node-acme")
    cursor = conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, "
        "unit, source_doc_id, confidence, extracted_by, locator) "
        "VALUES ('ACME', '2026-06-30', 'Q2', 'revenue', 100, 'USD', "
        "'actual', 1, 0.99, 'sec_xbrl', "
        '\'{"json_path":"facts.us-gaap.Revenues.units.USD[0]"}\')'
    )
    assert cursor.lastrowid is not None
    conn.commit()
    return int(cursor.lastrowid), "binding-1", scope_sha


def _bind(
    conn: sqlite3.Connection,
    *,
    revision: int,
    node_id: str,
) -> str:
    scope_sha = hashlib.sha256(f"scope-{revision}".encode()).hexdigest()
    locator_json = f'{{"accession_number":"{ACCESSION}"}}'
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions "
        "(binding_revision_id, idempotency_key, legacy_document_id, revision, "
        "document_version_id, evidence_node_id, scope_locator_json, "
        "scope_locator_sha256, scope_content_sha256, effective_at, knowledge_at, "
        "recorded_at, supersedes_binding_revision_id) "
        "VALUES (?, ?, 1, ?, 'snapshot-acme', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"binding-{revision}",
            f"binding-{revision}",
            revision,
            node_id,
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


def _accepted(
    fact_id: int,
    binding_id: str,
    binding_revision: int,
    scope_sha: str,
    *,
    revision: int = 1,
    suffix: str = "one",
    supersedes: str | None = None,
    original_locator_override: OriginalFactLocator | None = None,
) -> LegacyFactEvidenceMatchRevision:
    original_locator = (
        original_locator_override
        if original_locator_override is not None
        else OriginalFactLocator(
            root={
                "json_path": "facts.us-gaap.Revenues.units.USD[0]",
            }
        )
    )
    relocated_locator = CompanyFactsRelocatedLocator(
        accession_number=ACCESSION,
        namespace="us-gaap",
        concept="Revenues",
        unit="USD",
        entry_index=0,
        json_path="facts.us-gaap.Revenues.units.USD[0]",
    )
    matched_entry_sha = hashlib.sha256(b"matched-entry").hexdigest()
    return LegacyFactEvidenceMatchRevision(
        match_revision_id=f"match-{suffix}",
        idempotency_key=f"match-{suffix}",
        fact_table="financial_facts",
        fact_row_id=fact_id,
        issuer_id="issuer-acme",
        revision=revision,
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
            source_doc_id=1,
            extracted_by="sec_xbrl",
            locator=original_locator,
        ),
        original_locator=original_locator,
        relocated_locator=relocated_locator,
        legacy_binding_revision_id=binding_id,
        legacy_binding_revision=binding_revision,
        binding_scope_content_sha256=scope_sha,
        evidence_node_id="node-acme",
        matched_entry_sha256=matched_entry_sha,
        candidate_manifest=CompanyFactsCandidateManifestV1(
            schema_version="companyfacts_candidate_manifest.v1",
            candidates=(
                CompanyFactsCandidateV1(
                    entry_sha256=matched_entry_sha,
                    relocated_locator=relocated_locator,
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
        matcher_name="deterministic-companyfacts-relocator",
        matcher_version="1",
        matcher_config_sha256=SHA,
        outcome="accepted",
        reason_code="exact_companyfacts_entry",
        reason_details=CanonicalJSONObject(root={}),
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        supersedes_match_revision_id=(
            None if revision == 1 else (supersedes if supersedes is not None else "match-one")
        ),
    )


def test_migration_upgrade_and_downgrade_are_reversible(
    tmp_path: Path,
) -> None:
    path, conn = _database(tmp_path)
    try:
        objects = {
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name LIKE '%legacy_fact_evidence_match%'"
            )
        }
        assert (
            "table",
            "legacy_fact_evidence_match_revisions",
        ) in objects
        assert (
            "view",
            "v_legacy_fact_evidence_matches_current",
        ) in objects
        assert (
            "view",
            "v_legacy_fact_evidence_matches_accepted_current",
        ) in objects
    finally:
        conn.close()

    command.downgrade(_config(path), "0234_image_ocr_governance")
    downgraded = sqlite3.connect(path)
    try:
        assert (
            downgraded.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'legacy_fact_evidence_match_revisions'"
            ).fetchone()
            is None
        )
    finally:
        downgraded.close()
    command.upgrade(_config(path), HEAD)


def test_closed_payload_locator_and_candidate_contracts_reject_empty_or_ambiguous() -> None:
    with pytest.raises(ValueError):
        FinancialFactPayloadV1.model_validate({})
    with pytest.raises(ValueError, match="must not be empty"):
        OriginalFactLocator(root={})
    with pytest.raises(ValueError):
        CompanyFactsRelocatedLocator.model_validate({})

    locator = CompanyFactsRelocatedLocator(
        accession_number=ACCESSION,
        namespace="us-gaap",
        concept="Revenues",
        unit="USD",
        entry_index=0,
        json_path="facts.us-gaap.Revenues.units.USD[0]",
    )
    candidate = CompanyFactsCandidateV1(
        entry_sha256=hashlib.sha256(b"candidate").hexdigest(),
        relocated_locator=locator,
    )
    with pytest.raises(ValueError, match="duplicates"):
        CompanyFactsCandidateManifestV1(
            schema_version="companyfacts_candidate_manifest.v1",
            candidates=(candidate, candidate),
        )
    second_locator = locator.model_copy(
        update={
            "entry_index": 1,
            "json_path": "facts.us-gaap.Revenues.units.USD[1]",
        }
    )
    duplicate_content_at_distinct_location = CompanyFactsCandidateV1(
        entry_sha256=candidate.entry_sha256,
        relocated_locator=second_locator,
    )
    manifest = CompanyFactsCandidateManifestV1(
        schema_version="companyfacts_candidate_manifest.v1",
        candidates=tuple(
            sorted(
                (candidate, duplicate_content_at_distinct_location),
                key=lambda item: (
                    item.entry_sha256,
                    item.relocated_locator.canonical_json,
                ),
            )
        ),
    )
    assert len(manifest.candidates) == 2


def test_terminal_ambiguity_preserves_two_exact_candidates(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, binding_id, scope_sha = _seed_subject(conn)
    accepted = _accepted(fact_id, binding_id, 1, scope_sha)
    first = accepted.candidate_manifest.candidates[0]
    second_locator = first.relocated_locator.model_copy(
        update={
            "entry_index": 1,
            "json_path": "facts.us-gaap.Revenues.units.USD[1]",
        }
    )
    second = CompanyFactsCandidateV1(
        entry_sha256=first.entry_sha256,
        relocated_locator=second_locator,
    )
    manifest = CompanyFactsCandidateManifestV1(
        schema_version="companyfacts_candidate_manifest.v1",
        candidates=tuple(
            sorted(
                (first, second),
                key=lambda item: (
                    item.entry_sha256,
                    item.relocated_locator.canonical_json,
                ),
            )
        ),
    )
    ambiguous = accepted.model_copy(
        update={
            "outcome": "terminal",
            "reason_code": "ambiguous_matching_entries",
            "relocated_locator": None,
            "relocated_locator_sha256": None,
            "matched_entry_sha256": None,
            "candidate_manifest": manifest,
            "candidate_manifest_sha256": None,
            "candidate_count": 2,
            "matched_candidate_count": 2,
        }
    )
    try:
        result = LegacyFactEvidenceMatchLedger(conn).persist(ambiguous)
        conn.commit()
        assert result.created is True
        stored = conn.execute(
            "SELECT outcome, candidate_count, matched_candidate_count "
            "FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert stored is not None
        assert tuple(stored) == ("terminal", 2, 2)
    finally:
        conn.close()


def test_backdated_match_clock_and_payload_drift_fail_before_acceptance(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, binding_id, scope_sha = _seed_subject(conn)
    record = _accepted(fact_id, binding_id, 1, scope_sha)
    ledger = LegacyFactEvidenceMatchLedger(conn)
    try:
        second_locator = CompanyFactsRelocatedLocator(
            accession_number=ACCESSION,
            namespace="us-gaap",
            concept="SalesRevenueNet",
            unit="USD",
            entry_index=0,
            json_path="facts.us-gaap.SalesRevenueNet.units.USD[0]",
        )
        candidates = (
            *record.candidate_manifest.candidates,
            CompanyFactsCandidateV1(
                entry_sha256=hashlib.sha256(b"second-candidate").hexdigest(),
                relocated_locator=second_locator,
            ),
        )
        ambiguous_manifest = CompanyFactsCandidateManifestV1(
            schema_version="companyfacts_candidate_manifest.v1",
            candidates=tuple(
                sorted(
                    candidates,
                    key=lambda item: (
                        item.entry_sha256,
                        item.relocated_locator.canonical_json,
                    ),
                )
            ),
        )
        with pytest.raises(ValueError, match="exactly one"):
            LegacyFactEvidenceMatchRevision.model_validate(
                record.model_copy(
                    update={
                        "candidate_manifest": ambiguous_manifest,
                        "candidate_manifest_sha256": None,
                        "candidate_count": 2,
                        "matched_candidate_count": 2,
                    }
                ).model_dump()
            )

        drifted_payload = record.fact_payload.model_copy(update={"value": "101"})
        with pytest.raises(ValueError, match="exactly match"):
            ledger.persist(
                record.model_copy(
                    update={
                        "fact_payload": drifted_payload,
                        "fact_payload_fingerprint_sha256": None,
                    }
                )
            )

        before_binding = STAMP - timedelta(seconds=1)
        backdated = LegacyFactEvidenceMatchRevision.model_validate(
            record.model_copy(
                update={
                    "effective_at": before_binding,
                    "knowledge_at": before_binding,
                    "recorded_at": before_binding,
                }
            ).model_dump()
        )
        with pytest.raises(sqlite3.IntegrityError, match="predates binding"):
            ledger.persist(backdated)
    finally:
        conn.close()


def test_nested_original_locator_is_preserved_while_relocated_locator_is_strict(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, binding_id, scope_sha = _seed_subject(conn)
    nested = OriginalFactLocator(
        root={
            "table_cell": {
                "json_path": "facts.us-gaap.Revenues.units.USD[0]",
                "row": 1,
            }
        }
    )
    conn.execute(
        "UPDATE financial_facts SET locator = ? WHERE id = ?",
        (nested.canonical_json, fact_id),
    )
    record = _accepted(
        fact_id,
        binding_id,
        1,
        scope_sha,
        original_locator_override=nested,
    )
    try:
        LegacyFactEvidenceMatchLedger(conn).persist(record)
        stored = conn.execute(
            "SELECT original_locator_json, relocated_locator_json "
            "FROM v_legacy_fact_evidence_matches_accepted_current"
        ).fetchone()
        assert stored is not None
        assert str(stored[0]) == nested.canonical_json
        assert json.loads(str(stored[1]))["json_path"] == "facts.us-gaap.Revenues.units.USD[0]"
    finally:
        conn.close()


def test_kpi_null_original_locator_can_accept_strict_companyfacts_relocation(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    _, binding_id, scope_sha = _seed_subject(conn)
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) "
        "VALUES (1, 'ACME', 'Customers', 'count')"
    )
    cursor = conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, "
        "unit, source_doc_id, confidence, extracted_by, locator, "
        "source_excerpt, computed_from, formula_id, formula_version) "
        "VALUES ('ACME', '2026-06-30', 'Q2', 1, 50, 'count', 1, 0.9, "
        "'sec_xbrl', NULL, NULL, NULL, NULL, NULL)"
    )
    assert cursor.lastrowid is not None
    fact_id = int(cursor.lastrowid)
    relocated = CompanyFactsRelocatedLocator(
        accession_number=ACCESSION,
        namespace="dei",
        concept="EntityPublicFloat",
        unit="shares",
        entry_index=0,
        json_path="facts.dei.EntityPublicFloat.units.shares[0]",
    )
    entry_sha = hashlib.sha256(b"kpi-entry").hexdigest()
    record = LegacyFactEvidenceMatchRevision(
        match_revision_id="match-kpi",
        idempotency_key="match-kpi",
        fact_table="kpi_facts",
        fact_row_id=fact_id,
        issuer_id="issuer-acme",
        revision=1,
        fact_payload=KpiFactPayloadV1(
            schema_version="kpi_fact_payload.v1",
            fact_table="kpi_facts",
            fact_row_id=fact_id,
            ticker="ACME",
            period_end="2026-06-30",
            fiscal_period_type="Q2",
            kpi_definition_id=1,
            value="50",
            unit="count",
            source_doc_id=1,
            extracted_by="sec_xbrl",
            locator=None,
            source_excerpt=None,
            computed_from=None,
            formula_id=None,
            formula_version=None,
        ),
        original_locator=None,
        relocated_locator=relocated,
        legacy_binding_revision_id=binding_id,
        legacy_binding_revision=1,
        binding_scope_content_sha256=scope_sha,
        evidence_node_id="node-acme",
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
        matcher_name="test",
        matcher_version="1",
        matcher_config_sha256=SHA,
        outcome="accepted",
        reason_code="exact_companyfacts_entry",
        reason_details=CanonicalJSONObject(root={}),
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    try:
        ledger = LegacyFactEvidenceMatchLedger(conn)
        ledger.persist(record)
        assert (
            conn.execute(
                "SELECT original_locator_json FROM "
                "v_legacy_fact_evidence_matches_accepted_current "
                "WHERE fact_table = 'kpi_facts'"
            ).fetchone()[0]
            is None
        )

        derived_cursor = conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, "
            "unit, source_doc_id, confidence, extracted_by, locator, "
            "source_excerpt, computed_from, formula_id, formula_version) "
            "VALUES ('ACME', '2026-09-30', 'Q3', 1, 55, 'count', 1, 0.9, "
            "'metrics_engine_derived', NULL, NULL, 'input-observation', 1, 1)"
        )
        assert derived_cursor.lastrowid is not None
        derived_id = int(derived_cursor.lastrowid)
        derived_payload = KpiFactPayloadV1(
            schema_version="kpi_fact_payload.v1",
            fact_table="kpi_facts",
            fact_row_id=derived_id,
            ticker="ACME",
            period_end="2026-09-30",
            fiscal_period_type="Q3",
            kpi_definition_id=1,
            value="55",
            unit="count",
            source_doc_id=1,
            extracted_by="metrics_engine_derived",
            locator=None,
            source_excerpt=None,
            computed_from="input-observation",
            formula_id=1,
            formula_version=1,
        )
        derived_accepted = record.model_copy(
            update={
                "match_revision_id": "match-kpi-derived",
                "idempotency_key": "match-kpi-derived",
                "fact_row_id": derived_id,
                "fact_payload": derived_payload,
                "fact_payload_fingerprint_sha256": None,
            }
        )
        with pytest.raises(ValueError, match="non-derived"):
            ledger.persist(derived_accepted)

        retryable = LegacyFactEvidenceMatchRevision.model_validate(
            derived_accepted.model_copy(
                update={
                    "match_revision_id": "match-kpi-derived-retry",
                    "idempotency_key": "match-kpi-derived-retry",
                    "context_check": "fail",
                    "outcome": "retryable",
                    "reason_code": "derived_kpi_not_document_matchable",
                }
            ).model_dump()
        )
        ledger.persist(retryable)
        columns = tuple(
            str(row[1])
            for row in conn.execute("PRAGMA table_info(legacy_fact_evidence_match_revisions)")
        )
        stored_retry = conn.execute(
            "SELECT * FROM legacy_fact_evidence_match_revisions "
            "WHERE match_revision_id = 'match-kpi-derived-retry'"
        ).fetchone()
        assert stored_retry is not None
        direct_accepted = dict(zip(columns, tuple(stored_retry), strict=True))
        direct_accepted.update(
            {
                "match_revision_id": "match-kpi-derived-direct",
                "idempotency_key": "match-kpi-derived-direct",
                "revision": 2,
                "supersedes_match_revision_id": "match-kpi-derived-retry",
                "context_check": "pass",
                "outcome": "accepted",
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="reported_kpi"):
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions "
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(direct_accepted[column] for column in columns),
            )
    finally:
        conn.close()


def test_kpi_payload_currency_changes_canonical_fingerprint() -> None:
    payload = KpiFactPayloadV1(
        schema_version="kpi_fact_payload.v1",
        fact_table="kpi_facts",
        fact_row_id=1,
        ticker="ACME",
        period_end="2026-06-30",
        fiscal_period_type="Q2",
        kpi_definition_id=1,
        value="100",
        currency="USD",
        unit="actual",
        source_doc_id=1,
    )
    assert (
        payload.canonical_sha256 != payload.model_copy(update={"currency": "BRL"}).canonical_sha256
    )


def test_accepted_match_requires_all_six_checks_and_is_exactly_replayable(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, binding_id, scope_sha = _seed_subject(conn)
    record = _accepted(fact_id, binding_id, 1, scope_sha)
    ledger = LegacyFactEvidenceMatchLedger(conn)
    try:
        created = ledger.persist(record)
        replayed = ledger.persist(record)
        conn.commit()

        assert created.created is True
        assert replayed.created is False
        accepted = conn.execute(
            "SELECT revision, outcome, issuer_check, context_check, unit_check, "
            "sign_check, fiscal_period_check, value_check "
            "FROM v_legacy_fact_evidence_matches_accepted_current"
        ).fetchone()
        assert accepted is not None
        assert tuple(accepted) == (
            1,
            "accepted",
            "pass",
            "pass",
            "pass",
            "pass",
            "pass",
            "pass",
        )
        with pytest.raises(ValueError, match="six passing checks"):
            ledger.persist(record.model_copy(update={"value_check": "fail"}))

        with pytest.raises(sqlite3.IntegrityError, match="freezes semantic"):
            conn.execute(
                "UPDATE financial_facts SET value = 101 WHERE id = ?",
                (fact_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="freezes semantic"):
            conn.execute(
                "DELETE FROM financial_facts WHERE id = ?",
                (fact_id,),
            )
        columns = tuple(
            str(row[1])
            for row in conn.execute("PRAGMA table_info(legacy_fact_evidence_match_revisions)")
        )
        stored = conn.execute(
            "SELECT * FROM legacy_fact_evidence_match_revisions "
            "WHERE match_revision_id = 'match-one'"
        ).fetchone()
        assert stored is not None
        invalid = dict(zip(columns, tuple(stored), strict=True))
        invalid.update(
            {
                "match_revision_id": "match-database-invalid",
                "idempotency_key": "match-database-invalid",
                "revision": 2,
                "supersedes_match_revision_id": "match-one",
                "value_check": "fail",
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="acceptance"):
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions "
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(invalid[column] for column in columns),
            )

        mismatched_manifest = dict(invalid)
        mismatched_manifest.update(
            {
                "match_revision_id": "match-manifest-invalid",
                "idempotency_key": "match-manifest-invalid",
                "value_check": "pass",
            }
        )
        manifest = json.loads(str(mismatched_manifest["candidate_manifest_json"]))
        manifest["candidates"][0]["entry_sha256"] = hashlib.sha256(b"different-entry").hexdigest()
        mismatched_manifest["candidate_manifest_json"] = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="manifest candidate"):
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions "
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(mismatched_manifest[column] for column in columns),
            )

        nonhex = dict(invalid)
        nonhex.update(
            {
                "match_revision_id": "match-nonhex",
                "idempotency_key": "match-nonhex",
                "value_check": "pass",
                "matcher_config_sha256": "G" * 64,
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="hashes"):
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions "
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(nonhex[column] for column in columns),
            )

        mismatched_payload = dict(invalid)
        mismatched_payload.update(
            {
                "match_revision_id": "match-payload-invalid",
                "idempotency_key": "match-payload-invalid",
                "value_check": "pass",
            }
        )
        payload = json.loads(str(mismatched_payload["fact_payload_json"]))
        payload["value"] = "101"
        mismatched_payload["fact_payload_json"] = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="exact fact payload"):
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions "
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(mismatched_payload[column] for column in columns),
            )
    finally:
        conn.close()


def test_match_revisions_are_append_only_and_require_exact_parent(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, binding_id, scope_sha = _seed_subject(conn)
    record = _accepted(fact_id, binding_id, 1, scope_sha)
    ledger = LegacyFactEvidenceMatchLedger(conn)
    try:
        ledger.persist(record)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE legacy_fact_evidence_match_revisions "
                "SET reason_code = 'changed' WHERE match_revision_id = 'match-one'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM legacy_fact_evidence_match_revisions "
                "WHERE match_revision_id = 'match-one'"
            )
        wrong_parent = _accepted(
            fact_id,
            binding_id,
            1,
            scope_sha,
            revision=2,
            suffix="two",
        ).model_copy(update={"supersedes_match_revision_id": "wrong-parent"})
        with pytest.raises(sqlite3.IntegrityError, match="supersede prior"):
            ledger.persist(wrong_parent)
    finally:
        conn.close()


def test_stale_binding_invalidates_acceptance_and_exact_scope_triggers_fail_closed(
    tmp_path: Path,
) -> None:
    _, conn = _database(tmp_path)
    fact_id, binding_id, scope_sha = _seed_subject(conn)
    ledger = LegacyFactEvidenceMatchLedger(conn)
    first = _accepted(fact_id, binding_id, 1, scope_sha)
    try:
        ledger.persist(first)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_legacy_fact_evidence_matches_accepted_current"
            ).fetchone()[0]
            == 1
        )

        IssuerRegistry(conn).persist(
            IssuerEntity(
                issuer_id="issuer-other",
                idempotency_key="issuer-other",
                entity_kind="operating_company",
                created_at=STAMP,
            )
        )
        wrong_issuer = first.model_copy(
            update={
                "match_revision_id": "match-wrong-issuer",
                "idempotency_key": "match-wrong-issuer",
                "issuer_id": "issuer-other",
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="issuer must agree"):
            ledger.persist(wrong_issuer)

        missing_fact = first.model_copy(
            update={
                "match_revision_id": "match-missing-fact",
                "idempotency_key": "match-missing-fact",
                "fact_row_id": 999,
            }
        )
        with pytest.raises(ValueError, match="does not exist"):
            ledger.persist(missing_fact)

        wrong_scope = first.model_copy(
            update={
                "match_revision_id": "match-wrong-scope",
                "idempotency_key": "match-wrong-scope",
                "binding_scope_content_sha256": hashlib.sha256(b"wrong-scope").hexdigest(),
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="payload and source binding"):
            ledger.persist(wrong_scope)

        second_scope_sha = _bind(conn, revision=2, node_id="node-acme")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_legacy_fact_evidence_matches_accepted_current"
            ).fetchone()[0]
            == 0
        )

        stale = _accepted(
            fact_id,
            binding_id,
            1,
            scope_sha,
            revision=2,
            suffix="stale",
        )
        with pytest.raises(sqlite3.IntegrityError, match="current exact"):
            ledger.persist(stale)

        retryable = LegacyFactEvidenceMatchRevision.model_validate(
            stale.model_copy(
                update={
                    "match_revision_id": "match-retry",
                    "idempotency_key": "match-retry",
                    "outcome": "retryable",
                    "context_check": "fail",
                    "relocated_locator": None,
                    "relocated_locator_sha256": None,
                    "matched_entry_sha256": None,
                    "matched_candidate_count": 0,
                    "reason_code": "historical_binding_requires_relocation",
                }
            ).model_dump()
        )
        ledger.persist(retryable)
        retry_row = conn.execute(
            "SELECT outcome, legacy_binding_revision FROM v_legacy_fact_evidence_matches_current"
        ).fetchone()
        assert retry_row is not None
        assert tuple(retry_row) == ("retryable", 1)

        current = _accepted(
            fact_id,
            "binding-2",
            2,
            second_scope_sha,
            revision=3,
            suffix="three",
            supersedes="match-retry",
        )
        ledger.persist(current)
        accepted = conn.execute(
            "SELECT revision, legacy_binding_revision "
            "FROM v_legacy_fact_evidence_matches_accepted_current"
        ).fetchone()
        assert accepted is not None
        assert tuple(accepted) == (3, 2)
    finally:
        conn.close()
