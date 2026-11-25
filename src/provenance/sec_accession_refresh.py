"""Validate, capture, extract, and resume a single sealed SEC accession."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from filings.inline_xbrl_processor import ApprovedProcessorBundle, ProcessorPackageMember
from provenance.financial_statement_admission import FinancialStatementContextReview
from provenance.immutable_artifact import (
    population_database_lock_resources,
    validate_population_database_target,
)
from provenance.issuer_registry import IssuerRegistry, UnresolvedIssuerIdentityError
from provenance.sec_filing_xbrl_ingest import (
    FilingXbrlIngestRequest,
    FilingXbrlIngestResult,
    ingest_sec_filing_xbrl,
)
from provenance.sec_native_capture import (
    SecNativeCaptureError,
    SecNativeCaptureRequest,
    SecNativeCaptureResult,
    SessionLike,
    capture_expected_sec_documents,
    load_captured_sec_filing_package,
)
from runtime.job_runtime import JobLock, portfolio_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CIK_RE = re.compile(r"^\d{10}$")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SUBMISSION_CIK_RE = re.compile(r"CIK(\d{10})\.json$", re.IGNORECASE)
_ARCHIVE_CIK_RE = re.compile(r"^/Archives/edgar/data/(\d+)/")


class SecAccessionRefreshError(RuntimeError):
    """Fail-closed validation or execution refusal for single-accession refresh."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecAccessionRefreshRequest(_Closed):
    """Validated request for refreshing or resuming one SEC accession."""

    db_path: Path
    inventory_key: str = Field(min_length=1, max_length=256)
    accession_number: str = Field(pattern=_ACCESSION_RE.pattern)
    expected_cik: str = Field(pattern=_CIK_RE.pattern)
    expected_form_type: str | None = Field(default=None, min_length=1, max_length=32)
    expected_ticker: str | None = Field(default=None, min_length=1, max_length=32)
    task_id: str = Field(pattern=_TASK_ID_RE.pattern)
    checkpoint_root: Path
    blob_root: Path
    user_agent: str = Field(min_length=8, max_length=512)
    project_root: Path
    apply: bool = False
    capture_only: bool = False
    extract_only: bool = False
    cancel_pending: bool = False
    minimum_request_interval_seconds: float = Field(default=0.25, ge=0.0, le=10.0)
    max_document_bytes: int = Field(default=100_000_000, ge=1, le=1_000_000_000)
    offline_artifacts: tuple[ProcessorPackageMember, ...] = ()
    statement_context_reviews: tuple[FinancialStatementContextReview, ...] = ()
    runtime_root: Path | None = None
    bundle_python: Path | None = None
    sandbox_launcher: Path | None = None
    sandbox_state_root: Path | None = None
    recorded_at: datetime | None = None

    @field_validator("inventory_key")
    @classmethod
    def _clean_inventory_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("inventory_key must not be empty")
        return cleaned

    @field_validator("expected_form_type", "expected_ticker")
    @classmethod
    def _clean_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("optional selector must not be blank when supplied")
        return cleaned

    @model_validator(mode="after")
    def _mode_flags(self) -> SecAccessionRefreshRequest:
        if self.capture_only and self.extract_only:
            raise ValueError("capture_only and extract_only are mutually exclusive")
        if self.recorded_at is not None and self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware when supplied")
        return self


class AccessionAcquisitionReceipt(_Closed):
    """Typed receipt for the byte-acquisition stage (distinct from extraction)."""

    status: Literal[
        "not_run",
        "already_captured",
        "dry_run_fetched",
        "captured",
        "incomplete",
        "failed",
        "cancelled",
    ]
    mode: Literal["dry_run", "apply", "skipped", "cancelled"]
    expected_count: int = Field(ge=0)
    considered: int = Field(default=0, ge=0)
    fetched: int = Field(default=0, ge=0)
    deferred: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    records_created: int = Field(default=0, ge=0)
    records_replayed: int = Field(default=0, ge=0)
    reason_code: str | None = None
    expected_document_ids: tuple[str, ...] = ()


class AccessionExtractionReceipt(_Closed):
    """Typed receipt for the Inline-XBRL extraction/ingestion stage."""

    status: Literal[
        "not_run",
        "dry_run",
        "extracted",
        "exact_replay",
        "cancelled",
    ]
    reason_code: str | None = None
    extraction_run_id: str | None = None
    processor_artifact_sha256: str | None = None
    raw_fact_count: int = Field(default=0, ge=0)
    normalized_count: int = Field(default=0, ge=0)
    published_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    quarantined_count: int = Field(default=0, ge=0)
    exact_replay: bool = False
    publication_id: str | None = None


