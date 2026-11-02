"""Read/write API for the llm_artifacts table.

Pattern:
  - ``read_current(ticker, purpose, fiscal_period=None, scope='ticker')`` â†’
    the most recent non-superseded artifact, or None.
  - ``upsert(...)`` â†’ idempotent insert. When the existing row's input_sha256
    matches the new one and its output is verified and reusable, returns its id without inserting
    (cache hit). When it differs, marks the prior superseded and inserts a
    new row, preserving history. Returns the new row's id.
  - ``mark_dirty(ticker, purposes, reason)`` â†’ flips dirty=1 on a set of
    current artifacts. Called from the brief_dirty trigger chain (Phase 1.6).
  - ``drain_dirty(limit)`` â†’ returns the next batch of artifacts that need
    regeneration. Used by the daily drain cron (Phase 8).

The module is best-effort against missing DB / missing table â€” the LLM call
that produced the row must never fail because the store can't write.

JSON columns (source_doc_ids, parent_artifact_ids) are stored as TEXT
because SQLite's JSON1 extension isn't guaranteed across environments.
Helpers serialize / deserialize transparently.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast

from db_paths import resolve_db_path
from pipeline.db_writer_gate import WriterGateHeldError
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)


class DirtyQueueReadError(RuntimeError):
    """The dirty-artifact queue could not be read reliably."""


class DocumentProductStoreError(RuntimeError):
    """A native document-product artifact is missing, invalid, or ambiguous."""


DocumentProduct = Literal["transcript_summary",]
DocumentPeriodType = Literal["Q1", "Q2", "Q3", "Q4"]


@dataclass(frozen=True, slots=True)
class DocumentProductKey:
    issuer_id: str
    document_version_id: str
    period_start: date | None
    period_end: date
    period_type: DocumentPeriodType
    fiscal_year: int
    fiscal_quarter: int
    product: DocumentProduct

    def __post_init__(self) -> None:
        if not self.issuer_id or not self.document_version_id:
            raise ValueError("document product identity requires issuer and document IDs")
        fields: dict[str, object] = {
            "period_end": self.period_end,
            "period_start": self.period_start,
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
        }
        period_end = fields["period_end"]
        period_start = fields["period_start"]
        fiscal_year = fields["fiscal_year"]
        fiscal_quarter = fields["fiscal_quarter"]
        if isinstance(period_end, datetime) or not isinstance(period_end, date):
            raise ValueError("period_end must be a calendar date")
        if period_start is not None and (
            isinstance(period_start, datetime) or not isinstance(period_start, date)
        ):
            raise ValueError("period_start must be a calendar date or None")
        if period_start is not None and period_start > period_end:
            raise ValueError("period_start must not follow period_end")
        if (
            not isinstance(fiscal_year, int)
            or isinstance(fiscal_year, bool)
            or not isinstance(fiscal_quarter, int)
            or isinstance(fiscal_quarter, bool)
            or fiscal_year < 1
            or fiscal_quarter not in range(1, 5)
        ):
            raise ValueError("document product requires a valid fiscal year and quarter")
        if self.period_type != f"Q{fiscal_quarter}":
            raise ValueError("fiscal quarter must agree with period type")


@dataclass(frozen=True, slots=True)
class DocumentProductGeneration:
    input_manifest_json: str
    input_sha256: str
    generation_id: str
    generated_at: str
    model: str
    backend: str
    prompt_version: str
    prompt_sha256: str


@dataclass(frozen=True, slots=True)
class DocumentProductVerificationReceipt:
    receipt_json: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class DocumentProductEnvelope:
    key: DocumentProductKey
    generation: DocumentProductGeneration
    body_bytes: bytes
    body_sha256: str
    verification: DocumentProductVerificationReceipt


@dataclass(frozen=True, slots=True)
class DocumentProductArtifact:
    artifact_id: int
    key: DocumentProductKey
    generation: DocumentProductGeneration
    body_bytes: bytes
    body_sha256: str
    input_sha256: str
    output_sha256: str


class DocumentProductVerifier(Protocol):
    """Existing-owner adapter that validates the complete envelope and source."""

    def __call__(self, conn: sqlite3.Connection, envelope: DocumentProductEnvelope, /) -> None: ...


@dataclass(slots=True)
class Artifact:
    """One artifact row as the public API sees it. JSON columns are decoded."""

    id: int
    ticker: str | None
    scope: str
    purpose: str
    fiscal_period: str | None
    content_md: str | None
    content_json: object | None  # decoded from JSON, may be dict/list/None
    input_sha256: str
    output_sha256: str | None
    model: str | None
    prompt_version: str
    generated_at: datetime
    expires_at: datetime | None
    superseded_by_id: int | None
    dirty: bool
    dirty_reason: str | None
    source_doc_ids: list[int] = field(default_factory=list[int])
    parent_artifact_ids: list[int] = field(default_factory=list[int])
    llm_call_id: int | None = None
    issuer_id: str | None = None
    document_version_id: str | None = None
    document_period_start: str | None = None
    document_period_end: str | None = None
    document_period_type: str | None = None
    document_fiscal_year: int | None = None
    document_fiscal_quarter: int | None = None


@dataclass(slots=True)
class UpsertRequest:
    """Inputs to upsert(). Composes deterministically into the input_sha256
    cache key so any caller can be cache-correct without knowing the hash
    function. Add inputs here only when they actually affect the output â€”
    spurious inputs invalidate the cache unnecessarily."""

    ticker: str | None
    purpose: str
    scope: str = "ticker"
    fiscal_period: str | None = None
    content_md: str | None = None
    content_json: object | None = None
    model: str | None = None
    prompt_version: str = "v1"
    # Inputs that determine the cache key, hashed in order:
    cache_inputs: list[bytes | str] = field(default_factory=list[bytes | str])
    # Provenance â€” NOT hashed; these are descriptive metadata for the brief.
    source_doc_ids: list[int] = field(default_factory=list[int])
    parent_artifact_ids: list[int] = field(default_factory=list[int])
    expires_at: datetime | None = None
    llm_call_id: int | None = None
    # Dirty-drain callers can bind every exact queued predecessor to the same
    # successor in this transaction. Other producers leave this empty.
    supersede_artifact_ids: list[int] = field(default_factory=list[int])
    # Native-cache projection proves a new producer run and therefore must
    # persist a post-queue version even when its bytes match an older artifact.
    force_new_version: bool = False


def compute_input_sha256(*, prompt_version: str, cache_inputs: list[bytes | str]) -> str:
    """Deterministic cache-key hash. Caller passes the prompt_version explicitly
    and a list of all inputs that contribute to the output. The hash is order-
    sensitive so the caller controls reproducibility."""
    h = hashlib.sha256()
    h.update(b"v=" + prompt_version.encode("utf-8") + b"\n")
    for chunk in cache_inputs:
        b = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
        h.update(b"\n")
    return h.hexdigest()


def _compute_output_sha256(content_md: str | None, content_json: str | None) -> str | None:
    """Hash the stored Markdown, or exact JSON bytes when Markdown is empty."""
    output = content_md or content_json
    return hashlib.sha256(output.encode("utf-8")).hexdigest() if output else None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_document_envelope(envelope: DocumentProductEnvelope) -> dict[str, object]:
    if envelope.key.product not in {
        "transcript_summary",
    }:
        raise DocumentProductStoreError("document product is not supported")
    if not re.fullmatch(r"[0-9a-f]{64}", envelope.body_sha256):
        raise DocumentProductStoreError("document body checksum is malformed")
    body_fields: dict[str, object] = {"body": envelope.body_bytes}
    body_value = body_fields["body"]
    if not isinstance(body_value, bytes):
        raise DocumentProductStoreError("document body must be exact bytes")
    body_bytes = body_value
    try:
        body_text = body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentProductStoreError("document body is not UTF-8") from exc
    if _sha256_text(body_text) != envelope.body_sha256:
        raise DocumentProductStoreError("document body checksum does not match its bytes")
    if not re.fullmatch(r"[0-9a-f]{64}", envelope.generation.input_sha256):
        raise DocumentProductStoreError("document input checksum is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", envelope.generation.prompt_sha256):
        raise DocumentProductStoreError("document prompt checksum is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", envelope.verification.receipt_sha256):
        raise DocumentProductStoreError("document verification checksum is malformed")
    try:
        manifest_raw: object = json.loads(envelope.generation.input_manifest_json)
        receipt_raw: object = json.loads(envelope.verification.receipt_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DocumentProductStoreError("document manifest or receipt is invalid JSON") from exc
    if not isinstance(manifest_raw, dict) or not isinstance(receipt_raw, dict):
        raise DocumentProductStoreError("document manifest and receipt must be JSON objects")
    manifest = cast(dict[str, object], manifest_raw)
    receipt = cast(dict[str, object], receipt_raw)
    if _canonical_json(manifest) != envelope.generation.input_manifest_json:
        raise DocumentProductStoreError("document input manifest is not canonical JSON")
    if _canonical_json(receipt) != envelope.verification.receipt_json:
        raise DocumentProductStoreError("document verification receipt is not canonical JSON")
    if _sha256_text(envelope.verification.receipt_json) != envelope.verification.receipt_sha256:
        raise DocumentProductStoreError("document verification receipt checksum mismatch")
    expected_input = compute_input_sha256(
        prompt_version=envelope.generation.prompt_version,
        cache_inputs=[envelope.generation.input_manifest_json],
    )
    if expected_input != envelope.generation.input_sha256:
        raise DocumentProductStoreError("document input manifest checksum mismatch")
    try:
        prompt_raw = manifest["prompt"]
        document_raw = manifest["document"]
        canonical_binding_raw = manifest["canonical_issuer_binding"]
        fiscal_raw = manifest["fiscal_period_selection"]
        consumed_text = manifest["consumed_text_sha256"]
        extraction_raw = manifest["extraction"]
        anchors_context_raw = manifest["anchors_context"]
        parent_artifacts = manifest["parent_artifacts"]
        routing_raw = manifest["routing"]
        if not all(
            isinstance(value, dict)
            for value in (
                prompt_raw,
                document_raw,
                canonical_binding_raw,
                fiscal_raw,
                extraction_raw,
                anchors_context_raw,
                routing_raw,
            )
        ):
            raise TypeError("document manifest object fields have invalid shapes")
        prompt = cast(dict[str, object], prompt_raw)
        document = cast(dict[str, object], document_raw)
        fiscal = cast(dict[str, object], fiscal_raw)
        prompt_raw_bytes = prompt["prompt_bytes_base64"]
        if not isinstance(prompt_raw_bytes, str):
            raise TypeError("document prompt bytes must be base64 text")
        prompt_bytes = base64.b64decode(prompt_raw_bytes, validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise DocumentProductStoreError("document input manifest is incomplete") from exc
    if not isinstance(consumed_text, list) or not isinstance(parent_artifacts, list):
        raise DocumentProductStoreError("document input manifest fields have invalid shapes")
    try:
        prompt_text = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentProductStoreError("document prompt bytes are not UTF-8") from exc
    if _sha256_text(prompt_text) != envelope.generation.prompt_sha256:
        raise DocumentProductStoreError("document prompt checksum does not match manifest bytes")
    if prompt.get("prompt_version") != envelope.generation.prompt_version:
        raise DocumentProductStoreError("document prompt version does not match manifest")
    key = envelope.key
    expected_fiscal = {
        "issuer_id": key.issuer_id,
        "period_start": None if key.period_start is None else key.period_start.isoformat(),
        "period_end": key.period_end.isoformat(),
        "period_type": key.period_type,
        "fiscal_year": key.fiscal_year,
        "fiscal_quarter": key.fiscal_quarter,
    }
    if any(fiscal.get(field) != value for field, value in expected_fiscal.items()):
        raise DocumentProductStoreError("fiscal selection proof does not match exact key")
    if (
        not isinstance(fiscal.get("selection_authority"), str)
        or not fiscal["selection_authority"]
        or not isinstance(fiscal.get("selection_proof_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(fiscal["selection_proof_sha256"]))
    ):
        raise DocumentProductStoreError("fiscal selection authority proof is missing")
    if document.get("document_version_id") != key.document_version_id:
        raise DocumentProductStoreError("document input identity does not match exact key")
    raw_sha = document.get("raw_blob_sha256")
    if not isinstance(raw_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_sha):
        raise DocumentProductStoreError("document raw blob checksum is missing")
    fields: dict[str, object] = {
        "generation_id": envelope.generation.generation_id,
        "model": envelope.generation.model,
        "backend": envelope.generation.backend,
        "generated_at": envelope.generation.generated_at,
    }
    generation_id = fields["generation_id"]
    model = fields["model"]
    backend = fields["backend"]
    generated_at = fields["generated_at"]
    if not isinstance(generation_id, str) or not generation_id:
        raise DocumentProductStoreError("document generation identity is missing")
    if not isinstance(model, str) or not model:
        raise DocumentProductStoreError("actual document model is missing")
    if not isinstance(backend, str) or not backend:
        raise DocumentProductStoreError("actual document backend is missing")
    if not isinstance(generated_at, str):
        raise DocumentProductStoreError("document generation time is malformed")
    try:
        generation_time = datetime.fromisoformat(generated_at)
        uuid.UUID(generation_id)
    except ValueError as exc:
        raise DocumentProductStoreError("document generation identity or time is invalid") from exc
    if generation_time.tzinfo is None:
        raise DocumentProductStoreError("document generation time must be timezone-aware")
    return manifest


def _document_product_json(envelope: DocumentProductEnvelope) -> str:
    key = envelope.key
    generation = envelope.generation
    payload = {
        "schema_version": "document-product-envelope@1",
        "key": {
            "issuer_id": key.issuer_id,
            "document_version_id": key.document_version_id,
            "period_start": None if key.period_start is None else key.period_start.isoformat(),
            "period_end": key.period_end.isoformat(),
            "period_type": key.period_type,
            "fiscal_year": key.fiscal_year,
            "fiscal_quarter": key.fiscal_quarter,
            "product": key.product,
        },
        "generation": {
            "input_manifest_json": generation.input_manifest_json,
            "input_sha256": generation.input_sha256,
            "generation_id": generation.generation_id,
            "generated_at": generation.generated_at,
            "model": generation.model,
            "backend": generation.backend,
            "prompt_version": generation.prompt_version,
            "prompt_sha256": generation.prompt_sha256,
        },
        "body_base64": base64.b64encode(envelope.body_bytes).decode("ascii"),
        "body_sha256": envelope.body_sha256,
        "verification": {
            "receipt_json": envelope.verification.receipt_json,
            "receipt_sha256": envelope.verification.receipt_sha256,
        },
    }
    return _canonical_json(payload)


def _required_json_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise DocumentProductStoreError(f"document envelope field {field} must be text")
    return value


def _document_key_from_payload(payload: object) -> DocumentProductKey:
    if not isinstance(payload, dict):
        raise DocumentProductStoreError("document envelope key is not an object")
    key_data = cast(dict[str, object], payload)
    try:
        issuer_id = _required_json_string(key_data, "issuer_id")
        document_version_id = _required_json_string(key_data, "document_version_id")
        period_end_text = _required_json_string(key_data, "period_end")
        period_start_raw = key_data.get("period_start")
        if period_start_raw is not None and not isinstance(period_start_raw, str):
            raise TypeError("document start date must be a string or null")
        fiscal_year_raw = key_data.get("fiscal_year")
        if not isinstance(fiscal_year_raw, int) or isinstance(fiscal_year_raw, bool):
            raise TypeError("fiscal year must be an integer")
        fiscal_quarter_raw = key_data.get("fiscal_quarter")
        if not isinstance(fiscal_quarter_raw, int) or isinstance(fiscal_quarter_raw, bool):
            raise TypeError("fiscal quarter must be an integer")
        period_type = _required_json_string(key_data, "period_type")
        product = _required_json_string(key_data, "product")
        key = DocumentProductKey(
            issuer_id=issuer_id,
            document_version_id=document_version_id,
            period_start=(
                None if period_start_raw is None else date.fromisoformat(period_start_raw)
            ),
            period_end=date.fromisoformat(period_end_text),
            period_type=cast(DocumentPeriodType, period_type),
            fiscal_year=fiscal_year_raw,
            fiscal_quarter=fiscal_quarter_raw,
            product=cast(DocumentProduct, product),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DocumentProductStoreError("document envelope key is malformed") from exc
    if key.product not in {
        "transcript_summary",
    }:
        raise DocumentProductStoreError("document product is not supported")
    return key


def _document_envelope_from_json(content_json: str) -> DocumentProductEnvelope:
    try:
        payload_raw: object = json.loads(content_json)
        if not isinstance(payload_raw, dict):
            raise TypeError("document envelope must be an object")
        payload = cast(dict[str, object], payload_raw)
        key_json = payload.get("key")
        generation_raw = payload.get("generation")
        verification_raw = payload.get("verification")
        if not isinstance(generation_raw, dict):
            raise TypeError("document generation must be an object")
        if not isinstance(verification_raw, dict):
            raise TypeError("document verification must be an object")
        generation_json = cast(dict[str, object], generation_raw)
        verification_json = cast(dict[str, object], verification_raw)
        generation = DocumentProductGeneration(
            input_manifest_json=_required_json_string(generation_json, "input_manifest_json"),
            input_sha256=_required_json_string(generation_json, "input_sha256"),
            generation_id=_required_json_string(generation_json, "generation_id"),
            generated_at=_required_json_string(generation_json, "generated_at"),
            model=_required_json_string(generation_json, "model"),
            backend=_required_json_string(generation_json, "backend"),
            prompt_version=_required_json_string(generation_json, "prompt_version"),
            prompt_sha256=_required_json_string(generation_json, "prompt_sha256"),
        )
        verification = DocumentProductVerificationReceipt(
            receipt_json=_required_json_string(verification_json, "receipt_json"),
            receipt_sha256=_required_json_string(verification_json, "receipt_sha256"),
        )
        body_base64 = _required_json_string(payload, "body_base64")
        body_sha256 = _required_json_string(payload, "body_sha256")
        body_bytes = base64.b64decode(body_base64, validate=True)
        envelope = DocumentProductEnvelope(
            key=_document_key_from_payload(key_json),
            generation=generation,
            body_bytes=body_bytes,
            body_sha256=body_sha256,
            verification=verification,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise DocumentProductStoreError("document envelope is malformed") from exc
    if payload.get("schema_version") != "document-product-envelope@1":
        raise DocumentProductStoreError("document envelope schema is unsupported")
    if _document_product_json(envelope) != content_json:
        raise DocumentProductStoreError("document envelope is not canonical or has extra fields")
    _validate_document_envelope(envelope)
    return envelope


def _document_date_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(f"{text}T00:00:00").date().isoformat()
        except ValueError as exc:
            raise DocumentProductStoreError(f"registered {field_name} is malformed") from exc


def _validate_registered_document(
    conn: sqlite3.Connection,
    envelope: DocumentProductEnvelope,
    manifest: dict[str, object],
) -> str | None:
    rows = conn.execute(
        "SELECT current.issuer_id AS recorded_issuer_id, current.ticker, "
        "current.period_start, current.period_end, current.blob_sha256, "
        "canonical.issuer_id AS canonical_issuer_id, "
        "COALESCE(subject.binding_revision_id, legacy.binding_revision_id) "
        "AS binding_revision_id, "
        "CASE WHEN subject.binding_revision_id IS NOT NULL THEN 'recorded_subject' "
        "WHEN direct.issuer_id IS NOT NULL THEN 'native' "
        "WHEN legacy.binding_revision_id IS NOT NULL THEN 'legacy_issuer' "
        "ELSE 'unresolved' END AS binding_kind "
        "FROM v_evidence_document_current AS current "
        "JOIN v_evidence_document_versions_canonical AS canonical "
        "ON canonical.document_version_id=current.document_version_id "
        "LEFT JOIN issuer_entities AS direct ON direct.issuer_id=current.issuer_id "
        "LEFT JOIN v_recorded_subject_bindings_current AS subject "
        "ON subject.recorded_issuer_id=current.issuer_id AND subject.outcome='selected' "
        "LEFT JOIN v_legacy_issuer_bindings_current AS legacy "
        "ON legacy.recorded_issuer_id=current.issuer_id AND legacy.outcome='selected' "
        "WHERE current.document_version_id = ?",
        (envelope.key.document_version_id,),
    ).fetchall()
    if len(rows) != 1:
        raise DocumentProductStoreError("registered source document is missing or ambiguous")
    row = rows[0]
    key = envelope.key
    if str(row["canonical_issuer_id"]) != key.issuer_id:
        raise DocumentProductStoreError("registered canonical issuer does not match exact key")
    recorded_issuer_id = str(row["recorded_issuer_id"])
    binding_revision_id = (
        None if row["binding_revision_id"] is None else str(row["binding_revision_id"])
    )
    binding_kind = str(row["binding_kind"])
    if recorded_issuer_id != key.issuer_id and binding_revision_id is None:
        raise DocumentProductStoreError(
            "noncanonical recorded issuer requires a current binding revision"
        )
    if _document_date_text(row["period_start"], field_name="period_start") != (
        None if key.period_start is None else key.period_start.isoformat()
    ):
        raise DocumentProductStoreError("registered document start date does not match exact key")
    if (
        _document_date_text(row["period_end"], field_name="period_end")
        != key.period_end.isoformat()
    ):
        raise DocumentProductStoreError("registered document end date does not match exact key")
    document_manifest = manifest["document"]
    if not isinstance(document_manifest, dict) or document_manifest.get("raw_blob_sha256") != str(
        row["blob_sha256"]
    ):
        raise DocumentProductStoreError("document input manifest does not bind registered bytes")
    canonical_binding = manifest["canonical_issuer_binding"]
    if not isinstance(canonical_binding, dict) or canonical_binding != {
        "recorded_issuer_id": recorded_issuer_id,
        "canonical_issuer_id": key.issuer_id,
        "binding_revision_id": binding_revision_id,
        "binding_kind": binding_kind,
    }:
        raise DocumentProductStoreError(
            "issuer binding proof does not match current canonical view"
        )
    return None if row["ticker"] is None else str(row["ticker"])


def _document_artifact_from_row(
    row: sqlite3.Row,
    *,
    key: DocumentProductKey,
    verifier: DocumentProductVerifier,
    conn: sqlite3.Connection,
) -> DocumentProductArtifact:
    if row["content_md"] is not None or row["content_json"] is None:
        raise DocumentProductStoreError("document artifact is not JSON-envelope storage")
    envelope = _document_envelope_from_json(str(row["content_json"]))
    if envelope.key != key:
        raise DocumentProductStoreError("document envelope key differs from indexed key")
    if row["scope"] != "document" or row["purpose"] != key.product:
        raise DocumentProductStoreError("document artifact row metadata differs from its key")
    if (
        row["issuer_id"] != key.issuer_id
        or row["document_version_id"] != key.document_version_id
        or _document_date_text(row["document_period_start"], field_name="period_start")
        != (None if key.period_start is None else key.period_start.isoformat())
        or _document_date_text(row["document_period_end"], field_name="period_end")
        != key.period_end.isoformat()
        or row["document_period_type"] != key.period_type
        or int(row["document_fiscal_year"]) != key.fiscal_year
        or int(row["document_fiscal_quarter"]) != key.fiscal_quarter
        or row["fiscal_period"] != f"FY{key.fiscal_year}Q{key.fiscal_quarter}"
    ):
        raise DocumentProductStoreError("document artifact indexed key differs from envelope key")
    if row["input_sha256"] != envelope.generation.input_sha256:
        raise DocumentProductStoreError("document input hash differs from envelope")
    if (
        row["model"] != envelope.generation.model
        or row["prompt_version"] != envelope.generation.prompt_version
        or row["generated_at"] != envelope.generation.generated_at
    ):
        raise DocumentProductStoreError("document generation metadata differs from envelope")
    exact_output_sha = _sha256_text(str(row["content_json"]))
    if row["output_sha256"] != exact_output_sha:
        raise DocumentProductStoreError("document envelope output checksum mismatch")
    manifest = _validate_document_envelope(envelope)
    ticker = _validate_registered_document(conn, envelope, manifest)
    if row["ticker"] != ticker:
        raise DocumentProductStoreError("document artifact ticker differs from registered source")
    try:
        verifier(conn, envelope)
    except Exception as exc:
        raise DocumentProductStoreError("document verification adapter rejected envelope") from exc
    return DocumentProductArtifact(
        artifact_id=int(row["id"]),
        key=key,
        generation=envelope.generation,
        body_bytes=envelope.body_bytes,
        body_sha256=envelope.body_sha256,
        input_sha256=envelope.generation.input_sha256,
        output_sha256=exact_output_sha,
    )


def read_document_current(
    conn: sqlite3.Connection,
    key: DocumentProductKey,
    *,
    expected_input_sha256: str,
    verifier: DocumentProductVerifier,
    now: datetime | None = None,
) -> DocumentProductArtifact | None:
    """Read one exact, current, semantically reverified document product.

    Missing or stale products return None. Schema, corruption, ambiguity and
    verifier failures raise. This API never falls back to a quarter alias.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_input_sha256):
        raise DocumentProductStoreError("expected document input checksum is malformed")
    previous_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM llm_artifacts WHERE scope='document' "
            "AND issuer_id=? AND document_version_id=? "
            "AND document_period_start IS ? AND document_period_end=? "
            "AND document_period_type=? AND document_fiscal_year=? "
            "AND document_fiscal_quarter=? AND purpose=? "
            "AND superseded_by_id IS NULL ORDER BY id",
            (
                key.issuer_id,
                key.document_version_id,
                None if key.period_start is None else key.period_start.isoformat(),
                key.period_end.isoformat(),
                key.period_type,
                key.fiscal_year,
                key.fiscal_quarter,
                key.product,
            ),
        ).fetchall()
        if len(rows) > 1:
            raise DocumentProductStoreError(
                "multiple current artifacts exist for exact document key"
            )
        if not rows:
            return None
        row = rows[0]
        if row["dirty"]:
            return None
        expires = row["expires_at"]
        if expires is not None:
            try:
                expiry = _parse_dt(expires)
            except (TypeError, ValueError) as exc:
                raise DocumentProductStoreError(
                    "document artifact expiry timestamp is malformed"
                ) from exc
            reference = now if now is not None else datetime.now(UTC)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=UTC)
            if expiry <= reference:
                return None
        if row["input_sha256"] != expected_input_sha256:
            return None
        return _document_artifact_from_row(row, key=key, verifier=verifier, conn=conn)
    except sqlite3.Error as exc:
        raise DocumentProductStoreError("document artifact query failed") from exc
    finally:
        conn.row_factory = previous_factory


