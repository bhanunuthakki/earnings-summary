"""Retain a declared analysis with one existing SEC readable-text operation.

The operation artifacts own request identity across server restarts. Reads report
ledger metadata only; they do not fetch or validate large document blobs.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from operations.paths import operations_runtime_directory
from pipeline.sec_accession_refresh import (
    AccessionRefreshPlan,
    AccessionRefreshRequest,
    AccessionRefreshResult,
    RefreshBoundaryError,
    plan_accession_refresh,
    verify_plan,
)
from provenance.analysis_scope import (
    AnalysisAccessionSelection,
    AnalysisEvidenceScope,
    AnalysisScopeRequest,
    build_analysis_scope,
    verify_analysis_scope,
)
from provenance.immutable_artifact import (
    ImmutableArtifactSnapshot,
    publish_text_no_clobber,
    require_canonical_text_artifact,
    require_no_reparse_points,
)

MAX_SCOPE_DOCUMENTS = 1000
MAX_ATTEMPTS = 32
MAX_ARTIFACT_BYTES = 2_000_000
MAX_STATUS_BYTES = 8_000_000
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ATTEMPT = re.compile(
    r"^([0-9a-f]{32})\.(dispatch|started|result|unconfirmed|dispatch-failed)\.json$"
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpAnalysisRequest(AnalysisScopeRequest):
    require_latest_period: StrictBool = True
    required_period_ends: tuple[date, ...] = Field(min_length=1, max_length=24)
    extra_accessions: tuple[AnalysisAccessionSelection, ...] = Field(default=(), max_length=24)


class SecAccessionPlanInput(_Frozen):
    request_id: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    ticker: StrictStr = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
    cik: StrictStr = Field(pattern=r"^[0-9]{10}$")
    accession_number: StrictStr = Field(pattern=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
    analysis: HttpAnalysisRequest
    max_members: StrictInt = Field(default=250, ge=1, le=250)
    capture_batch_size: StrictInt = Field(default=25, ge=1, le=250)
    extraction_batch_size: StrictInt = Field(default=25, ge=1, le=250)
    max_document_bytes: StrictInt = Field(default=10_000_000, ge=1, le=100_000_000)

    def refresh_request(self, state_root: Path) -> AccessionRefreshRequest:
        return AccessionRefreshRequest(
            **self.model_dump(exclude={"analysis"}),
            issuer_id=self.analysis.issuer_id,
            inventory_key=self.analysis.inventory_key,
            repo_root=state_root,
        )


class SecAccessionApplyInput(_Frozen):
    plan_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["apply", "resume"]
    resume_from: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class BoundAccessionRequest(_Frozen):
    schema_version: Literal["sec-accession-analysis-request.v1"] = (
        "sec-accession-analysis-request.v1"
    )
    request_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: AnalysisEvidenceScope

    @property
    def commitment(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class AttemptReceipt(_Frozen):
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_id: str
    plan_sha256: str
    request_sha256: str
    recorded_at: datetime
    job_id: str | None = None
    state: Literal["dispatched", "running", "completion_unconfirmed", "dispatch_failed"] | None = (
        None
    )
    reason_code: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    result: AccessionRefreshResult | None = None


def operation_directory(state_root: Path, request_id: str) -> Path:
    if not _ID.fullmatch(request_id):
        raise RefreshBoundaryError("invalid_request_id")
    require_no_reparse_points(state_root)
    if not state_root.is_absolute() or state_root.resolve(strict=True) != state_root:
        raise RefreshBoundaryError("invalid_state_root")
    result = operations_runtime_directory(state_root) / "sec-accession-refresh" / request_id
    require_no_reparse_points(result)
    return result


def _read_metadata(path: Path, budget: list[int]) -> tuple[ImmutableArtifactSnapshot, bytes]:
    """Pin a regular file and bound reads even if another actor grows it."""
    require_no_reparse_points(path)
    lexical = path.lstat()
    if not stat.S_ISREG(lexical.st_mode) or lexical.st_size > min(MAX_ARTIFACT_BYTES, budget[0]):
        raise RefreshBoundaryError("artifact_unavailable_or_over_budget")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | (os.O_NONBLOCK if os.name == "posix" else 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > min(MAX_ARTIFACT_BYTES, budget[0])
            or (lexical.st_dev, lexical.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RefreshBoundaryError("artifact_unavailable_or_over_budget")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(min(MAX_ARTIFACT_BYTES, budget[0]) + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(current)
        or len(raw) > min(MAX_ARTIFACT_BYTES, budget[0])
    ):
        raise RefreshBoundaryError("artifact_changed_or_over_budget")
    budget[0] -= len(raw)
    return ImmutableArtifactSnapshot(
        path=path,
        device=after.st_dev,
        inode=after.st_ino,
        size_bytes=after.st_size,
        modified_time_ns=after.st_mtime_ns,
        changed_time_ns=after.st_ctime_ns,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    ), raw


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def load_bound_request(
    state_root: Path, request_id: str, *, budget: list[int] | None = None
) -> tuple[BoundAccessionRequest, AccessionRefreshPlan, tuple[ImmutableArtifactSnapshot, ...]]:
    directory = operation_directory(state_root, request_id)
    remaining = budget if budget is not None else [MAX_STATUS_BYTES]
    plan_snapshot, plan_raw = _read_metadata(directory / "plan.json", remaining)
    request_snapshot, request_raw = _read_metadata(directory / "request.json", remaining)
    plan = AccessionRefreshPlan.model_validate_json(plan_raw)
    bound = BoundAccessionRequest.model_validate_json(request_raw)
    require_canonical_text_artifact(plan_snapshot, plan.model_dump_json())
    require_canonical_text_artifact(request_snapshot, bound.model_dump_json())
    if (
        plan.request.request_id != request_id
        or bound.request_id != request_id
        or plan.request.repo_root != state_root
        or bound.plan_sha256 != plan.commitment
        or len(plan.documents) > 250
        or len(bound.scope.entries) > MAX_SCOPE_DOCUMENTS
    ):
        raise RefreshBoundaryError("request_plan_binding_mismatch")
    _check_selection(bound, plan)
    return bound, plan, (plan_snapshot, request_snapshot)


def _check_selection(bound: BoundAccessionRequest, plan: AccessionRefreshPlan) -> None:
    scope = bound.scope
    if (
        scope.request.issuer_id != plan.request.issuer_id
        or scope.request.inventory_key != plan.request.inventory_key
        or scope.inventory.inventory_key != plan.request.inventory_key
        or scope.inventory.snapshot_id != plan.snapshot_id
        or scope.inventory.revision != plan.inventory_revision
        or plan.request.accession_number not in scope.accession_numbers
    ):
        raise RefreshBoundaryError("analysis_selection_mismatch")
    selected = tuple(
        entry.expected_document
        for entry in scope.entries
        if entry.expected_document.accession_number == plan.request.accession_number
        and entry.role != "outside_scope"
    )
    if (
        not plan.documents
        or len(selected) != len(plan.documents)
        or {item.expected_document_id: item for item in selected}
        != {item.expected_document_id: item for item in plan.documents}
    ):
        raise RefreshBoundaryError("analysis_population_mismatch")


def _bound_scope_population(conn: sqlite3.Connection, scope: AnalysisScopeRequest) -> None:
    for table, limit in (
        ("expected_documents", MAX_SCOPE_DOCUMENTS),
        ("source_inventory_components", 250),
    ):
        rows = conn.execute(
            f"SELECT 1 FROM {table} WHERE snapshot_id=(SELECT snapshot_id "  # nosec B608 -- closed table list
            "FROM v_source_inventory_current WHERE inventory_key=?) LIMIT ?",
            (scope.inventory_key, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            raise RefreshBoundaryError("analysis_population_over_budget")


def verify_bound_request(
    conn: sqlite3.Connection, bound: BoundAccessionRequest, plan: AccessionRefreshPlan
) -> None:
    _check_selection(bound, plan)
    verify_plan(conn, plan)
    _bound_scope_population(conn, bound.scope.request)
    verify_analysis_scope(conn, bound.scope)


def prepare_bound_request(
    conn: sqlite3.Connection, state_root: Path, body: SecAccessionPlanInput
) -> tuple[BoundAccessionRequest, AccessionRefreshPlan]:
    directory = operation_directory(state_root, body.request_id)
    refresh = body.refresh_request(state_root)
    if max(body.analysis.cutoff_at, body.analysis.observed_through) > datetime.now(UTC):
        raise RefreshBoundaryError("analysis_clock_in_future")
    if body.capture_batch_size * body.max_document_bytes > 1_000_000_000:
        raise RefreshBoundaryError("capture_byte_budget_exceeded")
    if (directory / "request.json").exists():
        bound, plan, _snapshots = load_bound_request(state_root, body.request_id)
        # Compare schema content: the HTTP request has stricter field types only.
        if (
            plan.request != refresh
            or bound.scope.request.model_dump() != body.analysis.model_dump()
        ):
            raise RefreshBoundaryError("request_id_already_bound")
        verify_bound_request(conn, bound, plan)
        return bound, plan
    plan = plan_accession_refresh(conn, refresh)
    conn.execute("BEGIN")
    try:
        _bound_scope_population(conn, body.analysis)
        scope = build_analysis_scope(conn, body.analysis)
    finally:
        conn.rollback()
    bound = BoundAccessionRequest(
        request_id=body.request_id, plan_sha256=plan.commitment, scope=scope
    )
    _check_selection(bound, plan)
    for model in (plan, bound):
        if len(model.model_dump_json().encode()) + 1 > MAX_ARTIFACT_BYTES:
            raise RefreshBoundaryError("request_artifact_over_budget")
    publish_text_no_clobber(directory / "plan.json", plan.model_dump_json())
    publish_text_no_clobber(directory / "request.json", bound.model_dump_json())
    return bound, plan


def read_request_status(
    conn: sqlite3.Connection, state_root: Path, request_id: str
) -> dict[str, object]:
    remaining = [MAX_STATUS_BYTES]
    bound, plan, _snapshots = load_bound_request(state_root, request_id, budget=remaining)
    directory = operation_directory(state_root, request_id) / "attempts"
    records: dict[str, dict[str, AttemptReceipt]] = {}
    require_no_reparse_points(directory)
    if directory.exists():
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ATTEMPTS * 5:
                    raise RefreshBoundaryError("attempt_population_over_budget")
                match = _ATTEMPT.fullmatch(entry.name)
                if match is None:
                    raise RefreshBoundaryError("unexpected_attempt_artifact")
                snapshot, raw = _read_metadata(Path(entry.path), remaining)
                record = AttemptReceipt.model_validate_json(raw)
                states = {
                    "dispatch": "dispatched",
                    "started": "running",
                    "unconfirmed": "completion_unconfirmed",
                }
                if (
                    (match[2] in states and record.state != states[match[2]])
                    or (
                        match[2] == "result" and (record.result is None or record.state is not None)
                    )
                    or (
                        match[2] == "dispatch-failed"
                        and record.state not in ("dispatch_failed", "completion_unconfirmed")
                    )
                    or (match[2] == "dispatch" and record.job_id is None)
                    or (match[2] != "result" and record.result is not None)
                ):
                    raise RefreshBoundaryError("attempt_disposition_mismatch")
                if (
                    record.attempt_id != match[1]
                    or record.request_id != request_id
                    or record.plan_sha256 != plan.commitment
                    or record.request_sha256 != bound.commitment
                    or record.recorded_at.utcoffset() is None
                ):
                    raise RefreshBoundaryError("attempt_binding_mismatch")
                if record.result is not None and (
                    record.result.request_id != request_id
                    or record.result.plan_sha256 != plan.commitment
                    or {item.expected_document_id for item in record.result.items}
                    != {item.expected_document_id for item in plan.documents}
                    or len(record.result.items) != len(plan.documents)
                    or (
                        record.result.state == "succeeded"
                        and any(
                            item.status not in ("extracted", "reused")
                            for item in record.result.items
                        )
                    )
                ):
                    raise RefreshBoundaryError("attempt_result_mismatch")
                if snapshot.size_bytes == 0:
                    raise RefreshBoundaryError("empty_attempt_artifact")
                records.setdefault(record.attempt_id, {})[match[2]] = record
                if len(records) > MAX_ATTEMPTS:
                    raise RefreshBoundaryError("attempt_population_over_budget")
    ordered = sorted(
        records.items(),
        key=lambda item: (min(value.recorded_at for value in item[1].values()), item[0]),
    )
    latest = ordered[-1] if ordered else None
    state = "planned"
    job_id: str | None = None
    result: AccessionRefreshResult | None = None
    if latest:
        receipt = latest[1]
        dispatch = receipt.get("dispatch")
        job_id = dispatch.job_id if dispatch else None
        final = receipt.get("result")
        result = final.result if final else None
        if result is not None:
            state = result.state
        elif "dispatch-failed" in receipt:
            state = receipt["dispatch-failed"].state or "completion_unconfirmed"
        else:
            state = "completion_unconfirmed"
    # No blob opens or extractor calls on the HTTP path. This is not inspection acceptance.
    placeholders = ",".join("?" for _document in plan.documents)
    rows = conn.execute(
        "SELECT expected_document_id,coverage_status,document_version_id "
        f"FROM v_source_coverage_current WHERE expected_document_id IN ({placeholders}) LIMIT 251",  # nosec B608 -- placeholders only
        tuple(document.expected_document_id for document in plan.documents),
    ).fetchall()
    if len(rows) > 250:
        raise RefreshBoundaryError("ledger_metadata_over_budget")
    metadata = {str(row[0]): row for row in rows}
    coverage: list[dict[str, object]] = []
    for document in plan.documents:
        row = metadata.get(document.expected_document_id)
        coverage.append(
            {
                "expected_document_id": document.expected_document_id,
                "coverage_status": str(row[1]) if row else "missing",
                "document_version_id": str(row[2]) if row and row[2] else None,
            }
        )
    return {
        "request_id": request_id,
        "plan_sha256": plan.commitment,
        "request_sha256": bound.commitment,
        "scope_sha256": bound.scope.scope_sha256,
        "scope": bound.scope.model_dump(mode="json"),
        "state": state,
        "attempt_id": latest[0] if latest else None,
        "attempt_count": len(records),
        "job_id": job_id,
        "result": result.model_dump(mode="json") if result else None,
        "launch_confirmed": bool(latest and ("started" in latest[1] or "result" in latest[1])),
        "ledger_metadata": coverage,
        "blob_verification": "not_performed_by_status",
        "cancellation": "unavailable",
        "financial_readiness": "missing",
        "missing_reasons": [
            "successor_scope_required_for_new_observations",
            "financial_admission_missing",
            "research_report_readiness_unverified",
        ],
        "excluded_lanes": ["fmp", "ir", "llm", "xbrl", "financial_admission", "models"],
        "proof_scope": "selected_accession_readable_text_only",
    }
