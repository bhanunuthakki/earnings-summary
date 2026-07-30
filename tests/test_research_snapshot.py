# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceIntegrityError,
    DocumentProcessingEvidenceMissingError,
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from provenance.evidence_ledger import EvidenceLocator
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionLedger,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    DocumentProcessingDisposition,
    DocumentProcessingObligation,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ProcessingEvidenceReference,
    ResearchSnapshotRequest,
    ResearchUniverse,
    VerifiedResearchReference,
    _build_research_snapshot_with_verifier,
    _DefaultResearchReferenceVerifier,
    _document_family,
    _validate_document_obligation_subject_pairs,
    _verify_research_snapshot_with_verifier,
    admit,
    derive_obligations,
    record_disposition,
    seal_disposition,
    seal_processing_snapshot,
    verify_processing_snapshot,
)
from provenance.source_coverage import (
    ExpectedDocument as CoverageExpectedDocument,
)
from provenance.source_coverage import (
    SourceCoverageLedger,
    SourceInventorySnapshot,
    _expected_document_family,
)
from provenance.source_fact_publication import PublicationVerificationError
from tests.test_document_processing_evidence import (
    _pdf_table_fixture,
    _seed_exact_pptx_run,
    _seed_pdf_table_artifact_for_existing_document,
)
from tests.test_document_processing_evidence import (
    _seed_run as _seed_native_processing_run,
)
from tests.test_filing_xbrl_extraction_ledger import (
    STAMP,
)
from tests.test_filing_xbrl_extraction_ledger import (
    _database as _filing_database,
)
from tests.test_filing_xbrl_extraction_ledger import (
    _entry as _filing_entry,
)
from tests.test_filing_xbrl_extraction_ledger import (
    _output as _filing_output,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
PROCESSING_EVIDENCE_REVISION = "0252_research_universe_closure"
T1 = datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
T2 = T1 + timedelta(days=1)
POLICY = DocumentProcessingPolicy(policy_name="test", policy_version="v1")
SCOPE = DocumentProcessingScope(issuer_ids=("issuer-1",))


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _sql_sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


@pytest.fixture(scope="module")
def database_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("research-snapshot") / "template.db"
    config = _config(path)
    seed = sqlite3.connect(path)
    seed.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        """
    )
    seed.close()
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, PROCESSING_EVIDENCE_REVISION)
    template = sqlite3.connect(path)
    template.row_factory = sqlite3.Row
    template.execute("PRAGMA foreign_keys=ON")
    template.create_function("fact_sha256", 1, _sql_sha)
    _insert_foundation(template)
    _insert_resolution_publication_mapping(template)
    template.commit()
    template.close()
    return path


@pytest.fixture
def conn(tmp_path: Path, database_template: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "research-snapshot.db"
    shutil.copyfile(database_template, path)
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    database.create_function("fact_sha256", 1, _sql_sha)
    yield database
    database.close()


def _insert_foundation(conn: sqlite3.Connection) -> None:
    at = T1 - timedelta(hours=1)
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-1", "issuer-1", "operating_company", at),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-1",
            "reporting-1",
            "issuer-1",
            "legal_registrant",
            "Test Reporting Entity",
            at,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions ("
        "binding_revision_id,idempotency_key,recorded_issuer_id,revision,"
        "issuer_id,reporting_entity_id,security_id,outcome,decision_kind,"
        "reason_code,reason_details_json,material_dissent,effective_at,"
        "knowledge_at,recorded_at,supersedes_binding_revision_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "subject-binding-1",
            "subject-binding-1",
            "issuer-1",
            1,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "test",
            "{}",
            False,
            at,
            at,
            at,
            None,
        ),
    )
    _insert_document(conn, version=1, at=at)
    _insert_source_obligation(conn, key="obligation-a", at=at)
    _insert_research_corpus_foundation(conn, at=at)


def _insert_research_corpus_foundation(
    conn: sqlite3.Connection,
    *,
    at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "inventory-research",
            "inventory-research",
            "inventory-research",
            1,
            "issuer-1",
            "TEST",
            "ir_crawl",
            "https://example.test/inventory",
            "observation-1",
            "succeeded",
            True,
            "a" * 64,
            "test",
            at,
            at,
            at,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "expected-document-1",
            "expected-document-1",
            "inventory-research",
            "TEST:document-1",
            "issuer-1",
            "TEST",
            "ir_document",
            "web_page",
            "other",
            None,
            "https://example.test/1",
            None,
            None,
            None,
            None,
            at,
            "authoritative",
            at,
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_components VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "inventory-component-research",
            "inventory-component-research",
            "inventory-research",
            "primary",
            "primary",
            "https://example.test/inventory",
            "observation-1",
            "succeeded",
            True,
            None,
            0,
            at,
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?,?,?,?)",
        ("inventory-research", 1, "c" * 64, "complete", at),
    )
    binding_payload = (
        '{"document_family":"continuous_disclosure",'
        '"expected_document_id":"expected-document-1",'
        '"issuer_id":"issuer-1","reporting_entity_id":"reporting-1",'
        '"source_obligation_revision_id":"obligation-a:v1"}'
    )
    conn.execute(
        "INSERT INTO expected_document_obligation_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "expected-binding-1",
            "expected-binding-1",
            "expected-document-1",
            "obligation-a:v1",
            "issuer-1",
            "reporting-1",
            "continuous_disclosure",
            binding_payload,
            hashlib.sha256(binding_payload.encode()).hexdigest(),
            at,
            at,
            at,
        ),
    )
    for ordinal, manifest_id in enumerate(("manifest-a", "manifest-b"), start=1):
        conn.execute(
            "INSERT INTO search_corpus_manifests VALUES (?,?,?,?,?,?,?,?,?)",
            (
                manifest_id,
                manifest_id,
                manifest_id,
                1,
                "b" * 64,
                "test",
                at,
                None,
                at,
            ),
        )
        if ordinal == 1:
            conn.execute(
                "INSERT INTO search_manifest_source_inventories VALUES (?,?,?)",
                (manifest_id, "inventory-research", at),
            )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?,?,?)",
        (
            "membership-1",
            "manifest-a",
            "TEST:document-1",
            "document-1",
            "included",
            "test",
            at,
        ),
    )


def _insert_document(
    conn: sqlite3.Connection,
    *,
    version: int,
    at: datetime,
    media_type: str = "text/plain",
    document_type: str = "web_page",
    form_type: str = "other",
    raw_bytes: bytes | None = None,
) -> None:
    raw = f"blob-{version}".encode() if raw_bytes is None else raw_bytes
    blob = hashlib.sha256(raw).hexdigest()
    observation = f"observation-{version}"
    document = f"document-{version}"
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob, 10 if raw_bytes is None else len(raw), media_type, f"memory://{blob}", at),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            observation,
            observation,
            "issuer_web",
            f"https://example.test/{version}",
            blob,
            at,
            None,
            None,
            at,
            at,
            "a" * 64,
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            document,
            "document-key",
            version,
            observation,
            blob,
            "issuer-1",
            "TEST",
            document_type,
            form_type,
            None,
            None,
            None,
            None,
            at,
            "en",
            None if version == 1 else f"document-{version - 1}",
            None,
            at,
        ),
    )


def _insert_source_obligation(
    conn: sqlite3.Connection,
    *,
    key: str,
    at: datetime,
    document_family: str = "continuous_disclosure",
) -> None:
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{key}:v1",
            f"{key}:v1",
            key,
            1,
            "issuer-1",
            "reporting-1",
            "issuer_publisher",
            document_family,
            "required",
            "publisher_surface_exhaustion",
            at - timedelta(days=1),
            None,
            "deterministic",
            "test",
            "{}",
            at,
            at,
            at,
            None,
        ),
    )


def _insert_resolution_publication_mapping(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    for ordinal, publication_id in enumerate(("publication-a", "publication-b")):
        universe_id = f"universe-{ordinal}"
        conn.execute(
            "INSERT INTO canonical_fact_candidate_dispositions ("
            "candidate_disposition_id,idempotency_key,candidate_universe_id,"
            "candidate_ordinal,observation_id,source_fact_cell_id,"
            "binding_revision_id,binding_commitment_sha256,"
            "mapping_commitment_sha256,observation_payload_sha256,"
            "source_publication_id,source_publication_seal_id,"
            "source_publication_member_id,source_publication_member_sha256,"
            "source_record_commitment_sha256,filing_disposition_id,"
            "source_lane,eligibility,reason_code,reason_details_json,"
            "effective_at,knowledge_at,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"candidate-{ordinal}",
                f"candidate-{ordinal}",
                universe_id,
                0,
                f"observation-{ordinal}",
                f"source-cell-{ordinal}",
                f"binding-{ordinal}",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                publication_id,
                f"publication-seal-{ordinal}",
                f"publication-member-{ordinal}",
                "d" * 64,
                "e" * 64,
                None,
                "reported_source_publication",
                "eligible",
                "test",
                "{}",
                T1,
                T1,
                T1,
            ),
        )
        conn.execute(
            "INSERT INTO canonical_fact_resolution_snapshot_members VALUES (?,?,?,?,?,?,?)",
            (
                "resolution",
                ordinal,
                f"canonical-cell-{ordinal}",
                universe_id,
                f"relation-{ordinal}",
                f"resolution-revision-{ordinal}",
                "f" * 64,
            ),
        )
    conn.commit()


def _seal_all_dispositions(
    conn: sqlite3.Connection,
    *,
    cutoff: datetime,
    prefix: str,
) -> None:
    obligations = derive_obligations(conn, SCOPE, cutoff, POLICY)
    disposition_clock = cutoff - timedelta(minutes=30)
    seal_clock = cutoff - timedelta(minutes=15)
    for ordinal, obligation in enumerate(obligations):
        references: tuple[ProcessingEvidenceReference, ...] = ()
        status = "not_applicable"
        if obligation.applicability == "applicable":
            raise AssertionError("narrative-only fixture unexpectedly has a native lane")
        disposition_id = f"{prefix}:disposition:{ordinal}"
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=disposition_id,
                idempotency_key=disposition_id,
                processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
                terminal_status=status,
                reason_code="test",
                reason_details={"ordinal": ordinal},
                evidence=references,
                knowledge_at=disposition_clock,
                recorded_at=disposition_clock,
            ),
        )
        seal_disposition(conn, disposition_id, sealed_at=seal_clock)


def test_obligation_population_uses_k_for_knowledge_and_w_for_recording(
    conn: sqlite3.Connection,
) -> None:
    recorded_at = T1 + timedelta(hours=2)

    obligations = derive_obligations(
        conn,
        SCOPE,
        T1,
        POLICY,
        observed_through=recorded_at,
        recorded_at=recorded_at,
    )

    assert obligations
    assert all(item.knowledge_at <= T1 for item in obligations)
    assert {item.recorded_at for item in obligations} == {recorded_at}


def _processing_snapshot(conn: sqlite3.Connection) -> str:
    _seal_all_dispositions(conn, cutoff=T1, prefix="t1")
    snapshot_id = "processing:t1"
    receipt = seal_processing_snapshot(
        conn,
        processing_snapshot_id=snapshot_id,
        idempotency_key=snapshot_id,
        scope=SCOPE,
        cutoff_at=T1,
        policy=POLICY,
        recorded_at=T1,
    )
    assert receipt.member_count == 14
    return snapshot_id


def _sealed_filing_processing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[sqlite3.Connection, str, str]:
    real_upgrade = command.upgrade

    def _bounded_fixture_upgrade(config: Config, revision: str) -> None:
        real_upgrade(
            config,
            "0246_source_fact_publication_stream" if revision == "head" else revision,
        )

    monkeypatch.setattr(command, "upgrade", _bounded_fixture_upgrade)
    output = _filing_output((_filing_entry(0),))
    conn = _filing_database(tmp_path, output)
    conn.execute("DROP TRIGGER trg_evidence_content_blobs_append_only")
    conn.execute("UPDATE evidence_content_blobs SET media_type='application/xbrl+xml'")
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.close()
    command.upgrade(_config(path), "0246_source_fact_publication_stream")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.create_function("fact_sha256", 1, _sql_sha)
    receipt = FilingXbrlExtractionLedger(conn).publish(output)
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "filing-obligation:v1",
            "filing-obligation:v1",
            "filing-obligation",
            1,
            "issuer-1",
            "reporting-1",
            "sec_edgar",
            "operating_company_periodic",
            "required",
            "regulator_inventory",
            STAMP - timedelta(days=1),
            None,
            "deterministic",
            "test",
            "{}",
            STAMP,
            STAMP,
            STAMP,
            None,
        ),
    )
    conn.commit()
    scope = DocumentProcessingScope(issuer_ids=("issuer-1",))
    obligations = derive_obligations(conn, scope, STAMP, POLICY)
    seal = conn.execute(
        "SELECT disposition_seal_id,disposition_set_sha256,"
        "knowledge_at,recorded_at "
        "FROM filing_xbrl_extraction_disposition_seals"
    ).fetchone()
    assert seal is not None
    for ordinal, obligation in enumerate(obligations):
        evidence: tuple[ProcessingEvidenceReference, ...] = ()
        status = "not_applicable"
        if obligation.processing_lane == "filing_xbrl":
            status = "succeeded"
            evidence = (
                ProcessingEvidenceReference(
                    evidence_table=("filing_xbrl_extraction_disposition_seals"),
                    evidence_id=str(seal[0]),
                    evidence_commitment_sha256=str(seal[1]),
                    knowledge_at=datetime.fromisoformat(str(seal[2])),
                    recorded_at=datetime.fromisoformat(str(seal[3])),
                ),
            )
        disposition_id = f"filing-disposition:{ordinal}"
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=disposition_id,
                idempotency_key=disposition_id,
                processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
                terminal_status=status,
                reason_code="test",
                reason_details={},
                evidence=evidence,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            ),
        )
        seal_disposition(conn, disposition_id, sealed_at=STAMP)
    snapshot_id = "processing:filing"
    seal_processing_snapshot(
        conn,
        processing_snapshot_id=snapshot_id,
        idempotency_key=snapshot_id,
        scope=scope,
        cutoff_at=STAMP,
        policy=POLICY,
        recorded_at=STAMP,
    )
    return conn, snapshot_id, receipt.publication_receipt.publication_id


def _seed_transcript_processing_evidence(
    conn: sqlite3.Connection,
) -> tuple[
    DocumentProcessingScope,
    tuple[DocumentProcessingObligation, ...],
    dict[str, ProcessingEvidenceReference],
]:
    at = T1 - timedelta(hours=1)
    _insert_document(
        conn,
        version=2,
        at=at,
        document_type="earnings_transcript",
        form_type="transcript",
    )
    _insert_source_obligation(
        conn,
        key="transcript-obligation",
        at=at,
        document_family="issuer_earnings_materials",
    )
    document = conn.execute(
        "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='document-2'"
    ).fetchone()
    assert document is not None
    _seed_native_processing_run(
        conn,
        document_version_id="document-2",
        blob_sha=str(document[0]),
        run_id="transcript-run",
        extractor_name="legacy-evidence-backfill",
        extractor_code_version="evidence-backfill@1",
        extractor_config_sha256="b" * 64,
        children=(
            (
                "transcript_turn",
                "Prepared remarks.",
                EvidenceLocator(
                    transcript_turn_sequence=0,
                    transcript_speaker="CEO",
                    transcript_time_code_start="00:00:01",
                    transcript_time_code_end="00:00:10",
                    legacy_table="transcript_segments",
                    legacy_row_id=1,
                ),
            ),
            (
                "transcript_turn",
                "Question.",
                EvidenceLocator(
                    transcript_turn_sequence=1,
                    transcript_speaker="Analyst",
                    transcript_time_code_start="00:00:11",
                    transcript_time_code_end="00:00:20",
                    legacy_table="transcript_segments",
                    legacy_row_id=2,
                ),
            ),
        ),
    )
    references: dict[str, ProcessingEvidenceReference] = {}
    for lane in ("transcript_turns", "transcript_speakers"):
        receipt = publish_document_processing_evidence(
            conn,
            document_version_id="document-2",
            processing_lane=lane,
            cutoff_at=T1,
            recorded_at=T1,
        )
        verified = verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="document-2",
            processing_lane=lane,
            cutoff_at=T1,
        )
        references[lane] = ProcessingEvidenceReference(
            evidence_table="document_processing_evidence_seals",
            evidence_id=verified.evidence_seal_id,
            evidence_commitment_sha256=verified.member_set_sha256,
            knowledge_at=verified.knowledge_at,
            recorded_at=verified.recorded_at,
        )
    scope = DocumentProcessingScope(document_version_ids=("document-2",))
    obligations = derive_obligations(conn, scope, T1, POLICY)
    return scope, obligations, references


class _SealedDoubleVerifier:
    def verify(
        self,
        conn: sqlite3.Connection,
        *,
        requested_lane: str,
        reference_id: str,
        cutoff_at: datetime,
        request: ResearchSnapshotRequest,
    ) -> VerifiedResearchReference:
        del conn
        attributes: dict[str, object] = {}
        if ":" in requested_lane:
            kind, coordinate = requested_lane.split(":", 1)
            if kind in {"corpus", "lexical_projection", "vector_projection"}:
                attributes["manifest_id"] = coordinate
            if kind in {"vector_projection", "embedding_promotion"}:
                attributes.update({"provider": "test", "model": "embed-v1", "dimensions": 8})
            if kind == "canonical_fact_projection":
                attributes.update(
                    {
                        "ontology_snapshot_id": request.ontology_snapshot_id,
                        "resolution_snapshot_id": coordinate,
                    }
                )
        return VerifiedResearchReference(
            requested_lane=requested_lane,
            reference_table="sealed_test_doubles",
            reference_id=reference_id,
            commitment_sha256=hashlib.sha256(
                f"{requested_lane}:{reference_id}".encode()
            ).hexdigest(),
            knowledge_at=cutoff_at,
            recorded_at=cutoff_at,
            attributes=attributes,
        )


def _research_request(processing_snapshot_id: str) -> ResearchSnapshotRequest:
    return ResearchSnapshotRequest(
        research_snapshot_id="research:t1",
        idempotency_key="research:t1",
        research_universe=ResearchUniverse(
            issuer_id="issuer-1",
            reporting_entity_ids=("reporting-1",),
            document_version_ids=("document-1",),
            source_obligation_revision_ids=("obligation-a:v1",),
        ),
        processing_snapshot_ids=(processing_snapshot_id,),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id="manifest-a",
                lexical_index_run_id="lexical-a",
                vector_index_run_id="vector-a",
                embedding_promotion_id="promotion-a",
            ),
            CorpusProjectionBundle(
                corpus_manifest_id="manifest-b",
                lexical_index_run_id="lexical-b",
            ),
        ),
        source_fact_publication_ids=("publication-a", "publication-b"),
        ontology_snapshot_id="ontology",
        canonical_fact_resolution_snapshot_id="resolution",
        canonical_fact_projection_run_id="fact-projection",
        cutoff_at=T1,
        recorded_at=T1,
    )


def test_processing_is_exhaustive_and_t2_cannot_change_t1(
    conn: sqlite3.Connection,
) -> None:
    snapshot_id = _processing_snapshot(conn)
    obligation_count = conn.execute(
        "SELECT COUNT(*) FROM document_processing_obligation_revisions"
    ).fetchone()[0]
    first = verify_processing_snapshot(conn, snapshot_id)
    assert (
        conn.execute("SELECT COUNT(*) FROM document_processing_obligation_revisions").fetchone()[0]
        == obligation_count
    )
    assert first.member_count == 14

    _insert_document(conn, version=2, at=T2 - timedelta(hours=1))
    _insert_source_obligation(conn, key="obligation-b", at=T2 - timedelta(hours=1))
    t2 = derive_obligations(conn, SCOPE, T2, POLICY)
    assert len(t2) == 56
    assert {item.document_version_id for item in t2} == {
        "document-1",
        "document-2",
    }
    assert len({item.source_obligation_revision_id for item in t2}) == 2
    assert verify_processing_snapshot(conn, snapshot_id) == first


def test_missing_lane_blocks_processing_snapshot(conn: sqlite3.Connection) -> None:
    obligations = derive_obligations(conn, SCOPE, T1, POLICY)
    assert len(obligations) == 14
    with pytest.raises(ValueError, match="exactly one terminal seal"):
        seal_processing_snapshot(
            conn,
            processing_snapshot_id="processing:missing",
            idempotency_key="processing:missing",
            scope=SCOPE,
            cutoff_at=T1,
            policy=POLICY,
            recorded_at=T1,
        )
    for table in (
        "document_processing_snapshot_headers",
        "document_processing_snapshot_members",
        "document_processing_snapshot_seals",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_snapshot_verification_failure_rolls_back_header_members_and_seal(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seal_all_dispositions(conn, cutoff=T1, prefix="verification-failure")
    snapshot_id = "processing:verification-failure"

    def _reject_snapshot(*_args: object, **_kwargs: object) -> None:
        raise ValueError("forced snapshot verification failure")

    monkeypatch.setattr(
        "provenance.research_snapshot.verify_processing_snapshot",
        _reject_snapshot,
    )
    with pytest.raises(ValueError, match="forced snapshot verification failure"):
        seal_processing_snapshot(
            conn,
            processing_snapshot_id=snapshot_id,
            idempotency_key=snapshot_id,
            scope=SCOPE,
            cutoff_at=T1,
            policy=POLICY,
            recorded_at=T1,
        )
    for table in (
        "document_processing_snapshot_headers",
        "document_processing_snapshot_members",
        "document_processing_snapshot_seals",
    ):
        assert (
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE processing_snapshot_id=?",
                (snapshot_id,),
            ).fetchone()[0]
            == 0
        )


def test_arbitrary_processing_evidence_commitment_cannot_admit(
    conn: sqlite3.Connection,
) -> None:
    at = T1 - timedelta(minutes=45)
    _insert_document(conn, version=2, at=at, media_type="text/html")
    obligation = next(
        item
        for item in derive_obligations(conn, SCOPE, T1, POLICY)
        if item.document_version_id == "document-2"
        and item.processing_lane == "html_native_hierarchy"
    )
    arbitrary = ProcessingEvidenceReference(
        evidence_table="document_processing_evidence_seals",
        evidence_id="self-attested",
        evidence_commitment_sha256="a" * 64,
        knowledge_at=at,
        recorded_at=at,
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="processing_evidence_header_missing",
    ):
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id="self-attested",
                idempotency_key="self-attested",
                processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
                terminal_status="succeeded",
                reason_code="self_attested",
                reason_details={},
                evidence=(arbitrary,),
                knowledge_at=at,
                recorded_at=at,
            ),
        )


def test_exact_pptx_chart_disposition_admits_for_applicable_obligation(
    conn: sqlite3.Connection,
) -> None:
    at = T1 - timedelta(hours=1)
    _insert_document(
        conn,
        version=2,
        at=at,
        media_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        document_type="investor_presentation",
        form_type="presentation",
    )
    _insert_source_obligation(
        conn,
        key="presentation-obligation",
        at=at,
        document_family="issuer_presentations",
    )
    document = conn.execute(
        "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='document-2'"
    ).fetchone()
    assert document is not None
    _seed_exact_pptx_run(
        conn,
        document_version_id="document-2",
        blob_sha=str(document[0]),
        run_id="research-pptx-run",
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="document-2",
        processing_lane="pptx_charts",
        cutoff_at=T1,
        recorded_at=T1,
    )
    verified = verify_document_processing_evidence(
        conn,
        receipt.evidence_seal_id,
        document_version_id="document-2",
        processing_lane="pptx_charts",
        cutoff_at=T1,
    )
    scope = DocumentProcessingScope(document_version_ids=("document-2",))
    obligation = next(
        item
        for item in derive_obligations(conn, scope, T1, POLICY)
        if item.processing_lane == "pptx_charts"
    )
    assert obligation.applicability == "applicable"
    disposition_id = "pptx-chart-disposition"
    record_disposition(
        conn,
        DocumentProcessingDisposition(
            processing_disposition_id=disposition_id,
            idempotency_key=disposition_id,
            processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
            terminal_status="succeeded",
            reason_code="exact_native_chart_inventory",
            reason_details={},
            evidence=(
                ProcessingEvidenceReference(
                    evidence_table="document_processing_evidence_seals",
                    evidence_id=verified.evidence_seal_id,
                    evidence_commitment_sha256=verified.member_set_sha256,
                    knowledge_at=verified.knowledge_at,
                    recorded_at=verified.recorded_at,
                ),
            ),
            knowledge_at=T1,
            recorded_at=T1,
        ),
    )
    seal_disposition(conn, disposition_id, sealed_at=T1)
    stored = conn.execute(
        "SELECT terminal_status FROM document_processing_disposition_headers "
        "WHERE processing_disposition_id=?",
        (disposition_id,),
    ).fetchone()
    assert stored is not None
    assert stored[0] == "succeeded"


def test_exact_pdf_table_seal_admits_for_applicable_obligation(
    conn: sqlite3.Connection,
) -> None:
    at = T1 - timedelta(hours=1)
    raw = _pdf_table_fixture()
    _insert_document(
        conn,
        version=2,
        at=at,
        media_type="application/pdf",
        document_type="investor_presentation",
        form_type="presentation",
        raw_bytes=raw,
    )
    _insert_source_obligation(
        conn,
        key="pdf-presentation-obligation",
        at=at,
        document_family="issuer_presentations",
    )
    _artifact, artifact_id = _seed_pdf_table_artifact_for_existing_document(
        conn,
        document_version_id="document-2",
        raw_pdf_bytes=raw,
        run_id="research-pdf-table-run",
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="document-2",
        processing_lane="pdf_table",
        cutoff_at=T1,
        recorded_at=T1,
    )
    verified = verify_document_processing_evidence(
        conn,
        receipt.evidence_seal_id,
        document_version_id="document-2",
        processing_lane="pdf_table",
        cutoff_at=T1,
    )
    obligation = next(
        item
        for item in derive_obligations(
            conn,
            DocumentProcessingScope(document_version_ids=("document-2",)),
            T1,
            POLICY,
        )
        if item.processing_lane == "pdf_table"
    )
    assert obligation.applicability == "applicable"
    disposition_id = "pdf-table-disposition"
    record_disposition(
        conn,
        DocumentProcessingDisposition(
            processing_disposition_id=disposition_id,
            idempotency_key=disposition_id,
            processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
            terminal_status="succeeded",
            reason_code="exact_pdf_table_inventory",
            reason_details={"artifact_id": artifact_id},
            evidence=(
                ProcessingEvidenceReference(
                    evidence_table="document_processing_evidence_seals",
                    evidence_id=verified.evidence_seal_id,
                    evidence_commitment_sha256=verified.member_set_sha256,
                    knowledge_at=verified.knowledge_at,
                    recorded_at=verified.recorded_at,
                ),
            ),
            knowledge_at=T1,
            recorded_at=T1,
        ),
    )
    seal_disposition(conn, disposition_id, sealed_at=T1)
    assert (
        conn.execute(
            "SELECT terminal_status FROM document_processing_disposition_headers "
            "WHERE processing_disposition_id=?",
            (disposition_id,),
        ).fetchone()[0]
        == "succeeded"
    )


def test_native_transcript_evidence_admits_exactly_and_tampering_blocks_snapshot(
    conn: sqlite3.Connection,
) -> None:
    scope, obligations, references = _seed_transcript_processing_evidence(conn)
    applicable = {
        obligation.processing_lane: obligation
        for obligation in obligations
        if obligation.applicability == "applicable"
    }
    assert set(applicable) == {"transcript_turns", "transcript_speakers"}

    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="processing_evidence_coordinate_mismatch",
    ):
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id="transcript-wrong-lane",
                idempotency_key="transcript-wrong-lane",
                processing_obligation_revision_id=(
                    applicable["transcript_speakers"].processing_obligation_revision_id
                ),
                terminal_status="succeeded",
                reason_code="wrong_lane",
                reason_details={},
                evidence=(references["transcript_turns"],),
                knowledge_at=T1,
                recorded_at=T1,
            ),
        )

    correct_turns = references["transcript_turns"]
    wrong_clock = correct_turns.model_copy(
        update={"recorded_at": correct_turns.recorded_at - timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="clocks do not match the live seal"):
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id="transcript-wrong-clock",
                idempotency_key="transcript-wrong-clock",
                processing_obligation_revision_id=(
                    applicable["transcript_turns"].processing_obligation_revision_id
                ),
                terminal_status="succeeded",
                reason_code="wrong_clock",
                reason_details={},
                evidence=(wrong_clock,),
                knowledge_at=T1,
                recorded_at=T1,
            ),
        )

    for ordinal, obligation in enumerate(obligations):
        evidence: tuple[ProcessingEvidenceReference, ...] = ()
        terminal_status = "not_applicable"
        if obligation.applicability == "applicable":
            terminal_status = "succeeded"
            evidence = (references[obligation.processing_lane],)
        disposition_id = f"transcript-disposition:{ordinal}"
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=disposition_id,
                idempotency_key=disposition_id,
                processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
                terminal_status=terminal_status,
                reason_code="native_transcript",
                reason_details={"lane": obligation.processing_lane},
                evidence=evidence,
                knowledge_at=T1,
                recorded_at=T1,
            ),
        )
        seal_disposition(conn, disposition_id, sealed_at=T1)

    snapshot_id = "processing:transcript"
    seal_processing_snapshot(
        conn,
        processing_snapshot_id=snapshot_id,
        idempotency_key=snapshot_id,
        scope=scope,
        cutoff_at=T1,
        policy=POLICY,
        recorded_at=T1,
    )
    audited_tables = (
        "document_processing_obligation_revisions",
        "document_processing_disposition_headers",
        "document_processing_disposition_members",
        "document_processing_disposition_seals",
        "document_processing_snapshot_headers",
        "document_processing_snapshot_members",
        "document_processing_snapshot_seals",
        "document_processing_evidence_headers",
        "document_processing_evidence_members",
        "document_processing_evidence_seals",
    )
    before = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in audited_tables
    )
    assert verify_processing_snapshot(conn, snapshot_id).member_count == 14
    after = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in audited_tables
    )
    assert after == before

    conn.execute("DROP TRIGGER trg_evidence_nodes_append_only_delete")
    conn.execute("DROP TRIGGER trg_evidence_nodes_processing_evidence_frozen")
    conn.execute(
        "DELETE FROM evidence_nodes "
        "WHERE extraction_run_id='transcript-run' "
        "AND node_kind='transcript_turn' "
        "AND locator_json LIKE '%\"transcript_turn_sequence\":1%'"
    )
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="native_extraction_output_commitment_mismatch",
    ):
        verify_processing_snapshot(conn, snapshot_id)


def test_filing_xbrl_native_seal_admits_and_tampering_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, snapshot_id, publication_id = _sealed_filing_processing_snapshot(
        tmp_path,
        monkeypatch,
    )
    assert verify_processing_snapshot(conn, snapshot_id).member_count == 14
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM document_processing_disposition_headers "
            "WHERE terminal_status='succeeded'"
        ).fetchone()[0]
        == 1
    )
    conn.commit()

    member_tamper_path = tmp_path / "member-tamper.db"
    publication_tamper_path = tmp_path / "publication-tamper.db"
    member_tamper = sqlite3.connect(member_tamper_path)
    publication_tamper = sqlite3.connect(publication_tamper_path)
    conn.backup(member_tamper)
    conn.backup(publication_tamper)
    member_tamper.close()
    publication_tamper.close()
    conn.close()

    member_tamper = sqlite3.connect(member_tamper_path)
    member_tamper.row_factory = sqlite3.Row
    member_tamper.create_function("fact_sha256", 1, _sql_sha)
    member_tamper.execute("DROP TRIGGER trg_filing_xbrl_extraction_disposition_seals_append_only")
    member_tamper.execute(
        "UPDATE filing_xbrl_extraction_disposition_seals "
        "SET canonical_disposition_set_json='[]',"
        "disposition_set_sha256=?",
        (hashlib.sha256(b"[]").hexdigest(),),
    )
    with pytest.raises(
        ValueError,
        match=r"disposition set count does not reconcile|final seal mismatch",
    ):
        verify_processing_snapshot(member_tamper, snapshot_id)
    member_tamper.close()

    publication_tamper = sqlite3.connect(publication_tamper_path)
    publication_tamper.row_factory = sqlite3.Row
    publication_tamper.create_function("fact_sha256", 1, _sql_sha)
    publication_tamper.execute("PRAGMA foreign_keys=OFF")
    publication_tamper.execute("DROP TRIGGER trg_source_fact_publication_seals_append_only_delete")
    publication_tamper.execute(
        "DELETE FROM source_fact_publication_seals WHERE publication_id=?",
        (publication_id,),
    )
    with pytest.raises(PublicationVerificationError):
        verify_processing_snapshot(publication_tamper, snapshot_id)
    publication_tamper.close()


def test_processing_verification_rejects_semantically_false_member_json(
    conn: sqlite3.Connection,
) -> None:
    snapshot_id = _processing_snapshot(conn)
    conn.execute("DROP TRIGGER trg_document_processing_snapshot_members_update_append_only")
    false_json = '{"document_version_id":"not-the-row-coordinate"}'
    conn.execute(
        "UPDATE document_processing_snapshot_members "
        "SET canonical_member_json=?,member_sha256=? "
        "WHERE processing_snapshot_id=? AND member_ordinal=0",
        (
            false_json,
            hashlib.sha256(false_json.encode()).hexdigest(),
            snapshot_id,
        ),
    )
    with pytest.raises(ValueError, match="member commitment mismatch"):
        verify_processing_snapshot(conn, snapshot_id)


def test_multi_manifest_and_multi_publication_research_snapshot(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    verifier = _SealedDoubleVerifier()
    admission = _build_research_snapshot_with_verifier(conn, request, verifier=verifier)
    assert admission == _verify_research_snapshot_with_verifier(
        conn, request.research_snapshot_id, verifier=verifier
    )
    with pytest.raises((RuntimeError, ValueError)):
        admit(conn, request.research_snapshot_id)
    assert {
        "corpus:manifest-a",
        "corpus:manifest-b",
        "source_fact_publication:publication-a",
        "source_fact_publication:publication-b",
    } <= set(admission.requested_lanes)


def test_research_universe_reference_uses_actual_post_cutoff_seal_clock(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    sealed_at = T1 + timedelta(minutes=5)
    request = _research_request(processing).model_copy(
        update={
            "research_snapshot_id": "research:post-cutoff-seal",
            "idempotency_key": "research:post-cutoff-seal",
            "recorded_at": sealed_at,
        }
    )
    admission = _build_research_snapshot_with_verifier(
        conn,
        request,
        verifier=_SealedDoubleVerifier(),
    )
    member = conn.execute(
        "SELECT reference_knowledge_at,reference_recorded_at "
        "FROM research_snapshot_members WHERE research_snapshot_id=? "
        "AND requested_lane='research_universe'",
        (request.research_snapshot_id,),
    ).fetchone()
    universe = conn.execute(
        "SELECT cutoff_at,recorded_at FROM research_snapshot_universe_commitments "
        "WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    ).fetchone()
    assert member is not None and universe is not None
    assert datetime.fromisoformat(str(member[0])).replace(tzinfo=UTC) == T1
    assert datetime.fromisoformat(str(member[1])).replace(tzinfo=UTC) == sealed_at
    assert tuple(str(value) for value in universe) == (
        T1.replace(tzinfo=None).isoformat(sep=" "),
        sealed_at.replace(tzinfo=None).isoformat(sep=" "),
    )
    assert admission.research_snapshot_id == request.research_snapshot_id


def test_document_obligation_subject_pairing_rejects_two_entity_cross_swap() -> None:
    document_subjects = {
        "document-a": ("issuer-1", "reporting-1"),
        "document-b": ("issuer-1", "reporting-2"),
    }
    cross_swapped: tuple[tuple[object, ...], ...] = (
        (
            "expected-a",
            "obligation-reporting-2",
            "issuer-1",
            "reporting-2",
            "issuer_presentations",
            "document-a",
            "included",
        ),
        (
            "expected-b",
            "obligation-reporting-1",
            "issuer-1",
            "reporting-1",
            "issuer_presentations",
            "document-b",
            "included",
        ),
    )
    with pytest.raises(ValueError, match="exact source-obligation issuer"):
        _validate_document_obligation_subject_pairs(
            document_subjects,
            cross_swapped,
        )


@pytest.mark.parametrize(
    ("form_type", "issuer_kind", "expected_family"),
    (
        ("10-K", "operating_company", "operating_company_periodic"),
        ("N-CSR", "fund", "investment_company_periodic"),
        ("N-PORT/A", "fund", "investment_company_periodic"),
        ("8-K/A", "operating_company", "continuous_disclosure"),
    ),
)
def test_sec_source_duty_map_is_closed_and_issuer_kind_aware(
    form_type: str,
    issuer_kind: str,
    expected_family: str,
) -> None:
    record = CoverageExpectedDocument(
        expected_document_id="expected-policy",
        idempotency_key="expected-policy",
        snapshot_id="inventory-policy",
        expected_document_key=f"TEST:{form_type}",
        issuer_id="issuer-policy",
        source_kind="sec_filing",
        document_type="filing",
        form_type=form_type,
        expectation_basis="authoritative",
        recorded_at=T1,
    )
    assert _expected_document_family(record, issuer_kind=issuer_kind) == expected_family


def test_ir_supplement_uses_financial_statement_source_duty() -> None:
    assert _document_family("ir_supplement", "") == "issuer_financial_statements"
    assert _document_family("ir_doc", "supplement") == "issuer_financial_statements"

    expected = CoverageExpectedDocument(
        expected_document_id="expected-supplement",
        idempotency_key="expected-supplement",
        snapshot_id="inventory-supplement",
        expected_document_key="TEST:supplement",
        issuer_id="issuer-policy",
        source_kind="ir_document",
        document_type="supplement",
        form_type=None,
        expectation_basis="authoritative",
        recorded_at=T1,
    )
    assert (
        _expected_document_family(expected, issuer_kind="operating_company")
        == "issuer_financial_statements"
    )


@pytest.mark.parametrize(
    ("form_type", "issuer_kind"),
    (
        ("S-1", "operating_company"),
        ("N-CSR", "operating_company"),
        ("10-K", "fund"),
    ),
)
def test_sec_source_duty_map_rejects_unknown_or_wrong_issuer_kind(
    form_type: str,
    issuer_kind: str,
) -> None:
    record = CoverageExpectedDocument(
        expected_document_id="expected-policy-reject",
        idempotency_key="expected-policy-reject",
        snapshot_id="inventory-policy",
        expected_document_key=f"TEST:{form_type}",
        issuer_id="issuer-policy",
        source_kind="sec_filing",
        document_type="filing",
        form_type=form_type,
        expectation_basis="authoritative",
        recorded_at=T1,
    )
    with pytest.raises(ValueError):
        _expected_document_family(record, issuer_kind=issuer_kind)


def test_research_snapshot_rejects_processing_corpus_document_overlap(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    conn.execute(
        "INSERT INTO search_manifest_source_inventories VALUES (?,?,?)",
        ("manifest-b", "inventory-research", T1),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?,?,?)",
        (
            "membership-overlap",
            "manifest-b",
            "TEST:document-1",
            "document-1",
            "included",
            "test overlap",
            T1,
        ),
    )
    with pytest.raises(ValueError, match="must not overlap"):
        _build_research_snapshot_with_verifier(
            conn,
            _research_request(processing),
            verifier=_SealedDoubleVerifier(),
        )


def test_research_snapshot_rejects_missing_or_extra_universe_document(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    request = request.model_copy(
        update={
            "research_universe": request.research_universe.model_copy(
                update={"document_version_ids": ("document-1", "document-extra")}
            )
        }
    )
    with pytest.raises(ValueError, match="exact same document set"):
        _build_research_snapshot_with_verifier(
            conn,
            request,
            verifier=_SealedDoubleVerifier(),
        )


def test_research_snapshot_rejects_cross_reporting_entity_documents(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-2",
            "reporting-2",
            "issuer-1",
            "legal_registrant",
            "Other Reporting Entity",
            T1,
        ),
    )
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    request = request.model_copy(
        update={
            "research_universe": request.research_universe.model_copy(
                update={"reporting_entity_ids": ("reporting-2",)}
            )
        }
    )
    with pytest.raises(ValueError, match="document reporting-entity set"):
        _build_research_snapshot_with_verifier(
            conn,
            request,
            verifier=_SealedDoubleVerifier(),
        )


@pytest.mark.parametrize(
    ("snapshot_id", "reporting_ids", "document_ids"),
    (
        ("research:duplicate-subject", ("reporting-1", "reporting-1"), ("document-1",)),
        ("research:missing-document", ("reporting-1",), ("document-missing",)),
        ("research:cross-issuer", ("reporting-other",), ("document-1",)),
    ),
)
def test_universe_trigger_rejects_duplicate_nonexistent_or_cross_issuer_ids(
    conn: sqlite3.Connection,
    snapshot_id: str,
    reporting_ids: tuple[str, ...],
    document_ids: tuple[str, ...],
) -> None:
    if (
        conn.execute("SELECT 1 FROM issuer_entities WHERE issuer_id='issuer-other'").fetchone()
        is None
    ):
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-other", "issuer-other", "operating_company", T1),
        )
        conn.execute(
            "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
            (
                "reporting-other",
                "reporting-other",
                "issuer-other",
                "legal_registrant",
                "Other Issuer",
                T1,
            ),
        )
    universe = {
        "document_version_ids": list(document_ids),
        "issuer_id": "issuer-1",
        "reporting_entity_ids": list(reporting_ids),
        "source_obligation_revision_ids": ["obligation-a:v1"],
    }
    request_json = json.dumps(
        {"research_universe": universe},
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
        (
            snapshot_id,
            snapshot_id,
            request_json,
            hashlib.sha256(request_json.encode()).hexdigest(),
            T1,
            T1,
        ),
    )
    canonical = json.dumps(universe, sort_keys=True, separators=(",", ":"))
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        conn.execute(
            "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                "issuer-1",
                json.dumps(list(reporting_ids), separators=(",", ":")),
                json.dumps(list(document_ids), separators=(",", ":")),
                '["obligation-a:v1"]',
                canonical,
                hashlib.sha256(canonical.encode()).hexdigest(),
                T1,
                T1,
            ),
        )


def test_universe_trigger_rejects_false_digest(
    conn: sqlite3.Connection,
) -> None:
    universe = {
        "document_version_ids": ["document-1"],
        "issuer_id": "issuer-1",
        "reporting_entity_ids": ["reporting-1"],
        "source_obligation_revision_ids": ["obligation-a:v1"],
    }
    request_json = json.dumps(
        {"research_universe": universe},
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
        (
            "research:false-universe-digest",
            "research:false-universe-digest",
            request_json,
            hashlib.sha256(request_json.encode()).hexdigest(),
            T1,
            T1,
        ),
    )
    canonical = json.dumps(universe, sort_keys=True, separators=(",", ":"))
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        conn.execute(
            "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "research:false-universe-digest",
                "issuer-1",
                '["reporting-1"]',
                '["document-1"]',
                '["obligation-a:v1"]',
                canonical,
                "f" * 64,
                T1,
                T1,
            ),
        )


def test_expected_document_binding_trigger_rejects_false_digest(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "inventory:false-binding-digest",
            "inventory:false-binding-digest",
            "inventory:false-binding-digest",
            1,
            "issuer-1",
            "TEST",
            "ir_crawl",
            "https://example.test/digest-inventory",
            "observation-1",
            "succeeded",
            True,
            "a" * 64,
            "test",
            T1,
            T1,
            T1,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "expected:false-binding-digest",
            "expected:false-binding-digest",
            "inventory:false-binding-digest",
            "TEST:false-binding-digest",
            "issuer-1",
            "TEST",
            "ir_document",
            "presentation",
            "other",
            None,
            "https://example.test/digest-document",
            None,
            None,
            None,
            None,
            T1,
            "authoritative",
            T1,
        ),
    )
    payload = (
        '{"document_family":"continuous_disclosure",'
        '"expected_document_id":"expected:false-binding-digest",'
        '"issuer_id":"issuer-1","reporting_entity_id":"reporting-1",'
        '"source_obligation_revision_id":"obligation-a:v1"}'
    )
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        conn.execute(
            "INSERT INTO expected_document_obligation_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "binding:false-digest",
                "binding:false-digest",
                "expected:false-binding-digest",
                "obligation-a:v1",
                "issuer-1",
                "reporting-1",
                "continuous_disclosure",
                payload,
                "f" * 64,
                T1,
                T1,
                T1,
            ),
        )


def test_sec_8k_expected_document_binds_continuous_disclosure_revision(
    conn: sqlite3.Connection,
) -> None:
    at = T1 - timedelta(minutes=30)
    ledger = SourceCoverageLedger(conn)
    ledger.persist(
        SourceInventorySnapshot(
            snapshot_id="inventory-8k",
            idempotency_key="inventory-8k",
            inventory_key="inventory-8k",
            revision=1,
            issuer_id="issuer-1",
            ticker="TEST",
            source_kind="sec_submissions",
            source_url="https://www.sec.gov/Archives/edgar/data/test/submissions.json",
            source_observation_id="observation-1",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="9" * 64,
            collector_code_version="test",
            started_at=at,
            completed_at=at,
            recorded_at=at,
        )
    )
    ledger.persist(
        CoverageExpectedDocument(
            expected_document_id="expected-8k",
            idempotency_key="expected-8k",
            snapshot_id="inventory-8k",
            expected_document_key="TEST:8-K:0001",
            issuer_id="issuer-1",
            ticker="TEST",
            source_kind="sec_filing",
            document_type="filing",
            form_type="8-K",
            accession_number="0001",
            source_url="https://www.sec.gov/Archives/edgar/data/test/0001",
            expectation_basis="authoritative",
            recorded_at=at,
            source_obligation_revision_id="obligation-a:v1",
        )
    )
    row = conn.execute(
        "SELECT source_obligation_revision_id,document_family "
        "FROM expected_document_obligation_bindings "
        "WHERE expected_document_id='expected-8k'"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("obligation-a:v1", "continuous_disclosure")


@pytest.mark.parametrize(
    "publication_ids",
    [
        ("publication-a",),
        ("publication-a", "publication-b", "publication-extra"),
    ],
)
def test_research_snapshot_rejects_publication_omission_or_extra(
    conn: sqlite3.Connection,
    publication_ids: tuple[str, ...],
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing).model_copy(
        update={"source_fact_publication_ids": publication_ids}
    )
    with pytest.raises(ValueError, match="must exactly match"):
        _build_research_snapshot_with_verifier(conn, request, verifier=_SealedDoubleVerifier())


def test_empty_fact_plane_admits_exact_empty_publication_tuple(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing).model_copy(
        update={
            "research_snapshot_id": "research:empty-facts",
            "idempotency_key": "research:empty-facts",
            "source_fact_publication_ids": (),
            "canonical_fact_resolution_snapshot_id": "resolution-empty",
            "canonical_fact_projection_run_id": "fact-projection-empty",
        }
    )
    admission = _build_research_snapshot_with_verifier(
        conn, request, verifier=_SealedDoubleVerifier()
    )
    assert not any(
        lane.startswith("source_fact_publication:") for lane in admission.requested_lanes
    )
    assert "corpus:manifest-a" in admission.requested_lanes


def test_research_verification_rejects_wrong_commitment(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    verifier = _SealedDoubleVerifier()
    _build_research_snapshot_with_verifier(conn, request, verifier=verifier)
    conn.execute("DROP TRIGGER trg_research_snapshot_members_update_append_only")
    conn.execute(
        "UPDATE research_snapshot_members SET reference_commitment_sha256=? "
        "WHERE research_snapshot_id=? AND member_ordinal=0",
        ("0" * 64, request.research_snapshot_id),
    )
    with pytest.raises(ValueError, match="reference commitment mismatch"):
        _verify_research_snapshot_with_verifier(
            conn, request.research_snapshot_id, verifier=verifier
        )


def test_research_verification_rejects_semantically_false_member_json(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    verifier = _SealedDoubleVerifier()
    _build_research_snapshot_with_verifier(conn, request, verifier=verifier)
    conn.execute("DROP TRIGGER trg_research_snapshot_members_update_append_only")
    false_json = '{"requested_lane":"not-the-row-coordinate"}'
    conn.execute(
        "UPDATE research_snapshot_members "
        "SET canonical_member_json=?,member_sha256=? "
        "WHERE research_snapshot_id=? AND member_ordinal=0",
        (
            false_json,
            hashlib.sha256(false_json.encode()).hexdigest(),
            request.research_snapshot_id,
        ),
    )
    with pytest.raises(ValueError, match="member commitment mismatch"):
        _verify_research_snapshot_with_verifier(
            conn, request.research_snapshot_id, verifier=verifier
        )


def test_research_verification_rejects_extra_member(
    conn: sqlite3.Connection,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    verifier = _SealedDoubleVerifier()
    _build_research_snapshot_with_verifier(conn, request, verifier=verifier)
    conn.execute("DROP TRIGGER trg_research_snapshot_members_unsealed")
    member_json = (
        '{"reference_commitment_sha256":"'
        + "a" * 64
        + '","reference_id":"extra","reference_knowledge_at":"'
        + T1.isoformat()
        + '","reference_recorded_at":"'
        + T1.isoformat()
        + '","reference_table":"sealed_test_doubles",'
        '"requested_lane":"extra"}'
    )
    conn.execute(
        "INSERT INTO research_snapshot_members VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            request.research_snapshot_id,
            99,
            "extra",
            "sealed_test_doubles",
            "extra",
            "a" * 64,
            T1,
            T1,
            member_json,
            hashlib.sha256(member_json.encode()).hexdigest(),
        ),
    )
    with pytest.raises(ValueError, match="omitted, extra, or reordered"):
        _verify_research_snapshot_with_verifier(
            conn, request.research_snapshot_id, verifier=verifier
        )


@pytest.mark.parametrize("tamper", ["omission", "reorder"])
def test_research_verification_rejects_omission_or_reorder(
    conn: sqlite3.Connection,
    tamper: str,
) -> None:
    processing = _processing_snapshot(conn)
    request = _research_request(processing)
    verifier = _SealedDoubleVerifier()
    _build_research_snapshot_with_verifier(conn, request, verifier=verifier)
    if tamper == "omission":
        conn.execute("DROP TRIGGER trg_research_snapshot_members_delete_append_only")
        conn.execute(
            "DELETE FROM research_snapshot_members "
            "WHERE research_snapshot_id=? AND member_ordinal=0",
            (request.research_snapshot_id,),
        )
    else:
        conn.execute("DROP TRIGGER trg_research_snapshot_members_update_append_only")
        conn.execute(
            "UPDATE research_snapshot_members SET member_ordinal=999 "
            "WHERE research_snapshot_id=? AND member_ordinal=0",
            (request.research_snapshot_id,),
        )
        conn.execute(
            "UPDATE research_snapshot_members SET member_ordinal=0 "
            "WHERE research_snapshot_id=? AND member_ordinal=1",
            (request.research_snapshot_id,),
        )
        conn.execute(
            "UPDATE research_snapshot_members SET member_ordinal=1 "
            "WHERE research_snapshot_id=? AND member_ordinal=999",
            (request.research_snapshot_id,),
        )
    with pytest.raises(ValueError, match="omitted, extra, or reordered"):
        _verify_research_snapshot_with_verifier(
            conn, request.research_snapshot_id, verifier=verifier
        )


def test_semantic_bundle_requires_vector_and_promotion() -> None:
    with pytest.raises(ValidationError, match="vector seal and promotion"):
        CorpusProjectionBundle(
            corpus_manifest_id="manifest",
            lexical_index_run_id="lexical",
            vector_index_run_id="vector",
        )


def test_raw_source_fact_publication_never_admits_without_public_verifier(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(PublicationVerificationError):
        _DefaultResearchReferenceVerifier().verify(
            conn,
            requested_lane="source_fact_publication:raw",
            reference_id="raw",
            cutoff_at=T1,
            request=_research_request("processing:t1"),
        )


def test_embedding_promotion_absent_at_cutoff_fails_closed(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="absent at cutoff"):
        _DefaultResearchReferenceVerifier().verify(
            conn,
            requested_lane="embedding_promotion:manifest-a",
            reference_id="promotion-a",
            cutoff_at=T1,
            request=_research_request("processing:t1"),
        )