class SecAccessionRefreshResult(_Closed):
    """Top-level result + checkpoint state for one SEC accession refresh."""

    schema_version: Literal["sec-accession-refresh/v1"] = "sec-accession-refresh/v1"
    task_id: str
    inventory_key: str
    accession_number: str
    expected_cik: str
    form_type: str
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["dry_run", "apply", "cancel"]
    status: Literal["completed", "incomplete", "cancelled"]
    reason_code: str | None = None
    acquisition_receipt: AccessionAcquisitionReceipt
    extraction_receipt: AccessionExtractionReceipt


class _ValidatedSelection(_Closed):
    snapshot_id: str
    issuer_id: str
    ticker: str | None
    form_type: str
    expected_document_ids: tuple[str, ...]
    selection_sha256: str
    already_captured: bool


def _checkpoint_path(checkpoint_root: Path, task_id: str) -> Path:
    return checkpoint_root / f"{task_id}.json"


def _url_cik(url: str) -> str | None:
    parsed = urlparse(url)
    archive = _ARCHIVE_CIK_RE.match(parsed.path)
    if archive is not None:
        return archive.group(1).zfill(10)
    submission = _SUBMISSION_CIK_RE.search(parsed.path)
    if submission is not None:
        return submission.group(1).zfill(10)
    return None


