"""Contracts for strict, evidence-derived source coverage reconciliation."""

from __future__ import annotations

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
from provenance.evidence_links import DocumentObservationLink, EvidenceLinkLedger
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExplicitAbsence,
    SourceCoverageImport,
    reconcile_source_coverage,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    ExpectedDocument,
    build_grounded_search_corpus,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 4, 0, 0)
A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "coverage.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0219_source_coverage_ledger")
    # The reconciler's indexed status is a current-runtime contract.  Projection
    # seals were added later without changing the 0219 coverage tables, so this
    # focused fixture fast-forwards only that additive search publication gate.
    command.stamp(config, "0232_document_semantic_dispositions")
    command.upgrade(config, "0233_search_projection_seals")
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE search_projection_seals ADD COLUMN runtime_artifact_sha256 TEXT")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_evidence(conn: sqlite3.Connection, *, substantive: bool = True) -> None:
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=A,
            byte_size=10,
            media_type="text/plain",
            storage_uri="file:///acme",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs",
            idempotency_key="obs",
            source_kind="sec",
            source_url="https://sec.test/acme-10q",
            blob_sha256=A,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=B,
            collector_code_version="collector@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="doc",
            document_key="ACME:10-Q:2026Q1",
            version_sequence=1,
            observation_id="obs",
            blob_sha256=A,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000123456-26-000001",
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="run",
            idempotency_key="run",
            document_version_id="doc",
            input_sha256=A,
            extractor_name="parser",
            extractor_config_sha256=B,
            extractor_code_version="parser@1",
            output_sha256=C,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id="node",
            evidence_key="ACME:10-Q:2026Q1:passage",
            revision=1,
            extraction_run_id="run",
            node_kind="passage" if substantive else "document",
            text="Revenue grew year over year." if substantive else "Document placeholder.",
            recorded_at=STAMP,
        )
    )
    conn.commit()


def _seed_document(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    document_key: str,
    observation_id: str,
    source_url: str,
    issuer_id: str,
    ticker: str,
    document_type: str,
    form_type: str,
    accession_number: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    version_sequence: int = 1,
    replaces_document_version_id: str | None = None,
    blob_sha256: str = A,
    recorded_at: datetime = STAMP,
) -> None:
    ledger = EvidenceLedger(conn)
    if (
        conn.execute(
            "SELECT 1 FROM evidence_content_blobs WHERE sha256 = ?",
            (blob_sha256,),
        ).fetchone()
        is None
    ):
        ledger.persist(
            ContentBlob(
                sha256=blob_sha256,
                byte_size=10,
                media_type="application/octet-stream",
                storage_uri=f"file:///{blob_sha256}",
                recorded_at=recorded_at,
            )
        )
    ledger.persist(
        SourceObservation(
            observation_id=observation_id,
            idempotency_key=observation_id,
            source_kind="publisher",
            source_url=source_url,
            blob_sha256=blob_sha256,
            source_published_at=recorded_at,
            filing_at=recorded_at,
            accepted_at=recorded_at,
            observed_at=recorded_at,
            retrieved_at=recorded_at,
            retrieval_config_sha256=B,
            collector_code_version="collector@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=document_version_id,
            document_key=document_key,
            version_sequence=version_sequence,
            observation_id=observation_id,
            blob_sha256=blob_sha256,
            issuer_id=issuer_id,
            ticker=ticker,
            document_type=document_type,
            form_type=form_type,
            accession_number=accession_number,
            period_start=period_start,
            period_end=period_end,
            language="en",
            replaces_document_version_id=replaces_document_version_id,
            recorded_at=recorded_at,
        )
    )
    conn.commit()


def _expected(key: str = "ACME:2026Q1:10-Q") -> ExpectedDocumentImport:
    return ExpectedDocumentImport(
        expected_document_key=key,
        source_kind="sec_filing",
        document_type="filing",
        form_type="10-Q",
        accession_number="0000123456-26-000001",
        expectation_basis="authoritative",
    )


