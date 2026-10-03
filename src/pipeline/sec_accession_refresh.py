"""One selected SEC accession: immutable selection, native capture, readable text.

The caller owns the database/network/artifact lanes. Ledger evidence owns progress;
this adapter never writes financial facts, source seals, or provider policy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from operations.paths import operations_runtime_directory
from pipeline import source_policy
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_collection_target_in_connection,
)
from provenance import fulltext_backfill, fulltext_extractor_identity, sec_native_capture
from provenance.evidence_native_candidates import (
    resolve_local_storage_uri,
    select_evidence_native_candidates_by_id,
)
from provenance.fulltext_backfill import (
    FullTextBackfillRequest,
    backfill_fulltext_evidence,
    has_substantive_coverage,
)
from provenance.fulltext_extractor_identity import resolve_fulltext_extractor_identity
from provenance.immutable_artifact import read_stable_artifact
from provenance.population_document_processing import database_instance_id
from provenance.sec_native_capture import (
    ExpectedSecDocument,
    SecNativeCaptureHardStopError,
    SecNativeCaptureRequest,
    SecNativeCaptureResult,
    SessionLike,
    capture_expected_sec_documents,
)
from provenance.source_coverage import ExpectedDocument


class RefreshBoundaryError(ValueError):
    """A selection or authority changed; repair/replan before continuing."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccessionRefreshRequest(_Frozen):
    request_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
    issuer_id: str = Field(min_length=1, max_length=128)
    cik: str = Field(pattern=r"^[0-9]{10}$")
    inventory_key: str = Field(min_length=1, max_length=256)
    accession_number: str = Field(pattern=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
    repo_root: Path
    max_members: int = Field(default=250, ge=1, le=250)
    capture_batch_size: int = Field(default=25, ge=1, le=250)
    extraction_batch_size: int = Field(default=25, ge=1, le=250)
    max_document_bytes: int = Field(default=100_000_000, ge=1, le=100_000_000)

    @property
    def operation_root(self) -> Path:
        return (
            operations_runtime_directory(self.repo_root) / "sec-accession-refresh" / self.request_id
        )

    @property
    def blob_root(self) -> Path:
        return self.repo_root / "data" / "evidence" / "blobs"


class AccessionRefreshPlan(_Frozen):
    schema_version: Literal["sec-accession-refresh.v1"] = "sec-accession-refresh.v1"
    request: AccessionRefreshRequest
    database_path: str
    database_instance_id: str
    snapshot_id: str
    inventory_revision: int
    inventory_sha256: str
    implementation_sha256: str
    coverage_role: str
    documents: tuple[ExpectedDocument, ...]

    @property
    def commitment(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


ItemStatus = Literal[
    "capture_needed",
    "captured",
    "extracted",
    "reused",
    "authority_unavailable",
    "deferred",
    "failed",
    "quarantined",
    "not_attempted",
]


class RefreshItem(_Frozen):
    expected_document_id: str
    document_version_id: str | None = None
    status: ItemStatus
    reason_codes: tuple[str, ...] = ()


class AccessionRefreshResult(_Frozen):
    schema_version: Literal["sec-accession-refresh-result.v1"] = "sec-accession-refresh-result.v1"
    request_id: str
    plan_sha256: str
    state: Literal["planned", "succeeded", "partial", "blocked"]
    items: tuple[RefreshItem, ...]
    capture: SecNativeCaptureResult | None = None
    reason_code: str | None = None
    cancellation: Literal["unavailable"] = "unavailable"
    excluded_lanes: tuple[str, ...] = ("fmp", "ir", "llm", "xbrl", "financial_admission", "models")
    proof_scope: Literal["selected_accession_readable_text_only"] = (
        "selected_accession_readable_text_only"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _implementation_sha256() -> str:
    digests = [hashlib.sha256(Path(__file__).read_bytes()).hexdigest()]
    for module in (
        source_policy,
        fulltext_backfill,
        fulltext_extractor_identity,
        sec_native_capture,
    ):
        module_file = module.__file__
        if module_file is None:
            raise RefreshBoundaryError("implementation_identity_unavailable")
        digests.append(hashlib.sha256(Path(module_file).read_bytes()).hexdigest())
    return _digest(digests)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[object, ...]) -> list[sqlite3.Row]:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, args).fetchall()


def _database_path(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA database_list").fetchall()
    paths = [str(row[2]) for row in rows if row[1] == "main"]
    if len(paths) != 1 or not paths[0]:
        raise RefreshBoundaryError("file_database_required")
    return str(Path(paths[0]).resolve(strict=True))


def plan_accession_refresh(
    conn: sqlite3.Connection,
    request: AccessionRefreshRequest,
) -> AccessionRefreshPlan:
    """Read one consistent stored snapshot without network or durable writes."""
    if conn.in_transaction:
        raise RefreshBoundaryError("caller_transaction_not_allowed")
    conn.execute("BEGIN")
    try:
        return _build_plan(conn, request)
    finally:
        conn.rollback()


def _build_plan(
    conn: sqlite3.Connection,
    request: AccessionRefreshRequest,
) -> AccessionRefreshPlan:
    """Read stored authority only: no transport, checkpoint, or ledger writes."""
    if not request.repo_root.is_absolute() or request.repo_root != request.repo_root.resolve():
        raise RefreshBoundaryError("absolute_product_state_root_required")
    authorization = authorize_collection_target_in_connection(
        conn,
        request.ticker,
        requested=True,
        source=CollectionSource.SEC,
        artifact_kind=ArtifactKind.FILING_PACKAGE,
        require_corporate_instrument=True,
    )
    if not authorization.allowed or authorization.target is None:
        raise RefreshBoundaryError("stored_identity_or_source_policy_denied")
    inventory = _rows(
        conn,
        "SELECT * FROM v_source_inventory_sealed_complete WHERE inventory_key=?",
        (request.inventory_key,),
    )
    if len(inventory) != 1:
        raise RefreshBoundaryError("current_complete_inventory_required")
    row = inventory[0]
    if (
        row["issuer_id"] != request.issuer_id
        or row["ticker"] != request.ticker
        or row["source_kind"] != "sec_submissions"
        or row["source_url"] != f"https://data.sec.gov/submissions/CIK{request.cik}.json"
    ):
        raise RefreshBoundaryError("inventory_identity_mismatch")
    documents = tuple(
        ExpectedDocument.model_validate(dict(value))
        for value in _rows(
            conn,
            "SELECT expected.* FROM v_expected_documents_current expected "
            "WHERE expected.snapshot_id=? AND expected.accession_number=? "
            "ORDER BY expected.expected_document_key LIMIT ?",
            (row["snapshot_id"], request.accession_number, request.max_members + 1),
        )
    )
    if not documents or len(documents) > request.max_members:
        raise RefreshBoundaryError("selected_population_missing_or_over_budget")
    if sum(document.document_type == "filing" for document in documents) != 1:
        raise RefreshBoundaryError("one_primary_filing_required")
    for document in documents:
        if (
            document.issuer_id != request.issuer_id
            or document.ticker != request.ticker
            or document.source_kind != "sec_filing"
            or document.expectation_basis != "authoritative"
            or document.accession_number != request.accession_number
            or document.form_type is None
        ):
            raise RefreshBoundaryError("selected_document_identity_mismatch")
        if document.source_url is not None and document.primary_document is not None:
            if urlsplit(document.source_url).path.split("/")[4:5] != [str(int(request.cik))]:
                raise RefreshBoundaryError("document_cik_mismatch")
            ExpectedSecDocument(
                expected_document_id=document.expected_document_id,
                snapshot_id=document.snapshot_id,
                expected_document_key=document.expected_document_key,
                issuer_id=document.issuer_id,
                ticker=document.ticker,
                document_type=document.document_type,
                form_type=document.form_type,
                accession_number=request.accession_number,
                source_url=document.source_url,
                primary_document=document.primary_document,
                period_start=document.period_start,
                period_end=document.period_end,
                filing_at=document.filing_at,
                inventory_key=request.inventory_key,
                current_coverage_status=None,
            )
    return AccessionRefreshPlan(
        request=request,
        database_path=_database_path(conn),
        database_instance_id=database_instance_id(conn),
        snapshot_id=str(row["snapshot_id"]),
        inventory_revision=int(row["revision"]),
        inventory_sha256=_digest(dict(row)),
        implementation_sha256=_implementation_sha256(),
        coverage_role=authorization.target.coverage_role.value,
        documents=documents,
    )


def verify_plan(conn: sqlite3.Connection, plan: AccessionRefreshPlan) -> None:
    if plan_accession_refresh(conn, plan.request) != plan:
        raise RefreshBoundaryError("refresh_plan_no_longer_current")


def _captured_version(conn: sqlite3.Connection, document: ExpectedDocument) -> str | None:
    rows = _rows(
        conn,
        "SELECT coverage.coverage_status,version.*,observation.source_url AS observed_url,"
        "observation.blob_sha256 AS observed_sha FROM v_source_coverage_current coverage "
        "LEFT JOIN evidence_document_versions version "
        "ON version.document_version_id=coverage.document_version_id "
        "LEFT JOIN evidence_source_observations observation "
        "ON observation.observation_id=version.observation_id "
        "WHERE coverage.expected_document_id=?",
        (document.expected_document_id,),
    )
    if not rows or rows[0]["coverage_status"] not in {"captured", "extracted", "indexed"}:
        return None
    row = rows[0]
    if (
        row["issuer_id"] != document.issuer_id
        or row["ticker"] != document.ticker
        or row["document_type"] != document.document_type
        or row["form_type"] != document.form_type
        or row["accession_number"] != document.accession_number
        or row["observed_url"] != document.source_url
        or row["blob_sha256"] != row["observed_sha"]
    ):
        raise RefreshBoundaryError("captured_document_identity_mismatch")
    if (
        row["document_key"] != document.expected_document_key
        or row["legacy_document_id"] is not None
    ):
        raise RefreshBoundaryError("native_document_identity_required")
    for name, expected in (
        ("period_start", document.period_start),
        ("period_end", document.period_end),
    ):
        stored = row[name]
        actual = None if stored is None else datetime.fromisoformat(str(stored))
        if actual is not None:
            actual = actual.replace(tzinfo=UTC) if actual.tzinfo is None else actual.astimezone(UTC)
        if expected is not None:
            expected = (
                expected.replace(tzinfo=UTC)
                if expected.tzinfo is None
                else expected.astimezone(UTC)
            )
        if actual != expected:
            raise RefreshBoundaryError("captured_period_mismatch")
    return str(row["document_version_id"])


def _verify_bytes(conn: sqlite3.Connection, plan: AccessionRefreshPlan, version: str) -> bool:
    candidates = select_evidence_native_candidates_by_id(conn, document_version_ids=(version,))
    candidate = candidates[0]
    if candidate.byte_size > plan.request.max_document_bytes:
        raise RefreshBoundaryError("captured_document_over_budget")
    path = resolve_local_storage_uri(candidate.storage_uri, allowed_roots=(plan.request.blob_root,))
    if path is None or path.stat().st_size > plan.request.max_document_bytes:
        raise RefreshBoundaryError("captured_bytes_unavailable")
    snapshot, raw = read_stable_artifact(path)
    if snapshot.file_sha256 != candidate.blob_sha256 or len(raw) != candidate.byte_size:
        raise RefreshBoundaryError("captured_bytes_mismatch")
    return has_substantive_coverage(
        conn,
        version,
        resolve_fulltext_extractor_identity(candidate.source_ref, candidate.media_type),
    )


def inspect_accession_refresh(
    conn: sqlite3.Connection,
    plan: AccessionRefreshPlan,
) -> AccessionRefreshResult:
    """Project capture debt without fetching or parsing documents."""
    verify_plan(conn, plan)
    items: list[RefreshItem] = []
    for document in plan.documents:
        if document.source_url is None or document.primary_document is None:
            items.append(
                RefreshItem(
                    expected_document_id=document.expected_document_id,
                    status="authority_unavailable",
                )
            )
            continue
        version = _captured_version(conn, document)
        covered = _verify_bytes(conn, plan, version) if version else False
        items.append(
            RefreshItem(
                expected_document_id=document.expected_document_id,
                document_version_id=version,
                status="reused" if covered else "captured" if version else "capture_needed",
            )
        )
    return AccessionRefreshResult(
        request_id=plan.request.request_id,
        plan_sha256=plan.commitment,
        state="planned",
        items=tuple(items),
    )


def apply_accession_refresh(
    conn: sqlite3.Connection,
    plan: AccessionRefreshPlan,
    *,
    session: SessionLike,
    user_agent: str,
) -> AccessionRefreshResult:
    """One bounded attempt. Resume reuses the original plan and native checkpoint.

    The caller holds exact target/blob/checkpoint/network locks through this call.
    Successful sibling evidence survives an independent item's failure.
    """
    initial = inspect_accession_refresh(conn, plan)
    request = plan.request
    capture: SecNativeCaptureResult | None = None
    if any(item.status == "capture_needed" for item in initial.items):
        try:
            capture = capture_expected_sec_documents(
                conn,
                SecNativeCaptureRequest(
                    inventory_keys=(request.inventory_key,),
                    accession_numbers=(request.accession_number,),
                    checkpoint_root=request.operation_root / "capture",
                    blob_root=request.blob_root,
                    task_id=plan.commitment[:32],
                    user_agent=user_agent,
                    apply=True,
                    batch_size=request.capture_batch_size,
                    max_document_bytes=request.max_document_bytes,
                ),
                session=session,
            )
        except SecNativeCaptureHardStopError:
            return initial.model_copy(
                update={"state": "blocked", "reason_code": "sec_authorization_hard_stop"}
            )
    verify_plan(conn, plan)
    outcomes = {item.expected_document_id: item for item in capture.items} if capture else {}
    items: list[RefreshItem] = []
    attempted = 0
    for document in plan.documents:
        identity = document.expected_document_id
        if document.source_url is None or document.primary_document is None:
            items.append(RefreshItem(expected_document_id=identity, status="authority_unavailable"))
            continue
        version = _captured_version(conn, document)
        if version is None:
            fetched = outcomes.get(identity)
            items.append(
                RefreshItem(
                    expected_document_id=identity,
                    status=(
                        "deferred"
                        if fetched and fetched.outcome == "transient_deferred"
                        else "failed"
                        if fetched
                        else "not_attempted"
                    ),
                    reason_codes=(fetched.reason_code,) if fetched else ("capture_budget",),
                )
            )
            continue
        verify_plan(conn, plan)
        try:
            covered = _verify_bytes(conn, plan, version)
        except (OSError, ValueError) as exc:
            items.append(
                RefreshItem(
                    expected_document_id=identity,
                    document_version_id=version,
                    status="quarantined",
                    reason_codes=(type(exc).__name__,),
                )
            )
            continue
        if covered:
            items.append(
                RefreshItem(
                    expected_document_id=identity, document_version_id=version, status="reused"
                )
            )
            continue
        if attempted >= request.extraction_batch_size:
            items.append(
                RefreshItem(
                    expected_document_id=identity,
                    document_version_id=version,
                    status="not_attempted",
                    reason_codes=("extraction_budget",),
                )
            )
            continue
        attempted += 1
        try:
            extracted = backfill_fulltext_evidence(
                conn,
                FullTextBackfillRequest(
                    repo_root=request.repo_root,
                    content_roots=(request.blob_root,),
                    apply=True,
                    document_version_id=version,
                    source_lane="evidence_native",
                    batch_size=1,
                ),
            )
            status: ItemStatus = "quarantined"
            if extracted.documents_extracted == 1:
                status = "extracted"
            elif extracted.documents_skipped_covered == 1:
                status = "reused"
            items.append(
                RefreshItem(
                    expected_document_id=identity,
                    document_version_id=version,
                    status=status,
                    reason_codes=tuple(sorted(extracted.finding_counts)),
                )
            )
        except (OSError, ValueError) as exc:
            if conn.in_transaction:
                conn.rollback()
            items.append(
                RefreshItem(
                    expected_document_id=identity,
                    document_version_id=version,
                    status="quarantined",
                    reason_codes=(type(exc).__name__,),
                )
            )
    verify_plan(conn, plan)
    return AccessionRefreshResult(
        request_id=request.request_id,
        plan_sha256=plan.commitment,
        state="succeeded"
        if all(item.status in {"extracted", "reused"} for item in items)
        else "partial",
        items=tuple(items),
        capture=capture,
    )
