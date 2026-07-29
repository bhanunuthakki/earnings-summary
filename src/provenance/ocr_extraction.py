"""Governed OCR for PDF pages that deterministic native extraction cannot cover.

The lane has two distinct, append-only decisions.  A deterministic preflight
first records whether each page has sufficient native text.  Only pages marked
as requiring OCR can reach an explicitly supplied OCR provider.  Engine,
renderer, language-model artifacts, configuration, input bytes, page outputs,
confidence, and locators are all hash-bound to the resulting evidence run.

No OCR dependency is imported or model downloaded merely by importing this
module.  The production Tesseract adapter is constructed only by the dedicated
CLI when the user explicitly applies an OCR batch.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.evidence_ledger import (
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
)
from provenance.evidence_native_candidates import (
    EvidenceNativeDocumentCandidate,
    has_evidence_native_after,
    resolve_local_storage_uri,
    select_evidence_native_candidates,
    select_evidence_native_candidates_by_id,
)

_EXTRACTOR_NAME = "governed-pdf-ocr"
_EXTRACTOR_CODE_VERSION = "governed-pdf-ocr@1"
_DETECTOR_NAME = "pypdf-native-text-preflight"
_DETECTOR_CODE_VERSION = "pypdf-native-text-preflight@1"
_NORMALIZATION_VERSION = "nfkc-lines-v1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

PreflightOutcome = Literal[
    "native_sufficient", "ocr_required", "encrypted", "unreadable", "unsupported"
]
PageResultOutcome = Literal["accepted", "quarantined", "failed"]
_SourceLane = Literal["legacy", "evidence_native"]


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


class OCRBackfillRequest(BaseModel):
    """Validated controls for one bounded, dry-run-first OCR batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    content_roots: tuple[Path, ...] = ()
    apply: bool = False
    batch_size: int = Field(default=25, ge=1, le=1_000)
    source_lane: _SourceLane = "legacy"
    task_id: str = Field(default="ocr-evidence-backfill", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    document_version_ids: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("eng",)
    minimum_native_characters: int = Field(default=32, ge=1, le=10_000)
    minimum_mean_confidence: float = Field(default=50.0, ge=0.0, le=100.0)
    dpi: int = Field(default=300, ge=72, le=1_200)
    page_segmentation_mode: int = Field(default=6, ge=0, le=13)
    engine_mode: int = Field(default=1, ge=0, le=3)
    timeout_seconds: int = Field(default=120, ge=1, le=3_600)

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("languages must be a non-empty, duplicate-free tuple")
        if any(not language or not language.replace("_", "").isalnum() for language in value):
            raise ValueError("languages may contain only letters, digits, and underscores")
        return value

    @model_validator(mode="after")
    def _validate_explicit_documents(self) -> OCRBackfillRequest:
        if len(self.document_version_ids) != len(set(self.document_version_ids)):
            raise ValueError("document_version_ids must be unique")
        if self.document_version_ids and self.source_lane != "evidence_native":
            raise ValueError("explicit document versions require the evidence_native lane")
        if len(self.document_version_ids) > self.batch_size:
            raise ValueError("explicit document versions cannot exceed batch_size")
        return self


class OCRBackfillCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_lane: _SourceLane = "legacy"
    last_document_id: int = Field(default=0, ge=0)
    last_evidence_rowid: int = Field(default=0, ge=0)
    last_document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at: datetime


class OCRBackfillSummary(BaseModel):
    """Closed accounting for preflight, OCR, quarantine, and persistence."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    source_lane: _SourceLane
    mode: Literal["apply", "dry_run"]
    dry_run: bool
    batch_size: int
    run_at: datetime
    last_document_id_before: int
    last_document_id_after: int
    last_evidence_rowid_before: int = 0
    last_evidence_rowid_after: int = 0
    last_document_version_id_after: str | None = None
    has_more: bool
    documents_considered: int = 0
    documents_requiring_ocr: int = 0
    documents_native_sufficient: int = 0
    documents_ocr_succeeded: int = 0
    documents_ocr_failed: int = 0
    documents_skipped_covered: int = 0
    documents_quarantined: int = 0
    pages_requiring_ocr: int = 0
    pages_ocr_accepted: int = 0
    records_planned: int = 0
    records_created: int = 0
    records_replayed: int = 0
    finding_counts: dict[str, int] = Field(default_factory=dict[str, int])


class PDFPreflightPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    native_character_count: int = Field(ge=0)
    native_text_sha256: str = Field(pattern=_SHA256_PATTERN)
    requires_ocr: bool


class PDFPreflight(BaseModel):
    """Hash-only native-text assessment; source text is not duplicated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: PreflightOutcome
    page_count: int = Field(ge=0)
    pages: list[PDFPreflightPage]
    native_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_shape(self) -> PDFPreflight:
        page_numbers = [page.page_number for page in self.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("preflight page numbers must be unique")
        if page_numbers and (min(page_numbers) != 1 or max(page_numbers) != self.page_count):
            raise ValueError("preflight pages must cover 1..page_count")
        if len(page_numbers) != self.page_count:
            raise ValueError("preflight must contain exactly page_count pages")
        if self.outcome in {"native_sufficient", "ocr_required"}:
            if self.reason_code is not None:
                raise ValueError("successful preflight outcomes cannot carry a reason")
            any_required = any(page.requires_ocr for page in self.pages)
            if (self.outcome == "ocr_required") != any_required:
                raise ValueError("ocr_required must agree with page assessments")
        elif self.reason_code is None:
            raise ValueError("failed preflight outcomes require a reason")
        return self


class OCREngineDescriptor(BaseModel):
    """Exact local binaries and model artifact manifest used by an OCR run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_name: str = Field(min_length=1, max_length=128)
    engine_version: str = Field(min_length=1, max_length=255)
    engine_binary_sha256: str
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=255)
    model_manifest_sha256: str
    model_artifacts: dict[str, str]
    renderer_name: str = Field(min_length=1, max_length=128)
    renderer_version: str = Field(min_length=1, max_length=255)
    renderer_binary_sha256: str

    _engine_hash = field_validator("engine_binary_sha256")(_validate_sha256)
    _model_hash = field_validator("model_manifest_sha256")(_validate_sha256)
    _renderer_hash = field_validator("renderer_binary_sha256")(_validate_sha256)

    @field_validator("model_artifacts")
    @classmethod
    def _validate_model_artifacts(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("model_artifacts must identify at least one exact artifact")
        return {name: _validate_sha256(digest) for name, digest in value.items()}

    @model_validator(mode="after")
    def _bind_model_manifest(self) -> OCREngineDescriptor:
        if _canonical_sha256(self.model_artifacts) != self.model_manifest_sha256:
            raise ValueError("model_manifest_sha256 must hash canonical model_artifacts")
        return self


class OCRPageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    text: str
    mean_confidence: float = Field(ge=0.0, le=100.0)


class PDFInspector(Protocol):
    def inspect(self, raw_bytes: bytes, *, minimum_native_characters: int) -> PDFPreflight: ...


class OCRProvider(Protocol):
    @property
    def descriptor(self) -> OCREngineDescriptor: ...

    def extract_pages(
        self,
        raw_bytes: bytes,
        *,
        page_numbers: tuple[int, ...],
        languages: tuple[str, ...],
        dpi: int,
        page_segmentation_mode: int,
        engine_mode: int,
        timeout_seconds: int,
    ) -> list[OCRPageOutput]: ...


class OCRProviderError(RuntimeError):
    """A classified, safe-to-record local OCR execution failure."""

    def __init__(self, reason_code: str) -> None:
        if not reason_code or len(reason_code) > 128:
            raise ValueError("OCR provider failure requires a bounded reason code")
        self.reason_code = reason_code
        super().__init__(reason_code)


class PypdfPDFInspector:
    """Deterministic native text preflight; pypdf is imported on use."""

    def inspect(self, raw_bytes: bytes, *, minimum_native_characters: int) -> PDFPreflight:
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw_bytes))
            if reader.is_encrypted:
                return _failed_preflight("encrypted", "encrypted_pdf")
            pages: list[tuple[int, str]] = []
            for page_number, page in enumerate(reader.pages, start=1):
                pages.append((page_number, _normalize_text(page.extract_text() or "")))
        except Exception:
            return _failed_preflight("unreadable", "unreadable_pdf")
        if not pages:
            return _failed_preflight("unreadable", "pdf_has_no_pages")
        page_results = [
            PDFPreflightPage(
                page_number=page_number,
                native_character_count=_substantive_character_count(text),
                native_text_sha256=_sha256_text(text),
                requires_ocr=_substantive_character_count(text) < minimum_native_characters,
            )
            for page_number, text in pages
        ]
        native_output_sha256 = _canonical_sha256(
            {
                "normalization_version": _NORMALIZATION_VERSION,
                "pages": [
                    {"page_number": page_number, "text": text} for page_number, text in pages
                ],
            }
        )
        return PDFPreflight(
            outcome=(
                "ocr_required"
                if any(page.requires_ocr for page in page_results)
                else "native_sufficient"
            ),
            page_count=len(page_results),
            pages=page_results,
            native_output_sha256=native_output_sha256,
            reason_code=None,
        )


class TesseractCLIProvider:
    """Local-only Poppler/Tesseract adapter with hash-bound traineddata."""

    def __init__(
        self,
        *,
        tesseract_executable: Path,
        pdftoppm_executable: Path,
        tessdata_directory: Path,
        languages: tuple[str, ...],
    ) -> None:
        self._tesseract = _resolve_executable(tesseract_executable)
        self._pdftoppm = _resolve_executable(pdftoppm_executable)
        self._tessdata = tessdata_directory.resolve()
        if not self._tessdata.is_dir():
            raise OCRProviderError("tessdata_directory_missing")
        language_hashes: dict[str, str] = {}
        for language in languages:
            traineddata = self._tessdata / f"{language}.traineddata"
            if not traineddata.is_file():
                raise OCRProviderError("language_model_missing")
            language_hashes[language] = _sha256_file(traineddata)
        model_manifest_sha256 = _canonical_sha256(language_hashes)
        self._languages = languages
        self._descriptor = OCREngineDescriptor(
            engine_name="tesseract-cli",
            engine_version=_binary_version(self._tesseract, ("--version",)),
            engine_binary_sha256=_sha256_file(self._tesseract),
            model_name="tesseract-traineddata",
            model_version=f"model-manifest-sha256:{model_manifest_sha256}",
            model_manifest_sha256=model_manifest_sha256,
            model_artifacts=language_hashes,
            renderer_name="poppler-pdftoppm",
            renderer_version=_binary_version(self._pdftoppm, ("-v",)),
            renderer_binary_sha256=_sha256_file(self._pdftoppm),
        )

    @property
    def descriptor(self) -> OCREngineDescriptor:
        return self._descriptor

    def extract_pages(
        self,
        raw_bytes: bytes,
        *,
        page_numbers: tuple[int, ...],
        languages: tuple[str, ...],
        dpi: int,
        page_segmentation_mode: int,
        engine_mode: int,
        timeout_seconds: int,
    ) -> list[OCRPageOutput]:
        if languages != self._languages:
            raise OCRProviderError("language_configuration_mismatch")
        outputs: list[OCRPageOutput] = []
        try:
            with tempfile.TemporaryDirectory(prefix="earnings-summary-ocr-") as temporary:
                temporary_root = Path(temporary)
                pdf_path = temporary_root / "input.pdf"
                pdf_path.write_bytes(raw_bytes)
                for page_number in page_numbers:
                    prefix = temporary_root / f"page-{page_number}"
                    render = subprocess.run(
                        [
                            str(self._pdftoppm),
                            "-f",
                            str(page_number),
                            "-l",
                            str(page_number),
                            "-r",
                            str(dpi),
                            "-png",
                            "-singlefile",
                            str(pdf_path),
                            str(prefix),
                        ],
                        check=False,
                        capture_output=True,
                        timeout=timeout_seconds,
                    )
                    if render.returncode != 0:
                        raise OCRProviderError("renderer_failed")
                    image_path = prefix.with_suffix(".png")
                    if not image_path.is_file():
                        raise OCRProviderError("renderer_output_missing")
                    ocr = subprocess.run(
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
                    if ocr.returncode != 0:
                        raise OCRProviderError("ocr_engine_failed")
                    outputs.append(_parse_tesseract_tsv(page_number, ocr.stdout))
        except subprocess.TimeoutExpired:
            raise OCRProviderError("engine_timeout") from None
        except OSError:
            raise OCRProviderError("local_ocr_runtime_error") from None
        return outputs


class _DocumentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int | None = Field(default=None, gt=0)
    evidence_rowid: int | None = Field(default=None, gt=0)
    file_path: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    document_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_bytes_size: int | None = Field(default=None, ge=0)
    document_version_id: str | None = Field(default=None, max_length=128)
    blob_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    recorded_at: datetime | None = None


class _PlannedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int = Field(gt=0)
    locator: EvidenceLocator
    outcome: PageResultOutcome
    text: str | None
    output_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    mean_confidence: float | None = Field(default=None, ge=0.0, le=100.0)
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)


class _CandidatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: _DocumentCandidate
    preflight: PDFPreflight | None
    config_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    outcome: Literal[
        "unrecordable",
        "native_sufficient",
        "preflight_quarantined",
        "assessment_only",
        "covered",
        "ocr_succeeded",
        "ocr_failed",
    ]
    reason_code: str | None
    pages: list[_PlannedPage] = Field(default_factory=list[_PlannedPage])


def emit_structured_event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def backfill_ocr_evidence(
    conn: sqlite3.Connection,
    request: OCRBackfillRequest,
    *,
    inspector: PDFInspector | None = None,
    provider: OCRProvider | None = None,
) -> OCRBackfillSummary:
    """Assess and optionally OCR one bounded batch with one writer transaction."""

    _require_tables(conn, request.source_lane)
    root = request.repo_root.resolve()
    allowed_roots = _allowed_content_roots(request, root)
    checkpoint_path = _checkpoint_path(root, request)
    targeted = bool(request.document_version_ids)
    checkpoint = (
        _read_checkpoint(checkpoint_path, request.source_lane)
        if request.apply and not targeted
        else OCRBackfillCheckpoint(source_lane=request.source_lane, updated_at=datetime.now(UTC))
    )
    if request.source_lane == "evidence_native":
        candidates = (
            [
                _candidate_from_evidence_native(candidate)
                for candidate in select_evidence_native_candidates_by_id(
                    conn,
                    document_version_ids=request.document_version_ids,
                )
            ]
            if targeted
            else _evidence_native_candidates_after(
                conn, checkpoint.last_evidence_rowid, request.batch_size
            )
        )
    else:
        candidates = _candidates_after(conn, checkpoint.last_document_id, request.batch_size)
    summary = OCRBackfillSummary(
        task_id=request.task_id,
        source_lane=request.source_lane,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        batch_size=request.batch_size,
        run_at=datetime.now(UTC),
        last_document_id_before=checkpoint.last_document_id,
        last_document_id_after=checkpoint.last_document_id,
        last_evidence_rowid_before=checkpoint.last_evidence_rowid,
        last_evidence_rowid_after=checkpoint.last_evidence_rowid,
        last_document_version_id_after=checkpoint.last_document_version_id,
        has_more=False,
    )
    selected_inspector = inspector or PypdfPDFInspector()
    plans = [
        _plan_candidate(
            conn, candidate, allowed_roots, request, selected_inspector, provider, summary
        )
        for candidate in candidates
    ]
    if request.apply:
        if conn.in_transaction:
            raise RuntimeError("OCR backfill requires an idle SQLite connection")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for plan in plans:
                _persist_plan(conn, plan, request, provider, summary)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    if candidates:
        final_candidate = candidates[-1]
        if final_candidate.document_id is not None:
            summary.last_document_id_after = final_candidate.document_id
        if final_candidate.evidence_rowid is not None:
            summary.last_evidence_rowid_after = final_candidate.evidence_rowid
            summary.last_document_version_id_after = final_candidate.document_version_id
    summary.has_more = (
        False
        if targeted
        else (
            has_evidence_native_after(conn, summary.last_evidence_rowid_after, pdf_only=True)
            if request.source_lane == "evidence_native"
            else _has_documents_after(conn, summary.last_document_id_after)
        )
    )
    if request.apply and not targeted:
        _write_checkpoint(
            checkpoint_path,
            OCRBackfillCheckpoint(
                source_lane=request.source_lane,
                last_document_id=summary.last_document_id_after,
                last_evidence_rowid=summary.last_evidence_rowid_after,
                last_document_version_id=summary.last_document_version_id_after,
                updated_at=datetime.now(UTC),
            ),
        )
    if request.apply:
        emit_structured_event(
            "ocr_evidence_backfill_completed",
            task_id=request.task_id,
            records_created=summary.records_created,
            documents_ocr_succeeded=summary.documents_ocr_succeeded,
            documents_ocr_failed=summary.documents_ocr_failed,
        )
    else:
        emit_structured_event(
            "ocr_evidence_backfill_dry_run",
            task_id=request.task_id,
            documents_requiring_ocr=summary.documents_requiring_ocr,
            pages_requiring_ocr=summary.pages_requiring_ocr,
            records_planned=summary.records_planned,
        )
    return summary


def _plan_candidate(
    conn: sqlite3.Connection,
    candidate: _DocumentCandidate,
    allowed_roots: tuple[Path, ...],
    request: OCRBackfillRequest,
    inspector: PDFInspector,
    provider: OCRProvider | None,
    summary: OCRBackfillSummary,
) -> _CandidatePlan:
    summary.documents_considered += 1
    if candidate.document_version_id is None or candidate.blob_sha256 is None:
        return _unrecordable(candidate, "missing_document_version", summary)
    try:
        raw_bytes = _verified_bytes(conn, candidate, allowed_roots)
    except OCRProviderError as error:
        return _unrecordable(candidate, error.reason_code, summary)
    if b"%PDF-" not in raw_bytes[:1024]:
        preflight = _failed_preflight("unsupported", "unsupported_pdf")
    else:
        preflight = inspector.inspect(
            raw_bytes, minimum_native_characters=request.minimum_native_characters
        )
    summary.records_planned += 1 + len(preflight.pages)
    if preflight.outcome == "native_sufficient":
        summary.documents_native_sufficient += 1
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=None,
            outcome="native_sufficient",
            reason_code=None,
        )
    if preflight.outcome != "ocr_required":
        reason = _require_reason(preflight.reason_code)
        _quarantine(summary, _candidate_ref(candidate), reason)
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=None,
            outcome="preflight_quarantined",
            reason_code=reason,
        )

    required_pages = tuple(page.page_number for page in preflight.pages if page.requires_ocr)
    summary.documents_requiring_ocr += 1
    summary.pages_requiring_ocr += len(required_pages)
    if not request.apply:
        # Run + governance + document node + one evidence node and result per OCR page.
        summary.records_planned += 3 + (2 * len(required_pages))
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=None,
            outcome="ocr_succeeded",
            reason_code=None,
        )
    if provider is None:
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=None,
            outcome="assessment_only",
            reason_code=None,
        )
    # Run + governance + document node + one evidence node and result per OCR page.
    summary.records_planned += 3 + (2 * len(required_pages))
    if set(provider.descriptor.model_artifacts) != set(request.languages):
        raise RuntimeError("OCR model artifacts must exactly match requested languages")
    config_sha256 = _ocr_config_sha256(request, provider.descriptor)
    document_version_id = _require_document_version(candidate)
    if _has_covered_run(conn, document_version_id, config_sha256):
        summary.documents_skipped_covered += 1
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=config_sha256,
            outcome="covered",
            reason_code=None,
        )
    try:
        outputs = provider.extract_pages(
            raw_bytes,
            page_numbers=required_pages,
            languages=request.languages,
            dpi=request.dpi,
            page_segmentation_mode=request.page_segmentation_mode,
            engine_mode=request.engine_mode,
            timeout_seconds=request.timeout_seconds,
        )
    except OCRProviderError as error:
        pages = [
            _failed_page(candidate.source_ref, page_number, error.reason_code)
            for page_number in required_pages
        ]
        _ocr_failure(summary, _candidate_ref(candidate), error.reason_code)
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=config_sha256,
            outcome="ocr_failed",
            reason_code=error.reason_code,
            pages=pages,
        )
    pages, failure_reason = _evaluate_outputs(
        candidate.source_ref, required_pages, outputs, request
    )
    if failure_reason is not None:
        _ocr_failure(summary, _candidate_ref(candidate), failure_reason)
        return _CandidatePlan(
            candidate=candidate,
            preflight=preflight,
            config_sha256=config_sha256,
            outcome="ocr_failed",
            reason_code=failure_reason,
            pages=pages,
        )
    summary.documents_ocr_succeeded += 1
    summary.pages_ocr_accepted += len(pages)
    return _CandidatePlan(
        candidate=candidate,
        preflight=preflight,
        config_sha256=config_sha256,
        outcome="ocr_succeeded",
        reason_code=None,
        pages=pages,
    )


def _persist_plan(
    conn: sqlite3.Connection,
    plan: _CandidatePlan,
    request: OCRBackfillRequest,
    provider: OCRProvider | None,
    summary: OCRBackfillSummary,
) -> None:
    if plan.outcome == "unrecordable":
        return
    preflight = _require_preflight(plan)
    assessment_id = _assessment_id(_require_document_version(plan.candidate), request)
    assessment_values = (
        assessment_id,
        f"ocr-preflight:{_stable_token(_require_document_version(plan.candidate))}:"
        f"{_detector_config_sha256(request)}",
        _require_document_version(plan.candidate),
        _require_blob_sha(plan.candidate),
        _DETECTOR_NAME,
        _detector_config_sha256(request),
        _DETECTOR_CODE_VERSION,
        preflight.native_output_sha256,
        preflight.page_count,
        preflight.outcome,
        preflight.reason_code,
        _require_recorded_at(plan.candidate),
    )
    _persist_exact(
        conn,
        "ocr_document_assessments",
        (
            "assessment_id",
            "idempotency_key",
            "document_version_id",
            "input_sha256",
            "detector_name",
            "detector_config_sha256",
            "detector_code_version",
            "native_output_sha256",
            "page_count",
            "outcome",
            "reason_code",
            "assessed_at",
        ),
        assessment_values,
        ("assessment_id",),
        (assessment_id,),
        summary,
    )
    for page in preflight.pages:
        _persist_exact(
            conn,
            "ocr_preflight_pages",
            (
                "assessment_id",
                "page_number",
                "native_character_count",
                "native_text_sha256",
                "requires_ocr",
            ),
            (
                assessment_id,
                page.page_number,
                page.native_character_count,
                page.native_text_sha256,
                page.requires_ocr,
            ),
            ("assessment_id", "page_number"),
            (assessment_id, page.page_number),
            summary,
        )
    if plan.outcome in {
        "native_sufficient",
        "preflight_quarantined",
        "assessment_only",
        "covered",
    }:
        return
    if provider is None or plan.config_sha256 is None:
        raise RuntimeError("planned OCR persistence requires provider governance")
    _persist_ocr_run(conn, plan, request, provider.descriptor, assessment_id, summary)


def _persist_ocr_run(
    conn: sqlite3.Connection,
    plan: _CandidatePlan,
    request: OCRBackfillRequest,
    descriptor: OCREngineDescriptor,
    assessment_id: str,
    summary: OCRBackfillSummary,
) -> None:
    document_version_id = _require_document_version(plan.candidate)
    config_sha256 = _require_config_sha256(plan)
    run_id = _run_id(document_version_id, config_sha256)
    recorded_at = _require_recorded_at(plan.candidate)
    output_sha256 = _canonical_sha256(
        [
            {
                "page_number": page.page_number,
                "outcome": page.outcome,
                "output_sha256": page.output_sha256,
                "mean_confidence": page.mean_confidence,
                "locator_sha256": page.locator.canonical_sha256,
                "reason_code": page.reason_code,
            }
            for page in plan.pages
        ]
    )
    run = ExtractionRun(
        extraction_run_id=run_id,
        idempotency_key=f"ocr:{_stable_token(document_version_id)}:{config_sha256}",
        document_version_id=document_version_id,
        input_sha256=_require_blob_sha(plan.candidate),
        extractor_name=_EXTRACTOR_NAME,
        extractor_config_sha256=config_sha256,
        extractor_code_version=_EXTRACTOR_CODE_VERSION,
        output_sha256=output_sha256,
        started_at=recorded_at,
        completed_at=recorded_at,
        outcome="succeeded" if plan.outcome == "ocr_succeeded" else "failed",
    )
    _account(EvidenceLedger(conn).persist(run).created, summary)
    engine_config_json = _ocr_config_json(request, descriptor)
    _persist_exact(
        conn,
        "ocr_extraction_governance",
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
            "renderer_name",
            "renderer_version",
            "renderer_binary_sha256",
            "recorded_at",
        ),
        (
            run_id,
            assessment_id,
            descriptor.engine_name,
            descriptor.engine_version,
            descriptor.engine_binary_sha256,
            descriptor.model_name,
            descriptor.model_version,
            descriptor.model_manifest_sha256,
            _canonical_json(descriptor.model_artifacts),
            json.dumps(request.languages, separators=(",", ":")),
            engine_config_json,
            config_sha256,
            descriptor.renderer_name,
            descriptor.renderer_version,
            descriptor.renderer_binary_sha256,
            recorded_at,
        ),
        ("extraction_run_id",),
        (run_id,),
        summary,
    )
    nodes: dict[int, EvidenceNode] = {}
    if plan.outcome == "ocr_succeeded":
        document_node = _new_node(
            conn,
            evidence_key=f"ocr-document:{_stable_token(document_version_id)}",
            node_id=_node_id(run_id, "document"),
            extraction_run_id=run_id,
            parent_node_id=None,
            text=f"Governed OCR for evidence document version {document_version_id}.",
            locator=EvidenceLocator(source_ref=plan.candidate.source_ref),
            recorded_at=recorded_at,
            node_kind="document",
        )
        _account(EvidenceLedger(conn).persist(document_node).created, summary)
        for page in plan.pages:
            if page.text is None:
                raise RuntimeError("successful OCR page must carry text")
            node = _new_node(
                conn,
                evidence_key=(
                    f"ocr-content:{_stable_token(document_version_id)}:{page.page_number}"
                ),
                node_id=_node_id(run_id, f"page-{page.page_number}"),
                extraction_run_id=run_id,
                parent_node_id=document_node.node_id,
                text=page.text,
                locator=page.locator,
                recorded_at=recorded_at,
                node_kind="pdf_page",
            )
            _account(EvidenceLedger(conn).persist(node).created, summary)
            nodes[page.page_number] = node
    for page in plan.pages:
        node = nodes.get(page.page_number)
        _persist_exact(
            conn,
            "ocr_page_results",
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
                run_id,
                page.page_number,
                None if node is None else node.node_id,
                page.outcome,
                page.output_sha256,
                page.mean_confidence,
                page.locator.canonical_json,
                page.locator.canonical_sha256,
                page.reason_code,
                recorded_at,
            ),
            ("extraction_run_id", "page_number"),
            (run_id, page.page_number),
            summary,
        )


def _evaluate_outputs(
    source_ref: str,
    required_pages: tuple[int, ...],
    outputs: list[OCRPageOutput],
    request: OCRBackfillRequest,
) -> tuple[list[_PlannedPage], str | None]:
    by_page = {output.page_number: output for output in outputs}
    if len(by_page) != len(outputs) or set(by_page) != set(required_pages):
        return (
            [
                _failed_page(source_ref, page_number, "provider_page_contract_mismatch")
                for page_number in required_pages
            ],
            "provider_page_contract_mismatch",
        )
    pages: list[_PlannedPage] = []
    failure_reason: str | None = None
    for page_number in required_pages:
        output = by_page[page_number]
        text = _normalize_text(output.text)
        locator = EvidenceLocator(source_ref=source_ref, page_number=page_number)
        if not text:
            reason = "empty_ocr_output"
            failure_reason = failure_reason or reason
            pages.append(
                _PlannedPage(
                    page_number=page_number,
                    locator=locator,
                    outcome="quarantined",
                    text=None,
                    output_sha256=_sha256_text(text),
                    mean_confidence=output.mean_confidence,
                    reason_code=reason,
                )
            )
        elif output.mean_confidence < request.minimum_mean_confidence:
            reason = "confidence_below_threshold"
            failure_reason = failure_reason or reason
            pages.append(
                _PlannedPage(
                    page_number=page_number,
                    locator=locator,
                    outcome="quarantined",
                    text=None,
                    output_sha256=_sha256_text(text),
                    mean_confidence=output.mean_confidence,
                    reason_code=reason,
                )
            )
        else:
            pages.append(
                _PlannedPage(
                    page_number=page_number,
                    locator=locator,
                    outcome="accepted",
                    text=text,
                    output_sha256=_sha256_text(text),
                    mean_confidence=output.mean_confidence,
                    reason_code=None,
                )
            )
    if failure_reason is not None:
        pages = [
            (
                page
                if page.outcome != "accepted"
                else _PlannedPage(
                    page_number=page.page_number,
                    locator=page.locator,
                    outcome="quarantined",
                    text=None,
                    output_sha256=page.output_sha256,
                    mean_confidence=page.mean_confidence,
                    reason_code="document_ocr_incomplete",
                )
            )
            for page in pages
        ]
    return pages, failure_reason


def _failed_page(source_ref: str, page_number: int, reason: str) -> _PlannedPage:
    return _PlannedPage(
        page_number=page_number,
        locator=EvidenceLocator(source_ref=source_ref, page_number=page_number),
        outcome="failed",
        text=None,
        output_sha256=None,
        mean_confidence=None,
        reason_code=reason,
    )


def _failed_preflight(
    outcome: Literal["encrypted", "unreadable", "unsupported"], reason: str
) -> PDFPreflight:
    return PDFPreflight(
        outcome=outcome,
        page_count=0,
        pages=[],
        native_output_sha256=_canonical_sha256(
            {"normalization_version": _NORMALIZATION_VERSION, "pages": []}
        ),
        reason_code=reason,
    )


def _verified_bytes(
    conn: sqlite3.Connection,
    candidate: _DocumentCandidate,
    allowed_roots: tuple[Path, ...],
) -> bytes:
    path = resolve_local_storage_uri(candidate.file_path, allowed_roots=allowed_roots)
    if path is None:
        raise OCRProviderError("storage_uri_not_allowed_local_file")
    if not path.is_file():
        raise OCRProviderError("content_missing")
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest != candidate.document_sha256 or digest != candidate.blob_sha256:
        raise OCRProviderError("sha256_mismatch")
    if candidate.raw_bytes_size is not None and candidate.raw_bytes_size != len(raw_bytes):
        raise OCRProviderError("byte_size_mismatch")
    blob = conn.execute(
        "SELECT byte_size FROM evidence_content_blobs WHERE sha256 = ?",
        (candidate.blob_sha256,),
    ).fetchone()
    if blob is None or not isinstance(blob[0], int) or blob[0] != len(raw_bytes):
        raise OCRProviderError("evidence_blob_size_mismatch")
    return raw_bytes


def _detector_config_sha256(request: OCRBackfillRequest) -> str:
    return _canonical_sha256(
        {
            "detector_name": _DETECTOR_NAME,
            "detector_code_version": _DETECTOR_CODE_VERSION,
            "minimum_native_characters": request.minimum_native_characters,
            "normalization_version": _NORMALIZATION_VERSION,
        }
    )


def _ocr_config_json(request: OCRBackfillRequest, descriptor: OCREngineDescriptor) -> str:
    return _canonical_json(
        {
            "dpi": request.dpi,
            "engine_mode": request.engine_mode,
            "engine": descriptor.model_dump(mode="json"),
            "languages": list(request.languages),
            "minimum_mean_confidence": request.minimum_mean_confidence,
            "normalization_version": _NORMALIZATION_VERSION,
            "page_segmentation_mode": request.page_segmentation_mode,
            "timeout_seconds": request.timeout_seconds,
        }
    )


def _ocr_config_sha256(request: OCRBackfillRequest, descriptor: OCREngineDescriptor) -> str:
    return hashlib.sha256(_ocr_config_json(request, descriptor).encode("utf-8")).hexdigest()


def _has_covered_run(
    conn: sqlite3.Connection, document_version_id: str, config_sha256: str
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evidence_extraction_runs AS run "
            "JOIN ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = run.extraction_run_id "
            "WHERE run.document_version_id = ? AND run.extractor_name = ? "
            "AND run.extractor_config_sha256 = ? AND run.extractor_code_version = ? "
            "AND run.outcome = 'succeeded' LIMIT 1",
            (
                document_version_id,
                _EXTRACTOR_NAME,
                config_sha256,
                _EXTRACTOR_CODE_VERSION,
            ),
        ).fetchone()
        is not None
    )


def _persist_exact(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    identity_columns: tuple[str, ...],
    identity_values: tuple[object, ...],
    summary: OCRBackfillSummary,
) -> None:
    where = " AND ".join(f"{column} = ?" for column in identity_columns)
    select_sql = f"SELECT {', '.join(columns)} FROM {table} WHERE {where}"  # nosec B608 -- trusted internal SQL shape; values remain bound
    existing = conn.execute(
        select_sql,
        identity_values,
    ).fetchone()
    if existing is not None:
        if not _stored_values_match(tuple(existing), values):
            raise RuntimeError(f"immutable OCR replay conflict in {table}")
        summary.records_replayed += 1
        return
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values)  # nosec B608 -- trusted internal SQL shape; values remain bound
    summary.records_created += 1


def _stored_values_match(stored: tuple[object, ...], supplied: tuple[object, ...]) -> bool:
    if len(stored) != len(supplied):
        return False
    for existing, expected in zip(stored, supplied, strict=True):
        if isinstance(expected, datetime):
            try:
                if datetime.fromisoformat(str(existing)).replace(tzinfo=None) != expected.replace(
                    tzinfo=None
                ):
                    return False
            except ValueError:
                return False
        elif isinstance(expected, bool):
            if bool(existing) != expected:
                return False
        elif existing != expected:
            return False
    return True


def _new_node(
    conn: sqlite3.Connection,
    *,
    evidence_key: str,
    node_id: str,
    extraction_run_id: str,
    parent_node_id: str | None,
    text: str,
    locator: EvidenceLocator,
    recorded_at: datetime,
    node_kind: Literal["document", "pdf_page"],
) -> EvidenceNode:
    prior = conn.execute(
        "SELECT node_id, revision FROM evidence_nodes WHERE evidence_key = ? "
        "ORDER BY revision DESC LIMIT 1",
        (evidence_key,),
    ).fetchone()
    return EvidenceNode(
        node_id=node_id,
        evidence_key=evidence_key,
        revision=1 if prior is None else int(prior[1]) + 1,
        extraction_run_id=extraction_run_id,
        parent_node_id=parent_node_id,
        supersedes_node_id=None if prior is None else str(prior[0]),
        node_kind=node_kind,
        text=text,
        locator=locator,
        recorded_at=recorded_at,
    )


def _candidates_after(
    conn: sqlite3.Connection, last_document_id: int, batch_size: int
) -> list[_DocumentCandidate]:
    rows = conn.execute(
        "WITH selected_documents AS ("
        "SELECT * FROM documents WHERE id > ? "
        "AND (lower(file_path) LIKE '%.pdf' OR lower(file_path) LIKE '%.pdf#%') "
        "ORDER BY id LIMIT ?"
        ") SELECT document.id AS document_id, document.file_path, "
        "lower(document.sha256) AS document_sha256, document.raw_bytes_size, "
        "document_version.document_version_id, document_version.blob_sha256, "
        "document_version.recorded_at "
        "FROM selected_documents AS document "
        "LEFT JOIN evidence_document_versions AS document_version "
        "ON document_version.legacy_document_id = document.id "
        "ORDER BY document.id, document_version.version_sequence",
        (last_document_id, batch_size),
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def _evidence_native_candidates_after(
    conn: sqlite3.Connection, last_evidence_rowid: int, batch_size: int
) -> list[_DocumentCandidate]:
    return [
        _candidate_from_evidence_native(candidate)
        for candidate in select_evidence_native_candidates(
            conn,
            after_rowid=last_evidence_rowid,
            batch_size=batch_size,
            pdf_only=True,
        )
    ]


def _candidate_from_evidence_native(
    candidate: EvidenceNativeDocumentCandidate,
) -> _DocumentCandidate:
    return _DocumentCandidate(
        document_id=None,
        evidence_rowid=candidate.evidence_rowid,
        file_path=candidate.storage_uri,
        source_ref=candidate.source_ref,
        media_type=candidate.media_type,
        document_sha256=candidate.blob_sha256,
        raw_bytes_size=candidate.byte_size,
        document_version_id=candidate.document_version_id,
        blob_sha256=candidate.blob_sha256,
        recorded_at=_datetime(candidate.recorded_at),
    )


def _candidate_from_row(row: sqlite3.Row) -> _DocumentCandidate:
    raw_size = row["raw_bytes_size"]
    if raw_size is not None and (isinstance(raw_size, bool) or not isinstance(raw_size, int)):
        raise RuntimeError("documents.raw_bytes_size must be a non-negative integer")
    return _DocumentCandidate(
        document_id=_integer(row["document_id"], "documents.id"),
        file_path=_text(row["file_path"], "documents.file_path"),
        source_ref=_text(row["file_path"], "documents.file_path"),
        media_type=None,
        document_sha256=_text(row["document_sha256"], "documents.sha256").lower(),
        raw_bytes_size=raw_size,
        document_version_id=_optional_text(row["document_version_id"]),
        blob_sha256=_optional_text(row["blob_sha256"]),
        recorded_at=_datetime(row["recorded_at"]),
    )


def _has_documents_after(conn: sqlite3.Connection, last_document_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM documents WHERE id > ? "
            "AND (lower(file_path) LIKE '%.pdf' OR lower(file_path) LIKE '%.pdf#%') LIMIT 1",
            (last_document_id,),
        ).fetchone()
        is not None
    )


def _allowed_content_roots(request: OCRBackfillRequest, repo_root: Path) -> tuple[Path, ...]:
    roots = (repo_root, *(root.resolve() for root in request.content_roots))
    return tuple(dict.fromkeys(roots))


def _checkpoint_path(root: Path, request: OCRBackfillRequest) -> Path:
    filename = "state.json" if request.source_lane == "legacy" else "evidence-native-state.json"
    return root / ".tmp" / request.task_id / filename


def _require_tables(conn: sqlite3.Connection, source_lane: _SourceLane) -> None:
    tables = [
        "evidence_content_blobs",
        "evidence_source_observations",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "ocr_document_assessments",
        "ocr_preflight_pages",
        "ocr_extraction_governance",
        "ocr_page_results",
    ]
    if source_lane == "legacy":
        tables.append("documents")
    for table in tables:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            is None
        ):
            raise RuntimeError(f"Required table {table!r} is unavailable for OCR backfill")


def _read_checkpoint(path: Path, source_lane: _SourceLane) -> OCRBackfillCheckpoint:
    if not path.exists():
        return OCRBackfillCheckpoint(source_lane=source_lane, updated_at=datetime.now(UTC))
    checkpoint = OCRBackfillCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    if checkpoint.source_lane != source_lane:
        raise RuntimeError("OCR checkpoint source lane does not match request")
    return checkpoint


def _write_checkpoint(path: Path, checkpoint: OCRBackfillCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _candidate_ref(candidate: _DocumentCandidate) -> str:
    if candidate.document_version_id is not None:
        return candidate.document_version_id
    if candidate.document_id is not None:
        return str(candidate.document_id)
    return "unknown"


def _assessment_id(document_version_id: str, request: OCRBackfillRequest) -> str:
    semantic = "\0".join(
        (document_version_id, _detector_config_sha256(request), _DETECTOR_CODE_VERSION)
    )
    return f"ocr-assessment-{hashlib.sha256(semantic.encode()).hexdigest()[:48]}"


def _run_id(document_version_id: str, config_sha256: str) -> str:
    semantic = "\0".join(
        (document_version_id, _EXTRACTOR_NAME, config_sha256, _EXTRACTOR_CODE_VERSION)
    )
    return f"ocr-run-{hashlib.sha256(semantic.encode()).hexdigest()[:48]}"


def _node_id(run_id: str, suffix: str) -> str:
    semantic = "\0".join((run_id, suffix))
    return f"ocr-node-{hashlib.sha256(semantic.encode()).hexdigest()[:48]}"


def _stable_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:48]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _substantive_character_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_executable(path: Path) -> Path:
    supplied = str(path)
    discovered = shutil.which(supplied)
    resolved = Path(discovered).resolve() if discovered is not None else path.resolve()
    if not resolved.is_file():
        raise OCRProviderError("ocr_executable_missing")
    return resolved


def _binary_version(executable: Path, arguments: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise OCRProviderError("ocr_version_probe_failed") from None
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if result.returncode != 0 or not first_line:
        raise OCRProviderError("ocr_version_probe_failed")
    return first_line[:255]


def _parse_tesseract_tsv(page_number: int, raw_tsv: bytes) -> OCRPageOutput:
    try:
        decoded = raw_tsv.decode("utf-8")
        rows = csv.DictReader(io.StringIO(decoded), delimiter="\t")
        words: list[str] = []
        confidences: list[float] = []
        for row in rows:
            text = row["text"].strip()
            confidence = float(row["conf"])
            if text and confidence >= 0:
                words.append(text)
                confidences.append(confidence)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise OCRProviderError("invalid_ocr_output") from None
    if not confidences:
        return OCRPageOutput(page_number=page_number, text="", mean_confidence=0.0)
    return OCRPageOutput(
        page_number=page_number,
        text=" ".join(words),
        mean_confidence=sum(confidences) / len(confidences),
    )


def _unrecordable(
    candidate: _DocumentCandidate, reason: str, summary: OCRBackfillSummary
) -> _CandidatePlan:
    _quarantine(summary, _candidate_ref(candidate), reason)
    return _CandidatePlan(
        candidate=candidate,
        preflight=None,
        config_sha256=None,
        outcome="unrecordable",
        reason_code=reason,
    )


def _quarantine(summary: OCRBackfillSummary, document_ref: str, reason: str) -> None:
    summary.documents_quarantined += 1
    counts = Counter(summary.finding_counts)
    counts[reason] += 1
    summary.finding_counts = dict(counts)
    emit_structured_event("ocr_evidence_quarantined", document_ref=document_ref, reason=reason)


def _ocr_failure(summary: OCRBackfillSummary, document_ref: str, reason: str) -> None:
    summary.documents_ocr_failed += 1
    _quarantine(summary, document_ref, reason)


def _account(created: bool, summary: OCRBackfillSummary) -> None:
    if created:
        summary.records_created += 1
    else:
        summary.records_replayed += 1


def _require_preflight(plan: _CandidatePlan) -> PDFPreflight:
    if plan.preflight is None:
        raise RuntimeError("recordable OCR plan is missing preflight")
    return plan.preflight


def _require_document_version(candidate: _DocumentCandidate) -> str:
    if candidate.document_version_id is None:
        raise RuntimeError("OCR candidate is missing document version")
    return candidate.document_version_id


def _require_blob_sha(candidate: _DocumentCandidate) -> str:
    if candidate.blob_sha256 is None:
        raise RuntimeError("OCR candidate is missing blob hash")
    return candidate.blob_sha256


def _require_recorded_at(candidate: _DocumentCandidate) -> datetime:
    if candidate.recorded_at is None:
        raise RuntimeError("OCR candidate is missing document version clock")
    return candidate.recorded_at


def _require_config_sha256(plan: _CandidatePlan) -> str:
    if plan.config_sha256 is None:
        raise RuntimeError("OCR plan is missing extractor configuration hash")
    return plan.config_sha256


def _require_reason(reason: str | None) -> str:
    if reason is None:
        raise RuntimeError("failed OCR state is missing a reason code")
    return reason


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{name} must be an integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError("OCR document clock is invalid") from error
    raise RuntimeError("OCR document clock is invalid")