def _validate_selection_read_only(
    conn: sqlite3.Connection,
    request: SecAccessionRefreshRequest,
    *,
    now: datetime,
) -> _ValidatedSelection:
    inventory = conn.execute(
        "SELECT snapshot_id, issuer_id, ticker, source_kind, source_url "
        "FROM v_source_inventory_sealed_complete WHERE inventory_key = ?",
        (request.inventory_key,),
    ).fetchone()
    if inventory is None:
        raise SecAccessionRefreshError(
            "sealed_inventory_missing",
            f"no sealed complete inventory for key {request.inventory_key!r}",
        )
    snapshot_id = str(inventory[0])
    issuer_id = str(inventory[1])
    inv_ticker = None if inventory[2] is None else str(inventory[2])
    inv_source_kind = str(inventory[3])
    inv_source_url = None if inventory[4] is None else str(inventory[4])
    if inv_source_kind != "sec_submissions":
        raise SecAccessionRefreshError(
            "non_sec_inventory",
            f"inventory {request.inventory_key!r} has non-SEC source_kind {inv_source_kind!r}",
        )

    rows = conn.execute(
        "SELECT expected.expected_document_id, expected.issuer_id, expected.ticker, "
        "expected.document_type, expected.form_type, expected.source_url, expected.primary_document "
        "FROM v_expected_documents_current AS expected "
        "WHERE expected.snapshot_id = ? AND expected.accession_number = ? "
        "AND expected.source_kind = 'sec_filing' AND expected.expectation_basis = 'authoritative' "
        "ORDER BY expected.expected_document_key, expected.expected_document_id",
        (snapshot_id, request.accession_number),
    ).fetchall()
    if not rows:
        raise SecAccessionRefreshError(
            "accession_not_in_inventory",
            f"accession {request.accession_number} is absent from sealed inventory {request.inventory_key!r}",
        )

    doc_issuers = {str(r[1]) for r in rows}
    if doc_issuers != {issuer_id}:
        raise SecAccessionRefreshError(
            "issuer_mismatch",
            "expected documents do not share one issuer with the sealed inventory",
        )

    tickers = {str(r[2]).strip().upper() for r in rows if r[2] is not None and str(r[2]).strip()}
    if inv_ticker is not None and inv_ticker.strip():
        tickers.add(inv_ticker.strip().upper())
    if len(tickers) > 1:
        raise SecAccessionRefreshError("ticker_ambiguous", "multiple tickers map to one accession")
    resolved_ticker = next(iter(tickers), None)
    if request.expected_ticker is not None:
        want_ticker = request.expected_ticker.strip().upper()
        if resolved_ticker != want_ticker:
            raise SecAccessionRefreshError(
                "ticker_mismatch",
                f"accession ticker {resolved_ticker!r} != expected {want_ticker!r}",
            )

    raw_forms = {str(r[4]).strip() for r in rows if r[4] is not None and str(r[4]).strip()}
    if len(raw_forms) != 1:
        raise SecAccessionRefreshError(
            "form_type_ambiguous",
            "expected documents in accession lack a single unambiguous form_type",
        )
    actual_form = next(iter(raw_forms))
    if (
        request.expected_form_type is not None
        and actual_form.upper() != request.expected_form_type.strip().upper()
    ):
        raise SecAccessionRefreshError(
            "amendment_identity_mismatch",
            f"accession form_type {actual_form!r} != expected {request.expected_form_type!r}",
        )

    # Verify expected_cik from canonical registry when present, plus inventory/doc URLs.
    registry_ciks: set[str] = set()
    try:
        resolved = IssuerRegistry(conn).resolve_identifier(
            "sec_cik",
            request.expected_cik,
            knowledge_at=now,
        )
        if resolved.material_dissent or resolved.issuer_id != issuer_id:
            raise SecAccessionRefreshError(
                "cik_mismatch",
                f"canonical SEC CIK {request.expected_cik} does not match inventory issuer {issuer_id!r}",
            )
        registry_ciks.add(request.expected_cik)
    except (sqlite3.OperationalError, UnresolvedIssuerIdentityError):
        pass

    url_ciks = {_url_cik(str(r[5])) for r in rows if r[5] is not None}
    if inv_source_url is not None:
        inv_url_cik = _url_cik(inv_source_url)
        if inv_url_cik is not None:
            url_ciks.add(inv_url_cik)
    non_null_url_ciks = {c for c in url_ciks if c is not None}
    if any(c != request.expected_cik for c in non_null_url_ciks):
        raise SecAccessionRefreshError(
            "cik_mismatch",
            f"SEC URL CIK {sorted(non_null_url_ciks)} conflicts with expected_cik {request.expected_cik}",
        )
    if not registry_ciks and request.expected_cik not in non_null_url_ciks:
        raise SecAccessionRefreshError(
            "cik_unproven",
            f"cannot verify expected_cik {request.expected_cik} from registry or SEC URLs",
        )

    doc_tuples = sorted(
        (
            str(r[0]),
            str(r[3]),
            str(r[4] or ""),
            str(r[5] or ""),
            str(r[6] or ""),
        )
        for r in rows
    )
    selection_payload = {
        "accession_number": request.accession_number,
        "documents": doc_tuples,
        "expected_cik": request.expected_cik,
        "form_type": actual_form,
        "inventory_key": request.inventory_key,
        "issuer_id": issuer_id,
        "snapshot_id": snapshot_id,
    }
    selection_sha = hashlib.sha256(
        json.dumps(selection_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    already_captured = False
    try:
        captured_members = load_captured_sec_filing_package(
            conn,
            inventory_key=request.inventory_key,
            accession_number=request.accession_number,
        )
        captured_ids = {m.expected_document_id for m in captured_members}
        filing_doc_ids = {
            str(r[0]) for r in rows if str(r[3]) in {"filing", "sec_financial_report"}
        }
        if filing_doc_ids and filing_doc_ids.issubset(captured_ids):
            already_captured = True
    except SecNativeCaptureError:
        already_captured = False

    return _ValidatedSelection(
        snapshot_id=snapshot_id,
        issuer_id=issuer_id,
        ticker=resolved_ticker,
        form_type=actual_form,
        expected_document_ids=tuple(str(r[0]) for r in rows),
        selection_sha256=selection_sha,
        already_captured=already_captured,
    )


def _check_existing_checkpoint(
    checkpoint_path: Path,
    selection_sha256: str,
) -> SecAccessionRefreshResult | None:
    if not checkpoint_path.exists():
        return None
    existing = SecAccessionRefreshResult.model_validate_json(
        checkpoint_path.read_text(encoding="utf-8")
    )
    if existing.selection_sha256 != selection_sha256:
        raise SecAccessionRefreshError(
            "altered_selection",
            "existing checkpoint binds a different accession selection",
        )
    return existing


def _write_checkpoint(checkpoint_path: Path, result: SecAccessionRefreshResult) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = checkpoint_path.with_suffix(".json.tmp")
    tmp.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(checkpoint_path)


def _acquisition_receipt_from_capture(
    selection: _ValidatedSelection,
    result: SecNativeCaptureResult,
) -> AccessionAcquisitionReceipt:
    if result.failed > 0 or result.has_more:
        status: Literal[
            "not_run",
            "already_captured",
            "dry_run_fetched",
            "captured",
            "incomplete",
            "failed",
            "cancelled",
        ] = "failed" if result.failed > 0 else "incomplete"
        reason = "capture_failed" if result.failed > 0 else "capture_incomplete"
    elif result.deferred > 0:
        status = "incomplete"
        reason = "capture_deferred"
    elif result.mode == "dry_run":
        status = "dry_run_fetched"
        reason = None
    else:
        status = "captured"
        reason = None
    return AccessionAcquisitionReceipt(
        status=status,
        mode=result.mode,
        expected_count=len(selection.expected_document_ids),
        considered=result.considered,
        fetched=result.fetched,
        deferred=result.deferred,
        failed=result.failed,
        records_created=result.records_created,
        records_replayed=result.records_replayed,
        reason_code=reason,
        expected_document_ids=selection.expected_document_ids,
    )


def _extraction_receipt_from_ingest(
    ingest: FilingXbrlIngestResult,
) -> AccessionExtractionReceipt:
    if ingest.exact_replay:
        status: Literal["not_run", "dry_run", "extracted", "exact_replay", "cancelled"] = (
            "exact_replay"
        )
    elif ingest.mode == "dry_run":
        status = "dry_run"
    else:
        status = "extracted"
    return AccessionExtractionReceipt(
        status=status,
        reason_code=None,
        extraction_run_id=ingest.extraction_run_id,
        processor_artifact_sha256=ingest.processor_artifact_sha256,
        raw_fact_count=ingest.raw_fact_count,
        normalized_count=ingest.normalized_count,
        published_count=ingest.published_count,
        duplicate_count=ingest.duplicate_count,
        quarantined_count=ingest.quarantined_count,
        exact_replay=ingest.exact_replay,
        publication_id=ingest.publication_id,
    )


def refresh_sec_accession(
    request: SecAccessionRefreshRequest,
    *,
    session: SessionLike,
    approved_bundle: ApprovedProcessorBundle | None = None,
) -> SecAccessionRefreshResult:
    """Validate selection read-only first, then acquire/extract under bounded write locks."""
    if not isinstance(cast(object, request), SecAccessionRefreshRequest):
        raise SecAccessionRefreshError("request_invalid", "invalid refresh request")

    portfolio_db = portfolio_db_path(request.project_root)
    db_path = validate_population_database_target(request.db_path, portfolio_db)
    checkpoint_file = _checkpoint_path(request.checkpoint_root, request.task_id)
    now = request.recorded_at or datetime.now(UTC)

    # Read-only preflight before opening ANY write locks or making network calls.
    ro_conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        selection = _validate_selection_read_only(ro_conn, request, now=now)
    finally:
        ro_conn.close()

    prior = _check_existing_checkpoint(checkpoint_file, selection.selection_sha256)

    write_sets = [f"sec-accession-refresh-checkpoint:{request.task_id}"]
    needs_capture = (
        not request.cancel_pending and not request.extract_only and not selection.already_captured
    )
    if needs_capture:
        write_sets.append("sec-edgar-network")
        write_sets.append(f"sec-native-capture-checkpoint:{request.task_id}")
    will_extract = (
        not request.cancel_pending
        and not request.capture_only
        and (selection.already_captured or request.apply)
    )
    if will_extract and approved_bundle is not None:
        write_sets.extend(
            (
                f"filing-xbrl-accession:{request.accession_number}",
                f"filing-xbrl-bundle:{approved_bundle.manifest.manifest_sha256}",
            )
        )
    if request.apply and not request.cancel_pending:
        write_sets.extend(population_database_lock_resources(db_path, portfolio_db))
        write_sets.append(f"evidence-blobs:{request.blob_root.resolve()}")

    with JobLock(request.project_root, "sec-accession-refresh", write_sets):
        if request.cancel_pending:
            acq = (
                prior.acquisition_receipt
                if prior is not None
                else AccessionAcquisitionReceipt(
                    status="cancelled",
                    mode="cancelled",
                    expected_count=len(selection.expected_document_ids),
                    expected_document_ids=selection.expected_document_ids,
                )
            )
            ext = (
                prior.extraction_receipt
                if prior is not None
                else AccessionExtractionReceipt(
                    status="cancelled", reason_code="cancelled_by_operator"
                )
            )
            cancelled = SecAccessionRefreshResult(
                task_id=request.task_id,
                inventory_key=request.inventory_key,
                accession_number=request.accession_number,
                expected_cik=request.expected_cik,
                form_type=selection.form_type,
                selection_sha256=selection.selection_sha256,
                mode="cancel",
                status="cancelled",
                reason_code="cancelled_by_operator",
                acquisition_receipt=acq,
                extraction_receipt=ext,
            )
            _write_checkpoint(checkpoint_file, cancelled)
            return cancelled

        role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(db_path, role=role, schema_preflight=request.apply)
        try:
            # Stage 1: Acquisition (separate receipt from extraction)
            if selection.already_captured:
                acq_receipt = AccessionAcquisitionReceipt(
                    status="already_captured",
                    mode="apply" if request.apply else "dry_run",
                    expected_count=len(selection.expected_document_ids),
                    expected_document_ids=selection.expected_document_ids,
                )
            elif request.extract_only:
                raise SecAccessionRefreshError(
                    "capture_missing_for_extract_only",
                    f"accession {request.accession_number} is not completely captured in DB",
                )
            else:
                cap_req = SecNativeCaptureRequest(
                    inventory_keys=(request.inventory_key,),
                    accession_numbers=(request.accession_number,),
                    checkpoint_root=request.checkpoint_root / "native_capture",
                    blob_root=request.blob_root,
                    task_id=request.task_id,
                    user_agent=request.user_agent,
                    apply=request.apply,
                    batch_size=max(len(selection.expected_document_ids), 1),
                    max_document_bytes=request.max_document_bytes,
                    minimum_request_interval_seconds=request.minimum_request_interval_seconds,
                )
                cap_res = capture_expected_sec_documents(conn, cap_req, session=session)
                acq_receipt = _acquisition_receipt_from_capture(selection, cap_res)

            # Stage 2: Extraction (separate receipt; stop before extraction on incomplete capture)
            if request.capture_only:
                ext_receipt = AccessionExtractionReceipt(
                    status="not_run", reason_code="capture_only"
                )
                overall_status: Literal["completed", "incomplete"] = (
                    "completed"
                    if acq_receipt.status in {"already_captured", "captured", "dry_run_fetched"}
                    else "incomplete"
                )
                overall_reason = acq_receipt.reason_code
            elif acq_receipt.status in {"incomplete", "failed"}:
                ext_receipt = AccessionExtractionReceipt(
                    status="not_run", reason_code="acquisition_incomplete"
                )
                overall_status = "incomplete"
                overall_reason = acq_receipt.reason_code or "acquisition_incomplete"
            elif not request.apply and not selection.already_captured:
                ext_receipt = AccessionExtractionReceipt(
                    status="not_run", reason_code="capture_not_persisted_in_dry_run"
                )
                overall_status = "completed"
                overall_reason = None
            else:
                if (
                    approved_bundle is None
                    or request.runtime_root is None
                    or request.bundle_python is None
                    or request.sandbox_launcher is None
                ):
                    raise SecAccessionRefreshError(
                        "processor_bundle_required",
                        "extraction requires an approved Inline-XBRL processor bundle and installation paths",
                    )
                if request.apply and not selection.already_captured:
                    # Verify DB package now exists cleanly before extraction
                    load_captured_sec_filing_package(
                        conn,
                        inventory_key=request.inventory_key,
                        accession_number=request.accession_number,
                    )
                ingest_req = FilingXbrlIngestRequest(
                    inventory_key=request.inventory_key,
                    accession_number=request.accession_number,
                    expected_cik=request.expected_cik,
                    runtime_root=request.runtime_root,
                    bundle_python=request.bundle_python,
                    sandbox_launcher=request.sandbox_launcher,
                    sandbox_state_root=request.sandbox_state_root,
                    recorded_at=now,
                    offline_artifacts=request.offline_artifacts,
                    apply=request.apply,
                    statement_context_reviews=request.statement_context_reviews,
                )
                ingest_res = ingest_sec_filing_xbrl(
                    conn,
                    ingest_req,
                    approved_bundle=approved_bundle,
                )
                ext_receipt = _extraction_receipt_from_ingest(ingest_res)
                overall_status = "completed"
                overall_reason = None

            result = SecAccessionRefreshResult(
                task_id=request.task_id,
                inventory_key=request.inventory_key,
                accession_number=request.accession_number,
                expected_cik=request.expected_cik,
                form_type=selection.form_type,
                selection_sha256=selection.selection_sha256,
                mode="apply" if request.apply else "dry_run",
                status=overall_status,
                reason_code=overall_reason,
                acquisition_receipt=acq_receipt,
                extraction_receipt=ext_receipt,
            )
            _write_checkpoint(checkpoint_file, result)
            return result
        finally:
            conn.close()


__all__ = [
    "AccessionAcquisitionReceipt",
    "AccessionExtractionReceipt",
    "SecAccessionRefreshError",
    "SecAccessionRefreshRequest",
    "SecAccessionRefreshResult",
    "refresh_sec_accession",
]