def persist_document_generated_strict(
    conn: sqlite3.Connection,
    envelope: DocumentProductEnvelope,
    *,
    verifier: DocumentProductVerifier,
) -> DocumentProductArtifact:
    """Persist and read back a verified document product in caller state.

    The caller owns an active writer transaction and its commit/rollback and
    connection lifetime. This method uses a savepoint and never commits/closes.
    """
    if not conn.in_transaction:
        raise DocumentProductStoreError("strict document persistence requires caller transaction")
    previous_factory = conn.row_factory
    savepoint = f"strict_document_product_{uuid.uuid4().hex}"
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"SAVEPOINT {savepoint}")
        # Acquire SQLite's writer reservation before validating source identity.
        conn.execute("UPDATE llm_artifacts SET dirty=dirty WHERE id=-1")
        manifest = _validate_document_envelope(envelope)
        ticker = _validate_registered_document(conn, envelope, manifest)
        verifier(conn, envelope)
        key = envelope.key
        current_rows = conn.execute(
            "SELECT * FROM llm_artifacts WHERE scope='document' "
            "AND issuer_id=? AND document_version_id=? "
            "AND document_period_start IS ? AND document_period_end=? "
            "AND document_period_type=? AND document_fiscal_year=? "
            "AND document_fiscal_quarter=? AND purpose=? "
            "AND superseded_by_id IS NULL ORDER BY id",
            (
                key.issuer_id,
                key.document_version_id,
                None if key.period_start is None else key.period_start.isoformat(),
                key.period_end.isoformat(),
                key.period_type,
                key.fiscal_year,
                key.fiscal_quarter,
                key.product,
            ),
        ).fetchall()
        if len(current_rows) > 1:
            raise DocumentProductStoreError(
                "multiple current artifacts exist for exact document key"
            )
        if current_rows:
            current = _document_artifact_from_row(
                current_rows[0], key=key, verifier=verifier, conn=conn
            )
            if current.input_sha256 == envelope.generation.input_sha256:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                return current
        content_json = _document_product_json(envelope)
        output_sha = _sha256_text(content_json)
        cur = conn.execute(
            "INSERT INTO llm_artifacts (ticker, scope, purpose, fiscal_period, "
            "content_md, content_json, input_sha256, output_sha256, model, "
            "prompt_version, generated_at, expires_at, superseded_by_id, dirty, "
            "dirty_reason, "
            "issuer_id, document_version_id, document_period_start, document_period_end, "
            "document_period_type, document_fiscal_year, document_fiscal_quarter) "
            "VALUES (?, 'document', ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, "
            "?, ?, ?, ?, ?, ?, ?)",
            (
                ticker,
                key.product,
                f"FY{key.fiscal_year}Q{key.fiscal_quarter}",
                content_json,
                envelope.generation.input_sha256,
                output_sha,
                envelope.generation.model,
                envelope.generation.prompt_version,
                envelope.generation.generated_at,
                key.issuer_id,
                key.document_version_id,
                None if key.period_start is None else key.period_start.isoformat(),
                key.period_end.isoformat(),
                key.period_type,
                key.fiscal_year,
                key.fiscal_quarter,
            ),
        )
        artifact_id = int(cur.lastrowid or 0)
        if current_rows:
            conn.execute(
                "UPDATE llm_artifacts SET superseded_by_id=? WHERE id=? "
                "AND superseded_by_id IS NULL AND scope='document'",
                (artifact_id, int(current_rows[0]["id"])),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise DocumentProductStoreError("document artifact current predecessor changed")
        readback = conn.execute("SELECT * FROM llm_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if readback is None:
            raise DocumentProductStoreError("document artifact readback is missing")
        result = _document_artifact_from_row(readback, key=key, verifier=verifier, conn=conn)
        if result.body_bytes != envelope.body_bytes or result.generation != envelope.generation:
            raise DocumentProductStoreError("document artifact readback differs from request")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.row_factory = previous_factory


def _output_integrity_reason(row: sqlite3.Row) -> str | None:
    if not row["output_sha256"]:
        reason = "output_checksum_missing"
    elif row["output_sha256"] != _compute_output_sha256(row["content_md"], row["content_json"]):
        reason = "output_checksum_mismatch"
    else:
        return None
    log.warning(
        {"event": "artifact_output_unverified", "artifact_id": int(row["id"]), "reason": reason}
    )
    return reason


def _readable_artifact(row: sqlite3.Row, *, for_regeneration: bool = False) -> Artifact | None:
    reason = _output_integrity_reason(row)
    if reason == "output_checksum_mismatch" and not for_regeneration:
        return None
    artifact = _row_to_artifact(row)
    if reason is not None:
        # Missing historical checksums stay visible but cannot be reused.
        # Queue readers retain identity so regeneration can supersede the row.
        artifact.dirty = True
        artifact.dirty_reason = reason
        if reason == "output_checksum_mismatch":
            artifact.content_md = None
            artifact.content_json = None
    return artifact


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    raw_src = row["source_doc_ids"]
    raw_par = row["parent_artifact_ids"]
    src_ids: list[int] = []
    par_ids: list[int] = []
    if raw_src:
        try:
            decoded = json.loads(raw_src)
            if isinstance(decoded, list):
                decoded_values = cast("list[object]", decoded)
                src_ids = [int(v) for v in decoded_values if isinstance(v, (int, float))]
        except (json.JSONDecodeError, TypeError):
            pass
    if raw_par:
        try:
            decoded = json.loads(raw_par)
            if isinstance(decoded, list):
                decoded_values = cast("list[object]", decoded)
                par_ids = [int(v) for v in decoded_values if isinstance(v, (int, float))]
        except (json.JSONDecodeError, TypeError):
            pass

    json_blob_raw = row["content_json"]
    content_json: object | None = None
    if json_blob_raw:
        try:
            content_json = json.loads(json_blob_raw)
        except json.JSONDecodeError:
            content_json = None

    available_columns = set(row.keys())
    return Artifact(
        id=int(row["id"]),
        ticker=row["ticker"],
        scope=row["scope"],
        purpose=row["purpose"],
        fiscal_period=row["fiscal_period"],
        content_md=row["content_md"],
        content_json=content_json,
        input_sha256=row["input_sha256"],
        output_sha256=row["output_sha256"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        generated_at=_parse_dt(row["generated_at"]),
        expires_at=_parse_dt(row["expires_at"]) if row["expires_at"] else None,
        superseded_by_id=row["superseded_by_id"],
        dirty=bool(row["dirty"]),
        dirty_reason=row["dirty_reason"],
        source_doc_ids=src_ids,
        parent_artifact_ids=par_ids,
        llm_call_id=row["llm_call_id"],
        issuer_id=(row["issuer_id"] if "issuer_id" in available_columns else None),
        document_version_id=(
            row["document_version_id"] if "document_version_id" in available_columns else None
        ),
        document_period_start=(
            row["document_period_start"] if "document_period_start" in available_columns else None
        ),
        document_period_end=(
            row["document_period_end"] if "document_period_end" in available_columns else None
        ),
        document_period_type=(
            row["document_period_type"] if "document_period_type" in available_columns else None
        ),
        document_fiscal_year=(
            None
            if "document_fiscal_year" not in available_columns
            or row["document_fiscal_year"] is None
            else int(row["document_fiscal_year"])
        ),
        document_fiscal_quarter=(
            None
            if "document_fiscal_quarter" not in available_columns
            or row["document_fiscal_quarter"] is None
            else int(row["document_fiscal_quarter"])
        ),
    )


def _parse_dt(v: object) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _cache_state_is_reusable(
    *,
    dirty: bool,
    expires_at: datetime | None,
    current_input_sha256: str | None = None,
    expected_input_sha256: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Single source of truth for dirty, expiry, and input-key reuse."""
    if dirty:
        return False
    if expected_input_sha256 is not None and current_input_sha256 != expected_input_sha256:
        return False
    if expires_at is None:
        return True
    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > reference


def artifact_is_fresh(artifact: Artifact, *, now: datetime | None = None) -> bool:
    """Return whether an artifact is safe to reuse as current LLM context.

    ``read_current`` intentionally exposes dirty and expired rows so UI callers
    can label those states. Context-building callers use this predicate to keep
    stale recommendations out of a new model prompt.
    """
    return bool(artifact.output_sha256) and _cache_state_is_reusable(
        dirty=artifact.dirty,
        expires_at=artifact.expires_at,
        now=now,
    )


def artifact_is_reusable(
    artifact: Artifact,
    *,
    input_sha256: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Canonical cache-hit predicate for artifact generators and consumers.

    A reusable artifact must be clean, unexpired, andâ€”when the caller has
    recomputed its deterministic cache keyâ€”match the current inputs. Keeping
    this rule here prevents individual prompt builders from accidentally
    treating hash equality alone as freshness.
    """
    return bool(artifact.output_sha256) and _cache_state_is_reusable(
        dirty=artifact.dirty,
        expires_at=artifact.expires_at,
        current_input_sha256=artifact.input_sha256,
        expected_input_sha256=input_sha256,
        now=now,
    )


def read_current(
    *,
    ticker: str | None,
    purpose: str,
    fiscal_period: str | None = None,
    scope: str = "ticker",
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> Artifact | None:
    """Read the most recent non-superseded artifact for the scope tuple.
    Returns None when no artifact exists (e.g. first-run for this ticker)
    or when DB / table is unavailable or the stored output checksum differs.
    Legacy rows without a checksum remain visible as dirty, unverified rows.
    """
    db_conn = conn or _open(db_path, role=SQLiteConnectionRole.READ_ONLY)
    if db_conn is None:
        return None
    try:
        db_conn.row_factory = sqlite3.Row
        row = db_conn.execute(
            """
            SELECT * FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ?
              AND scope <> 'document'
              AND document_version_id IS NULL
              AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
              AND superseded_by_id IS NULL
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (ticker, scope, purpose, fiscal_period),
        ).fetchone()
        return _readable_artifact(row) if row else None
    except sqlite3.Error as exc:
        # Best-effort read: _open() already guards a missing table, but a table
        # that exists with a *drifted* schema (a legacy DB predating a column
        # add, or a partial test fixture) would otherwise raise here and crash
        # the render path. Degrade to "no cached artifact" instead â€” the caller
        # recomputes or shows the empty state, same as a genuine cache miss.
        log.warning({"event": "artifact_read_current_failed", "error": str(exc)})
        return None
    finally:
        if conn is None:
            db_conn.close()


def read_artifact(
    artifact_id: int,
    *,
    db_path: Path | str | None = None,
) -> Artifact | None:
    """Read by stable id, including superseded rows; reject checksum mismatches."""
    conn = _open(db_path, role=SQLiteConnectionRole.READ_ONLY)
    if conn is None:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM llm_artifacts WHERE id = ? AND document_version_id IS NULL",
            (artifact_id,),
        ).fetchone()
        return _readable_artifact(row) if row else None
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "artifact_read_by_id_failed",
                "artifact_id": artifact_id,
                "error": str(exc),
            }
        )
        return None
    finally:
        conn.close()


def require_historical_artifact(conn: sqlite3.Connection, artifact_id: int) -> Artifact:
    """Read exact historical bytes in the caller's transaction or fail closed.

    Unlike the best-effort cache readers, this is for source-bound historical
    processing. Missing, malformed, dirty, and checksum-drifted rows cannot
    become an apparent cache miss.
    """
    previous_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM llm_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise ValueError("IR historical artifact is missing")
        try:
            artifact = _readable_artifact(row)
        except (ValueError, TypeError, KeyError, IndexError):
            raise ValueError("IR historical artifact is malformed") from None
        if artifact is None or artifact.dirty or artifact.output_sha256 is None:
            raise ValueError("IR historical artifact is unavailable")
        return artifact
    finally:
        conn.row_factory = previous_factory


@dataclass(frozen=True, slots=True)
class HistoricalArtifactVersion:
    """An exact-key historical row, including an explicit decoding failure."""

    artifact_id: int
    artifact: Artifact | None
    error: str | None = None
    generated_at_raw: str | None = None


def historical_versions(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    purpose: str,
    fiscal_period: str | None,
    scope: str = "ticker",
    verify_readability: bool = False,
) -> list[HistoricalArtifactVersion]:
    """Read all exact-key versions in the caller's snapshot; never filter by currentness.

    The historical consumer must verify commitments and normalize aware times.
    Query failures propagate, so they cannot become an empty historical baseline.
    """
    previous_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM llm_artifacts WHERE ticker=? AND scope=? "
            "AND scope <> 'document' AND document_version_id IS NULL "
            "AND purpose=? AND fiscal_period IS ? ORDER BY id",
            (ticker, scope, purpose, fiscal_period),
        ).fetchall()
        versions: list[HistoricalArtifactVersion] = []
        for row in rows:
            ident = int(row["id"])
            try:
                artifact = _readable_artifact(row) if verify_readability else _row_to_artifact(row)
            except (ValueError, TypeError, KeyError, IndexError):
                versions.append(HistoricalArtifactVersion(ident, None, "malformed_artifact"))
            else:
                versions.append(
                    HistoricalArtifactVersion(
                        ident,
                        artifact,
                        error="unreadable_artifact" if artifact is None else None,
                        generated_at_raw=str(row["generated_at"]),
                    )
                )
        return versions
    finally:
        conn.row_factory = previous_factory


def upsert(
    req: UpsertRequest,
    *,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[int | None, bool]:
    """Idempotent upsert.

    Returns (artifact_id, was_cache_hit):
      - was_cache_hit=True  â†’ inputs match and verified output is reusable; existing id
                              returned, no new row inserted.
      - was_cache_hit=False â†’ either no prior row (insert) or prior input_sha256
                              differs (supersede + insert). New id returned.

    With a supplied connection, use a savepoint and retain caller transaction
    ownership. The caller commits or rolls back and closes the connection.

    On DB unavailability returns (None, False) â€” caller must treat as "go
    ahead, compute the artifact" (the LLM call still produces output, just
    no persistence).
    """
    owns_connection = conn is None
    if req.scope == "document":
        raise ValueError("document scope is reserved for strict document-product persistence")
    conn = conn or _open(db_path)
    if conn is None:
        return (None, False)
    savepoint = "llm_artifact_upsert"
    if not owns_connection:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.row_factory = sqlite3.Row
        input_sha = compute_input_sha256(
            prompt_version=req.prompt_version, cache_inputs=req.cache_inputs
        )

        existing = conn.execute(
            """
            SELECT id, input_sha256, dirty, expires_at,
                   content_md, content_json, output_sha256 FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ?
              AND scope <> 'document'
              AND document_version_id IS NULL
              AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
              AND superseded_by_id IS NULL
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (req.ticker, req.scope, req.purpose, req.fiscal_period),
        ).fetchone()

        # Cache hit â€” same input hash AND not marked dirty. The dirty flag wins
        # over hash equality because a trigger may have flagged the row even
        # though inputs didn't change (e.g. prompt_version bump applied
        # retroactively to the table).
        existing_expires = (
            _parse_dt(existing["expires_at"])
            if existing is not None and existing["expires_at"]
            else None
        )
        if (
            not req.force_new_version
            and existing is not None
            and _output_integrity_reason(existing) is None
            and _cache_state_is_reusable(
                dirty=bool(existing["dirty"]),
                expires_at=existing_expires,
                current_input_sha256=str(existing["input_sha256"]),
                expected_input_sha256=input_sha,
            )
        ):
            existing_id = int(existing["id"])
            _supersede_exact_obligations(conn, req=req, successor_id=existing_id)
            if owns_connection:
                conn.commit()
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return (existing_id, True)

        # Missing/corrupt output also requires a new version; retain prior bytes.
        # Either no existing row, hash drift, or dirty â€” insert a new row and
        # supersede any prior current row.
        content_json_str = (
            json.dumps(req.content_json, ensure_ascii=False)
            if req.content_json is not None
            else None
        )
        output_sha = _compute_output_sha256(req.content_md, content_json_str)
        effective_expires_at = req.expires_at or default_expires_at(req.purpose)
        cur = conn.execute(
            """
            INSERT INTO llm_artifacts(
              ticker, scope, purpose, fiscal_period,
              content_md, content_json,
              input_sha256, output_sha256, model, prompt_version,
              generated_at, expires_at,
              superseded_by_id, dirty, dirty_reason,
              source_doc_ids, parent_artifact_ids, llm_call_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,NULL,?,?,?)
            """,
            (
                req.ticker,
                req.scope,
                req.purpose,
                req.fiscal_period,
                req.content_md,
                content_json_str,
                input_sha,
                output_sha,
                req.model,
                req.prompt_version,
                datetime.now(UTC).isoformat(),
                effective_expires_at.isoformat() if effective_expires_at else None,
                json.dumps(req.source_doc_ids) if req.source_doc_ids else None,
                json.dumps(req.parent_artifact_ids) if req.parent_artifact_ids else None,
                req.llm_call_id,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        if existing is not None:
            conn.execute(
                "UPDATE llm_artifacts SET superseded_by_id = ? "
                "WHERE id = ? AND scope <> 'document' AND document_version_id IS NULL",
                (new_id, int(existing["id"])),
            )
        _supersede_exact_obligations(conn, req=req, successor_id=new_id)
        if owns_connection:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return (new_id, False)
    except sqlite3.Error as exc:
        if owns_connection:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        log.warning({"event": "artifact_upsert_failed", "error": str(exc)})
        return (None, False)
    finally:
        if owns_connection:
            conn.close()


def _supersede_exact_obligations(
    conn: sqlite3.Connection,
    *,
    req: UpsertRequest,
    successor_id: int,
) -> None:
    """Bind exact same-scope queue rows to ``successor_id`` before commit."""
    predecessor_ids = sorted(
        {artifact_id for artifact_id in req.supersede_artifact_ids if artifact_id != successor_id}
    )
    if not predecessor_ids:
        return
    for predecessor_id in predecessor_ids:
        conn.execute(
            """
            UPDATE llm_artifacts
            SET superseded_by_id = ?
            WHERE id = ?
              AND scope <> 'document'
              AND document_version_id IS NULL
              AND COALESCE(ticker, '') = COALESCE(?, '')
              AND scope = ?
              AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
              AND superseded_by_id IS NULL
            """,
            (
                successor_id,
                predecessor_id,
                req.ticker,
                req.scope,
                req.purpose,
                req.fiscal_period,
            ),
        )
        row = conn.execute(
            """
            SELECT id, ticker, scope, purpose, fiscal_period, superseded_by_id
            FROM llm_artifacts
            WHERE id = ? AND scope <> 'document'
              AND document_version_id IS NULL
            """,
            (predecessor_id,),
        ).fetchone()
        if (
            row is None
            or (row["ticker"] or None) != (req.ticker or None)
            or str(row["scope"]) != req.scope
            or str(row["purpose"]) != req.purpose
            or (row["fiscal_period"] or None) != (req.fiscal_period or None)
            or int(row["superseded_by_id"] or 0) != successor_id
        ):
            raise sqlite3.IntegrityError(
                f"artifact obligation {predecessor_id} was not atomically bound to "
                f"successor {successor_id}"
            )


# Purposes whose output depends on financial_facts / kpi_facts /
# segment_periods state. When upstream facts change for a ticker (SEC ingest,
# FMP refresh, manual correction), these artifacts are marked dirty so the
# stale cache is not served.
#
# Two refresh paths consume this set, by purpose family:
#   * Brief-side purposes (bear_case, valuation_basis, qa_topics, â€¦) are
#     regenerated by the drain executor â€” see _PURPOSE_TO_REGENERATOR in
#     execution/refresh_dirty_artifacts.py, which must cover exactly those.
#   * Trigger/news-side purposes (earnings_tone_diff, kpi_inflection_context,
#     saydo_due_context, material_news_classification, news_structuring) have
#     NO standalone drain regenerator â€” recomputing them is a side effect of the
#     daily trigger scan / news fetch, which already honor the dirty flag. The
#     drain intentionally classifies these as "refreshed by the daily scan"
#     rather than warning "no regenerator". Their TTLs (above) only let the
#     expiry sweep reap orphans.
FACT_DEPENDENT_PURPOSES: tuple[str, ...] = (
    "bear_case",
    "company_description",
    "filing_intelligence",
    "valuation_basis",
    "exec_comp_alignment",
    "qa_topics",
    # Earnings-tone diff caches the LLM comparison of a new transcript
    # against the prior 4 quarters' transcripts. Keyed by transcript ids,
    # so a fact-side restatement of the *same* transcript doesn't move
    # the key â€” but the trigger framework re-runs on every transcript
    # arrival anyway. Inclusion here participates in the existing
    # mark-dirty chain so a restatement that touches financial_facts
    # (which the comparison anchor reads) invalidates the cached diff.
    "earnings_tone_diff",
    # KPI-inflection context: the optional "why it matters" line attached to a
    # kpi_inflection alert. Keyed by (kpi_name, period_end, factual core,
    # thesis anchor). A fact-side restatement that moves the KPI series should
    # invalidate the cached prose, so it joins the mark-dirty chain here.
    "kpi_inflection_context",
    # SayDo-due context: the optional "what it means for the thesis" line
    # attached to a saydo_due alert. Keyed by (kpi_name, period_target, factual
    # core, thesis anchor). A fact-side restatement that moves the realized
    # value should invalidate the cached prose, so it joins the chain here.
    "saydo_due_context",
    # Material-news classification: the batched LLM materiality scoring of a
    # ticker's recent headlines against its thesis anchor. Keyed by (ticker,
    # sorted news ids, anchor sha). A fact-side restatement that moves the
    # thesis anchor should invalidate the cached scoring, so it joins the chain.
    "material_news_classification",
    # News structuring: the WebSearch->structured-rows extraction (the
    # FMP-independent news feed). Keyed by (ticker, utc date, anchor sha), so the
    # date key already bounds staleness to 24h and the anchor sha re-invalidates
    # on an anchor change at read time. Joining the chain makes it consistent
    # with its four trigger/news siblings: a fact-side restatement that moves the
    # thesis anchor proactively dirties the cached extraction rather than waiting
    # for the next day's date-key rollover.
    "news_structuring",
    # Investment Decision Card (P1.1, personal_investment_partner_prd.md
    # Â§8.1): the per-ticker synthesis over the thesis, DCF, bear case, and
    # candidate fit. A fact-side restatement of any of those should
    # invalidate the cached card, so it joins the mark-dirty chain here.
    # Regenerator: execution/build_investment_decision_card.py (see
    # execution/refresh_dirty_artifacts.py's _PURPOSE_TO_REGENERATOR).
    "investment_decision_card",
)


# Default TTL per purpose. The artifact's `expires_at` is set to now+TTL at
# upsert time when the caller doesn't pass one explicitly, and the drain
# loop treats `expires_at < now` as a soft dirty signal so cached LLM
# outputs don't live forever even if no fact change ever flips brief_dirty.
# Tuned by data sensitivity:
#   bear_case / company_description: company narrative changes slowly ->
#     30 days
#   qa_topics / exec_comp_alignment / valuation_basis:
#     anchored to the most recent quarter -> 14 days
#   filing_intelligence: anchored to a specific 10-K/10-Q -> 60 days
# A purpose not in this table gets no default TTL (caller must opt in).
_DEFAULT_TTL_DAYS: dict[str, int] = {
    "bear_case": 30,
    "company_description": 30,
    "qa_topics": 14,
    "exec_comp_alignment": 14,
    "valuation_basis": 14,
    "filing_intelligence": 60,
    # Earnings-tone diff is anchored to a specific transcript-id tuple
    # (current + prior 4). The TTL acts as a safety net so a months-old
    # cached diff is re-attempted even if the trigger never re-fires;
    # the transcript-id key drives the primary invalidation.
    "earnings_tone_diff": 30,
    # KPI-inflection context is anchored to a (kpi_name, period_end) tuple.
    # The TTL is a safety net; the input_sha256 (factual core + anchor) drives
    # the primary invalidation when the underlying numbers move.
    "kpi_inflection_context": 30,
    # SayDo-due context is anchored to a (kpi_name, period_target) tuple. The
    # TTL is a safety net; the input_sha256 (factual core + anchor) drives the
    # primary invalidation when the realized value moves.
    "saydo_due_context": 30,
    # Material-news classification is anchored to a (ticker, news-id-set,
    # anchor) tuple via input_sha256. The TTL is a safety net so a stale cached
    # scoring is re-attempted even if the same news batch lingers; the news-id
    # set drives the primary invalidation when new stories land.
    "material_news_classification": 30,
    # News structuring is keyed by (ticker, utc date, anchor sha), so a fresh
    # entry is created each day and yesterday's is never read again. A short TTL
    # lets the drain's expiry sweep reap those daily orphans rather than letting
    # them accumulate in llm_artifacts forever; the date key drives the primary
    # daily invalidation.
    "news_structuring": 7,
}


def default_expires_at(purpose: str, *, now: datetime | None = None) -> datetime | None:
    """Compute the canonical expires_at for an artifact of the given purpose.

    Returns None for purposes without a TTL policy â€” caller may still set
    expires_at explicitly on the UpsertRequest. now is injectable for tests.
    """
    days = _DEFAULT_TTL_DAYS.get(purpose)
    if days is None:
        return None
    now = now if now is not None else datetime.now(UTC)
    return now + timedelta(days=days)


def mark_artifacts_dirty_for_fact_change(
    *,
    ticker: str,
    reason: str,
    db_path: Path | str | None = None,
    purposes: tuple[str, ...] | list[str] = FACT_DEPENDENT_PURPOSES,
) -> int:
    """Mark every fact-dependent artifact for `ticker` dirty.

    Called from the brief-rebuild path whenever upstream facts may have
    changed: the daily worker before each rebuild, the SEC silent-staleness
    detector, and any future "facts restated" trigger. Without this chain
    the LLM artifact cache stays valid even though the inputs to the prompt
    have shifted underneath it.

    Returns the count of rows actually flipped from cleanâ†’dirty.
    """
    return mark_dirty(
        ticker=ticker,
        purposes=list(purposes),
        reason=reason,
        db_path=db_path,
    )


def mark_dirty(
    *,
    ticker: str,
    purposes: list[str],
    reason: str,
    db_path: Path | str | None = None,
) -> int:
    """Flip dirty=1 on all current artifacts matching (ticker, purpose IN ...).
    Returns the count of rows updated. Best-effort â€” returns 0 on DB error."""
    if not purposes:
        return 0
    conn = _open(db_path)
    if conn is None:
        return 0
    try:
        placeholders = ",".join("?" * len(purposes))
        cur = conn.execute(
            f"""
            UPDATE llm_artifacts
            SET dirty = 1, dirty_reason = ?
            WHERE ticker = ?
              AND purpose IN ({placeholders})
              AND scope <> 'document'
              AND document_version_id IS NULL
              AND superseded_by_id IS NULL
              AND dirty = 0
            """,
            (reason, ticker, *purposes),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as exc:
        log.warning({"event": "artifact_mark_dirty_failed", "error": str(exc)})
        return 0
    finally:
        conn.close()


def drain_dirty(
    *,
    limit: int = 50,
    db_path: Path | str | None = None,
    now: datetime | None = None,
    strict: bool = False,
) -> list[Artifact]:
    """Return up to `limit` artifacts that need regeneration.

    An artifact is considered due when:
      * dirty=1 (an upstream change flipped it), OR
      * expires_at < now (TTL has elapsed since the row was generated)

    Both paths land in the same drain queue so callers don't have to run
    two separate sweeps. Caller regenerates them via the purpose-specific
    generator + upsert; dirty=0 + a fresh expires_at are written on the new
    row. Optional callers retain best-effort empty results; operational
    drains set ``strict`` so unavailable or drifted queue and roster state
    fails closed. Corrupt queued payloads are cleared from returned objects,
    while their identities remain available for append/supersede recovery.
    """
    conn = _open(db_path, role=SQLiteConnectionRole.READ_ONLY)
    if conn is None:
        if strict:
            raise DirtyQueueReadError("dirty queue database or table unavailable")
        return []
    now_iso = (now if now is not None else datetime.now(UTC)).isoformat()
    try:
        conn.row_factory = sqlite3.Row
        has_tracked = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracked_companies'"
        ).fetchone()
        if has_tracked is None and strict:
            raise DirtyQueueReadError("tracked company roster unavailable")
        membership_filter = ""
        if has_tracked is not None:
            membership_filter = """
              AND (
                ticker IS NULL
                OR EXISTS (
                    SELECT 1 FROM tracked_companies tc
                    WHERE UPPER(tc.ticker) = UPPER(llm_artifacts.ticker)
                      AND tc.archived_at IS NULL
                      AND tc.list_type IN ('portfolio', 'evaluation')
                )
              )
            """
        rows = conn.execute(
            f"""
            SELECT * FROM llm_artifacts
            WHERE scope <> 'document'
              AND document_version_id IS NULL
              AND superseded_by_id IS NULL
              AND (
                dirty = 1
                OR (expires_at IS NOT NULL AND expires_at < ?)
              )
              {membership_filter}
            ORDER BY generated_at ASC
            LIMIT ?
            """,
            (now_iso, int(limit)),
        ).fetchall()
        return [
            artifact
            for row in rows
            if (artifact := _readable_artifact(row, for_regeneration=True)) is not None
        ]
    except sqlite3.Error as exc:
        if strict:
            raise DirtyQueueReadError("dirty queue query failed") from exc
        # Best-effort: a drifted schema degrades to "nothing to drain" rather
        # than crashing the drain cron (see read_current for rationale).
        log.warning({"event": "artifact_drain_dirty_failed", "error": str(exc)})
        return []
    except (IndexError, TypeError, ValueError) as exc:
        if strict:
            raise DirtyQueueReadError("dirty queue row could not be decoded") from exc
        raise
    finally:
        conn.close()


def history(
    *,
    ticker: str,
    purpose: str,
    fiscal_period: str | None = None,
    scope: str = "ticker",
    limit: int = 20,
    db_path: Path | str | None = None,
) -> list[Artifact]:
    """Return all artifacts for the scope tuple (current + superseded), newest
    first. Powers "show me the evolution of META's bear case" queries."""
    conn = _open(db_path, role=SQLiteConnectionRole.READ_ONLY)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ?
              AND scope <> 'document'
              AND document_version_id IS NULL
              AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
            ORDER BY generated_at DESC
            LIMIT ?
            """,
            (ticker, scope, purpose, fiscal_period, int(limit)),
        ).fetchall()
        return [artifact for row in rows if (artifact := _readable_artifact(row)) is not None]
    except sqlite3.Error as exc:
        # Best-effort: a drifted schema degrades to empty history rather than
        # crashing the query surface (see read_current for rationale).
        log.warning({"event": "artifact_history_failed", "error": str(exc)})
        return []
    finally:
        conn.close()


def quarter_index(
    *,
    ticker: str,
    purpose: str,
    scope: str = "ticker",
    limit: int = 40,
    db_path: Path | str | None = None,
) -> list[Artifact]:
    """Return the current artifact for each fiscal period, newest period first.

    ``history`` stays scoped to one logical period and returns its supersession
    chain. This reader is the orthogonal ticker x quarter index: it omits
    superseded rows and spans every non-null fiscal period for the purpose.
    """
    conn = _open(db_path, role=SQLiteConnectionRole.READ_ONLY)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ?
              AND scope <> 'document'
              AND document_version_id IS NULL
              AND purpose = ?
              AND fiscal_period IS NOT NULL
              AND superseded_by_id IS NULL
            ORDER BY fiscal_period DESC, generated_at DESC
            LIMIT ?
            """,
            (ticker, scope, purpose, int(limit)),
        ).fetchall()
        return [artifact for row in rows if (artifact := _readable_artifact(row)) is not None]
    except sqlite3.Error as exc:
        log.warning({"event": "artifact_quarter_index_failed", "error": str(exc)})
        return []
    finally:
        conn.close()


def _open(
    db_path: Path | str | None,
    *,
    role: SQLiteConnectionRole = SQLiteConnectionRole.WRITER,
) -> sqlite3.Connection | None:
    """Open a connection or return None when the DB or table is unavailable.
    Best-effort pattern matches llm_call_ledger so the LLM pipeline never
    fails on telemetry."""
    try:
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = connect_sqlite(
            path,
            role=role,
            schema_preflight=role is SQLiteConnectionRole.WRITER,
        )
        # Verify table exists â€” graceful return otherwise
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        if cur is None:
            conn.close()
            return None
        return conn
    except WriterGateHeldError as exc:
        # A daily-chain writer-gate timeout while opening the WRITER connection:
        # the store stays best-effort (return None — the caller recomputes the
        # artifact), but the dropped cache write is OBSERVABLE: warning plus a
        # durable drop counter beside the database, never silence.
        path = resolve_db_path(db_path)
        log.warning(
            {
                "event": "artifact_store_open_gate_timeout",
                "role": str(role),
                "error": str(exc)[:200],
            }
        )
        if path is not None:
            try:
                from telemetry_health import DROPPED_LLM_ARTIFACT_WRITE, record_dropped_write

                record_dropped_write(
                    DROPPED_LLM_ARTIFACT_WRITE,
                    db_path=path,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # never escalate from a failure handler
                log.debug({"event": "artifact_drop_counter_failed"})
        return None
    except (sqlite3.Error, OSError) as exc:
        log.debug({"event": "artifact_store_open_failed", "error": str(exc)})
        return None
