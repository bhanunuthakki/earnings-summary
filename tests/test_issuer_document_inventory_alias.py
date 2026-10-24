"""Exact immutable source aliases do not rewrite registered document identity."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from execution import capture_issuer_document_inventory as cli
from execution import manage_issuer_document_sources as manage_cli
from ir_pipeline.source_inventory import (
    IRDiscoverySnapshot,
    IRDocumentCandidate,
    IRSourceInventoryRequest,
    _expected_candidate,
)
from pipeline import issuer_document_inventory as inventory
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    DocumentObservationLink,
    EvidenceLinkLedger,
)
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from provenance.issuer_registry_bootstrap import bootstrap_sec_submissions_issuer
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityRegistry,
)
from provenance.source_coverage_reconcile import _exact_document
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from tests.test_sec_submissions_issuer_bootstrap import _request as sec_profile_request

CLOCK = datetime(2026, 8, 6, tzinfo=UTC)
URL = "https://investor.example.test/bkng/release.pdf"
OLD_URL = "reindex_subdir:release.pdf"
PAYLOAD = b"%PDF-1.4\nsynthetic exact source bytes"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _reviewed_period_fixture(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> tuple[sqlite3.Connection, inventory.CurrentReportedPeriodReview]:
    db = migrated_db(tmp_path / "period-review.db")
    conn = connect_sqlite(db, role=SQLiteConnectionRole.WRITER)
    subject = bootstrap_sec_submissions_issuer(
        conn,
        request=sec_profile_request(
            tmp_path,
            apply=True,
            raw_body=json.dumps(
                {
                    "cik": 1691493,
                    "name": "Nu Holdings Ltd.",
                    "entityType": "operating",
                    "tickers": ["NU"],
                    "exchanges": ["NYSE"],
                    "fiscalYearEnd": "1231",
                }
            ).encode(),
        ),
    )
    issuer_id = subject.canonical_issuer_id
    profile_id = str(
        conn.execute(
            "SELECT profile_revision_id FROM issuer_profile_revisions WHERE issuer_id=?",
            (issuer_id,),
        ).fetchone()[0]
    )
    subject_id = str(
        conn.execute(
            "SELECT binding_revision_id FROM recorded_subject_binding_revisions "
            "WHERE recorded_issuer_id='legacy-ticker:NU' ORDER BY revision DESC LIMIT 1"
        ).fetchone()[0]
    )
    wording = "Nu Holdings Reports Second Quarter 2026 Financial Results"
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(72, 730, wording)
    pdf.showPage()
    pdf.save()
    raw = stream.getvalue()
    from provenance.fulltext_backfill import project_pdf_page_texts

    page = project_pdf_page_texts(raw)[0]
    start = page.index(wording)
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "current-quarter.pdf"
    path.write_bytes(raw)
    reviewed_at = datetime(2026, 10, 20, tzinfo=UTC)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(raw),
            media_type="application/pdf",
            storage_uri=path.as_uri(),
            recorded_at=reviewed_at,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="ir-current-source",
            idempotency_key="ir-current-source",
            source_kind="ir_document",
            source_url="https://investors.nu/current.pdf",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=reviewed_at,
            retrieved_at=reviewed_at,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="ir-current-version",
            document_key="ir-current-key",
            version_sequence=1,
            observation_id="ir-current-source",
            blob_sha256=digest,
            issuer_id=issuer_id,
            ticker="NU",
            document_type="ir_document",
            form_type="IR",
            period_start=None,
            period_end=None,
            as_of_at=None,
            language="en",
            recorded_at=reviewed_at,
        )
    )
    EvidenceLinkLedger(conn).persist_location(
        BlobLocationObservation(
            location_observation_id="ir-current-location",
            idempotency_key="ir-current-location",
            blob_sha256=digest,
            storage_uri=path.as_uri(),
            location_kind="local",
            availability_state="present",
            location_sequence=1,
            verified_at=reviewed_at,
            verified_byte_size=len(raw),
            verified_sha256=digest,
            recorded_at=reviewed_at,
        )
    )
    conn.commit()
    review = inventory.CurrentReportedPeriodReview(
        document_version_id="ir-current-version",
        source_sha256=digest,
        issuer_id=issuer_id,
        ticker="NU",
        profile_revision_id=profile_id,
        subject_binding_revision_id=subject_id,
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        page_number=1,
        char_start=start,
        char_end=start + len(wording),
        exact_quote=wording,
        reviewer_identity="codex-agent:synthetic-reviewer",
        review_evidence="First-page issuer headline states reported second-quarter results.",
        knowledge_cutoff=datetime(2026, 10, 21, tzinfo=UTC),
        observed_through=datetime(2026, 10, 21, tzinfo=UTC),
        reviewed_at=reviewed_at,
    )
    return conn, review


def test_current_reported_period_review_binds_native_quote_and_replays(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, review = _reviewed_period_fixture(tmp_path, migrated_db)
    try:
        node_id = inventory.persist_current_reported_period_review(conn, review)
        assert inventory.persist_current_reported_period_review(conn, review) == node_id
        assert (
            inventory.verified_current_reported_period_review(
                conn,
                document_version_id=review.document_version_id,
                source_sha256=review.source_sha256,
                period_end=review.period_end,
            )
            == review
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs WHERE extractor_name='ir-current-reported-period'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_reviewed_generic_ir_document_bridges_inventory_without_metadata_rewrite(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, review = _reviewed_period_fixture(tmp_path, migrated_db)
    url = "https://investors.nu/current.pdf"
    later = datetime(2026, 10, 22, tzinfo=UTC)
    try:
        inventory.persist_current_reported_period_review(conn, review)
        request = IRSourceInventoryRequest(
            issuer_id=review.issuer_id,
            ticker="NU",
            ir_url="https://investors.nu/",
            revision=1,
            discovery=IRDiscoverySnapshot(
                pages=(), candidates=(), crawl_complete=True, crawl_stop_reason="frontier_exhausted"
            ),
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic@1",
            started_at=later,
            completed_at=later,
            recorded_at=later,
            reconciled_at=later,
        )
        expected = _expected_candidate(
            conn,
            request,
            IRDocumentCandidate(
                url=url,
                link_text="Current quarter results",
                filename_hint="current.pdf",
                source_page="https://investors.nu/",
            ),
            authoritative=True,
        )
        assert expected.document_type == "earnings_release"
        assert expected.period_end is not None
        assert expected.period_end.date() == review.period_end
        assert expected.absence is not None
        details = dict(expected.absence.reason_details)
        assert (
            details["native_period_review_node_id"]
            == inventory.current_reported_period_review_identity(review)[0]
        )
        assert (
            details["native_period_review_sha256"]
            == inventory.current_reported_period_review_identity(review)[1]
        )
        assert _exact_document(
            conn, expected, issuer_id=review.issuer_id, ticker="NU", reconciled_at=later
        ) == (review.document_version_id, 1)
        assert (
            inventory.reviewed_current_period_for_url(
                conn,
                issuer_id=review.issuer_id,
                ticker="NU",
                source_url="https://investors.nu/unrelated.pdf",
                observed_through=later,
            )
            is None
        )
        assert (
            _exact_document(
                conn,
                expected.model_copy(update={"source_url": "https://investors.nu/unrelated.pdf"}),
                issuer_id=review.issuer_id,
                ticker="NU",
                reconciled_at=later,
            )
            is None
        )
        assert tuple(
            conn.execute(
                "SELECT document_type,period_end FROM evidence_document_versions WHERE document_version_id=?",
                (review.document_version_id,),
            ).fetchone()
        ) == ("ir_document", None)
        assert (
            inventory.reviewed_current_period_for_url(
                conn,
                issuer_id=review.issuer_id,
                ticker="NU",
                source_url=url,
                observed_through=review.reviewed_at,
            )
            is None
        )
        (tmp_path / "current-quarter.pdf").unlink()
        assert (
            inventory.reviewed_current_period_for_url(
                conn,
                issuer_id=review.issuer_id,
                ticker="NU",
                source_url=url,
                observed_through=later,
            )
            is None
        )
        assert (
            _exact_document(
                conn, expected, issuer_id=review.issuer_id, ticker="NU", reconciled_at=later
            )
            is None
        )
    finally:
        conn.close()


def test_current_reported_period_review_uses_existing_managed_phase(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn, review = _reviewed_period_fixture(tmp_path, migrated_db)
    db_path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.close()
    request = tmp_path / "review-request.json"
    request.write_text(review.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(manage_cli, "require_managed_sqlite_runtime", lambda: None)
    assert (
        manage_cli.main(
            [
                "review-period",
                "--request",
                str(request),
                "--state-root",
                str(tmp_path),
                "--db",
                str(db_path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "review-period"
    assert result["period_end"] == "2026-06-30"
    assert result["review_node_id"].startswith("ir-current-period-review:")


@pytest.mark.parametrize(
    "change",
    [
        {"exact_quote": "Nu Holdings Expects Second Quarter 2026 Financial Results"},
        {"exact_quote": "Nu Holdings Reports Prior Second Quarter 2026 Financial Results"},
        {"fiscal_year": 2025},
        {"period_end": date(2026, 9, 30)},
        {"document_version_id": "unrelated-version"},
        {"subject_binding_revision_id": "unrelated-subject"},
        {"source_sha256": "0" * 64},
        {"profile_revision_id": "missing-profile"},
        {"knowledge_cutoff": datetime(2026, 10, 19, tzinfo=UTC)},
    ],
)
def test_current_reported_period_review_rejects_false_or_unbound_claim(
    tmp_path: Path, migrated_db: Callable[..., Path], change: dict[str, object]
) -> None:
    conn, review = _reviewed_period_fixture(tmp_path, migrated_db)
    try:
        with pytest.raises((ValueError, inventory.IssuerDocumentInventoryError)):
            inventory.persist_current_reported_period_review(conn, review.model_copy(update=change))
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs WHERE extractor_name='ir-current-reported-period'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def _fixture(
    db: Path, root: Path, *, link_clock: datetime = CLOCK, duplicate_version: bool = False
) -> dict[str, object]:
    relative = "ir_documents/BKNG/release.pdf"
    raw = root / relative
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(PAYLOAD)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            7365,
            "BKNG",
            "ir_doc",
            "ir_press_release",
            "2026-06-30",
            relative,
            SHA,
            CLOCK.isoformat(),
            "ok",
            len(PAYLOAD),
            OLD_URL,
        ),
    )
    issuer = IssuerRegistry(conn)
    issuer.persist(
        IssuerEntity(
            issuer_id="issuer:bkng",
            idempotency_key="issuer:bkng",
            entity_kind="operating_company",
            created_at=CLOCK,
        )
    )
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        ReportingEntity(
            reporting_entity_id="reporting:bkng",
            idempotency_key="reporting:bkng",
            issuer_id="issuer:bkng",
            reporting_entity_kind="legal_registrant",
            display_name="Booking",
            created_at=CLOCK,
        )
    )
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="subject:1",
            idempotency_key="subject:1",
            recorded_issuer_id="legacy-ticker:BKNG",
            revision=1,
            issuer_id="issuer:bkng",
            reporting_entity_id="reporting:bkng",
            outcome="selected",
            decision_kind="deterministic",
            material_dissent=False,
            reason_code="synthetic",
            reason_details=(("scope", "synthetic"),),
            effective_at=CLOCK,
            knowledge_at=CLOCK,
            recorded_at=CLOCK,
        )
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=SHA,
            byte_size=len(PAYLOAD),
            media_type="application/pdf",
            storage_uri=raw.as_uri(),
            recorded_at=CLOCK,
        )
    )
    for oid, url in [("old-observation", OLD_URL), ("primary-observation", URL)]:
        ledger.persist(
            SourceObservation(
                observation_id=oid,
                idempotency_key=oid,
                source_kind="ir_document",
                source_url=url,
                blob_sha256=SHA,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=CLOCK,
                retrieved_at=CLOCK,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic@1",
            )
        )
    ledger.persist(
        DocumentVersion(
            document_version_id="legacy-doc-7365",
            document_key="legacy:7365",
            version_sequence=1,
            observation_id="old-observation",
            blob_sha256=SHA,
            issuer_id="legacy-ticker:BKNG",
            ticker="BKNG",
            document_type="ir_press_release",
            form_type="IR",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            as_of_at=None,
            replaces_document_version_id=None,
            period_end=datetime(2026, 6, 30),
            language="en",
            legacy_document_id=7365,
            recorded_at=CLOCK,
        )
    )
    EvidenceLinkLedger(conn).persist_link(
        DocumentObservationLink(
            link_id="primary-link",
            document_version_id="legacy-doc-7365",
            observation_id="primary-observation",
            link_kind="retrieval",
            linked_at=link_clock,
        )
    )
    if duplicate_version:
        other = b"different PDF content"
        other_sha = hashlib.sha256(other).hexdigest()
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                7366,
                "BKNG",
                "ir_doc",
                "ir_press_release",
                "2026-06-30",
                relative,
                other_sha,
                CLOCK.isoformat(),
                "ok",
                len(other),
                "reindex_subdir:other.pdf",
            ),
        )
        ledger.persist(
            ContentBlob(
                sha256=other_sha,
                byte_size=len(other),
                media_type="application/pdf",
                storage_uri=raw.as_uri(),
                recorded_at=CLOCK,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="other-primary",
                idempotency_key="other-primary",
                source_kind="ir_document",
                source_url=URL,
                blob_sha256=other_sha,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=CLOCK,
                retrieved_at=CLOCK,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic@1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="ambiguous-version",
                document_key="other-key",
                version_sequence=1,
                observation_id="other-primary",
                blob_sha256=other_sha,
                issuer_id="legacy-ticker:BKNG",
                ticker="BKNG",
                document_type="ir_press_release",
                form_type="IR",
                period_end=datetime(2026, 6, 30),
                language="en",
                legacy_document_id=7366,
                recorded_at=CLOCK,
            )
        )
        EvidenceLinkLedger(conn).persist_link(
            DocumentObservationLink(
                link_id="ambiguous-link",
                document_version_id="ambiguous-version",
                observation_id="other-primary",
                link_kind="retrieval",
                linked_at=CLOCK,
            )
        )
    conn.commit()
    conn.close()
    return {
        "schema_version": "issuer_document_inventory_request.v2_alias",
        "ticker": "BKNG",
        "fiscal_year": 2026,
        "fiscal_quarter": 2,
        "period_end": "2026-06-30",
        "knowledge_cutoff": CLOCK.isoformat(),
        "observed_through": CLOCK.isoformat(),
        "expected_documents": [
            {
                "source_url": URL,
                "document_type": "ir_press_release",
                "alias": {
                    "document_id": 7365,
                    "document_version_id": "legacy-doc-7365",
                    "blob_sha256": SHA,
                    "source_observation_id": "primary-observation",
                    "document_link_id": "primary-link",
                    "recorded_issuer_id": "legacy-ticker:BKNG",
                    "issuer_id": "issuer:bkng",
                    "reporting_entity_id": "reporting:bkng",
                    "subject_binding_revision_id": "subject:1",
                    "registered_source_url": OLD_URL,
                    "lineage_sha256": "0" * 64,
                },
            }
        ],
    }


def _seal(db: Path, payload: dict[str, object]) -> str:
    request = inventory.load_issuer_document_inventory_request(json.dumps(payload))
    assert isinstance(request, inventory.IssuerDocumentAliasInventoryRequest)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        item = request.expected_documents[0]
        proof = inventory.inspect_issuer_document_alias(conn, expected=item, request=request)
    finally:
        conn.close()
    digest = hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    alias = item.alias.model_copy(update={"lineage_sha256": digest})
    selected = item.model_copy(update={"alias": alias})
    return request.model_copy(update={"expected_documents": (selected,)}).model_dump_json()


def test_alias_cli_preserves_original_document_and_v1_failure(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = migrated_db(tmp_path / "isolated.db")
    root = tmp_path / "repo"
    (root / ".tmp").mkdir(parents=True)
    payload = _fixture(db, root)
    request = root / "request.json"
    request.write_text(_seal(db, payload))
    output = root / ".tmp" / "alias.json"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert (
        cli.main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(root),
                "--request",
                str(request),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    receipt = inventory.load_issuer_document_inventory_receipt(output.read_bytes())
    assert receipt.schema_version == "issuer_document_inventory_receipt.v2_alias"
    assert receipt.records[0].document_id == 7365
    assert receipt.records[0].registered_source_url == OLD_URL
    assert receipt.records[0].source_url == URL
    assert receipt.records[0].alias.document_version_id == "legacy-doc-7365"
    assert output.read_bytes() == (receipt.canonical_json + "\n").encode()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    old = {k: v for k, v in payload.items() if k not in {"knowledge_cutoff", "observed_through"}}
    old["schema_version"] = "issuer_document_inventory_request.v1"
    old["expected_documents"] = [{"source_url": URL, "document_type": "ir_press_release"}]
    request.write_text(json.dumps(old))
    assert (
        cli.main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(root),
                "--request",
                str(request),
                "--output",
                str(root / ".tmp" / "v1.json"),
            ]
        )
        == 2
    )
    assert "missing_document" in capsys.readouterr().err


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "version",
        "observation",
        "link",
        "issuer",
        "entity",
        "subject",
        "registered_url",
        "type",
        "period",
        "cutoff",
        "commitment",
        "raw_bytes",
    ],
)
def test_alias_rejects_unbound_or_changed_evidence(
    migrated_db: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    db = migrated_db(tmp_path / "isolated.db")
    root = tmp_path / "repo"
    (root / ".tmp").mkdir(parents=True)
    payload = _fixture(db, root)
    sealed = json.loads(_seal(db, payload))
    item = sealed["expected_documents"][0]
    alias = item["alias"]
    if case == "hash":
        alias["blob_sha256"] = "b" * 64
    elif case == "version":
        alias["document_version_id"] = "missing-version"
    elif case == "observation":
        alias["source_observation_id"] = "missing-observation"
    elif case == "link":
        alias["document_link_id"] = "missing-link"
    elif case == "issuer":
        alias["issuer_id"] = "wrong-issuer"
    elif case == "entity":
        alias["reporting_entity_id"] = "wrong-entity"
    elif case == "subject":
        alias["subject_binding_revision_id"] = "missing-subject"
    elif case == "registered_url":
        alias["registered_source_url"] = "reindex_subdir:wrong.pdf"
    elif case == "type":
        item["document_type"] = "ir_presentation"
    elif case == "period":
        sealed.update({"period_end": "2026-03-31", "fiscal_quarter": 1})
    elif case == "cutoff":
        sealed["knowledge_cutoff"] = "2026-08-05T00:00:00Z"
    elif case == "commitment":
        alias["lineage_sha256"] = "c" * 64
    elif case == "raw_bytes":
        (root / "ir_documents/BKNG/release.pdf").write_bytes(b"changed")
    request = root / "request.json"
    request.write_text(json.dumps(sealed))
    out = root / ".tmp" / "receipt.json"
    assert (
        cli.main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(root),
                "--request",
                str(request),
                "--output",
                str(out),
            ]
        )
        == 2
    )
    assert not out.exists()
    assert "issuer_document_inventory_failed" in capsys.readouterr().err


def test_alias_rejects_superseded_subject(migrated_db: Callable[..., Path], tmp_path: Path) -> None:
    db = migrated_db(tmp_path / "isolated.db")
    root = tmp_path / "repo"
    (root / ".tmp").mkdir(parents=True)
    payload = _fixture(db, root)
    raw = _seal(db, payload)
    conn = sqlite3.connect(db)
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="subject:2",
            idempotency_key="subject:2",
            recorded_issuer_id="legacy-ticker:BKNG",
            revision=2,
            outcome="retired",
            decision_kind="deterministic",
            material_dissent=False,
            reason_code="synthetic",
            reason_details=(("scope", "synthetic"),),
            effective_at=CLOCK,
            knowledge_at=CLOCK,
            recorded_at=CLOCK,
            supersedes_binding_revision_id="subject:1",
        )
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    request = inventory.load_issuer_document_inventory_request(raw)
    with pytest.raises(inventory.IssuerDocumentInventoryError, match="alias_subject_not_current"):
        inventory.build_issuer_document_inventory(
            conn, database_path=db, repo_root=root, request=request
        )
    conn.close()


def test_v1_wire_does_not_gain_alias_fields() -> None:
    raw = '{"expected_documents":[{"document_type":"ir_press_release","source_url":"https://investor.example.test/bkng/release.pdf"}],"fiscal_quarter":2,"fiscal_year":2026,"period_end":"2026-06-30","schema_version":"issuer_document_inventory_request.v1","ticker":"BKNG"}'
    request = inventory.load_issuer_document_inventory_request(raw)
    assert type(request) is inventory.IssuerDocumentInventoryRequest
    assert request.canonical_json == raw
    unknown = json.loads(raw)
    unknown["schema_version"] = "issuer_document_inventory_request.v3"
    with pytest.raises(ValueError):
        inventory.load_issuer_document_inventory_request(json.dumps(unknown))


@pytest.mark.parametrize("case", ["future_link", "ambiguous_source"])
def test_alias_rejects_precise_future_clock_and_ambiguous_source(
    migrated_db: Callable[..., Path], tmp_path: Path, case: str
) -> None:
    db = migrated_db(tmp_path / "isolated.db")
    root = tmp_path / "repo"
    (root / ".tmp").mkdir(parents=True)
    payload = _fixture(
        db,
        root,
        link_clock=CLOCK + timedelta(microseconds=1) if case == "future_link" else CLOCK,
        duplicate_version=case == "ambiguous_source",
    )
    expected = "alias_lineage_after_cutoff" if case == "future_link" else "alias_source_ambiguous"
    with pytest.raises(inventory.IssuerDocumentInventoryError, match=expected):
        _seal(db, payload)


def test_alias_fiscal_request_retains_shared_period_and_clock_rules(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db = tmp_path / "alias-fiscal.db"
    migrated_db(db)
    root = tmp_path / "repo"
    payload = _fixture(db, root)
    witness = inventory.FiscalPeriodWitness(
        issuer_id="issuer:bkng",
        profile_revision_id="synthetic:profile",
        fiscal_year_end="01-31",
        document_version_id="legacy-doc-7365",
        document_sha256=SHA,
        knowledge_cutoff=CLOCK,
        observed_through=CLOCK,
    )
    payload.update(
        fiscal_year=2027, fiscal_quarter=2, period_end="2026-07-31", fiscal_period_witness=witness
    )
    request = inventory.IssuerDocumentAliasInventoryRequest.model_validate(payload)
    assert request.fiscal_period_witness == witness
    assert request.period_end == date(2026, 7, 31)
    with pytest.raises(ValueError, match="clocks must match"):
        inventory.IssuerDocumentAliasInventoryRequest.model_validate(
            {
                **payload,
                "knowledge_cutoff": CLOCK + timedelta(seconds=1),
                "observed_through": CLOCK + timedelta(seconds=1),
            }
        )
    with pytest.raises(ValueError, match="fiscal quarter"):
        inventory.IssuerDocumentAliasInventoryRequest.model_validate(
            {**payload, "period_end": "2026-06-30"}
        )


def test_absent_fiscal_witness_preserves_fixed_predecessor_bytes() -> None:
    raw = '{"expected_documents":[{"document_type":"ir_press_release","source_url":"https://investor.example.test/bkng/release.pdf"}],"fiscal_quarter":2,"fiscal_year":2026,"period_end":"2026-06-30","schema_version":"issuer_document_inventory_request.v1","ticker":"BKNG"}'
    request = inventory.IssuerDocumentInventoryRequest.model_validate_json(raw)
    explicit_unknown = request.model_copy(update={"fiscal_period_witness": None})
    assert explicit_unknown.canonical_json.encode() == raw.encode()
    assert explicit_unknown.request_sha256 == hashlib.sha256(raw.encode()).hexdigest()