def _request(
    *, apply: bool, expected: tuple[ExpectedDocumentImport, ...] | None = None
) -> SourceCoverageImport:
    return SourceCoverageImport(
        inventory_key="ACME:sec-submissions",
        revision=1,
        issuer_id="issuer-acme",
        ticker="ACME",
        source_kind="sec_submissions",
        source_url="https://sec.test/submissions/ACME",
        source_observation_id="obs",
        outcome="succeeded",
        authoritative=True,
        retrieval_config_sha256=B,
        collector_code_version="coverage-import@1",
        started_at=STAMP,
        completed_at=STAMP,
        recorded_at=STAMP,
        reconciled_at=STAMP,
        expected_documents=expected or (_expected(),),
        apply=apply,
    )


def test_reconciler_dry_run_derives_extracted_without_writing(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        result = reconcile_source_coverage(conn, _request(apply=False))

        assert result.mode == "dry_run"
        assert result.assessment_statuses == ("extracted",)
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def test_reconciler_accepts_verified_fts_lineage_without_vector_memberships(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        corpus = build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key="ACME:reporting",
                revision=1,
                selector_code_version="corpus-builder@1",
                recorded_at=STAMP,
                expected_documents=(
                    ExpectedDocument(
                        expected_document_key="ACME:2026Q1:10-Q",
                        document_version_id="doc",
                        membership_status="included",
                        reason="verified source evidence",
                    ),
                ),
                required_extractor_names=("parser",),
                apply=True,
            ),
        )

        result = reconcile_source_coverage(conn, _request(apply=False))

        assert corpus.completion_status == "complete"
        assert conn.execute("SELECT COUNT(*) FROM search_index_memberships").fetchone()[0] == 0
        assert result.assessment_statuses == ("indexed",)
    finally:
        conn.close()


