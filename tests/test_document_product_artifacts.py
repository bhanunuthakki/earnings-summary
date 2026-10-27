from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from llm_artifact_store import (
    DocumentProductEnvelope,
    DocumentProductGeneration,
    DocumentProductKey,
    DocumentProductStoreError,
    DocumentProductVerificationReceipt,
    compute_input_sha256,
    historical_versions,
    persist_document_generated_strict,
    read_artifact,
    read_current,
    read_document_current,
)
from models.runs import StageName, StageStatus
from pipeline.run_accounting import (
    end_run_owned,
    make_pipeline_key,
    record_artifact_stage_owned,
    start_run,
)

_NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
_RAW_BYTES = b"synthetic quarterly source"
_RAW_SHA = hashlib.sha256(_RAW_BYTES).hexdigest()


def test_artifact_stage_receipts_and_completion_share_the_caller_transaction(
    document_db: sqlite3.Connection,
) -> None:
    """Use real migrated SQL for artifact, both journals, and attempt rollback."""
    document_db.row_factory = sqlite3.Row
    inputs = {"document_version_id": "document-q1", "product": "transcript_summary"}
    pipeline_key = make_pipeline_key("test-transcript-summary", ["SYNTH"], inputs)
    run_id = start_run(document_db, "test-transcript-summary", ["SYNTH"], invocation_inputs=inputs)
    document_db.execute("BEGIN IMMEDIATE")
    artifact = persist_document_generated_strict(document_db, _envelope(), verifier=_test_verifier)

    def record(stage: StageName, projection_sha256: str | None = None) -> None:
        record_artifact_stage_owned(
            document_db,
            run_id=run_id,
            pipeline_key=pipeline_key,
            ticker="SYNTH",
            artifact_id=artifact.artifact_id,
            artifact_output_sha256=artifact.output_sha256,
            stage=stage,
            projection_sha256=projection_sha256,
        )

    with pytest.raises(ValueError, match="exact body projection"):
        record(StageName.PUBLISH)
    record(StageName.SYNTHESIZE)
    record(StageName.PUBLISH, artifact.body_sha256)
    end_run_owned(document_db, run_id=run_id, pipeline_key=pipeline_key, status=StageStatus.OK)
    rows = document_db.execute(
        "SELECT artifact_id,receipt_json,receipt_sha256 FROM pipeline_stage_transitions "
        "WHERE attempt_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    assert len(rows) == 2
    assert all(row["artifact_id"] == artifact.artifact_id for row in rows)
    assert all(
        hashlib.sha256(row["receipt_json"].encode()).hexdigest() == row["receipt_sha256"]
        for row in rows
    )
    assert (
        document_db.execute(
            "SELECT COUNT(*) FROM stage_transitions WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 2
    )
    assert document_db.in_transaction
    document_db.rollback()
    assert (
        document_db.execute("SELECT COUNT(*) FROM llm_artifacts WHERE scope='document'").fetchone()[
            0
        ]
        == 0
    )
    assert (
        document_db.execute(
            "SELECT COUNT(*) FROM pipeline_stage_transitions WHERE attempt_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        document_db.execute(
            "SELECT status FROM pipeline_attempts WHERE attempt_id=?", (run_id,)
        ).fetchone()[0]
        == "in_progress"
    )


@pytest.fixture
def document_db(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> Generator[sqlite3.Connection, None, None]:
    path = migrated_db(tmp_path / "document-product.db")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _seed_document(connection, document_version_id="document-q1")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _seed_document(connection: sqlite3.Connection, *, document_version_id: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO issuer_entities "
        "(issuer_id,idempotency_key,entity_kind,created_at) VALUES (?,?,?,?)",
        ("issuer-synthetic", "issuer-synthetic-key", "operating_company", _NOW.isoformat()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO evidence_content_blobs "
        "(sha256,byte_size,media_type,storage_uri,recorded_at) VALUES (?,?,?,?,?)",
        (_RAW_SHA, len(_RAW_BYTES), "text/plain", "synthetic://document", _NOW.isoformat()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,"
        "retrieved_at,retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            f"observation-{document_version_id}",
            f"observation-key-{document_version_id}",
            "issuer_publisher",
            "https://synthetic.invalid/q1",
            _RAW_SHA,
            _NOW.isoformat(),
            _NOW.isoformat(),
            "1" * 64,
            "test-collector@1",
        ),
    )
    connection.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,blob_sha256,"
        "issuer_id,ticker,document_type,form_type,period_start,period_end,language,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            document_version_id,
            f"SYNTH:quarterly:{document_version_id}",
            1,
            f"observation-{document_version_id}",
            _RAW_SHA,
            "issuer-synthetic",
            "SYNTH",
            "transcript",
            "earnings_call_transcript",
            "2026-01-01",
            "2026-03-31",
            "en",
            _NOW.isoformat(),
        ),
    )


def _envelope(
    *,
    document_version_id: str = "document-q1",
    body: bytes = b"Verified summary body.",
    input_marker: str = "source-text-v1",
) -> DocumentProductEnvelope:
    key = DocumentProductKey(
        issuer_id="issuer-synthetic",
        document_version_id=document_version_id,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        period_type="Q1",
        fiscal_year=2026,
        fiscal_quarter=1,
        product="transcript_summary",
    )
    prompt_bytes = b"Summarize the selected source evidence."
    prompt_sha = hashlib.sha256(prompt_bytes).hexdigest()
    manifest = {
        "anchors_context": {"context_sha256": "2" * 64},
        "canonical_issuer_binding": {
            "recorded_issuer_id": "issuer-synthetic",
            "canonical_issuer_id": "issuer-synthetic",
            "binding_revision_id": None,
            "binding_kind": "native",
        },
        "consumed_text_sha256": [hashlib.sha256(input_marker.encode()).hexdigest()],
        "document": {
            "document_version_id": document_version_id,
            "raw_blob_sha256": _RAW_SHA,
        },
        "extraction": {
            "extractor_name": "synthetic-test-extractor",
            "extractor_version": "1",
            "config_sha256": "3" * 64,
            "limits": {"characters": 1000},
        },
        "fiscal_period_selection": {
            "issuer_id": key.issuer_id,
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "period_type": "Q1",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "selection_authority": "test-fixture-only",
            "selection_proof_sha256": "4" * 64,
        },
        "parent_artifacts": [],
        "prompt": {
            "prompt_version": "r3-test-prompt@1",
            "prompt_bytes_base64": base64.b64encode(prompt_bytes).decode("ascii"),
        },
        "routing": {"config_sha256": "5" * 64, "capability_profile": "test"},
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_sha = compute_input_sha256(
        prompt_version="r3-test-prompt@1", cache_inputs=[manifest_json]
    )
    body_sha = hashlib.sha256(body).hexdigest()
    receipt = {
        "schema_version": "test-verification-receipt@1",
        "disposition": "accepted",
        "selected_corpus_sha256": "6" * 64,
        "claim_map_sha256": "7" * 64,
        "output_sha256": body_sha,
    }
    receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    return DocumentProductEnvelope(
        key=key,
        generation=DocumentProductGeneration(
            input_manifest_json=manifest_json,
            input_sha256=input_sha,
            generation_id=str(uuid.uuid4()),
            generated_at=_NOW.isoformat(),
            model="synthetic-model",
            backend="synthetic-backend",
            prompt_version="r3-test-prompt@1",
            prompt_sha256=prompt_sha,
        ),
        body_bytes=body,
        body_sha256=body_sha,
        verification=DocumentProductVerificationReceipt(
            receipt_json=receipt_json,
            receipt_sha256=hashlib.sha256(receipt_json.encode()).hexdigest(),
        ),
    )


def _test_verifier(_connection: sqlite3.Connection, envelope: DocumentProductEnvelope) -> None:
    """Storage-boundary stub; semantic qualification belongs to the root adapter."""
    receipt = json.loads(envelope.verification.receipt_json)
    if receipt.get("output_sha256") != envelope.body_sha256:
        raise ValueError("receipt output differs from summary body")
    if receipt.get("disposition") != "accepted":
        raise ValueError("test receipt is not accepted")


def test_document_product_persists_exact_body_and_reverifies_current_read(
    document_db: sqlite3.Connection,
) -> None:
    envelope = _envelope()
    original_factory = document_db.row_factory
    document_db.execute("BEGIN IMMEDIATE")

    stored = persist_document_generated_strict(document_db, envelope, verifier=_test_verifier)

    assert document_db.in_transaction
    assert document_db.row_factory is original_factory
    assert stored.body_bytes == envelope.body_bytes
    assert stored.key == envelope.key
    document_db.commit()

    current = read_document_current(
        document_db,
        envelope.key,
        expected_input_sha256=envelope.generation.input_sha256,
        verifier=_test_verifier,
    )
    database_path = Path(str(document_db.execute("PRAGMA database_list").fetchone()[2]))

    assert current == stored
    assert read_artifact(stored.artifact_id, db_path=database_path) is None
    assert (
        read_current(
            ticker="SYNTH",
            purpose="transcript_summary",
            fiscal_period="FY2026Q1",
            scope="document",
            conn=document_db,
        )
        is None
    )
    assert (
        historical_versions(
            document_db,
            ticker="SYNTH",
            purpose="transcript_summary",
            fiscal_period="FY2026Q1",
            scope="document",
        )
        == []
    )


def test_document_products_with_same_fiscal_period_do_not_supersede_each_other(
    document_db: sqlite3.Connection,
) -> None:
    second = "document-q1-second"
    _seed_document(document_db, document_version_id=second)
    document_db.commit()
    first_envelope = _envelope()
    second_envelope = _envelope(document_version_id=second)
    document_db.execute("BEGIN IMMEDIATE")
    first = persist_document_generated_strict(document_db, first_envelope, verifier=_test_verifier)
    second_artifact = persist_document_generated_strict(
        document_db, second_envelope, verifier=_test_verifier
    )
    document_db.commit()

    assert first.artifact_id != second_artifact.artifact_id
    assert document_db.execute(
        "SELECT superseded_by_id FROM llm_artifacts WHERE id=?", (first.artifact_id,)
    ).fetchone() == (None,)
    assert document_db.execute(
        "SELECT superseded_by_id FROM llm_artifacts WHERE id=?", (second_artifact.artifact_id,)
    ).fetchone() == (None,)


def test_document_product_versions_supersede_only_forward_and_exactly(
    document_db: sqlite3.Connection,
) -> None:
    first_envelope = _envelope()
    second_envelope = _envelope(body=b"Revised verified summary.", input_marker="source-text-v2")
    document_db.execute("BEGIN IMMEDIATE")
    first = persist_document_generated_strict(document_db, first_envelope, verifier=_test_verifier)
    second = persist_document_generated_strict(
        document_db, second_envelope, verifier=_test_verifier
    )
    document_db.commit()

    assert second.artifact_id > first.artifact_id
    assert document_db.execute(
        "SELECT superseded_by_id FROM llm_artifacts WHERE id=?", (first.artifact_id,)
    ).fetchone() == (second.artifact_id,)
    with pytest.raises(sqlite3.IntegrityError, match="document artifact envelope and identity"):
        document_db.execute(
            "UPDATE llm_artifacts SET superseded_by_id=NULL WHERE id=?", (first.artifact_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot delete a document successor"):
        document_db.execute("DELETE FROM llm_artifacts WHERE id=?", (second.artifact_id,))


def test_strict_document_persistence_requires_owned_transaction_and_rolls_back_savepoint(
    document_db: sqlite3.Connection,
) -> None:
    envelope = _envelope()
    with pytest.raises(DocumentProductStoreError, match="requires caller transaction"):
        persist_document_generated_strict(document_db, envelope, verifier=_test_verifier)

    document_db.execute("CREATE TEMP TABLE caller_work (value INTEGER)")
    document_db.execute("BEGIN IMMEDIATE")
    document_db.execute("INSERT INTO caller_work VALUES (7)")
    previous_factory = document_db.row_factory

    def reject_verification(
        _connection: sqlite3.Connection, _envelope: DocumentProductEnvelope
    ) -> None:
        raise ValueError("semantic verification deferred")

    with pytest.raises(ValueError, match="semantic verification deferred"):
        persist_document_generated_strict(document_db, envelope, verifier=reject_verification)

    assert document_db.in_transaction
    assert document_db.row_factory is previous_factory
    assert document_db.execute("SELECT value FROM caller_work").fetchall() == [(7,)]
    assert document_db.execute(
        "SELECT COUNT(*) FROM llm_artifacts WHERE scope='document'"
    ).fetchone() == (0,)
    document_db.rollback()


def test_readback_rejects_body_tampering_even_if_outer_hash_is_recomputed(
    document_db: sqlite3.Connection,
) -> None:
    envelope = _envelope()
    document_db.execute("BEGIN IMMEDIATE")
    stored = persist_document_generated_strict(document_db, envelope, verifier=_test_verifier)
    document_db.commit()
    content_json = str(
        document_db.execute(
            "SELECT content_json FROM llm_artifacts WHERE id=?", (stored.artifact_id,)
        ).fetchone()[0]
    )
    payload = json.loads(content_json)
    payload["body_base64"] = base64.b64encode(b"tampered body").decode("ascii")
    changed_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changed_sha = hashlib.sha256(changed_json.encode()).hexdigest()
    document_db.execute("DROP TRIGGER trg_llm_artifact_document_immutable")
    document_db.execute(
        "UPDATE llm_artifacts SET content_json=?, output_sha256=? WHERE id=?",
        (changed_json, changed_sha, stored.artifact_id),
    )
    document_db.commit()

    with pytest.raises(DocumentProductStoreError, match="body checksum"):
        read_document_current(
            document_db,
            envelope.key,
            expected_input_sha256=envelope.generation.input_sha256,
            verifier=_test_verifier,
        )


def test_document_artifact_and_linked_stage_receipt_are_immutable(
    document_db: sqlite3.Connection,
) -> None:
    envelope = _envelope()
    document_db.execute("BEGIN IMMEDIATE")
    stored = persist_document_generated_strict(document_db, envelope, verifier=_test_verifier)
    document_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="document artifact envelope"):
        document_db.execute(
            "UPDATE llm_artifacts SET input_sha256=? WHERE id=?",
            ("8" * 64, stored.artifact_id),
        )

    document_db.execute(
        "INSERT INTO pipeline_runs (pipeline_key,directive,ticker_scope,first_started_at) "
        "VALUES ('document-receipt-test','test','SYNTH',?)",
        (_NOW.isoformat(),),
    )
    document_db.execute(
        "INSERT INTO pipeline_attempts (attempt_id,pipeline_key,started_at,status) "
        "VALUES ('document-receipt-attempt','document-receipt-test',?,'in_progress')",
        (_NOW.isoformat(),),
    )
    receipt_json = json.dumps(
        {"artifact_id": stored.artifact_id, "schema_version": "stage-receipt@1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt_sha = hashlib.sha256(receipt_json.encode()).hexdigest()
    document_db.execute(
        "INSERT INTO pipeline_stage_transitions "
        "(attempt_id,ticker,stage,status,started_at,artifact_id,receipt_json,receipt_sha256) "
        "VALUES ('document-receipt-attempt','SYNTH','PUBLISH','ok',?,?,?,?)",
        (_NOW.isoformat(), stored.artifact_id, receipt_json, receipt_sha),
    )
    with pytest.raises(sqlite3.IntegrityError, match="stage receipt is immutable"):
        document_db.execute(
            "UPDATE pipeline_stage_transitions SET artifact_id=NULL, receipt_json=NULL, "
            "receipt_sha256=NULL WHERE attempt_id='document-receipt-attempt'"
        )


def test_current_document_read_fails_closed_on_malformed_expiry(
    document_db: sqlite3.Connection,
) -> None:
    envelope = _envelope()
    document_db.execute("BEGIN IMMEDIATE")
    stored = persist_document_generated_strict(document_db, envelope, verifier=_test_verifier)
    document_db.commit()
    document_db.execute("DROP TRIGGER trg_llm_artifact_document_immutable")
    document_db.execute(
        "UPDATE llm_artifacts SET expires_at='invalid-time' WHERE id=?",
        (stored.artifact_id,),
    )
    document_db.commit()

    with pytest.raises(DocumentProductStoreError, match="expiry timestamp is malformed"):
        read_document_current(
            document_db,
            envelope.key,
            expected_input_sha256=envelope.generation.input_sha256,
            verifier=_test_verifier,
        )
