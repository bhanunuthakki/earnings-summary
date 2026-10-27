"""Exact-document completion for retained transcript summaries.

This module accepts one canonical document version. It does not discover
documents from quarter filenames or the legacy document index.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Protocol, cast

from db_paths import db_path_context
from llm.anchors import (
    compose_anchor_block,
    load_bear_anchor,
    load_ir_anchor,
    load_thesis_anchor,
)
from llm.cli import LLMCallResult, PreparedLLMCall, execute_prepared_call, prepare_llm_call
from llm.prompt_versions import prompt_version_for
from llm_artifact_store import (
    DocumentPeriodType,
    DocumentProductArtifact,
    DocumentProductEnvelope,
    DocumentProductGeneration,
    DocumentProductKey,
    DocumentProductVerificationReceipt,
    compute_input_sha256,
    persist_document_generated_strict,
    read_document_current,
)
from llm_client import summary_prompt
from models.documents import DocType
from models.runs import StageName, StageStatus
from pipeline.issuer_document_inventory import resolve_registered_document_period
from pipeline.run_accounting import (
    assert_attempt_owned,
    end_run_owned,
    make_pipeline_key,
    record_artifact_stage_owned,
    start_run,
)
from provenance.evidence_native_candidates import (
    read_verified_local_evidence_bytes,
    select_evidence_native_candidates_by_id,
)
from provenance.fulltext_backfill import project_source_text
from provenance.fulltext_extractor_identity import (
    resolve_fulltext_extractor_identity,
    select_approved_fulltext_run,
)
from provenance.immutable_artifact import publish_bytes_no_clobber, read_stable_artifact
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class TranscriptSummaryVerifier(Protocol):
    """Required existing-owner semantic audit and strict artifact validator."""

    def __call__(self, conn: sqlite3.Connection, envelope: DocumentProductEnvelope, /) -> None: ...

    def preflight(self, key: DocumentProductKey, input_manifest_json: str, /) -> None: ...

    def audit(
        self,
        key: DocumentProductKey,
        generation: DocumentProductGeneration,
        body_bytes: bytes,
        /,
    ) -> DocumentProductVerificationReceipt: ...


PreparedCall = Callable[..., PreparedLLMCall]
ExecuteCall = Callable[..., LLMCallResult]


@dataclass(frozen=True, slots=True)
class TranscriptSummaryResult:
    status: str
    document_version_id: str
    artifact_id: int | None = None
    projection_path: Path | None = None


class TranscriptSummaryError(RuntimeError):
    """A stable fail-closed transcript summary error."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _manifest_int(manifest: dict[str, object], field: str) -> int:
    value = manifest[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TranscriptSummaryError(f"{field}_invalid")
    return value


def _load_anchor_context(state_root: Path, ticker: str, db_path: Path) -> dict[str, str]:
    with db_path_context(db_path):
        return {
            "thesis": load_thesis_anchor(state_root, ticker),
            "bear_case": load_bear_anchor(state_root, ticker),
            "investor_relations": load_ir_anchor(state_root, ticker),
        }


def resolve_transcript_summary_source(
    conn: sqlite3.Connection,
    document_version_id: str,
    *,
    state_root: Path,
) -> tuple[dict[str, object], str, str | None]:
    registered = resolve_registered_document_period(conn, document_version_id)
    witness = registered.request.fiscal_period_witness
    if witness is None:
        raise TranscriptSummaryError("registered_document_has_no_fiscal_period_witness")
    rows = conn.execute(
        "SELECT ticker,document_type FROM v_evidence_document_current WHERE document_version_id=?",
        (document_version_id,),
    ).fetchall()
    if len(rows) != 1:
        raise TranscriptSummaryError("registered_document_missing_or_ambiguous")
    ticker, document_type = str(rows[0][0]).upper(), str(rows[0][1]).casefold()
    if document_type not in {
        "transcript",  # historical registration vocabulary
        DocType.IR_TRANSCRIPT.value,
        DocType.EARNINGS_CALL_TRANSCRIPT.value,
    }:
        raise TranscriptSummaryError("document_version_is_not_a_transcript")
    candidates = select_evidence_native_candidates_by_id(
        conn, document_version_ids=(document_version_id,), include_legacy=True
    )
    if len(candidates) != 1:
        raise TranscriptSummaryError("registered_document_missing_or_ambiguous")
    candidate = candidates[0]
    raw = read_verified_local_evidence_bytes(
        conn,
        storage_uri=candidate.storage_uri,
        expected_sha256=candidate.blob_sha256,
        expected_byte_size=candidate.byte_size,
        allowed_roots=(state_root.resolve(),),
        document_version_id=document_version_id,
    )
    extractor = resolve_fulltext_extractor_identity(candidate.source_ref, candidate.media_type)
    extraction_run_id = select_approved_fulltext_run(
        conn,
        document_version_id,
        extractor,
    )
    full_text = project_source_text(raw.raw_bytes, candidate.source_ref, candidate.media_type)
    return (
        {
            "document_version_id": document_version_id,
            "issuer_id": witness.issuer_id,
            "ticker": ticker,
            "document_type": document_type,
            "source_ref": candidate.source_ref,
            "blob_sha256": candidate.blob_sha256,
            "raw_byte_size": candidate.byte_size,
            "binding_kind": registered.binding_kind,
            "recorded_issuer_id": registered.recorded_issuer_id,
            "binding_revision_id": registered.binding_revision_id,
            "period_start": _date(registered.period_start),
            "period_end": registered.request.period_end.isoformat(),
            "fiscal_year": registered.request.fiscal_year,
            "fiscal_quarter": registered.request.fiscal_quarter,
            "fiscal_selection_request": {
                "document_version_id": document_version_id,
                "fiscal_year": None,
                "fiscal_quarter": None,
            },
            "fiscal_selection_proof": {
                "request": registered.request.model_dump(mode="json"),
                "binding_kind": registered.binding_kind,
                "binding_revision_id": registered.binding_revision_id,
            },
            "extractor": {
                "name": extractor.name,
                "code_version": extractor.code_version,
                "config_sha256": extractor.config_sha256,
                "approved_extraction_run_id": extraction_run_id,
                "selection_cutoff": None,
            },
            "fulltext_sha256": _sha256(full_text.encode("utf-8")),
            "consumed_fulltext_sha256": _sha256(full_text.encode("utf-8")),
            "fulltext_characters": len(full_text),
            "prompt_text_limit": None,
        },
        full_text,
        candidate.source_ref,
    )


def _projection_path(state_root: Path, artifact: DocumentProductArtifact) -> Path:
    return (
        state_root
        / "transcript_summaries"
        / f"artifact-{artifact.artifact_id}-{artifact.body_sha256}.md"
    )


def _publish_projection(state_root: Path, artifact: DocumentProductArtifact) -> tuple[Path, str]:
    try:
        body = artifact.body_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TranscriptSummaryError("transcript_summary_body_is_not_utf8") from exc
    if not body:
        raise TranscriptSummaryError("transcript_summary_body_is_empty")
    path = _projection_path(state_root, artifact)
    payload = artifact.body_bytes
    publish_bytes_no_clobber(path, payload)
    snapshot, readback = read_stable_artifact(path, allowed_root=state_root.resolve())
    if readback != payload or snapshot.file_sha256 != artifact.body_sha256:
        raise TranscriptSummaryError("transcript_summary_projection_readback_mismatch")
    return path, snapshot.file_sha256


def _complete_publication(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    pipeline_key: str,
    ticker: str,
    artifact: DocumentProductArtifact,
    projection_sha256: str,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert_attempt_owned(conn, run_id=run_id, pipeline_key=pipeline_key)
        record_artifact_stage_owned(
            conn,
            run_id=run_id,
            pipeline_key=pipeline_key,
            ticker=ticker,
            stage=StageName.PUBLISH,
            artifact_id=artifact.artifact_id,
            artifact_output_sha256=artifact.output_sha256,
            projection_sha256=projection_sha256,
            period_end=datetime.combine(artifact.key.period_end, time.min),
        )
        end_run_owned(
            conn,
            run_id=run_id,
            pipeline_key=pipeline_key,
            status=StageStatus.OK,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@dataclass(frozen=True, slots=True)
class _SummaryInputs:
    source: dict[str, object]
    anchors: dict[str, str]
    prompt: str
    prepared: PreparedLLMCall
    manifest_json: str
    input_sha256: str
    key: DocumentProductKey


def _prepare_summary_inputs(
    conn: sqlite3.Connection,
    document_version_id: str,
    *,
    db_path: Path,
    state_root: Path,
    prepare_call: PreparedCall,
) -> _SummaryInputs:
    source_manifest, full_text, _source_ref = resolve_transcript_summary_source(
        conn, document_version_id, state_root=state_root
    )
    extractor_manifest = cast(dict[str, object], source_manifest["extractor"])
    ticker = str(source_manifest["ticker"])
    anchor_inputs = _load_anchor_context(state_root, ticker, db_path)
    anchor_block = compose_anchor_block(
        anchor_inputs["thesis"],
        anchor_inputs["bear_case"],
        anchor_inputs["investor_relations"],
    )
    consumed_text = full_text
    prompt = summary_prompt(consumed_text, anchor_block=anchor_block)
    prepared = prepare_call(
        prompt,
        purpose="transcript_summary",
        scope="document",
        db_path=db_path,
    )
    selection_proof = {
        "request": source_manifest["fiscal_selection_proof"],
        "binding_kind": source_manifest["binding_kind"],
        "binding_revision_id": source_manifest["binding_revision_id"],
    }
    selection_proof_sha256 = _sha256(_canonical_json(selection_proof).encode("utf-8"))
    fiscal_quarter = _manifest_int(source_manifest, "fiscal_quarter")
    manifest = {
        "schema_version": "transcript-summary-input@1",
        "document": {
            "document_version_id": document_version_id,
            "raw_blob_sha256": source_manifest["blob_sha256"],
            "source_ref": source_manifest["source_ref"],
            "raw_byte_size": source_manifest["raw_byte_size"],
        },
        "canonical_issuer_binding": {
            "recorded_issuer_id": source_manifest["recorded_issuer_id"],
            "canonical_issuer_id": source_manifest["issuer_id"],
            "binding_revision_id": source_manifest["binding_revision_id"],
            "binding_kind": source_manifest["binding_kind"],
        },
        "fiscal_period_selection": {
            "issuer_id": source_manifest["issuer_id"],
            "period_start": source_manifest["period_start"],
            "period_end": source_manifest["period_end"],
            "period_type": f"Q{fiscal_quarter}",
            "fiscal_year": source_manifest["fiscal_year"],
            "fiscal_quarter": fiscal_quarter,
            "selection_authority": "issuer_document_inventory.resolve_registered_document_period",
            "selection_proof_sha256": selection_proof_sha256,
            "selection_request": source_manifest["fiscal_selection_request"],
            "selection_proof": selection_proof,
        },
        "consumed_text_sha256": [source_manifest["consumed_fulltext_sha256"]],
        "extraction": {
            "extractor_name": extractor_manifest["name"],
            "extractor_version": extractor_manifest["code_version"],
            "config_sha256": extractor_manifest["config_sha256"],
            "approved_extraction_run_id": extractor_manifest["approved_extraction_run_id"],
            "selection_cutoff": extractor_manifest["selection_cutoff"],
            "limits": {"truncation": "none"},
            "fulltext_sha256": source_manifest["fulltext_sha256"],
        },
        "anchors_context": anchor_inputs,
        "parent_artifacts": [],
        "prompt": {
            "prompt_version": prepared.prompt_version,
            "prompt_bytes_base64": base64.b64encode(prepared.prompt.encode("utf-8")).decode(
                "ascii"
            ),
        },
        "routing": {
            "purpose": prepared.purpose,
            "scope": prepared.scope,
            "initial_model": prepared.model,
            "initial_backend": prepared.backend,
            "fallback_disabled": prepared.fallback_disabled,
            "config_sha256": prepared.routing_config_sha256,
            "capability_profile": asdict(prepared.capability_profile),
            "requested_backend": prepared.requested_backend,
        },
    }
    manifest_json = _canonical_json(manifest)
    prompt_version = prepared.prompt_version or prompt_version_for("transcript_summary")
    input_sha256 = compute_input_sha256(
        prompt_version=prompt_version,
        cache_inputs=[manifest_json],
    )
    period_end = date.fromisoformat(str(source_manifest["period_end"]))
    period_start_raw = source_manifest["period_start"]
    key = DocumentProductKey(
        issuer_id=str(source_manifest["issuer_id"]),
        document_version_id=document_version_id,
        period_start=(
            None if period_start_raw is None else date.fromisoformat(str(period_start_raw))
        ),
        period_end=period_end,
        period_type=cast(DocumentPeriodType, f"Q{source_manifest['fiscal_quarter']}"),
        fiscal_year=_manifest_int(source_manifest, "fiscal_year"),
        fiscal_quarter=fiscal_quarter,
        product="transcript_summary",
    )
    return _SummaryInputs(
        source_manifest, anchor_inputs, prompt, prepared, manifest_json, input_sha256, key
    )


def read_transcript_summary(
    conn: sqlite3.Connection,
    document_version_id: str,
    *,
    db_path: Path,
    state_root: Path,
    verifier: TranscriptSummaryVerifier,
) -> DocumentProductArtifact | None:
    """Rebuild current inputs and read one exact verified summary without writes."""
    inputs = _prepare_summary_inputs(
        conn,
        document_version_id,
        db_path=db_path,
        state_root=state_root,
        prepare_call=prepare_llm_call,
    )
    verifier.preflight(inputs.key, inputs.manifest_json)
    return read_document_current(
        conn,
        inputs.key,
        expected_input_sha256=inputs.input_sha256,
        verifier=verifier,
    )


def generate_transcript_summary(
    document_version_id: str,
    *,
    db_path: Path,
    state_root: Path,
    verifier: TranscriptSummaryVerifier,
    prepare_call: PreparedCall = prepare_llm_call,
    execute_call: ExecuteCall = execute_prepared_call,
) -> TranscriptSummaryResult:
    """Read, generate, audit, and complete one exact registered transcript."""
    if not document_version_id or document_version_id != document_version_id.strip():
        raise TranscriptSummaryError("document_version_id_invalid")
    database = db_path.expanduser().resolve(strict=True)
    source_root = state_root.expanduser().resolve(strict=True)

    with connect_sqlite(database, role=SQLiteConnectionRole.WRITER) as conn:
        inputs = _prepare_summary_inputs(
            conn,
            document_version_id,
            db_path=database,
            state_root=source_root,
            prepare_call=prepare_call,
        )
        source_manifest, anchor_inputs = inputs.source, inputs.anchors
        prompt, prepared = inputs.prompt, inputs.prepared
        manifest_json, input_sha256, key = inputs.manifest_json, inputs.input_sha256, inputs.key
        ticker = str(source_manifest["ticker"])
        verifier.preflight(key, manifest_json)
        existing = read_document_current(
            conn,
            key,
            expected_input_sha256=input_sha256,
            verifier=verifier,
        )
        logical_inputs = {
            "issuer_id": key.issuer_id,
            "document_version_id": key.document_version_id,
            "period_start": _date(key.period_start),
            "period_end": key.period_end.isoformat(),
            "period_type": key.period_type,
            "fiscal_year": key.fiscal_year,
            "fiscal_quarter": key.fiscal_quarter,
            "product": key.product,
        }
        pipeline_key = make_pipeline_key("transcript_summary", [ticker], logical_inputs)

        # A verified current artifact repairs a missing per-artifact projection
        # without making another model call. Force only supersedes an interrupted
        # attempt after the exact artifact has already passed its verifier.
        run_id = start_run(
            conn,
            "transcript_summary",
            [ticker],
            invocation_inputs=logical_inputs,
            force=existing is not None,
            deduplicate_completed=False,
        )
        if existing is not None:
            projection, projection_sha = _publish_projection(source_root, existing)
            _complete_publication(
                conn,
                run_id=run_id,
                pipeline_key=pipeline_key,
                ticker=ticker,
                artifact=existing,
                projection_sha256=projection_sha,
            )
            return TranscriptSummaryResult(
                "repaired_projection", document_version_id, existing.artifact_id, projection
            )

        # The call runs after start_run committed its reservation and outside a
        # SQLite transaction. The read-only source snapshot is rebuilt before
        # any artifact write, so changed bytes or anchors defer completion.
        result = execute_call(prepared, ticker=ticker, run_id=run_id)
        receipt = result.receipt
        if receipt.routing_config_sha256 != prepared.routing_config_sha256:
            raise TranscriptSummaryError("transcript_summary_routing_changed")
        generation = DocumentProductGeneration(
            input_manifest_json=manifest_json,
            input_sha256=input_sha256,
            generation_id=receipt.generation_id,
            generated_at=receipt.generated_at.isoformat(),
            model=receipt.model,
            backend=receipt.backend,
            prompt_version=prepared.prompt_version,
            prompt_sha256=receipt.prompt_sha256,
        )
        body = result.text.encode("utf-8")
        check_manifest, _check_text, _check_ref = resolve_transcript_summary_source(
            conn, document_version_id, state_root=source_root
        )
        if check_manifest != source_manifest:
            raise TranscriptSummaryError("transcript_summary_source_changed_during_generation")
        check_anchors = _load_anchor_context(source_root, ticker, database)
        if check_anchors != anchor_inputs:
            raise TranscriptSummaryError("transcript_summary_context_changed_during_generation")
        check_prepared = prepare_call(
            prompt,
            purpose="transcript_summary",
            scope="document",
            db_path=database,
        )
        if check_prepared != prepared:
            raise TranscriptSummaryError("transcript_summary_call_configuration_changed")
        verification = verifier.audit(key, generation, body)
        envelope = DocumentProductEnvelope(
            key=key,
            generation=generation,
            body_bytes=body,
            body_sha256=_sha256(body),
            verification=verification,
        )

        conn.execute("BEGIN IMMEDIATE")
        try:
            assert_attempt_owned(conn, run_id=run_id, pipeline_key=pipeline_key)
            checked_source, _checked_text, _checked_ref = resolve_transcript_summary_source(
                conn, document_version_id, state_root=source_root
            )
            if checked_source != source_manifest:
                raise TranscriptSummaryError("transcript_summary_source_changed_before_persist")
            locked_anchors = _load_anchor_context(source_root, ticker, database)
            if locked_anchors != anchor_inputs:
                raise TranscriptSummaryError("transcript_summary_context_changed_before_persist")
            locked_prepared = prepare_call(
                prompt,
                purpose="transcript_summary",
                scope="document",
                db_path=database,
            )
            if locked_prepared != prepared:
                raise TranscriptSummaryError("transcript_summary_call_configuration_changed")
            artifact = persist_document_generated_strict(conn, envelope, verifier=verifier)
            record_artifact_stage_owned(
                conn,
                run_id=run_id,
                pipeline_key=pipeline_key,
                ticker=ticker,
                stage=StageName.SYNTHESIZE,
                artifact_id=artifact.artifact_id,
                artifact_output_sha256=artifact.output_sha256,
                period_end=datetime.combine(artifact.key.period_end, time.min),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        projection, projection_sha = _publish_projection(source_root, artifact)
        _complete_publication(
            conn,
            run_id=run_id,
            pipeline_key=pipeline_key,
            ticker=ticker,
            artifact=artifact,
            projection_sha256=projection_sha,
        )
        return TranscriptSummaryResult(
            "generated", document_version_id, artifact.artifact_id, projection
        )