def test_apply_replays_exact_import_without_new_coverage_revision(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        first = reconcile_source_coverage(conn, _request(apply=True))
        second = reconcile_source_coverage(conn, _request(apply=True))

        assert first.records_created == 3
        assert second.records_created == 0
        assert (
            conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone()[0]
            == "extracted"
        )
        assert conn.execute("SELECT COUNT(*) FROM source_coverage_assessments").fetchone()[0] == 1
    finally:
        conn.close()


def test_placeholder_only_succeeded_extraction_remains_captured(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn, substantive=False)

        result = reconcile_source_coverage(conn, _request(apply=False))

        assert result.assessment_statuses == ("captured",)
    finally:
        conn.close()


def test_missing_evidence_requires_explicit_status_and_apply_rolls_back(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        absent = ExpectedDocumentImport(
            expected_document_key="ACME:2026Q1:deck",
            source_kind="ir_document",
            document_type="investor_presentation",
            expectation_basis="publisher_candidate",
        )
        with pytest.raises(ValueError, match="explicit absence"):
            reconcile_source_coverage(conn, _request(apply=True, expected=(_expected(), absent)))
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def test_explicit_authority_unavailable_status_is_preserved(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        absent = ExpectedDocumentImport(
            expected_document_key="ACME:2026Q1:deck",
            source_kind="ir_document",
            document_type="investor_presentation",
            expectation_basis="publisher_candidate",
            absence=ExplicitAbsence(
                coverage_status="authority_unavailable",
                reason_code="ir_archive_not_authoritative",
                reason_details=(("source", "issuer archive was unavailable"),),
                material_dissent=True,
            ),
        )
        result = reconcile_source_coverage(conn, _request(apply=True, expected=(absent,)))

        assert result.assessment_statuses == ("authority_unavailable",)
        assert (
            conn.execute("SELECT material_dissent FROM source_coverage_assessments").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_cli_parses_closed_json_and_stays_read_only_without_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution.reconcile_source_coverage import main

    conn = _conn(tmp_path)
    db_path = tmp_path / "coverage.db"
    try:
        _seed_evidence(conn)
    finally:
        conn.close()
    payload = _request(apply=False).model_dump(mode="json", exclude={"apply"})
    input_path = tmp_path / "source-inventory.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--db", str(db_path), "--input", str(input_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry_run"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_inventory_has_no_expected_documents_and_is_persisted_for_audit(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        request = SourceCoverageImport(
            inventory_key="ACME:ir-crawl",
            revision=1,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="ir_crawl",
            source_url="https://ir.test/acme",
            outcome="failed",
            authoritative=False,
            retrieval_config_sha256=B,
            collector_code_version="coverage-import@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            reconciled_at=STAMP,
            expected_documents=(),
            apply=True,
        )

        result = reconcile_source_coverage(conn, request)

        assert result.records_created == 1
        assert result.expected_document_count == 0
        assert (
            conn.execute("SELECT outcome FROM source_inventory_snapshots").fetchone()[0] == "failed"
        )
    finally:
        conn.close()


def test_later_reconciliation_after_new_extraction_creates_a_new_clocked_revision(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    later = STAMP + timedelta(hours=2)
    try:
        _seed_evidence(conn)
        reconcile_source_coverage(conn, _request(apply=True))
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id="run-later",
                idempotency_key="run-later",
                document_version_id="doc",
                input_sha256=A,
                extractor_name="parser",
                extractor_config_sha256=B,
                extractor_code_version="parser@2",
                output_sha256=C,
                started_at=later,
                completed_at=later,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="node-later",
                evidence_key="ACME:10-Q:2026Q1:later-passage",
                revision=1,
                extraction_run_id="run-later",
                node_kind="passage",
                text="Later extraction preserved the guidance update.",
                recorded_at=later,
            )
        )
        conn.commit()

        request = _request(apply=True).model_copy(update={"reconciled_at": later})
        result = reconcile_source_coverage(conn, request)

        assert result.records_created == 1
        assert conn.execute(
            "SELECT revision, extraction_run_id, knowledge_at, recorded_at "
            "FROM v_source_coverage_current"
        ).fetchone() == (2, "run-later", str(later), str(later))
    finally:
        conn.close()


def test_exact_source_url_matches_secondary_retrieval_link(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        ledger = EvidenceLedger(conn)
        ledger.persist(
            SourceObservation(
                observation_id="obs-secondary",
                idempotency_key="obs-secondary",
                source_kind="sec",
                source_url="https://sec.test/acme-10q-secondary",
                blob_sha256=A,
                source_published_at=STAMP,
                filing_at=STAMP,
                accepted_at=STAMP,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=B,
                collector_code_version="collector@1",
            )
        )
        EvidenceLinkLedger(conn).persist_link(
            DocumentObservationLink(
                link_id="secondary-link",
                document_version_id="doc",
                observation_id="obs-secondary",
                link_kind="retrieval",
                linked_at=STAMP,
            )
        )
        conn.commit()
        expected = _expected().model_copy(
            update={"accession_number": None, "source_url": "https://sec.test/acme-10q-secondary"}
        )

        result = reconcile_source_coverage(conn, _request(apply=False, expected=(expected,)))

        assert result.assessment_statuses == ("extracted",)
    finally:
        conn.close()


def test_attachment_url_prevents_accession_parent_from_satisfying_child(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        ledger = EvidenceLedger(conn)
        ledger.persist(
            SourceObservation(
                observation_id="obs-attachment",
                idempotency_key="obs-attachment",
                source_kind="sec_filing_package",
                source_url="https://sec.test/acme-exhibit-99-1.htm",
                blob_sha256=A,
                source_published_at=STAMP,
                filing_at=STAMP,
                accepted_at=STAMP,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=B,
                collector_code_version="collector@1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="doc-attachment",
                document_key="ACME:10-Q:2026Q1:EX-99.1",
                version_sequence=1,
                observation_id="obs-attachment",
                blob_sha256=A,
                issuer_id="issuer-acme",
                ticker="ACME",
                document_type="sec_exhibit",
                form_type="10-Q",
                accession_number="0000123456-26-000001",
                exhibit_id="EX-99.1",
                language="en",
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id="run-attachment",
                idempotency_key="run-attachment",
                document_version_id="doc-attachment",
                input_sha256=A,
                extractor_name="parser",
                extractor_config_sha256=B,
                extractor_code_version="parser@1",
                output_sha256=C,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="node-attachment",
                evidence_key="ACME:10-Q:2026Q1:EX-99.1:passage",
                revision=1,
                extraction_run_id="run-attachment",
                node_kind="passage",
                text="The earnings exhibit reports updated guidance.",
                recorded_at=STAMP,
            )
        )
        conn.commit()
        expected = _expected("ACME:2026Q1:EX-99.1").model_copy(
            update={
                "document_type": "sec_exhibit",
                "source_url": "https://sec.test/acme-exhibit-99-1.htm",
                "primary_document": "acme-exhibit-99-1.htm",
            }
        )

        reconcile_source_coverage(conn, _request(apply=True, expected=(expected,)))

        assert conn.execute(
            "SELECT document_version_id FROM v_source_coverage_current"
        ).fetchone() == ("doc-attachment",)
    finally:
        conn.close()


def test_exact_authority_identity_bridges_legacy_and_canonical_issuer_ids(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        conn.execute(
            "CREATE TABLE issuer_entities ("
            "issuer_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
            "entity_kind TEXT NOT NULL, created_at DATETIME NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE legacy_issuer_binding_revisions ("
            "binding_revision_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
            "recorded_issuer_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "issuer_id TEXT, outcome TEXT NOT NULL, decision_kind TEXT NOT NULL, "
            "reason_code TEXT NOT NULL, reason_details_json TEXT NOT NULL, "
            "material_dissent BOOLEAN NOT NULL, effective_at DATETIME NOT NULL, "
            "knowledge_at DATETIME NOT NULL, recorded_at DATETIME NOT NULL, "
            "supersedes_binding_revision_id TEXT)"
        )
        conn.execute(
            "CREATE INDEX ix_test_legacy_issuer_binding_current "
            "ON legacy_issuer_binding_revisions (recorded_issuer_id, revision)"
        )
        conn.execute(
            "INSERT INTO issuer_entities "
            "(issuer_id, idempotency_key, entity_kind, created_at) "
            "VALUES (?, ?, 'operating_company', ?)",
            ("issuer-canonical-acme", "issuer-canonical-acme", STAMP),
        )
        conn.execute(
            "INSERT INTO legacy_issuer_binding_revisions "
            "(binding_revision_id, idempotency_key, recorded_issuer_id, revision, "
            "issuer_id, outcome, decision_kind, reason_code, reason_details_json, "
            "material_dissent, effective_at, knowledge_at, recorded_at, "
            "supersedes_binding_revision_id) "
            "VALUES (?, ?, ?, 1, ?, 'selected', 'manual', ?, '{}', 0, ?, ?, ?, NULL)",
            (
                "binding-acme",
                "binding-acme",
                "issuer-acme",
                "issuer-canonical-acme",
                "reviewed_legacy_identity",
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        conn.commit()
        canonical_request = _request(apply=False).model_copy(
            update={"issuer_id": "issuer-canonical-acme"}
        )

        result = reconcile_source_coverage(conn, canonical_request)

        assert result.assessment_statuses == ("extracted",)
    finally:
        conn.close()


def test_shared_source_url_never_crosses_canonical_issuer_scope(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        _seed_document(
            conn,
            document_version_id="doc-other",
            document_key="OTHER:10-Q:2026Q1",
            observation_id="obs-other",
            source_url="https://sec.test/acme-10q",
            issuer_id="issuer-other",
            ticker="OTHER",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000123456-26-000001",
            version_sequence=99,
        )
        expected = _expected().model_copy(update={"source_url": "https://sec.test/acme-10q"})

        result = reconcile_source_coverage(conn, _request(apply=False, expected=(expected,)))

        assert result.assessment_statuses == ("extracted",)
    finally:
        conn.close()


def test_reused_ticker_is_not_treated_as_issuer_identity(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        _seed_document(
            conn,
            document_version_id="doc-reused-ticker",
            document_key="OTHER:10-Q:2026Q2",
            observation_id="obs-reused-ticker",
            source_url="https://sec.test/reused-ticker",
            issuer_id="issuer-other",
            ticker="ACME",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000999999-26-000002",
        )
        expected = ExpectedDocumentImport(
            expected_document_key="ACME:2026Q2:10-Q",
            source_kind="sec_filing",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000999999-26-000002",
            source_url="https://sec.test/reused-ticker",
            expectation_basis="authoritative",
            absence=ExplicitAbsence(
                coverage_status="not_discovered",
                reason_code="issuer_scoped_evidence_absent",
                reason_details=(("issuer_id", "issuer-acme"),),
            ),
        )

        result = reconcile_source_coverage(conn, _request(apply=False, expected=(expected,)))

        assert result.assessment_statuses == ("not_discovered",)
    finally:
        conn.close()


def test_stable_ir_url_uses_period_to_resolve_logical_document(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    q1_start = datetime(2026, 1, 1)
    q1_end = datetime(2026, 3, 31)
    try:
        _seed_evidence(conn)
        stable_url = "https://ir.test/acme/latest-presentation.pdf"
        _seed_document(
            conn,
            document_version_id="deck-q1",
            document_key="ACME:deck:2026Q1",
            observation_id="obs-deck-q1",
            source_url=stable_url,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="investor_presentation",
            form_type="IR",
            period_start=q1_start,
            period_end=q1_end,
        )
        _seed_document(
            conn,
            document_version_id="deck-q2",
            document_key="ACME:deck:2026Q2",
            observation_id="obs-deck-q2",
            source_url=stable_url,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="investor_presentation",
            form_type="IR",
            period_start=datetime(2026, 4, 1),
            period_end=datetime(2026, 6, 30),
        )
        _seed_document(
            conn,
            document_version_id="deck-q1-future-version",
            document_key="ACME:deck:2026Q1",
            observation_id="obs-deck-q1-future-version",
            source_url=stable_url,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="investor_presentation",
            form_type="IR",
            period_start=q1_start,
            period_end=q1_end,
            version_sequence=2,
            replaces_document_version_id="deck-q1",
            blob_sha256=D,
            recorded_at=STAMP + timedelta(days=1),
        )
        expected = ExpectedDocumentImport(
            expected_document_key="ACME:2026Q1:deck",
            source_kind="ir_document",
            document_type="investor_presentation",
            form_type="IR",
            source_url=stable_url,
            period_start=q1_start,
            period_end=q1_end,
            expectation_basis="publisher_candidate",
        )

        result = reconcile_source_coverage(conn, _request(apply=True, expected=(expected,)))

        assert result.assessment_statuses == ("captured",)
        assert conn.execute(
            "SELECT document_version_id FROM v_source_coverage_current"
        ).fetchone() == ("deck-q1",)
    finally:
        conn.close()


def test_ambiguous_exact_candidates_are_not_resolved_by_recency(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        stable_url = "https://ir.test/acme/ambiguous.pdf"
        for suffix in ("a", "b"):
            _seed_document(
                conn,
                document_version_id=f"ambiguous-{suffix}",
                document_key=f"ACME:deck:{suffix}",
                observation_id=f"obs-ambiguous-{suffix}",
                source_url=stable_url,
                issuer_id="issuer-acme",
                ticker="ACME",
                document_type="investor_presentation",
                form_type="IR",
                version_sequence=2 if suffix == "b" else 1,
            )
        expected = ExpectedDocumentImport(
            expected_document_key="ACME:ambiguous:deck",
            source_kind="ir_document",
            document_type="investor_presentation",
            form_type="IR",
            source_url=stable_url,
            expectation_basis="publisher_candidate",
            absence=ExplicitAbsence(
                coverage_status="quarantined",
                reason_code="ambiguous_exact_evidence",
                reason_details=(("candidate_document_keys", "2"),),
                material_dissent=True,
            ),
        )

        result = reconcile_source_coverage(conn, _request(apply=False, expected=(expected,)))

        assert result.assessment_statuses == ("quarantined",)
    finally:
        conn.close()
