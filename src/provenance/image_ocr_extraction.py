"""Governed OCR for standalone JPEG and PNG evidence documents.

This lane is evidence-native only. It never downloads an engine or model,
never trusts a filename as content identity, and never emits evidence from an
unaccepted OCR result. Every attempted image is bounded and hash-reverified
before its dimensions or text are inspected.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.evidence_ledger import (
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
)
from provenance.evidence_native_candidates import (
    EvidenceNativeDocumentCandidate,
    resolve_local_storage_uri,
    select_evidence_native_candidates_by_id,
)

IMAGE_OCR_EXTRACTOR_NAME = "governed-image-ocr"
IMAGE_OCR_EXTRACTOR_CODE_VERSION = "governed-image-ocr@1"
_EXTRACTOR_NAME = IMAGE_OCR_EXTRACTOR_NAME
_EXTRACTOR_CODE_VERSION = IMAGE_OCR_EXTRACTOR_CODE_VERSION
_DETECTOR_NAME = "bounded-image-header-preflight"
_DETECTOR_CODE_VERSION = "bounded-image-header-preflight@1"
_NORMALIZATION_VERSION = "nfkc-lines-v1"
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

AssessmentOutcome = Literal["ocr_required", "unsupported", "unreadable", "quarantined"]
ResultOutcome = Literal["accepted", "quarantined", "failed"]
PlanOutcome = Literal["covered", "assessment_only", "accepted", "quarantined", "failed"]


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


class ImageOCRRequest(BaseModel):
    """Strict controls for one bounded, dry-run-first image OCR batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    content_roots: tuple[Path, ...] = ()
    apply: bool = False
    batch_size: int = Field(default=25, ge=1, le=1_000)
    task_id: str = Field(
        default="image-ocr-evidence-backfill",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    document_version_ids: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("eng",)
    maximum_image_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=250 * 1024 * 1024)
    maximum_width: int = Field(default=20_000, ge=1, le=100_000)
    maximum_height: int = Field(default=20_000, ge=1, le=100_000)
    maximum_pixels: int = Field(default=40_000_000, ge=1, le=1_000_000_000)
    minimum_substantive_characters: int = Field(default=8, ge=1, le=10_000)
    minimum_mean_confidence: float = Field(default=50.0, ge=0.0, le=100.0)
    page_segmentation_mode: int = Field(default=6, ge=0, le=13)
    engine_mode: int = Field(default=1, ge=0, le=3)
    timeout_seconds: int = Field(default=120, ge=1, le=3_600)

    @field_validator("languages")
    @classmethod
    def _languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("languages must be non-empty and duplicate-free")
        if any(not item or not item.replace("_", "").isalnum() for item in value):
            raise ValueError("language codes may contain only letters, digits, and underscores")
        return value

    @model_validator(mode="after")
    def _documents(self) -> Self:
        if len(self.document_version_ids) != len(set(self.document_version_ids)):
            raise ValueError("document_version_ids must be unique")
        if len(self.document_version_ids) > self.batch_size:
            raise ValueError("explicit document versions cannot exceed batch_size")
        return self


class ImageOCRCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    last_evidence_rowid: int = Field(default=0, ge=0)
    last_document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at: datetime


class ImageOCRSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    mode: Literal["apply", "dry_run"]
    dry_run: bool
    batch_size: int
    run_at: datetime
    last_evidence_rowid_before: int
    last_evidence_rowid_after: int
    last_document_version_id_after: str | None
    has_more: bool
    documents_considered: int = 0
    documents_eligible: int = 0
    documents_accepted: int = 0
    documents_quarantined: int = 0
    documents_failed: int = 0
    documents_skipped_covered: int = 0
    records_planned: int = 0
    records_created: int = 0
    records_replayed: int = 0
    finding_counts: dict[str, int] = Field(default_factory=dict[str, int])


class ImageInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media_type: Literal["image/jpeg", "image/png"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def pixel_count(self) -> int:
        return self.width * self.height


class ImageOCREngineDescriptor(BaseModel):
    """Exact Tesseract binary and traineddata identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_name: str = Field(min_length=1, max_length=128)
    engine_version: str = Field(min_length=1, max_length=255)
    engine_binary_sha256: str
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=255)
    model_manifest_sha256: str
    model_artifacts: dict[str, str]

    _engine_hash = field_validator("engine_binary_sha256")(_validate_sha256)
    _model_hash = field_validator("model_manifest_sha256")(_validate_sha256)

    @field_validator("model_artifacts")
    @classmethod
    def _artifacts(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("model_artifacts must not be empty")
        return {key: _validate_sha256(digest) for key, digest in value.items()}

    @model_validator(mode="after")
    def _manifest(self) -> Self:
        if _canonical_sha256(self.model_artifacts) != self.model_manifest_sha256:
            raise ValueError("model manifest must hash canonical model_artifacts")
        return self


class ImageOCROutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    mean_confidence: float = Field(ge=0.0, le=100.0)


class ImageInspector(Protocol):
    def inspect(self, raw_bytes: bytes) -> ImageInspection: ...


class ImageOCRProvider(Protocol):
    @property
    def descriptor(self) -> ImageOCREngineDescriptor: ...

    def extract(
        self,
        raw_bytes: bytes,
        *,
        media_type: str,
        languages: tuple[str, ...],
        page_segmentation_mode: int,
        engine_mode: int,
        timeout_seconds: int,
    ) -> ImageOCROutput: ...


class ImageOCRProviderError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if not reason_code or len(reason_code) > 128:
            raise ValueError("image OCR failures require a bounded reason code")
        self.reason_code = reason_code
        super().__init__(reason_code)


class HeaderImageInspector:
    """Read only bounded PNG/JPEG headers; never decompress image pixels."""

    def inspect(self, raw_bytes: bytes) -> ImageInspection:
        if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            if len(raw_bytes) < 24 or raw_bytes[12:16] != b"IHDR":
                raise ImageOCRProviderError("malformed_png_header")
            width = int.from_bytes(raw_bytes[16:20], "big")
            height = int.from_bytes(raw_bytes[20:24], "big")
            if width <= 0 or height <= 0:
                raise ImageOCRProviderError("invalid_image_dimensions")
            return ImageInspection(media_type="image/png", width=width, height=height)
        if not raw_bytes.startswith(b"\xff\xd8"):
            raise ImageOCRProviderError("unsupported_image_format")
        offset = 2
        start_of_frame = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while offset < len(raw_bytes):
            while offset < len(raw_bytes) and raw_bytes[offset] != 0xFF:
                offset += 1
            while offset < len(raw_bytes) and raw_bytes[offset] == 0xFF:
                offset += 1
            if offset >= len(raw_bytes):
                break
            marker = raw_bytes[offset]
            offset += 1
            if marker in {0x01, *range(0xD0, 0xD9)}:
                continue
            if offset + 2 > len(raw_bytes):
                break
            segment_length = int.from_bytes(raw_bytes[offset : offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(raw_bytes):
                raise ImageOCRProviderError("malformed_jpeg_segment")
            if marker in start_of_frame:
                if segment_length < 7:
                    raise ImageOCRProviderError("malformed_jpeg_dimensions")
                height = int.from_bytes(raw_bytes[offset + 3 : offset + 5], "big")
                width = int.from_bytes(raw_bytes[offset + 5 : offset + 7], "big")
                if width <= 0 or height <= 0:
                    raise ImageOCRProviderError("invalid_image_dimensions")
                return ImageInspection(media_type="image/jpeg", width=width, height=height)
            if marker == 0xDA:
                break
            offset += segment_length
        raise ImageOCRProviderError("jpeg_dimensions_missing")


class TesseractImageProvider:
    """Explicit local-only Tesseract adapter with exact traineddata hashes."""

    def __init__(
        self,
        *,
        tesseract_executable: Path,
        tessdata_directory: Path,
        languages: tuple[str, ...],
    ) -> None:
        self._tesseract = _resolve_executable(tesseract_executable)
        self._tessdata = tessdata_directory.resolve()
        if not self._tessdata.is_dir():
            raise ImageOCRProviderError("tessdata_directory_missing")
        artifacts: dict[str, str] = {}
        for language in languages:
            path = self._tessdata / f"{language}.traineddata"
            if not path.is_file():
                raise ImageOCRProviderError("language_model_missing")
            artifacts[language] = _sha256_file(path)
        manifest = _canonical_sha256(artifacts)
        self._languages = languages
        self._descriptor = ImageOCREngineDescriptor(
            engine_name="tesseract-cli",
            engine_version=_binary_version(self._tesseract),
            engine_binary_sha256=_sha256_file(self._tesseract),
            model_name="tesseract-traineddata",
            model_version=f"model-manifest-sha256:{manifest}",
            model_manifest_sha256=manifest,
            model_artifacts=artifacts,
        )

    @property
    def descriptor(self) -> ImageOCREngineDescriptor:
        return self._descriptor

    def extract(
        self,
        raw_bytes: bytes,
        *,
        media_type: str,
        languages: tuple[str, ...],
        page_segmentation_mode: int,
        engine_mode: int,
        timeout_seconds: int,
    ) -> ImageOCROutput:
        if languages != self._languages:
            raise ImageOCRProviderError("language_configuration_mismatch")
        suffix = ".png" if media_type == "image/png" else ".jpg"
        try:
            with tempfile.TemporaryDirectory(prefix="earnings-summary-image-ocr-") as temporary:
                image_path = Path(temporary) / f"input{suffix}"
                image_path.write_bytes(raw_bytes)
                result = subprocess.run(
                    [
                        str(self._tesseract),
                        str(image_path),
                        "stdout",
                        "-l",
                        "+".join(languages),
                        "--tessdata-dir",
                        str(self._tessdata),
                        "--oem",
                        str(engine_mode),
                        "--psm",
                        str(page_segmentation_mode),
                        "tsv",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            raise ImageOCRProviderError("engine_timeout") from None
        except OSError:
            raise ImageOCRProviderError("local_ocr_runtime_error") from None
        if result.returncode != 0:
            raise ImageOCRProviderError("ocr_engine_failed")
        return parse_tesseract_tsv(result.stdout)


class _Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: EvidenceNativeDocumentCandidate
    observed_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    observed_byte_size: int | None = Field(default=None, ge=0)
    inspection: ImageInspection | None = None
    outcome: AssessmentOutcome
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)
    assessment_id: str = Field(min_length=1, max_length=128)
    assessed_at: datetime
    raw_bytes: bytes = Field(exclude=True, default=b"")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.outcome == "ocr_required":
            if self.reason_code is not None or self.inspection is None or not self.raw_bytes:
                raise ValueError("OCR-required image assessment must be complete")
        elif self.reason_code is None:
            raise ValueError("non-eligible image assessment requires a reason")
        return self


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment: _Assessment
    outcome: PlanOutcome
    config_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    run_id: str | None = Field(default=None, max_length=128)
    output: ImageOCROutput | None = None
    output_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reason_code: str | None = Field(default=None, max_length=128)
    recorded_at: datetime | None = None


def emit_structured_event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def backfill_image_ocr_evidence(
    conn: sqlite3.Connection,
    request: ImageOCRRequest,
    *,
    inspector: ImageInspector | None = None,
    provider: ImageOCRProvider | None = None,
) -> ImageOCRSummary:
    """Plan or atomically persist one bounded evidence-native image batch."""

    _require_schema(conn)
    if request.apply and provider is None:
        raise ValueError("image OCR apply requires an explicit local provider")
    if provider is not None and set(provider.descriptor.model_artifacts) != set(request.languages):
        raise ValueError("OCR model artifacts must exactly match requested languages")
    run_at = datetime.now(UTC)
    root = request.repo_root.resolve()
    roots = _allowed_roots(root, request.content_roots)
    checkpoint_path = root / ".tmp" / request.task_id / "state.json"
    targeted = bool(request.document_version_ids)
    checkpoint = (
        _read_checkpoint(checkpoint_path)
        if request.apply and not targeted
        else ImageOCRCheckpoint(updated_at=run_at)
    )
    candidates = (
        select_evidence_native_candidates_by_id(
            conn, document_version_ids=request.document_version_ids
        )
        if targeted
        else _select_candidates(
            conn,
            after_rowid=checkpoint.last_evidence_rowid,
            batch_size=request.batch_size,
        )
    )
    candidates = [candidate for candidate in candidates if _is_supported_candidate(candidate)]
    last_rowid = (
        checkpoint.last_evidence_rowid
        if not candidates
        else max(candidate.evidence_rowid for candidate in candidates)
    )
    last_version = None if not candidates else candidates[-1].document_version_id
    summary = ImageOCRSummary(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        batch_size=request.batch_size,
        run_at=run_at,
        last_evidence_rowid_before=checkpoint.last_evidence_rowid,
        last_evidence_rowid_after=last_rowid,
        last_document_version_id_after=last_version,
        has_more=False if targeted else _has_candidates_after(conn, last_rowid),
    )
    output_cache: dict[tuple[str, str], ImageOCROutput] = {}
    plans = [
        _plan_candidate(
            conn,
            candidate,
            request,
            roots,
            inspector or HeaderImageInspector(),
            provider,
            run_at,
            summary,
            output_cache,
        )
        for candidate in candidates
    ]
    if request.apply:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for plan in plans:
                _persist_plan(conn, request, provider, plan, summary)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if not targeted:
            _write_checkpoint(
                checkpoint_path,
                ImageOCRCheckpoint(
                    last_evidence_rowid=last_rowid,
                    last_document_version_id=last_version,
                    updated_at=run_at,
                ),
            )
        emit_structured_event(
            "image_ocr_evidence_backfill_applied",
            task_id=request.task_id,
            documents_accepted=summary.documents_accepted,
            documents_quarantined=summary.documents_quarantined,
            documents_failed=summary.documents_failed,
            records_created=summary.records_created,
        )
    else:
        emit_structured_event(
            "image_ocr_evidence_backfill_dry_run",
            task_id=request.task_id,
            documents_eligible=summary.documents_eligible,
            records_planned=summary.records_planned,
        )
    return summary


def _plan_candidate(
    conn: sqlite3.Connection,
    candidate: EvidenceNativeDocumentCandidate,
    request: ImageOCRRequest,
    roots: tuple[Path, ...],
    inspector: ImageInspector,
    provider: ImageOCRProvider | None,
    run_at: datetime,
    summary: ImageOCRSummary,
    output_cache: dict[tuple[str, str], ImageOCROutput],
) -> _Plan:
    summary.documents_considered += 1
    assessment = _assess_candidate(conn, candidate, request, roots, inspector, run_at)
    summary.records_planned += 1
    if assessment.outcome != "ocr_required":
        _quarantine(summary, assessment.reason_code or "image_assessment_failed")
        return _Plan(
            assessment=assessment,
            outcome="assessment_only",
            reason_code=assessment.reason_code,
        )
    summary.documents_eligible += 1
    if not request.apply:
        summary.records_planned += 5
        return _Plan(assessment=assessment, outcome="accepted")
    if provider is None:
        raise RuntimeError("apply plan lost its OCR provider")
    inspection = assessment.inspection
    if inspection is None:
        raise RuntimeError("eligible image assessment lost its dimensions")
    config_sha256 = _ocr_config_sha256(request, provider.descriptor)
    if _has_covered_run(conn, candidate.document_version_id, config_sha256):
        summary.documents_skipped_covered += 1
        return _Plan(
            assessment=assessment,
            outcome="covered",
            config_sha256=config_sha256,
        )
    cache_key = (candidate.blob_sha256, config_sha256)
    raw_output = output_cache.get(cache_key)
    if raw_output is None:
        raw_output = _accepted_blob_output(
            conn,
            blob_sha256=candidate.blob_sha256,
            config_sha256=config_sha256,
        )
    if raw_output is None:
        try:
            raw_output = provider.extract(
                assessment.raw_bytes,
                media_type=inspection.media_type,
                languages=request.languages,
                page_segmentation_mode=request.page_segmentation_mode,
                engine_mode=request.engine_mode,
                timeout_seconds=request.timeout_seconds,
            )
        except ImageOCRProviderError as error:
            # Failed run + governance + one explicit failed image result.
            summary.records_planned += 3
            summary.documents_failed += 1
            _finding(summary, error.reason_code)
            output_sha = _canonical_sha256({"outcome": "failed", "reason": error.reason_code})
            run_id = _run_id(candidate.document_version_id, config_sha256, output_sha)
            return _Plan(
                assessment=assessment,
                outcome="failed",
                config_sha256=config_sha256,
                run_id=run_id,
                output_sha256=output_sha,
                reason_code=error.reason_code,
                recorded_at=_existing_run_time(conn, run_id) or run_at,
            )
    output_cache[cache_key] = raw_output
    output = ImageOCROutput(
        text=_normalize_text(raw_output.text),
        mean_confidence=raw_output.mean_confidence,
    )
    output_sha = hashlib.sha256(output.text.encode("utf-8")).hexdigest()
    reason: str | None = None
    if _substantive_character_count(output.text) < request.minimum_substantive_characters:
        reason = "insufficient_ocr_text"
    elif output.mean_confidence < request.minimum_mean_confidence:
        reason = "confidence_below_threshold"
    outcome: PlanOutcome = "accepted" if reason is None else "quarantined"
    if reason is None:
        # Run + governance + document node + image passage + accepted result.
        summary.records_planned += 5
        summary.documents_accepted += 1
    else:
        # Failed run + governance + one explicit quarantined image result.
        summary.records_planned += 3
        _quarantine(summary, reason)
    run_fingerprint = _canonical_sha256(
        {
            "outcome": outcome,
            "output_sha256": output_sha,
            "mean_confidence": output.mean_confidence,
            "reason_code": reason,
        }
    )
    run_id = _run_id(candidate.document_version_id, config_sha256, run_fingerprint)
    return _Plan(
        assessment=assessment,
        outcome=outcome,
        config_sha256=config_sha256,
        run_id=run_id,
        output=output,
        output_sha256=output_sha,
        reason_code=reason,
        recorded_at=_existing_run_time(conn, run_id) or run_at,
    )


def _assess_candidate(
    conn: sqlite3.Connection,
    candidate: EvidenceNativeDocumentCandidate,
    request: ImageOCRRequest,
    roots: tuple[Path, ...],
    inspector: ImageInspector,
    run_at: datetime,
) -> _Assessment:
    observed_sha: str | None = None
    observed_size: int | None = None
    inspection: ImageInspection | None = None
    raw_bytes = b""
    outcome: AssessmentOutcome = "quarantined"
    reason: str | None = None
    path = resolve_local_storage_uri(candidate.storage_uri, allowed_roots=roots)
    if path is None:
        reason = "storage_uri_not_allowed_local_file"
    elif not path.is_file():
        reason = "content_missing"
    else:
        observed_size = path.stat().st_size
        if (
            candidate.byte_size > request.maximum_image_bytes
            or observed_size > request.maximum_image_bytes
        ):
            observed_sha = _sha256_file(path)
            if observed_sha != candidate.blob_sha256:
                reason = "sha256_mismatch"
            elif observed_size != candidate.byte_size:
                reason = "byte_size_mismatch"
            else:
                reason = "image_byte_limit_exceeded"
        else:
            raw_bytes = path.read_bytes()
            observed_size = len(raw_bytes)
            observed_sha = hashlib.sha256(raw_bytes).hexdigest()
            if observed_sha != candidate.blob_sha256:
                reason = "sha256_mismatch"
            elif observed_size != candidate.byte_size:
                reason = "byte_size_mismatch"
            else:
                blob = conn.execute(
                    "SELECT byte_size FROM evidence_content_blobs WHERE sha256 = ?",
                    (candidate.blob_sha256,),
                ).fetchone()
                if blob is None or not isinstance(blob[0], int) or blob[0] != observed_size:
                    reason = "evidence_blob_size_mismatch"
                else:
                    try:
                        inspection = inspector.inspect(raw_bytes)
                    except ImageOCRProviderError as error:
                        reason = error.reason_code
                        outcome = (
                            "unsupported"
                            if error.reason_code == "unsupported_image_format"
                            else "unreadable"
                        )
                    if inspection is not None:
                        if inspection.media_type != candidate.media_type.lower():
                            reason = "media_type_mismatch"
                            outcome = "unsupported"
                        elif (
                            inspection.width > request.maximum_width
                            or inspection.height > request.maximum_height
                            or inspection.pixel_count > request.maximum_pixels
                        ):
                            reason = "image_dimension_limit_exceeded"
                        else:
                            outcome = "ocr_required"
    detector_sha = _detector_config_sha256(request)
    semantic = {
        "document_version_id": candidate.document_version_id,
        "input_sha256": candidate.blob_sha256,
        "observed_sha256": observed_sha,
        "observed_byte_size": observed_size,
        "media_type": candidate.media_type.lower(),
        "width": None if inspection is None else inspection.width,
        "height": None if inspection is None else inspection.height,
        "outcome": outcome,
        "reason_code": reason,
        "detector_config_sha256": detector_sha,
    }
    assessment_id = "image-ocr-assessment:" + _canonical_sha256(semantic)
    assessed_at = _existing_assessment_time(conn, assessment_id) or run_at
    return _Assessment(
        candidate=candidate,
        observed_sha256=observed_sha,
        observed_byte_size=observed_size,
        inspection=inspection,
        outcome=outcome,
        reason_code=reason,
        assessment_id=assessment_id,
        assessed_at=assessed_at,
        raw_bytes=raw_bytes if outcome == "ocr_required" else b"",
    )


def _persist_plan(
    conn: sqlite3.Connection,
    request: ImageOCRRequest,
    provider: ImageOCRProvider | None,
    plan: _Plan,
    summary: ImageOCRSummary,
) -> None:
    assessment = plan.assessment
    inspection = assessment.inspection
    _persist_exact(
        conn,
        "image_ocr_assessments",
        (
            "assessment_id",
            "idempotency_key",
            "document_version_id",
            "input_sha256",
            "observed_sha256",
            "observed_byte_size",
            "media_type",
            "width",
            "height",
            "pixel_count",
            "page_count",
            "detector_name",
            "detector_config_sha256",
            "detector_code_version",
            "outcome",
            "reason_code",
            "assessed_at",
        ),
        (
            assessment.assessment_id,
            assessment.assessment_id,
            assessment.candidate.document_version_id,
            assessment.candidate.blob_sha256,
            assessment.observed_sha256,
            assessment.observed_byte_size,
            assessment.candidate.media_type.lower(),
            None if inspection is None else inspection.width,
            None if inspection is None else inspection.height,
            None if inspection is None else inspection.pixel_count,
            1 if inspection is not None else 0,
            _DETECTOR_NAME,
            _detector_config_sha256(request),
            _DETECTOR_CODE_VERSION,
            assessment.outcome,
            assessment.reason_code,
            assessment.assessed_at,
        ),
        ("assessment_id",),
        (assessment.assessment_id,),
        summary,
    )
    if plan.outcome in {"assessment_only", "covered"}:
        return
    if provider is None or plan.config_sha256 is None or plan.run_id is None:
        raise RuntimeError("OCR persistence plan is missing provider governance")
    recorded_at = plan.recorded_at
    if recorded_at is None:
        raise RuntimeError("OCR persistence plan is missing its clock")
    run_outcome = "succeeded" if plan.outcome == "accepted" else "failed"
    run_output_sha = _canonical_sha256(
        {
            "result_outcome": plan.outcome,
            "output_sha256": plan.output_sha256,
            "mean_confidence": None if plan.output is None else plan.output.mean_confidence,
            "reason_code": plan.reason_code,
        }
    )
    run = ExtractionRun(
        extraction_run_id=plan.run_id,
        idempotency_key=plan.run_id,
        document_version_id=assessment.candidate.document_version_id,
        input_sha256=assessment.candidate.blob_sha256,
        extractor_name=_EXTRACTOR_NAME,
        extractor_config_sha256=plan.config_sha256,
        extractor_code_version=_EXTRACTOR_CODE_VERSION,
        output_sha256=run_output_sha,
        started_at=recorded_at,
        completed_at=recorded_at,
        outcome=run_outcome,
    )
    _account(EvidenceLedger(conn).persist(run).created, summary)
    _persist_exact(
        conn,
        "image_ocr_extraction_governance",
        (
            "extraction_run_id",
            "assessment_id",
            "engine_name",
            "engine_version",
            "engine_binary_sha256",
            "model_name",
            "model_version",
            "model_manifest_sha256",
            "model_artifacts_json",
            "languages_json",
            "engine_config_json",
            "extractor_config_sha256",
            "recorded_at",
        ),
        (
            plan.run_id,
            assessment.assessment_id,
            provider.descriptor.engine_name,
            provider.descriptor.engine_version,
            provider.descriptor.engine_binary_sha256,
            provider.descriptor.model_name,
            provider.descriptor.model_version,
            provider.descriptor.model_manifest_sha256,
            _canonical_json(provider.descriptor.model_artifacts),
            _canonical_json(request.languages),
            _engine_config_json(request),
            plan.config_sha256,
            recorded_at,
        ),
        ("extraction_run_id",),
        (plan.run_id,),
        summary,
    )
    node: EvidenceNode | None = None
    if plan.outcome == "accepted":
        if plan.output is None:
            raise RuntimeError("accepted OCR plan has no output")
        locator = EvidenceLocator(source_ref=assessment.candidate.source_ref, page_number=1)
        document_node = _new_node(
            conn,
            evidence_key=f"image-ocr-document:{_stable_token(assessment.candidate.document_version_id)}",
            node_id=_node_id(plan.run_id, "document"),
            run_id=plan.run_id,
            parent_node_id=None,
            node_kind="document",
            text=(
                "Governed OCR for standalone image evidence document "
                f"{assessment.candidate.document_version_id}."
            ),
            locator=EvidenceLocator(source_ref=assessment.candidate.source_ref),
            recorded_at=recorded_at,
        )
        _account(EvidenceLedger(conn).persist(document_node).created, summary)
        node = _new_node(
            conn,
            evidence_key=f"image-ocr-content:{_stable_token(assessment.candidate.document_version_id)}",
            node_id=_node_id(plan.run_id, "image-page-1"),
            run_id=plan.run_id,
            parent_node_id=document_node.node_id,
            node_kind="passage",
            text=plan.output.text,
            locator=locator,
            recorded_at=recorded_at,
        )
        _account(EvidenceLedger(conn).persist(node).created, summary)
    locator = EvidenceLocator(source_ref=assessment.candidate.source_ref, page_number=1)
    result_outcome: ResultOutcome
    if plan.outcome == "accepted":
        result_outcome = "accepted"
    elif plan.outcome == "quarantined":
        result_outcome = "quarantined"
    else:
        result_outcome = "failed"
    _persist_exact(
        conn,
        "image_ocr_results",
        (
            "extraction_run_id",
            "page_number",
            "node_id",
            "outcome",
            "output_sha256",
            "mean_confidence",
            "locator_json",
            "locator_sha256",
            "reason_code",
            "recorded_at",
        ),
        (
            plan.run_id,
            1,
            None if node is None else node.node_id,
            result_outcome,
            plan.output_sha256 if result_outcome != "failed" else None,
            None
            if plan.output is None or result_outcome == "failed"
            else plan.output.mean_confidence,
            locator.canonical_json,
            locator.canonical_sha256,
            plan.reason_code,
            recorded_at,
        ),
        ("extraction_run_id",),
        (plan.run_id,),
        summary,
    )


def parse_tesseract_tsv(raw_tsv: bytes) -> ImageOCROutput:
    """Parse exact TSV bytes and reject malformed or confidence-free output."""

    try:
        decoded = raw_tsv.decode("utf-8")
    except UnicodeDecodeError:
        raise ImageOCRProviderError("tsv_not_utf8") from None
    reader = csv.DictReader(io.StringIO(decoded), delimiter="\t")
    required = {"page_num", "block_num", "par_num", "line_num", "word_num", "conf", "text"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ImageOCRProviderError("tsv_schema_mismatch")
    lines: dict[tuple[int, int, int, int], list[tuple[int, str]]] = {}
    confidences: list[float] = []
    try:
        for row in reader:
            text = _normalize_text(row.get("text") or "")
            if not text:
                continue
            confidence = float(row["conf"])
            if confidence < 0:
                continue
            if confidence > 100:
                raise ValueError
            line_key = (
                int(row["page_num"]),
                int(row["block_num"]),
                int(row["par_num"]),
                int(row["line_num"]),
            )
            word_number = int(row["word_num"])
            lines.setdefault(line_key, []).append((word_number, text))
            confidences.append(confidence)
    except (KeyError, TypeError, ValueError):
        raise ImageOCRProviderError("tsv_value_invalid") from None
    ordered_lines = [
        " ".join(text for _, text in sorted(words)) for _, words in sorted(lines.items())
    ]
    text = _normalize_text("\n".join(ordered_lines))
    if not confidences:
        return ImageOCROutput(text=text, mean_confidence=0.0)
    return ImageOCROutput(
        text=text,
        mean_confidence=sum(confidences) / len(confidences),
    )


def _select_candidates(
    conn: sqlite3.Connection, *, after_rowid: int, batch_size: int
) -> list[EvidenceNativeDocumentCandidate]:
    rows = conn.execute(
        "SELECT document.rowid, document.document_version_id, "
        "lower(document.blob_sha256), blob.byte_size, lower(blob.media_type), "
        "COALESCE((SELECT location.storage_uri "
        "FROM v_evidence_blob_locations_current AS location "
        "WHERE location.blob_sha256 = document.blob_sha256 "
        "AND location.location_kind = 'local' "
        "AND location.availability_state = 'present' "
        "AND location.verified_sha256 = document.blob_sha256 "
        "AND location.verified_byte_size = blob.byte_size "
        "ORDER BY location.verified_at DESC, location.storage_uri LIMIT 1), "
        "blob.storage_uri), observation.source_url, document.recorded_at "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = document.observation_id "
        "WHERE document.legacy_document_id IS NULL AND document.rowid > ? "
        "AND lower(blob.media_type) IN ('image/jpeg', 'image/png') "
        "ORDER BY document.rowid LIMIT ?",
        (after_rowid, batch_size),
    ).fetchall()
    return [
        EvidenceNativeDocumentCandidate(
            evidence_rowid=_positive_integer(row[0], "evidence_rowid"),
            document_version_id=_text(row[1], "document_version_id"),
            blob_sha256=_text(row[2], "blob_sha256"),
            byte_size=_nonnegative_integer(row[3], "byte_size"),
            media_type=_text(row[4], "media_type"),
            storage_uri=_text(row[5], "storage_uri"),
            source_ref=_text(row[6], "source_ref"),
            recorded_at=_datetime(row[7], "recorded_at"),
        )
        for row in rows
    ]


def _has_candidates_after(conn: sqlite3.Connection, after_rowid: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evidence_document_versions AS document "
            "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
            "WHERE document.legacy_document_id IS NULL AND document.rowid > ? "
            "AND lower(blob.media_type) IN ('image/jpeg', 'image/png') LIMIT 1",
            (after_rowid,),
        ).fetchone()
        is not None
    )


def _is_supported_candidate(candidate: EvidenceNativeDocumentCandidate) -> bool:
    return candidate.media_type.lower() in _IMAGE_MEDIA_TYPES


def _has_covered_run(
    conn: sqlite3.Connection, document_version_id: str, config_sha256: str
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evidence_extraction_runs AS run "
            "JOIN image_ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = run.extraction_run_id "
            "JOIN image_ocr_results AS result "
            "ON result.extraction_run_id = run.extraction_run_id "
            "WHERE run.document_version_id = ? AND run.extractor_name = ? "
            "AND run.extractor_config_sha256 = ? AND run.outcome = 'succeeded' "
            "AND result.outcome = 'accepted' LIMIT 1",
            (document_version_id, _EXTRACTOR_NAME, config_sha256),
        ).fetchone()
        is not None
    )


def _accepted_blob_output(
    conn: sqlite3.Connection, *, blob_sha256: str, config_sha256: str
) -> ImageOCROutput | None:
    """Reuse only an exact, accepted immutable output for these bytes/config."""

    rows = conn.execute(
        "SELECT DISTINCT node.text, result.mean_confidence, result.output_sha256 "
        "FROM evidence_extraction_runs AS run "
        "JOIN image_ocr_extraction_governance AS governance "
        "ON governance.extraction_run_id = run.extraction_run_id "
        "JOIN image_ocr_results AS result "
        "ON result.extraction_run_id = run.extraction_run_id "
        "JOIN evidence_nodes AS node ON node.node_id = result.node_id "
        "WHERE run.input_sha256 = ? AND run.extractor_name = ? "
        "AND run.extractor_config_sha256 = ? AND run.outcome = 'succeeded' "
        "AND result.outcome = 'accepted' "
        "ORDER BY run.completed_at, run.extraction_run_id LIMIT 2",
        (blob_sha256, _EXTRACTOR_NAME, config_sha256),
    ).fetchall()
    if not rows:
        return None
    normalized = {
        (
            _text(row[0], "accepted image OCR text"),
            float(str(row[1])),
            _text(row[2], "accepted image OCR output hash"),
        )
        for row in rows
    }
    if len(normalized) != 1:
        raise RuntimeError("accepted image OCR outputs conflict for identical bytes/config")
    text, confidence, output_sha256 = normalized.pop()
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != output_sha256:
        raise RuntimeError("accepted image OCR output hash does not match stored text")
    return ImageOCROutput(text=text, mean_confidence=confidence)


def _new_node(
    conn: sqlite3.Connection,
    *,
    evidence_key: str,
    node_id: str,
    run_id: str,
    parent_node_id: str | None,
    node_kind: Literal["document", "passage"],
    text: str,
    locator: EvidenceLocator,
    recorded_at: datetime,
) -> EvidenceNode:
    row = conn.execute(
        "SELECT node_id, revision, extraction_run_id, parent_node_id, node_kind, "
        "text, locator_json, locator_sha256, recorded_at "
        "FROM evidence_nodes "
        "WHERE evidence_key = ? ORDER BY revision DESC LIMIT 1",
        (evidence_key,),
    ).fetchone()
    if row is not None and str(row[2]) == run_id:
        return EvidenceNode(
            node_id=str(row[0]),
            evidence_key=evidence_key,
            revision=int(str(row[1])),
            extraction_run_id=run_id,
            parent_node_id=None if row[3] is None else str(row[3]),
            node_kind=node_kind,
            text=str(row[5]),
            locator=locator,
            locator_sha256=str(row[7]),
            recorded_at=_datetime(row[8], "node recorded_at"),
        )
    return EvidenceNode(
        node_id=node_id,
        evidence_key=evidence_key,
        revision=1 if row is None else int(str(row[1])) + 1,
        extraction_run_id=run_id,
        parent_node_id=parent_node_id,
        supersedes_node_id=None if row is None else str(row[0]),
        node_kind=node_kind,
        text=text,
        locator=locator,
        locator_sha256=locator.canonical_sha256,
        recorded_at=recorded_at,
    )


def _persist_exact(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    key_columns: tuple[str, ...],
    key_values: tuple[object, ...],
    summary: ImageOCRSummary,
) -> None:
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        "ON CONFLICT DO NOTHING",
        values,
    )
    if cursor.rowcount == 1:
        summary.records_created += 1
        return
    where = " AND ".join(f"{column} = ?" for column in key_columns)
    stored = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where}",
        key_values,
    ).fetchone()
    if stored is None or not _stored_values_match(tuple(stored), values):
        raise ValueError(f"immutable {table} identity conflicts")
    summary.records_replayed += 1


def _stored_values_match(stored: tuple[object, ...], supplied: tuple[object, ...]) -> bool:
    if len(stored) != len(supplied):
        return False
    for actual, expected in zip(stored, supplied, strict=True):
        if isinstance(expected, datetime):
            try:
                actual_time = datetime.fromisoformat(str(actual)).replace(tzinfo=None)
            except ValueError:
                return False
            if actual_time != expected.replace(tzinfo=None):
                return False
        elif isinstance(expected, bool):
            if int(str(actual)) != int(expected):
                return False
        elif actual != expected:
            return False
    return True


def _detector_config_sha256(request: ImageOCRRequest) -> str:
    return _canonical_sha256(
        {
            "detector_name": _DETECTOR_NAME,
            "detector_code_version": _DETECTOR_CODE_VERSION,
            "maximum_image_bytes": request.maximum_image_bytes,
            "maximum_width": request.maximum_width,
            "maximum_height": request.maximum_height,
            "maximum_pixels": request.maximum_pixels,
            "media_types": sorted(_IMAGE_MEDIA_TYPES),
        }
    )


def _engine_config_json(request: ImageOCRRequest) -> str:
    return _canonical_json(
        {
            "engine_mode": request.engine_mode,
            "languages": request.languages,
            "minimum_mean_confidence": request.minimum_mean_confidence,
            "minimum_substantive_characters": request.minimum_substantive_characters,
            "normalization_version": _NORMALIZATION_VERSION,
            "page_segmentation_mode": request.page_segmentation_mode,
            "timeout_seconds": request.timeout_seconds,
        }
    )


def _ocr_config_sha256(request: ImageOCRRequest, descriptor: ImageOCREngineDescriptor) -> str:
    return _canonical_sha256(
        {
            "extractor_name": _EXTRACTOR_NAME,
            "extractor_code_version": _EXTRACTOR_CODE_VERSION,
            "detector_config_sha256": _detector_config_sha256(request),
            "engine": descriptor.model_dump(mode="json"),
            "engine_config": json.loads(_engine_config_json(request)),
        }
    )


def _existing_assessment_time(conn: sqlite3.Connection, assessment_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT assessed_at FROM image_ocr_assessments WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    return None if row is None else _datetime(row[0], "assessed_at")


def _existing_run_time(conn: sqlite3.Connection, run_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT started_at FROM evidence_extraction_runs WHERE extraction_run_id = ?",
        (run_id,),
    ).fetchone()
    return None if row is None else _datetime(row[0], "started_at")


def _assessment_tables() -> set[str]:
    return {
        "evidence_content_blobs",
        "evidence_source_observations",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "image_ocr_assessments",
        "image_ocr_extraction_governance",
        "image_ocr_results",
    }


def _require_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if missing := sorted(_assessment_tables() - tables):
        raise RuntimeError("image OCR schema is incomplete: " + ", ".join(missing))
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' "
            "AND name = 'v_evidence_blob_locations_current'"
        ).fetchone()
        is None
    ):
        raise RuntimeError("image OCR requires v_evidence_blob_locations_current")


def _read_checkpoint(path: Path) -> ImageOCRCheckpoint:
    if not path.exists():
        return ImageOCRCheckpoint(updated_at=datetime.now(UTC))
    return ImageOCRCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))


def _write_checkpoint(path: Path, checkpoint: ImageOCRCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _allowed_roots(root: Path, content_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots = [root, *(path.resolve() for path in content_roots)]
    return tuple(dict.fromkeys(path.resolve() for path in roots))


def _run_id(document_version_id: str, config_sha256: str, output_sha256: str) -> str:
    return "image-ocr-run:" + _stable_token(
        f"{document_version_id}\0{config_sha256}\0{output_sha256}"
    )


def _node_id(run_id: str, suffix: str) -> str:
    return "image-ocr-node:" + _stable_token(f"{run_id}\0{suffix}")


def _stable_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.strip() for line in normalized.splitlines() if line.strip())


def _substantive_character_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_executable(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ImageOCRProviderError("tesseract_executable_missing")
    return resolved


def _binary_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ImageOCRProviderError("tesseract_version_unavailable") from None
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace").strip()
    first_line = output.splitlines()[0] if output else ""
    if result.returncode != 0 or not first_line:
        raise ImageOCRProviderError("tesseract_version_unavailable")
    return first_line[:255]


def _finding(summary: ImageOCRSummary, reason: str) -> None:
    summary.finding_counts = dict(Counter(summary.finding_counts) + Counter({reason: 1}))


def _quarantine(summary: ImageOCRSummary, reason: str) -> None:
    summary.documents_quarantined += 1
    _finding(summary, reason)


def _account(created: bool, summary: ImageOCRSummary) -> None:
    if created:
        summary.records_created += 1
    else:
        summary.records_replayed += 1


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be non-empty text")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative integer")
    return value


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"{name} must be ISO-8601") from error
    raise RuntimeError(f"{name} must be a datetime")
