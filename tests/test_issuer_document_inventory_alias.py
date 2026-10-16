"""Exact immutable source aliases do not rewrite registered document identity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution import capture_issuer_document_inventory as cli
from pipeline import issuer_document_inventory as inventory
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import DocumentObservationLink, EvidenceLinkLedger
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityRegistry,
)

CLOCK = datetime(2026, 8, 6, tzinfo=UTC)
URL = "https://investor.example.test/bkng/release.pdf"
OLD_URL = "reindex_subdir:release.pdf"
PAYLOAD = b"%PDF-1.4\nsynthetic exact source bytes"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


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
