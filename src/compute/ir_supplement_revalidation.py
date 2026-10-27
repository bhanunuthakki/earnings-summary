"""Exact historical IR supplement processing and source-only candidate recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from compute.ir_supplement_capture import (
    ChunkResult,
    SupplementCapture,
    SupplementExchange,
)
from llm.structured import parse_json_payload
from llm_artifact_store import (
    Artifact,
    UpsertRequest,
    compute_input_sha256,
    read_current,
    require_historical_artifact,
    upsert,
)
from log_redact import redact
from models.runs import StageStatus
from pipeline.run_accounting import (
    PipelineRunSuppressedError,
    end_run,
    make_pipeline_key,
    start_run,
)

REVALIDATION_PRODUCER_VERSION = "ir-supplement-native-continuation-v16-table-cell-boundaries"
REVALIDATION_PROCESSOR_VERSION = "ir-supplement-revalidation-v18-percent-cell-boundaries"
REVALIDATION_PURPOSE = "ir_supplement_revalidation"


class RevalidationSection(BaseModel):
    """One pinned, historical producer artifact for one native section."""

    model_config = ConfigDict(extra="forbid", strict=True)

    page: int = Field(gt=0)
    start: int = Field(ge=0)
    length: int = Field(gt=0)
    artifact_id: int = Field(gt=0)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    llm_call_id: int = Field(gt=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RevalidationManifest(BaseModel):
    """Closed V16 input set. It cannot select current or inferred artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ticker: str = Field(min_length=1)
    document_id: int = Field(gt=0)
    document_version_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_attempt_id: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    requested_backend: str = Field(min_length=1)
    period_end: str = Field(min_length=10)
    max_input_chars: int = Field(gt=1)
    max_facts_per_doc: int = Field(gt=1)
    max_chunks: int = Field(gt=1)
    max_structured_calls: int = Field(gt=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_identity: str = Field(min_length=1)
    sections: list[RevalidationSection] = Field(min_length=1, max_length=256)


class RevalidationReceipt(BaseModel):
    """New processing only; original producing identity remains V16."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["ir-supplement-revalidation.v1"] = "ir-supplement-revalidation.v1"
    artifact_id: int
    attempt_id: str
    document_version_id: str
    source_sha256: str
    manifest_sha256: str
    producing_capture_version: str
    validator_version: str
    validator_code_sha256: str
    section_artifact_ids: list[int]
    section_count: int
    page_count: int
    row_count: int
    status: Literal["processed_source_only"] = "processed_source_only"
    provider_calls: Literal[0] = 0
    financial_writes: Literal[0] = 0


@dataclass(frozen=True)
class RevalidatedCapture:
    """Strict source-only consumer view with both producing and processing IDs."""

    receipt: RevalidationReceipt
    page_rows: list[tuple[int, list[dict[str, object]]]]
    exchanges: list[SupplementExchange]


def _historical_section(
    capture: SupplementCapture,
    pin: RevalidationSection,
    body: str,
) -> tuple[Artifact, ChunkResult, list[dict[str, object]]]:
    """Use the original call and this owner's source checks; never infer an exchange."""
    scope = f"ir_supplement:{capture.document_id}:page:{pin.page}:section:{pin.start}:{pin.length}"
    prompt = capture.prompt(capture.section_text(pin.page, pin.start, body))
    expected_input = compute_input_sha256(
        prompt_version=REVALIDATION_PRODUCER_VERSION,
        cache_inputs=capture.section_inputs(pin.page, pin.start, prompt),
    )
    artifact = require_historical_artifact(capture.conn, pin.artifact_id)
    if (
        artifact.purpose != "kpi_summary_enumerate"
        or artifact.scope != scope
        or artifact.ticker != capture.ticker
        or artifact.prompt_version != REVALIDATION_PRODUCER_VERSION
        or artifact.input_sha256 != pin.input_sha256
        or expected_input != pin.input_sha256
        or artifact.output_sha256 != pin.output_sha256
        or artifact.source_doc_ids != [capture.document_id]
        or artifact.llm_call_id != pin.llm_call_id
        or artifact.superseded_by_id is not None
    ):
        raise ValueError("IR historical section identity is unavailable")
    try:
        original = ChunkResult.model_validate(artifact.content_json)
    except ValidationError:
        raise ValueError("IR historical section body is malformed") from None
    if (
        original.status not in {"processed", "rejected"}
        or artifact.model != original.exchange.model
        or original.exchange.llm_call_id != pin.llm_call_id
        or original.exchange.response_sha256 != pin.response_sha256
        or original.exchange.run_id == ""
    ):
        raise ValueError("IR historical section call is unavailable")
    capture.validated_attempts(original, prompt)
    rows = capture.validate_rows(parse_json_payload(original.exchange.raw_response, expect="array"))
    if len(rows) >= capture.max_facts or (original.status == "processed" and original.rows != rows):
        raise ValueError("IR historical section population is unavailable")
    if original.status == "rejected" and original.rows:
        raise ValueError("IR rejected historical section contains processed rows")
    processed = ChunkResult(
        status="processed",
        rows=rows,
        exchange=original.exchange,
        attempts=original.attempts,
    )
    capture.validate_result(processed, body, prompt)
    return artifact, original, rows


def _revalidation_manifest_sha(manifest: RevalidationManifest) -> str:
    return hashlib.sha256(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _require_producer_invocation(
    capture: SupplementCapture, manifest: RevalidationManifest
) -> None:
    """Bind caller-supplied V16 route/config to the immutable original run key."""
    if (
        capture.model != manifest.requested_model
        or capture.backend != manifest.requested_backend
        or capture.max_input_chars != manifest.max_input_chars
        or capture.max_facts != manifest.max_facts_per_doc
        or capture.max_chunks != manifest.max_chunks
        or capture.max_structured_calls != manifest.max_structured_calls
        or capture.schema_sha256 != manifest.schema_sha256
        or capture.projection_identity != manifest.projection_identity
        or hashlib.sha256(capture.prompt("").encode()).hexdigest() != manifest.prompt_sha256
    ):
        raise ValueError("IR historical producer configuration differs")
    inputs = {
        "document_id": manifest.document_id,
        "source_sha256": manifest.source_sha256,
        "period_end": manifest.period_end,
        "model": manifest.requested_model,
        "backend": manifest.requested_backend,
        "max_input_chars": manifest.max_input_chars,
        "max_facts_per_doc": manifest.max_facts_per_doc,
        "source_scope": "direct_pdf_rich",
        "max_chunks": manifest.max_chunks,
        "max_structured_calls": manifest.max_structured_calls,
        "continuation_version": "v16-table-cell-boundaries-or-v17-reviewed-pages-table-cell-boundaries",
        "prompt_sha256": manifest.prompt_sha256,
        "schema_sha256": manifest.schema_sha256,
        "projection_identity": manifest.projection_identity,
    }
    expected = make_pipeline_key("capture_ir_supplement_sections", [manifest.ticker], inputs)
    stored = capture.conn.execute(
        "SELECT pipeline_key FROM pipeline_attempts WHERE attempt_id=?",
        (manifest.failed_attempt_id,),
    ).fetchone()
    if stored is None or stored[0] != expected:
        raise ValueError("IR historical producer invocation is unavailable")


def _read_revalidation_receipt(
    conn: sqlite3.Connection, attempt_id: str, document_id: int
) -> RevalidationReceipt:
    artifact = read_current(
        ticker=None,
        purpose=REVALIDATION_PURPOSE,
        scope=f"ir_revalidation:{document_id}:attempt:{attempt_id}:complete",
        conn=conn,
    )
    if (
        artifact is None
        or artifact.dirty
        or artifact.model is not None
        or artifact.llm_call_id is not None
        or artifact.prompt_version != REVALIDATION_PROCESSOR_VERSION
        or artifact.source_doc_ids != [document_id]
    ):
        raise ValueError("IR revalidation completion receipt is unavailable")
    if not isinstance(artifact.content_json, dict):
        raise ValueError("IR revalidation completion receipt is malformed")
    if "artifact_id" in artifact.content_json:
        raise ValueError("IR revalidation completion receipt has an invalid identity")
    try:
        receipt = RevalidationReceipt.model_validate(
            {**cast("dict[str, object]", artifact.content_json), "artifact_id": artifact.id}
        )
    except ValidationError:
        raise ValueError("IR revalidation completion receipt is malformed") from None
    if receipt.artifact_id != artifact.id or receipt.attempt_id != attempt_id:
        raise ValueError("IR revalidation completion identity differs")
    if artifact.parent_artifact_ids != receipt.section_artifact_ids or receipt.section_count != len(
        receipt.section_artifact_ids
    ):
        raise ValueError("IR revalidation section set differs")
    return receipt


def revalidate_historical_sections(
    capture: SupplementCapture,
    raw_bytes: bytes,
    manifest: RevalidationManifest,
    *,
    validator_code_sha256: str,
    before_commit: Callable[[], None] | None = None,
    retain_candidates: Callable[[RevalidationReceipt], None] | None = None,
) -> RevalidationReceipt:
    """Reprocess a complete V16 native document with V18; never call a model."""
    if capture.conn.in_transaction:
        raise ValueError("IR revalidation requires an idle connection")
    if (
        manifest.ticker != capture.ticker
        or manifest.document_id != capture.document_id
        or manifest.document_version_id != capture.document_version_id
        or manifest.source_sha256 != capture.source_sha256
        or len(validator_code_sha256) != 64
        or any(char not in "0123456789abcdef" for char in validator_code_sha256)
    ):
        raise ValueError("IR revalidation source or validator identity differs")
    planned = capture.plan_sections(raw_bytes)
    if capture.reviewed_non_financial_pages:
        raise ValueError("IR V16 revalidation review scope is unavailable")
    capture.capture_version = REVALIDATION_PRODUCER_VERSION
    actual = [(page, start, len(body)) for page, start, body in planned]
    pinned = [(pin.page, pin.start, pin.length) for pin in manifest.sections]
    if (
        not actual
        or actual != pinned
        or len({pin.artifact_id for pin in manifest.sections}) != len(pinned)
    ):
        raise ValueError("IR revalidation complete section population differs")
    old_attempt = capture.conn.execute(
        "SELECT directive,status FROM ingestion_runs WHERE run_id=?",
        (manifest.failed_attempt_id,),
    ).fetchone()
    if old_attempt is None or tuple(old_attempt) != (
        "capture_ir_supplement_sections",
        StageStatus.FAILED.value,
    ):
        raise ValueError("IR historical failed attempt is unavailable")
    _require_producer_invocation(capture, manifest)

    def validate_all() -> list[tuple[Artifact, ChunkResult, list[dict[str, object]]]]:
        validated = [
            _historical_section(capture, pin, body)
            for pin, (_page, _start, body) in zip(manifest.sections, planned, strict=True)
        ]
        if any(
            original.exchange.run_id != manifest.failed_attempt_id for _, original, _ in validated
        ):
            raise ValueError("IR historical governed run differs")
        return validated

    validated = validate_all()
    manifest_sha = _revalidation_manifest_sha(manifest)
    try:
        attempt_id = start_run(
            capture.conn,
            directive="revalidate_ir_supplement_sections",
            ticker_scope=[capture.ticker],
            invocation_inputs={
                "manifest_sha256": manifest_sha,
                "validator_code_sha256": validator_code_sha256,
                "validator_version": REVALIDATION_PROCESSOR_VERSION,
                "source_sha256": manifest.source_sha256,
            },
            deduplicate_completed=True,
        )
    except PipelineRunSuppressedError as exc:
        if exc.status is not StageStatus.OK:
            raise
        receipt = _read_revalidation_receipt(capture.conn, exc.attempt_id, capture.document_id)
        if (
            receipt.source_sha256 != manifest.source_sha256
            or receipt.validator_code_sha256 != validator_code_sha256
            or receipt.section_count != len(planned)
        ):
            raise ValueError("IR revalidation replay receipt differs") from None
        read_revalidated_capture(capture, raw_bytes, manifest, receipt)
        return receipt
    try:
        capture.conn.execute("BEGIN IMMEDIATE")
        validated = validate_all()
        section_ids: list[int] = []
        total_rows = 0
        for pin, (artifact, original, rows) in zip(manifest.sections, validated, strict=True):
            total_rows += len(rows)
            content = {
                "schema_version": "ir-supplement-processed-section.v1",
                "producing_capture_version": REVALIDATION_PRODUCER_VERSION,
                "validator_version": REVALIDATION_PROCESSOR_VERSION,
                "validator_code_sha256": validator_code_sha256,
                "original_artifact_id": artifact.id,
                "original_output_sha256": artifact.output_sha256,
                "original_call_id": original.exchange.llm_call_id,
                "response_sha256": original.exchange.response_sha256,
                "source_sha256": manifest.source_sha256,
                "document_version_id": manifest.document_version_id,
                "page": pin.page,
                "start": pin.start,
                "length": pin.length,
                "rows": rows,
            }
            scope = (
                f"ir_revalidation:{capture.document_id}:attempt:{attempt_id}:"
                f"page:{pin.page}:section:{pin.start}:{pin.length}"
            )
            section_id, _ = upsert(
                UpsertRequest(
                    ticker=None,
                    purpose=REVALIDATION_PURPOSE,
                    scope=scope,
                    content_json=content,
                    prompt_version=REVALIDATION_PROCESSOR_VERSION,
                    cache_inputs=[manifest_sha, validator_code_sha256, str(artifact.id)],
                    source_doc_ids=[capture.document_id],
                    parent_artifact_ids=[artifact.id],
                ),
                conn=capture.conn,
            )
            if section_id is None:
                raise RuntimeError("IR revalidation section could not be retained")
            saved = require_historical_artifact(capture.conn, section_id)
            if (
                saved.content_json != content
                or saved.model is not None
                or saved.llm_call_id is not None
                or saved.parent_artifact_ids != [artifact.id]
            ):
                raise ValueError("IR revalidation section replay differs")
            section_ids.append(section_id)
        completion = {
            "schema_version": "ir-supplement-revalidation.v1",
            "attempt_id": attempt_id,
            "document_version_id": manifest.document_version_id,
            "source_sha256": manifest.source_sha256,
            "manifest_sha256": manifest_sha,
            "producing_capture_version": REVALIDATION_PRODUCER_VERSION,
            "validator_version": REVALIDATION_PROCESSOR_VERSION,
            "validator_code_sha256": validator_code_sha256,
            "section_artifact_ids": section_ids,
            "section_count": len(section_ids),
            "page_count": capture.page_count,
            "row_count": total_rows,
            "status": "processed_source_only",
            "provider_calls": 0,
            "financial_writes": 0,
        }
        complete_id, _ = upsert(
            UpsertRequest(
                ticker=None,
                purpose=REVALIDATION_PURPOSE,
                scope=f"ir_revalidation:{capture.document_id}:attempt:{attempt_id}:complete",
                content_json=completion,
                prompt_version=REVALIDATION_PROCESSOR_VERSION,
                cache_inputs=[manifest_sha, validator_code_sha256, *[str(x) for x in section_ids]],
                source_doc_ids=[capture.document_id],
                parent_artifact_ids=section_ids,
            ),
            conn=capture.conn,
        )
        if complete_id is None:
            raise RuntimeError("IR revalidation completion could not be retained")
        receipt = RevalidationReceipt.model_validate({"artifact_id": complete_id, **completion})
        saved_complete = require_historical_artifact(capture.conn, complete_id)
        if saved_complete.content_json != completion:
            raise ValueError("IR revalidation completion replay differs")
        if before_commit is not None:
            before_commit()
        capture.assert_review_scope_unchanged()
        if hashlib.sha256(raw_bytes).hexdigest() != manifest.source_sha256:
            raise ValueError("IR revalidation source changed before commit")
        if retain_candidates is not None:
            retain_candidates(receipt)
        end_run(capture.conn, attempt_id, StageStatus.OK)
        return receipt
    except Exception as exc:
        capture.conn.rollback()
        end_run(capture.conn, attempt_id, StageStatus.FAILED, error_summary=redact(exc))
        raise


def read_revalidated_capture(
    capture: SupplementCapture,
    raw_bytes: bytes,
    manifest: RevalidationManifest,
    receipt: RevalidationReceipt,
    *,
    _in_progress: bool = False,
) -> RevalidatedCapture:
    """Read an exact completed processing receipt for candidate-only consumers."""
    if (
        manifest.ticker != capture.ticker
        or manifest.document_id != capture.document_id
        or manifest.document_version_id != capture.document_version_id
        or manifest.source_sha256 != capture.source_sha256
        or hashlib.sha256(raw_bytes).hexdigest() != manifest.source_sha256
        or receipt.producing_capture_version != REVALIDATION_PRODUCER_VERSION
        or receipt.validator_version != REVALIDATION_PROCESSOR_VERSION
        or receipt.manifest_sha256 != _revalidation_manifest_sha(manifest)
    ):
        raise ValueError("IR revalidation reader identity differs")
    old_attempt = capture.conn.execute(
        "SELECT directive,status FROM ingestion_runs WHERE run_id=?",
        (manifest.failed_attempt_id,),
    ).fetchone()
    if old_attempt is None or tuple(old_attempt) != (
        "capture_ir_supplement_sections",
        StageStatus.FAILED.value,
    ):
        raise ValueError("IR historical failed attempt is unavailable")
    _require_producer_invocation(capture, manifest)
    committed = _read_revalidation_receipt(capture.conn, receipt.attempt_id, manifest.document_id)
    if committed != receipt or receipt.document_version_id != manifest.document_version_id:
        raise ValueError("IR revalidation committed receipt differs")
    attempt = capture.conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_id=?", (receipt.attempt_id,)
    ).fetchone()
    expected_status = StageStatus.IN_PROGRESS.value if _in_progress else StageStatus.OK.value
    if (
        attempt is None
        or attempt[0] != expected_status
        or (_in_progress and not capture.conn.in_transaction)
    ):
        raise ValueError("IR revalidation attempt is not complete")
    planned = capture.plan_sections(raw_bytes)
    if capture.reviewed_non_financial_pages:
        raise ValueError("IR revalidation reader scope differs")
    capture.capture_version = REVALIDATION_PRODUCER_VERSION
    if receipt.page_count != capture.page_count or receipt.section_count != len(planned):
        raise ValueError("IR revalidation page or section count differs")
    if [(p, s, len(b)) for p, s, b in planned] != [
        (p.page, p.start, p.length) for p in manifest.sections
    ]:
        raise ValueError("IR revalidation source section population differs")
    page_rows: list[tuple[int, list[dict[str, object]]]] = []
    exchanges: list[SupplementExchange] = []
    for pin, (_page, _start, body), processed_id in zip(
        manifest.sections, planned, receipt.section_artifact_ids, strict=True
    ):
        original, result, rows = _historical_section(capture, pin, body)
        if result.exchange.run_id != manifest.failed_attempt_id:
            raise ValueError("IR revalidation original governed run differs")
        processed = require_historical_artifact(capture.conn, processed_id)
        content = processed.content_json
        if not isinstance(content, dict):
            raise ValueError("IR revalidation processed section is malformed")
        if (
            processed.purpose != REVALIDATION_PURPOSE
            or processed.scope
            != (
                f"ir_revalidation:{manifest.document_id}:attempt:{receipt.attempt_id}:"
                f"page:{pin.page}:section:{pin.start}:{pin.length}"
            )
            or processed.prompt_version != REVALIDATION_PROCESSOR_VERSION
            or processed.model is not None
            or processed.llm_call_id is not None
            or processed.parent_artifact_ids != [original.id]
            or processed.source_doc_ids != [manifest.document_id]
            or processed.superseded_by_id is not None
            or content.get("producing_capture_version") != REVALIDATION_PRODUCER_VERSION
            or content.get("schema_version") != "ir-supplement-processed-section.v1"
            or content.get("validator_version") != REVALIDATION_PROCESSOR_VERSION
            or content.get("validator_code_sha256") != receipt.validator_code_sha256
            or content.get("source_sha256") != manifest.source_sha256
            or content.get("document_version_id") != manifest.document_version_id
            or content.get("original_artifact_id") != original.id
            or content.get("original_call_id") != result.exchange.llm_call_id
            or content.get("original_output_sha256") != original.output_sha256
            or content.get("response_sha256") != result.exchange.response_sha256
            or content.get("page") != pin.page
            or content.get("start") != pin.start
            or content.get("length") != pin.length
            or content.get("rows") != rows
        ):
            raise ValueError("IR revalidation processed section differs")
        page_rows.append((pin.page, rows))
        exchanges.append(result.exchange)
    if sum(len(rows) for _, rows in page_rows) != receipt.row_count:
        raise ValueError("IR revalidation row population differs")
    return RevalidatedCapture(receipt, page_rows, exchanges)
