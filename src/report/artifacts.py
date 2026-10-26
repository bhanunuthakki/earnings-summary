"""Immutable report bodies, manifests, and the compact Brief Library index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Collection, Generator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from bs4 import BeautifulSoup
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from dcf.artifact_promotion import hold_dcf_artifacts
from pipeline.db_writer_gate import hold_writer_gate
from report.file_capture import (
    CAPTURE_WRITE_SETS,
    FileCaptureMismatchError,
    FileCaptureReceipt,
    hold_capture_write_set,
    replay_file_capture,
)
from report.generation_context import (
    ReportCandidateReview,
    ReportCaptureUnavailable,
    ReportGenerationContext,
    ReportModelBinding,
    Sha256,
)
from report.legacy_body import (
    LegacyReaderBody,
    ReaderExtractionReceipt,
    extract_legacy_reader_body,
)
from report.read_capture import (
    CaptureLimits,
    CaptureReceipt,
    ReadCaptureUnavailable,
    replay_read_capture,
)
from report.render_clock import render_now
from research.memo_report_evidence import DecisionBriefReadiness
from runtime.job_runtime import JobLock, current_lock_claim
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

CoverageRole = Literal["portfolio", "evaluation", "unknown"]
ReaderMode = Literal["shared_body", "legacy_standalone"]
ReportRequestKind = Literal["candidate_refresh", "candidate_publish"]
ReportRequestStatus = Literal["running", "completed", "failed"]
_FISCAL_PERIOD_LABEL = re.compile(r"Q[1-4] 20\d{2}")
_EARNINGS_PERIOD_SELECTOR = (
    '.quarter-select[data-quarter-group="earnings"] .qbtn.active[data-quarter]'
)


class ReportSectionRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_id: str
    label: str
    group_id: str


class ReportInteractionManifest(BaseModel):
    """The governed interaction boundary shared by both report wrappers."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_interactions.v1"] = "report_interactions.v1"
    copilot_entry_point: Literal["/api/ask/stream"] = "/api/ask/stream"
    proposal_mode: Literal["explicit_approval"] = "explicit_approval"
    comments_mode: Literal["anchored_receipts"] = "anchored_receipts"


class RenderedReportBody(BaseModel):
    """One complete report body before standalone or Work OS chrome is added."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_body.v1"] = "report_body.v1"
    artifact_id: str
    ticker: str
    report_date: date
    fiscal_period_label: str | None = None
    body_html: str
    body_sha256: str
    sections: tuple[ReportSectionRef, ...]
    interaction_manifest: ReportInteractionManifest

    @classmethod
    def from_html(
        cls,
        *,
        ticker: str,
        report_date: date,
        body_html: str,
        sections: tuple[ReportSectionRef, ...],
        interaction_manifest: ReportInteractionManifest,
        fiscal_period_label: str | None = None,
    ) -> RenderedReportBody:
        normalized_ticker = ticker.strip().upper()
        body_sha256 = hashlib.sha256(body_html.encode("utf-8")).hexdigest()
        return cls(
            artifact_id=(
                f"report_{normalized_ticker}_{report_date.isoformat()}_{body_sha256[:20]}"
            ),
            ticker=normalized_ticker,
            report_date=report_date,
            fiscal_period_label=fiscal_period_label,
            body_html=body_html,
            body_sha256=body_sha256,
            sections=sections,
            interaction_manifest=interaction_manifest,
        )


def _relative_artifact_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact paths must remain relative to the repository")
    return path.as_posix()


class ReportFiscalPeriodEvidence(BaseModel):
    """Exact immutable-report locator supporting one fiscal-period label."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_fiscal_period_evidence.v1"] = "report_fiscal_period_evidence.v1"
    extractor_version: Literal["earnings_quarter_selector.v1"] = "earnings_quarter_selector.v1"
    source_path: str
    source_sha256: str
    source_locator: Literal[
        '.quarter-select[data-quarter-group="earnings"] .qbtn.active[data-quarter]'
    ] = _EARNINGS_PERIOD_SELECTOR
    observed_values: tuple[str, ...]

    _source_relative = field_validator("source_path")(_relative_artifact_path)


class ReportArtifactRef(BaseModel):
    """Stable identity and locations for one persisted research brief."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_artifact.v1"] = "report_artifact.v1"
    artifact_id: str
    ticker: str
    title: str
    artifact_kind: Literal["full_brief"] = "full_brief"
    publication_status: Literal["retained", "draft_preview"] = "retained"
    coverage_role: CoverageRole
    report_date: date
    fiscal_period_label: str | None = None
    generated_at: datetime
    reader_mode: ReaderMode
    standalone_path: str
    body_path: str | None = None
    manifest_path: str
    workspace_sha256: str
    body_sha256: str | None = None
    section_ids: tuple[str, ...] = ()
    provenance_ref: int | None = None
    fiscal_period_evidence: ReportFiscalPeriodEvidence | None = None
    generation_context: ReportGenerationContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    _standalone_relative = field_validator("standalone_path")(_relative_artifact_path)
    _manifest_relative = field_validator("manifest_path")(_relative_artifact_path)

    @field_validator("artifact_id", "ticker")
    @classmethod
    def _safe_directory_component(cls, value: str) -> str:
        if value in {"", ".", ".."} or any(
            character in value for character in ("/", "\\", ":", "\0")
        ):
            raise ValueError("artifact identities must be single safe directory components")
        return value

    @field_validator("body_path")
    @classmethod
    def _body_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact_path(value)


class ReportPublicationApproval(BaseModel):
    """One owner action, committed atomically with canonical current selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_action_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    artifact_id: str
    ticker: str
    body_sha256: Sha256
    workspace_sha256: Sha256
    manifest_sha256: Sha256
    context_sha256: Sha256
    verification_receipt_sha256: Sha256
    model_sha256: Sha256
    evidence_sha256: Sha256
    expected_current_artifact_id: str | None
    approved_at: AwareDatetime


class ReportArtifactIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["report_artifact_index.v1", "report_artifact_index.v2"] = (
        "report_artifact_index.v2"
    )
    generated_at: datetime
    items: tuple[ReportArtifactRef, ...] = ()
    publications: tuple[ReportPublicationApproval, ...] = ()

    @model_validator(mode="after")
    def unique_actions(self) -> ReportArtifactIndex:
        actions = tuple(item.owner_action_id for item in self.publications)
        if len(set(actions)) != len(actions):
            raise ValueError("report approval actions must be unique")
        if self.schema_version == "report_artifact_index.v1" and self.publications:
            raise ValueError("legacy report indexes cannot contain publication approvals")
        return self


class ReportRequestConflictError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ReportPublicationConflictError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ReportRequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic_request_key: str
    kind: ReportRequestKind
    ticker: str
    owner_action_id: str | None = None
    status: ReportRequestStatus
    pid: int
    started_at: str
    updated_at: str
    artifact_id: str | None = None
    result_payload: dict[str, object] | None = None


class ReportPublicationEligibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    reason_code: str | None = None
    published_at: datetime | None = None


class ReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    added: int
    skipped: int


class ReportFiscalPeriodBackfillItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    ticker: str
    status: Literal["eligible", "applied", "skipped_existing", "unresolved", "failed"]
    fiscal_period_label: str | None = None
    evidence: ReportFiscalPeriodEvidence | None = None
    unresolved_reason: str | None = None
    error: str | None = None


class ReportFiscalPeriodBackfillResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_fiscal_period_backfill.v1"] = "report_fiscal_period_backfill.v1"
    apply: bool
    candidates: int
    eligible: int
    applied: int
    skipped_existing: int
    unresolved: int
    failed: int
    unresolved_reasons: dict[str, int] = Field(default_factory=dict)
    items: tuple[ReportFiscalPeriodBackfillItem, ...]


class LegacyBodyMigrationItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    ticker: str
    status: Literal["eligible", "migrated", "rolled_back", "failed"]
    body_sha256: str | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None


class LegacyBodyMigrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["legacy_body_migration.v1"] = "legacy_body_migration.v1"
    apply: bool
    rollback: bool = False
    candidates: int
    eligible: int
    migrated: int
    rolled_back: int = 0
    failed: int
    skipped_shared: int
    items: tuple[LegacyBodyMigrationItem, ...]


def report_artifact_index_path(repo_root: Path) -> Path:
    return repo_root / "output" / "research" / "report_artifacts.v1.json"


@contextmanager
def hold_report_index(repo_root: Path, *, wait_s: float = 5) -> Generator[None]:
    """Own the full index read/modify/write across processes and code checkouts.

    Lock order: model artifacts, mutable sources, database fence, report index.
    The resolved output owner also covers the canonical Windows output junction.
    Synchronous nested calls can reuse this context's exact live claim.
    """
    root = report_artifact_index_path(repo_root).parent.resolve()
    write_set = "report-artifact-index"
    if current_lock_claim(root, write_set) is not None:
        yield
        return
    with JobLock(root, "report-index", [write_set], wait_s=wait_s):
        yield


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _repo_relative(repo_root: Path, path: Path) -> str:
    """Return a stable logical path while admitting the governed output junction."""

    if ".." in path.parts:
        raise ValueError("report artifacts must remain inside the repository")
    lexical_root = Path(os.path.abspath(repo_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError("report artifacts must remain inside the repository") from exc

    resolved_path = path.resolve()
    resolved_root = repo_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as root_exc:
        if not relative.parts or relative.parts[0] != "output":
            raise ValueError("report artifacts must remain inside the repository") from root_exc
        resolved_output = (lexical_root / "output").resolve()
        try:
            resolved_path.relative_to(resolved_output)
        except ValueError as exc:
            raise ValueError("report artifacts must remain inside the repository") from exc
    return relative.as_posix()


def validate_report_artifact_path(repo_root: Path, path: Path) -> None:
    """Use the publisher's retained-root and governed output authority."""
    _repo_relative(repo_root, path)


def _read_index(repo_root: Path) -> ReportArtifactIndex:
    path = report_artifact_index_path(repo_root)
    if not path.exists():
        return ReportArtifactIndex(generated_at=render_now())
    return ReportArtifactIndex.model_validate_json(path.read_text(encoding="utf-8"))


def load_report_artifact_index(repo_root: Path) -> ReportArtifactIndex:
    """Read the compact index and exclude entries whose deliverable is gone."""

    index = _read_index(repo_root)
    existing = tuple(item for item in index.items if (repo_root / item.standalone_path).is_file())
    return index.model_copy(update={"items": existing})


def _artifact_dir(repo_root: Path, ticker: str, artifact_id: str) -> Path:
    path = repo_root / "output" / "research" / ticker.upper() / "artifacts" / artifact_id
    validate_report_artifact_path(repo_root, path)
    return path


def _load_sidecar(model: type[BaseModel], path: Path) -> BaseModel | None:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def save_report_candidate_sidecars(
    repo_root: Path,
    *,
    ticker: str,
    artifact_id: str,
    capture_receipt: CaptureReceipt,
    file_capture_receipt: FileCaptureReceipt,
    readiness: DecisionBriefReadiness,
    candidate_review: ReportCandidateReview,
) -> None:
    directory = _artifact_dir(repo_root, ticker, artifact_id)
    for name, model in (
        ("capture_receipt.json", capture_receipt),
        ("file_capture_receipt.json", file_capture_receipt),
        ("readiness.json", readiness),
        ("candidate_review.json", candidate_review),
    ):
        _atomic_write_text(directory / name, model.model_dump_json(indent=2) + "\n")
    review_json = candidate_review.model_dump_json(indent=2) + "\n"
    artifact = next(
        (item for item in _read_index(repo_root).items if item.artifact_id == artifact_id), None
    )
    if artifact is None:
        raise ValueError("candidate artifact is missing from index")
    review_sha = hashlib.sha256(review_json.encode("utf-8")).hexdigest()
    _atomic_write_text(candidate_review_path(repo_root, artifact, review_sha), review_json)


def resolve_report_model_binding(
    repo_root: Path,
    ticker: str,
    *,
    conn: sqlite3.Connection,
    model_run_id: int | None = None,
) -> ReportModelBinding | ReportCaptureUnavailable:
    symbol = ticker.strip().upper()
    if model_run_id is not None:
        row = conn.execute(
            "SELECT id, ticker, input_sha256, workbook_sha256, engine_version, provenance_json, assumption_snapshot_json FROM dcf_runs WHERE id = ? AND ticker = ?",
            (model_run_id, symbol),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, ticker, input_sha256, workbook_sha256, engine_version, provenance_json, assumption_snapshot_json FROM dcf_runs WHERE ticker = ? ORDER BY is_latest DESC, valuation_date DESC, id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    if row is None or not row[2] or not row[3] or not row[4]:
        return ReportCaptureUnavailable(reason_code="selected_context_unavailable")
    family = "fcff_dcf"
    try:
        defaults = json.loads(
            (repo_root / "micro_thesis" / "holdings" / f"{symbol}.json").read_text(encoding="utf-8")
        )
        family = defaults.get("dcf_defaults", {}).get("valuation_model", family)
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return ReportModelBinding(
        row_id=int(row[0]),
        ticker=symbol,
        family=str(family),
        input_sha256=str(row[2]),
        workbook_sha256=str(row[3]),
        engine_version=str(row[4]),
        provenance_sha256=hashlib.sha256(str(row[5] or "{}").encode("utf-8")).hexdigest(),
        assumption_snapshot_sha256=hashlib.sha256(str(row[6] or "{}").encode("utf-8")).hexdigest(),
    )


def load_report_candidate_review(
    repo_root: Path, ticker: str, artifact_id: str
) -> ReportCandidateReview | None:
    value = _load_sidecar(
        ReportCandidateReview,
        _artifact_dir(repo_root, ticker, artifact_id) / "candidate_review.json",
    )
    return value if isinstance(value, ReportCandidateReview) else None


def load_report_capture_receipt(
    repo_root: Path, ticker: str, artifact_id: str
) -> CaptureReceipt | None:
    value = _load_sidecar(
        CaptureReceipt, _artifact_dir(repo_root, ticker, artifact_id) / "capture_receipt.json"
    )
    return value if isinstance(value, CaptureReceipt) else None


def load_report_file_capture_receipt(
    repo_root: Path, ticker: str, artifact_id: str
) -> FileCaptureReceipt | None:
    value = _load_sidecar(
        FileCaptureReceipt,
        _artifact_dir(repo_root, ticker, artifact_id) / "file_capture_receipt.json",
    )
    return value if isinstance(value, FileCaptureReceipt) else None


def publish_report_candidate(
    repo_root: Path,
    *,
    db_path: Path,
    ticker: str,
    artifact_id: str,
    expected_current_artifact_id: str | None,
    owner_action_id: str,
    approved_at: datetime | None = None,
) -> tuple[ReportPublicationApproval, bool]:
    symbol = ticker.strip().upper()
    with ExitStack() as stack:
        stack.enter_context(hold_dcf_artifacts(repo_root, symbol, owner="report-publish"))
        stack.enter_context(hold_capture_write_set(repo_root, CAPTURE_WRITE_SETS))
        stack.enter_context(hold_writer_gate(db_path, owner="report-publish"))
        writer_conn = connect_sqlite(
            db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True
        )
        stack.callback(writer_conn.close)
        writer_conn.execute("BEGIN IMMEDIATE")
        stack.enter_context(hold_report_index(repo_root))
        index = _read_index(repo_root)
        existing = next(
            (item for item in index.publications if item.owner_action_id == owner_action_id), None
        )
        if existing is not None:
            if (existing.artifact_id, existing.expected_current_artifact_id) == (
                artifact_id,
                expected_current_artifact_id,
            ):
                return existing, True
            raise ReportPublicationConflictError("owner_action_id_conflict")
        artifact = next(
            (
                item
                for item in index.items
                if item.artifact_id == artifact_id and item.ticker == symbol
            ),
            None,
        )
        if (
            artifact is None
            or artifact.publication_status != "draft_preview"
            or artifact.reader_mode != "shared_body"
            or artifact.body_sha256 is None
            or not isinstance(artifact.generation_context, ReportGenerationContext)
        ):
            raise ReportPublicationConflictError("candidate_unavailable")
        capture = load_report_capture_receipt(repo_root, symbol, artifact_id)
        file_receipt = load_report_file_capture_receipt(repo_root, symbol, artifact_id)
        review = load_report_candidate_review(repo_root, symbol, artifact_id)
        if capture is None or file_receipt is None or review is None:
            raise ReportPublicationConflictError("candidate_sidecars_missing")
        if not review.ready or review.reason_codes:
            raise ReportPublicationConflictError(
                "candidate_review_blocked", {"reason_codes": list(review.reason_codes)}
            )
        live_binding = resolve_report_model_binding(repo_root, symbol, conn=writer_conn)
        if (
            isinstance(live_binding, ReportCaptureUnavailable)
            or live_binding != artifact.generation_context.model
        ):
            raise ReportPublicationConflictError("model_changed_since_candidate")
        try:
            replay_read_capture(db_path, capture, limits=CaptureLimits())
        except ReadCaptureUnavailable as exc:
            raise ReportPublicationConflictError("sql_snapshot_drifted") from exc
        try:
            replay_file_capture(file_receipt, repo_root)
        except FileCaptureMismatchError as exc:
            raise ReportPublicationConflictError("file_snapshot_drifted") from exc
        current = latest_report_artifacts_for_tickers(repo_root, (symbol,)).get(symbol)
        current_id = current.artifact_id if current else None
        if (
            current_id != expected_current_artifact_id
            or artifact.generation_context.expected_current_artifact_id
            != expected_current_artifact_id
        ):
            raise ReportPublicationConflictError("current_report_changed")
        review_json = review.model_dump_json(indent=2) + "\n"
        review_sha256 = hashlib.sha256(review_json.encode("utf-8")).hexdigest()
        _atomic_write_text(candidate_review_path(repo_root, artifact, review_sha256), review_json)
        approval = ReportPublicationApproval(
            owner_action_id=owner_action_id,
            artifact_id=artifact.artifact_id,
            ticker=symbol,
            body_sha256=str(artifact.body_sha256),
            workspace_sha256=artifact.workspace_sha256,
            manifest_sha256=hashlib.sha256(
                (repo_root / artifact.manifest_path).read_bytes()
            ).hexdigest(),
            context_sha256=artifact.generation_context.sha256,
            verification_receipt_sha256=review_sha256,
            model_sha256=artifact.generation_context.model_sha256,
            evidence_sha256=artifact.generation_context.evidence_sha256,
            expected_current_artifact_id=expected_current_artifact_id,
            approved_at=approved_at
            or max(render_now(), artifact.generated_at, review.evaluated_at),
        )
        _verify_publication_record(repo_root, artifact, approval)
        _write_index(repo_root, index.items, publications=(*index.publications, approval))
        return approval, False


def build_report_sync_status(
    repo_root: Path,
    ticker: str,
    *,
    conn: sqlite3.Connection,
    candidate_artifact_id: str | None = None,
) -> dict[str, object]:
    symbol = ticker.strip().upper()
    current = latest_report_artifacts_for_tickers(repo_root, (symbol,)).get(symbol)
    binding = resolve_report_model_binding(repo_root, symbol, conn=conn)
    candidate = (
        next(
            (
                item
                for item in _read_index(repo_root).items
                if item.artifact_id == candidate_artifact_id and item.ticker == symbol
            ),
            None,
        )
        if candidate_artifact_id
        else None
    )
    review = (
        load_report_candidate_review(repo_root, symbol, candidate_artifact_id)
        if candidate_artifact_id
        else None
    )
    stale = (
        current is None
        or not isinstance(binding, ReportModelBinding)
        or current.generation_context is None
        or current.generation_context.model != binding
    )
    stale_reason = "model_binding_changed_or_missing" if stale else None
    if not stale and current is not None:
        file_receipt = load_report_file_capture_receipt(repo_root, symbol, current.artifact_id)
        read_capture = load_report_capture_receipt(repo_root, symbol, current.artifact_id)
        if file_receipt is None or read_capture is None:
            stale = True
            stale_reason = "missing_capture_receipts"
        else:
            try:
                replay_file_capture(file_receipt, repo_root)
            except (FileCaptureMismatchError, OSError, ValueError):
                stale = True
                stale_reason = "consumed_evidence_drift"
            if not stale:
                db_file = next(
                    (
                        str(row[2])
                        for row in conn.execute("PRAGMA database_list")
                        if str(row[1]) == "main"
                    ),
                    None,
                )
                if db_file and Path(db_file).is_file():
                    try:
                        replay_read_capture(db_file, read_capture, limits=CaptureLimits())
                    except (ReadCaptureUnavailable, OSError, ValueError):
                        stale = True
                        stale_reason = "consumed_evidence_drift"
    payload: dict[str, object] = {
        "schema_version": "report_sync_status.v1",
        "ticker": symbol,
        "current_artifact_id": current.artifact_id if current else None,
        "model_status": "available" if isinstance(binding, ReportModelBinding) else "unavailable",
        "model_binding": binding.model_dump(mode="json")
        if isinstance(binding, ReportModelBinding)
        else None,
        "report_stale": stale,
        "stale_reason": stale_reason,
        "candidate_artifact_id": candidate.artifact_id if candidate else None,
        "candidate_review": review.model_dump(mode="json") if review else None,
    }
    payload["etag"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def find_matching_candidate_artifact(
    repo_root: Path, *, ticker: str, context_sha256: str
) -> ReportArtifactRef | None:
    symbol = ticker.upper()
    items = sorted(
        load_report_artifact_index(repo_root).items,
        key=lambda item: (item.generated_at, item.artifact_id),
        reverse=True,
    )
    for item in items:
        if (
            item.ticker != symbol
            or item.publication_status != "draft_preview"
            or item.reader_mode != "shared_body"
            or not isinstance(item.generation_context, ReportGenerationContext)
            or item.generation_context.sha256 != context_sha256
            or item.body_path is None
            or not (repo_root / item.standalone_path).is_file()
            or not (repo_root / item.body_path).is_file()
            or load_report_candidate_review(repo_root, symbol, item.artifact_id) is None
        ):
            continue
        return item
    return None


def _request_index_path(repo_root: Path) -> Path:
    return repo_root / "output" / "research" / "_index" / "requests.json"


def _read_request_records(repo_root: Path) -> list[ReportRequestRecord]:
    path = _request_index_path(repo_root)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("report request index must be a JSON array")
    items: list[object] = cast("list[object]", payload)
    return [ReportRequestRecord.model_validate(item) for item in items]


def _write_request_records(repo_root: Path, records: list[ReportRequestRecord]) -> None:
    _atomic_write_text(
        _request_index_path(repo_root),
        json.dumps([item.model_dump(mode="json") for item in records], indent=2) + "\n",
    )


def begin_or_reuse_report_request(
    repo_root: Path,
    *,
    semantic_request_key: str,
    kind: ReportRequestKind,
    ticker: str,
    owner_action_id: str | None = None,
) -> tuple[ReportRequestRecord, bool]:
    with hold_report_index(repo_root):
        records = _read_request_records(repo_root)
        if owner_action_id is not None and any(
            record.owner_action_id == owner_action_id
            and record.semantic_request_key != semantic_request_key
            for record in records
        ):
            raise ReportRequestConflictError("owner_action_id_conflict")
        prior = next(
            (record for record in records if record.semantic_request_key == semantic_request_key),
            None,
        )
        if prior is not None:
            if prior.status == "completed":
                return prior, True
            if prior.status == "running":
                try:
                    os.kill(prior.pid, 0)
                except ProcessLookupError:
                    records.remove(prior)
                else:
                    return prior, True
            else:
                records.remove(prior)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = ReportRequestRecord(
            semantic_request_key=semantic_request_key,
            kind=kind,
            ticker=ticker.upper(),
            owner_action_id=owner_action_id,
            status="running",
            pid=os.getpid(),
            started_at=now,
            updated_at=now,
        )
        records.append(record)
        _write_request_records(repo_root, records)
        return record, False


def claim_drifted_report_request(
    repo_root: Path,
    *,
    semantic_request_key: str,
    ticker: str,
    owner_action_id: str | None = None,
) -> tuple[ReportRequestRecord, bool]:
    with hold_report_index(repo_root):
        records = _read_request_records(repo_root)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for position, record in enumerate(records):
            if record.semantic_request_key == semantic_request_key:
                if record.status == "running":
                    try:
                        os.kill(record.pid, 0)
                        return record, True
                    except ProcessLookupError:
                        pass
                updated = record.model_copy(
                    update={
                        "status": "running",
                        "updated_at": now,
                        "pid": os.getpid(),
                        "owner_action_id": owner_action_id or record.owner_action_id,
                        "artifact_id": None,
                        "result_payload": None,
                    }
                )
                records[position] = updated
                _write_request_records(repo_root, records)
                return updated, False
        new_record = ReportRequestRecord(
            semantic_request_key=semantic_request_key,
            kind="candidate_refresh",
            ticker=ticker.upper(),
            owner_action_id=owner_action_id,
            status="running",
            pid=os.getpid(),
            started_at=now,
            updated_at=now,
        )
        records.append(new_record)
        _write_request_records(repo_root, records)
        return new_record, False


def complete_report_request(
    repo_root: Path,
    *,
    semantic_request_key: str,
    status: Literal["completed", "failed"],
    artifact_id: str | None = None,
    result_payload: dict[str, object] | None = None,
) -> ReportRequestRecord:
    with hold_report_index(repo_root):
        records = _read_request_records(repo_root)
        for position, record in enumerate(records):
            if record.semantic_request_key == semantic_request_key:
                updated = record.model_copy(
                    update={
                        "status": status,
                        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "artifact_id": artifact_id,
                        "result_payload": result_payload,
                    }
                )
                records[position] = updated
                _write_request_records(repo_root, records)
                return updated
    raise KeyError("report request was not found")


def candidate_review_path(repo_root: Path, artifact: ReportArtifactRef, digest: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("candidate review identity must be a SHA256")
    path = (repo_root / artifact.manifest_path).parent / "candidate_reviews" / f"{digest}.json"
    validate_report_artifact_path(repo_root, path)
    return path


def _read_verified_bytes(repo_root: Path, relative: str | None, digest: str | None) -> bytes:
    if relative is None or digest is None:
        raise ValueError("report publication bytes are missing")
    path = repo_root / relative
    validate_report_artifact_path(repo_root, path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("report publication bytes differ")
    return raw


def _verify_publication_record(
    repo_root: Path, artifact: ReportArtifactRef, approval: ReportPublicationApproval
) -> None:
    context = artifact.generation_context
    if (
        context is None
        or (
            approval.ticker,
            approval.body_sha256,
            approval.workspace_sha256,
            approval.context_sha256,
            approval.model_sha256,
            approval.evidence_sha256,
            approval.expected_current_artifact_id,
        )
        != (
            artifact.ticker,
            artifact.body_sha256,
            artifact.workspace_sha256,
            context.sha256,
            context.model_sha256,
            context.evidence_sha256,
            context.expected_current_artifact_id,
        )
        or approval.approved_at < artifact.generated_at
    ):
        raise ValueError("report approval binding differs")
    manifest = ReportArtifactRef.model_validate_json(
        _read_verified_bytes(repo_root, artifact.manifest_path, approval.manifest_sha256)
    )
    excluded = {"fiscal_period_label", "fiscal_period_evidence"}
    if manifest.model_dump(exclude=excluded) != artifact.model_dump(exclude=excluded):
        raise ValueError("report approval manifest differs")
    path = candidate_review_path(repo_root, artifact, approval.verification_receipt_sha256)
    review = ReportCandidateReview.model_validate_json(
        _read_verified_bytes(
            repo_root, _repo_relative(repo_root, path), approval.verification_receipt_sha256
        )
    )
    if (
        not review.ready
        or review.reason_codes
        or (
            review.artifact_id,
            review.body_sha256,
            review.workspace_sha256,
            review.context_sha256,
            review.model_sha256,
            review.evidence_sha256,
        )
        != (
            artifact.artifact_id,
            artifact.body_sha256,
            artifact.workspace_sha256,
            context.sha256,
            context.model_sha256,
            context.evidence_sha256,
        )
        or review.evaluated_at > approval.approved_at
    ):
        raise ValueError("report approval review differs or is blocked")
    content = DecisionBriefReadiness.model_validate_json(
        _read_verified_bytes(repo_root, review.content_receipt_path, review.content_receipt_sha256)
    )
    if (
        content.artifact_id != artifact.artifact_id
        or content.ticker != artifact.ticker
        or content.body_sha256 != artifact.body_sha256
        or content.status != "verified"
        or content.reason_codes
        or content.context_review_sha256 is None
        or content.evaluated_at != review.evaluated_at
    ):
        raise ValueError("report approval content proof differs or is blocked")
    _read_verified_bytes(repo_root, artifact.body_path, artifact.body_sha256)
    _read_verified_bytes(repo_root, artifact.standalone_path, artifact.workspace_sha256)


def publication_eligibility(
    repo_root: Path,
    artifact: ReportArtifactRef,
    *,
    index: ReportArtifactIndex | None = None,
    as_of: datetime | None = None,
) -> ReportPublicationEligibility:
    """Share exact approval and knowledge-cutoff rules across public readers/replay."""
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise ValueError("publication cutoff requires a timezone")
    if artifact.publication_status == "retained":
        if as_of is not None and artifact.generated_at > as_of:
            return ReportPublicationEligibility(
                eligible=False, reason_code="report_not_published_at_cutoff"
            )
        return ReportPublicationEligibility(eligible=True, published_at=artifact.generated_at)
    current = _read_index(repo_root) if index is None else index
    approvals = sorted(
        (item for item in current.publications if item.artifact_id == artifact.artifact_id),
        key=lambda item: item.approved_at,
        reverse=True,
    )
    for approval in approvals:
        if as_of is not None and approval.approved_at > as_of:
            continue
        try:
            _verify_publication_record(repo_root, artifact, approval)
            return ReportPublicationEligibility(eligible=True, published_at=approval.approved_at)
        except (OSError, UnicodeError, ValueError):
            continue
    return ReportPublicationEligibility(
        eligible=False,
        reason_code="report_approval_missing_invalid_or_late"
        if approvals
        else "memo_draft_preview_not_accepted",
    )


def latest_report_artifacts(
    index: ReportArtifactIndex, *, repo_root: Path | None = None, as_of: datetime | None = None
) -> tuple[ReportArtifactRef, ...]:
    """Return the exact Full Brief population surfaced by the Brief Library."""

    latest_by_ticker: dict[str, ReportArtifactRef] = {}
    clocks: dict[str, datetime] = {}
    for artifact in index.items:
        if repo_root is None and artifact.publication_status == "draft_preview":
            continue
        eligibility = (
            publication_eligibility(repo_root, artifact, index=index, as_of=as_of)
            if repo_root is not None
            else ReportPublicationEligibility(eligible=True, published_at=artifact.generated_at)
        )
        if not eligibility.eligible or eligibility.published_at is None:
            continue
        published_at = eligibility.published_at
        prior = latest_by_ticker.get(artifact.ticker)
        if prior is None or (published_at, artifact.artifact_id) > (
            clocks[artifact.ticker],
            prior.artifact_id,
        ):
            latest_by_ticker[artifact.ticker] = artifact
            clocks[artifact.ticker] = published_at
    return tuple(
        sorted(
            latest_by_ticker.values(),
            key=lambda artifact: (clocks[artifact.ticker], artifact.artifact_id),
            reverse=True,
        )
    )


def latest_report_artifacts_for_tickers(
    repo_root: Path,
    tickers: Collection[str],
) -> dict[str, ReportArtifactRef]:
    """Resolve only the newest existing indexed brief for each requested ticker.

    This avoids building the full library or statting unrelated deliverables on
    a per-roster URL lookup. The index remains the sole artifact authority.
    """
    requested = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
    if not requested:
        return {}
    latest: dict[str, ReportArtifactRef] = {}
    index = _read_index(repo_root)
    clocks: dict[str, datetime] = {}
    for artifact in index.items:
        ticker = artifact.ticker.strip().upper()
        if ticker not in requested:
            continue
        if not (repo_root / artifact.standalone_path).is_file():
            continue
        eligibility = publication_eligibility(repo_root, artifact, index=index)
        if not eligibility.eligible or eligibility.published_at is None:
            continue
        prior = latest.get(ticker)
        if prior is None or (eligibility.published_at, artifact.artifact_id) > (
            clocks[ticker],
            prior.artifact_id,
        ):
            latest[ticker] = artifact
            clocks[ticker] = eligibility.published_at
    return latest


def _write_index(
    repo_root: Path,
    items: tuple[ReportArtifactRef, ...],
    *,
    publications: tuple[ReportPublicationApproval, ...] | None = None,
) -> None:
    root = report_artifact_index_path(repo_root).parent.resolve()
    if current_lock_claim(root, "report-artifact-index") is None:
        raise RuntimeError("report index mutation requires its read/modify/write owner")
    ordered = tuple(
        sorted(items, key=lambda item: (item.generated_at, item.artifact_id), reverse=True)
    )
    index = ReportArtifactIndex(
        generated_at=render_now(),
        items=ordered,
        publications=_read_index(repo_root).publications if publications is None else publications,
    )
    _atomic_write_text(
        report_artifact_index_path(repo_root),
        index.model_dump_json(indent=2) + "\n",
    )


def _write_manifest(repo_root: Path, ref: ReportArtifactRef) -> None:
    _atomic_write_text(repo_root / ref.manifest_path, ref.model_dump_json(indent=2) + "\n")


def persist_report_artifact(
    *,
    repo_root: Path,
    body: RenderedReportBody,
    standalone_path: Path,
    generated_at: datetime,
    coverage_role: CoverageRole,
    title: str,
    provenance_ref: int | None = None,
    publication_status: Literal["retained", "draft_preview"] = "retained",
    generation_context: ReportGenerationContext | None = None,
) -> ReportArtifactRef:
    """Persist a shared body and atomically upsert its immutable index entry."""

    with hold_report_index(repo_root):
        if not standalone_path.is_file():
            raise FileNotFoundError(standalone_path)
        reader_body = extract_legacy_reader_body(body.body_html, artifact_id=body.artifact_id)
        artifact_dir = (
            repo_root / "output" / "research" / body.ticker / "artifacts" / body.artifact_id
        )
        standalone_snapshot_path = artifact_dir / "standalone.html"
        body_path = artifact_dir / "body.html"
        manifest_path = artifact_dir / "manifest.json"
        if manifest_path.is_file():
            existing = ReportArtifactRef.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if existing.publication_status != publication_status:
                raise ValueError(
                    "report artifact publication status differs from retained identity"
                )
            if existing.generation_context != generation_context:
                raise ValueError(
                    "report artifact generation context differs from immutable identity"
                )
            existing_body = repo_root / (existing.body_path or "")
            existing_standalone = repo_root / existing.standalone_path
            if not existing_body.is_file() or not existing_standalone.is_file():
                raise FileNotFoundError("persisted report artifact is incomplete")
            if hashlib.sha256(existing_body.read_bytes()).hexdigest() != existing.body_sha256:
                raise ValueError("persisted report body checksum mismatch")
            if (
                hashlib.sha256(existing_standalone.read_bytes()).hexdigest()
                != existing.workspace_sha256
            ):
                raise ValueError("persisted standalone report checksum mismatch")
            prior = _read_index(repo_root)
            retained = tuple(
                item for item in prior.items if item.artifact_id != existing.artifact_id
            )
            _write_index(repo_root, (existing, *retained))
            return existing
        _atomic_write_text(
            standalone_snapshot_path,
            standalone_path.read_text(encoding="utf-8"),
        )
        _atomic_write_text(body_path, reader_body.body_html)
        ref = ReportArtifactRef(
            artifact_id=body.artifact_id,
            ticker=body.ticker,
            title=title,
            coverage_role=coverage_role,
            report_date=body.report_date,
            fiscal_period_label=body.fiscal_period_label,
            generated_at=generated_at,
            reader_mode="shared_body",
            standalone_path=_repo_relative(repo_root, standalone_snapshot_path),
            body_path=_repo_relative(repo_root, body_path),
            manifest_path=_repo_relative(repo_root, manifest_path),
            workspace_sha256=hashlib.sha256(standalone_snapshot_path.read_bytes()).hexdigest(),
            body_sha256=reader_body.body_sha256,
            section_ids=tuple(section.section_id for section in body.sections),
            provenance_ref=provenance_ref,
            publication_status=publication_status,
            generation_context=generation_context,
        )
        _write_manifest(repo_root, ref)
        prior = _read_index(repo_root)
        retained = tuple(item for item in prior.items if item.artifact_id != ref.artifact_id)
        _write_index(repo_root, (ref, *retained))
        return ref


def reconcile_legacy_workspace_reports(
    repo_root: Path,
    *,
    coverage_role_for: Callable[[str], CoverageRole] | None = None,
) -> ReconcileResult:
    """Index surviving legacy reports without regenerating their historical facts."""

    with hold_report_index(repo_root):

        def unknown_role(_ticker: str) -> CoverageRole:
            return "unknown"

        role_for: Callable[[str], CoverageRole] = coverage_role_for or unknown_role
        prior = _read_index(repo_root)
        items = list(prior.items)
        known_paths = {item.standalone_path for item in items}
        added = 0
        skipped = 0
        research_root = repo_root / "output" / "research"
        for workspace_path in sorted(research_root.glob("*/*_workspace.html")):
            relative_workspace = _repo_relative(repo_root, workspace_path)
            if relative_workspace in known_paths:
                skipped += 1
                continue
            report_date_text = workspace_path.name.removesuffix("_workspace.html")
            try:
                report_date = date.fromisoformat(report_date_text)
            except ValueError:
                skipped += 1
                continue
            ticker = workspace_path.parent.name.upper()
            workspace_sha256 = hashlib.sha256(workspace_path.read_bytes()).hexdigest()
            artifact_id = f"report_{ticker}_{report_date.isoformat()}_{workspace_sha256[:20]}"
            manifest_path = workspace_path.with_name(f"{report_date.isoformat()}_manifest.json")
            ref = ReportArtifactRef(
                artifact_id=artifact_id,
                ticker=ticker,
                title=f"{ticker} Full Research Brief",
                coverage_role=role_for(ticker),
                report_date=report_date,
                generated_at=datetime.fromtimestamp(workspace_path.stat().st_mtime, tz=UTC),
                reader_mode="legacy_standalone",
                standalone_path=relative_workspace,
                manifest_path=_repo_relative(repo_root, manifest_path),
                workspace_sha256=workspace_sha256,
            )
            _write_manifest(repo_root, ref)
            items.append(ref)
            known_paths.add(relative_workspace)
            added += 1
        _write_index(repo_root, tuple(items))
        return ReconcileResult(added=added, skipped=skipped)


MigrationPlan = tuple[
    ReportArtifactRef,
    ReportArtifactRef,
    LegacyReaderBody,
    ReaderExtractionReceipt,
]


def _migration_artifact_paths(
    repo_root: Path, artifact: ReportArtifactRef
) -> tuple[Path, Path, Path]:
    artifact_dir = (
        repo_root / "output" / "research" / artifact.ticker / "artifacts" / artifact.artifact_id
    )
    return (
        artifact_dir / "body.html",
        artifact_dir / "reader_extraction.v1.json",
        artifact_dir / "reader_manifest.v1.json",
    )


def _activate_migration_plans(
    repo_root: Path,
    prior: ReportArtifactIndex,
    plans: list[MigrationPlan],
) -> None:
    """Stage and verify the complete batch before one atomic index activation."""

    staging_parent = repo_root / ".tmp"
    staging_parent.mkdir(parents=True, exist_ok=True)
    replacements = {migrated.artifact_id: migrated for _, migrated, _, _ in plans}
    updated = tuple(replacements.get(item.artifact_id, item) for item in prior.items)
    created: list[Path] = []
    with tempfile.TemporaryDirectory(
        prefix="report-body-migration-", dir=staging_parent
    ) as staging_name:
        staging_root = Path(staging_name)
        transfers: list[tuple[Path, Path, str]] = []
        for legacy, migrated, extracted, receipt in plans:
            stage_dir = (
                staging_root / hashlib.sha256(legacy.artifact_id.encode("utf-8")).hexdigest()
            )
            stage_body = stage_dir / "body.html"
            stage_receipt = stage_dir / "reader_extraction.v1.json"
            stage_manifest = stage_dir / "reader_manifest.v1.json"
            _atomic_write_text(stage_body, extracted.body_html)
            _atomic_write_text(stage_receipt, receipt.model_dump_json(indent=2) + "\n")
            _atomic_write_text(stage_manifest, migrated.model_dump_json(indent=2) + "\n")
            if hashlib.sha256(stage_body.read_bytes()).hexdigest() != extracted.body_sha256:
                raise ValueError("staged reader body checksum mismatch")
            ReaderExtractionReceipt.model_validate_json(stage_receipt.read_text(encoding="utf-8"))
            ReportArtifactRef.model_validate_json(stage_manifest.read_text(encoding="utf-8"))
            body_path, receipt_path, manifest_path = _migration_artifact_paths(repo_root, legacy)
            transfers.extend(
                (
                    (stage_body, body_path, extracted.body_sha256),
                    (
                        stage_receipt,
                        receipt_path,
                        hashlib.sha256(stage_receipt.read_bytes()).hexdigest(),
                    ),
                    (
                        stage_manifest,
                        manifest_path,
                        hashlib.sha256(stage_manifest.read_bytes()).hexdigest(),
                    ),
                )
            )
        for _staged, final, expected_sha256 in transfers:
            if final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() != expected_sha256:
                raise ValueError(f"derived migration target already differs: {final.name}")
        try:
            for staged, final, expected_sha256 in transfers:
                final.parent.mkdir(parents=True, exist_ok=True)
                if not final.exists():
                    staged.replace(final)
                    created.append(final)
                if hashlib.sha256(final.read_bytes()).hexdigest() != expected_sha256:
                    raise ValueError(f"derived migration target checksum mismatch: {final.name}")
            _write_index(repo_root, updated)
        except (OSError, ValueError):
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise


def migrate_legacy_report_bodies(
    repo_root: Path,
    *,
    tickers: set[str] | None = None,
    apply: bool = False,
) -> LegacyBodyMigrationResult:
    """Derive inert shared-reader bodies while keeping standalone artifacts immutable."""

    with hold_report_index(repo_root) if apply else nullcontext():
        normalized_tickers = (
            None if tickers is None else {value.strip().upper() for value in tickers}
        )
        prior = _read_index(repo_root)
        selected = [
            artifact
            for artifact in prior.items
            if normalized_tickers is None or artifact.ticker in normalized_tickers
        ]
        candidates = [
            artifact for artifact in selected if artifact.reader_mode == "legacy_standalone"
        ]
        skipped_shared = len(selected) - len(candidates)
        plans: list[MigrationPlan] = []
        results: list[LegacyBodyMigrationItem] = []
        failed = 0
        for artifact in candidates:
            try:
                source_path = repo_root / artifact.standalone_path
                if not source_path.is_file():
                    raise FileNotFoundError(source_path)
                source_bytes = source_path.read_bytes()
                source_sha256 = hashlib.sha256(source_bytes).hexdigest()
                if source_sha256 != artifact.workspace_sha256:
                    raise ValueError("standalone report checksum mismatch")
                extracted = extract_legacy_reader_body(
                    source_bytes.decode("utf-8"), artifact_id=artifact.artifact_id
                )
                body_path, _receipt_path, manifest_path = _migration_artifact_paths(
                    repo_root, artifact
                )
                body_relative = _repo_relative(repo_root, body_path)
                migrated_ref = artifact.model_copy(
                    update={
                        "reader_mode": "shared_body",
                        "body_path": body_relative,
                        "body_sha256": extracted.body_sha256,
                        "manifest_path": _repo_relative(repo_root, manifest_path),
                        "section_ids": extracted.section_ids,
                    }
                )
                receipt = ReaderExtractionReceipt(
                    artifact_id=artifact.artifact_id,
                    source_path=artifact.standalone_path,
                    source_sha256=source_sha256,
                    legacy_manifest_path=artifact.manifest_path,
                    body_path=body_relative,
                    body_sha256=extracted.body_sha256,
                    text_sha256=extracted.text_sha256,
                    section_ids=extracted.section_ids,
                    id_map=extracted.id_map,
                    source_metrics=extracted.source_metrics,
                    preserved_metrics=extracted.preserved_metrics,
                    heading_count=extracted.heading_count,
                    table_count=extracted.table_count,
                    link_count=extracted.link_count,
                    source_link_count=extracted.source_link_count,
                    warnings=extracted.warnings,
                )
                plans.append((artifact, migrated_ref, extracted, receipt))
                results.append(
                    LegacyBodyMigrationItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="eligible",
                        body_sha256=extracted.body_sha256,
                        warnings=extracted.warnings,
                    )
                )
            except (OSError, UnicodeError, ValueError) as exc:
                failed += 1
                results.append(
                    LegacyBodyMigrationItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="failed",
                        error=str(exc),
                    )
                )
        migrated = 0
        if apply and plans and not failed:
            try:
                _activate_migration_plans(repo_root, prior, plans)
            except (OSError, ValueError) as exc:
                failed = len(plans)
                results = [
                    item.model_copy(update={"status": "failed", "error": str(exc)})
                    if item.status == "eligible"
                    else item
                    for item in results
                ]
            else:
                migrated = len(plans)
                results = [
                    item.model_copy(update={"status": "migrated"})
                    if item.status == "eligible"
                    else item
                    for item in results
                ]
        return LegacyBodyMigrationResult(
            apply=apply,
            candidates=len(candidates),
            eligible=len(plans),
            migrated=migrated,
            failed=failed,
            skipped_shared=skipped_shared,
            items=tuple(results),
        )


def rollback_legacy_report_bodies(
    repo_root: Path,
    *,
    tickers: set[str] | None = None,
) -> LegacyBodyMigrationResult:
    """Atomically reactivate preserved legacy manifests, then remove derived bodies."""

    with hold_report_index(repo_root):
        normalized_tickers = (
            None if tickers is None else {value.strip().upper() for value in tickers}
        )
        prior = _read_index(repo_root)
        candidates = [
            artifact
            for artifact in prior.items
            if artifact.reader_mode == "shared_body"
            and artifact.manifest_path.endswith("/reader_manifest.v1.json")
            and (normalized_tickers is None or artifact.ticker in normalized_tickers)
        ]
        replacements: dict[str, ReportArtifactRef] = {}
        cleanup_paths: dict[str, tuple[Path, Path, Path]] = {}
        results: list[LegacyBodyMigrationItem] = []
        failed = 0
        for artifact in candidates:
            try:
                if artifact.body_path is None:
                    raise ValueError("migrated artifact has no body path")
                body_path = repo_root / artifact.body_path
                receipt_path = body_path.with_name("reader_extraction.v1.json")
                receipt = ReaderExtractionReceipt.model_validate_json(
                    receipt_path.read_text(encoding="utf-8")
                )
                if receipt.artifact_id != artifact.artifact_id:
                    raise ValueError("migration receipt artifact identity mismatch")
                legacy_manifest_path = repo_root / _relative_artifact_path(
                    receipt.legacy_manifest_path
                )
                legacy_ref = ReportArtifactRef.model_validate_json(
                    legacy_manifest_path.read_text(encoding="utf-8")
                )
                if (
                    legacy_ref.artifact_id != artifact.artifact_id
                    or legacy_ref.reader_mode != "legacy_standalone"
                ):
                    raise ValueError("preserved legacy manifest is incompatible")
                source_path = repo_root / legacy_ref.standalone_path
                if (
                    hashlib.sha256(source_path.read_bytes()).hexdigest()
                    != legacy_ref.workspace_sha256
                ):
                    raise ValueError("preserved standalone report checksum mismatch")
                replacements[artifact.artifact_id] = legacy_ref
                cleanup_paths[artifact.artifact_id] = (
                    body_path,
                    receipt_path,
                    repo_root / artifact.manifest_path,
                )
                results.append(
                    LegacyBodyMigrationItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="eligible",
                        body_sha256=artifact.body_sha256,
                    )
                )
            except (OSError, ValueError) as exc:
                failed += 1
                results.append(
                    LegacyBodyMigrationItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="failed",
                        error=str(exc),
                    )
                )
        rolled_back = 0
        if replacements and not failed:
            updated = tuple(replacements.get(item.artifact_id, item) for item in prior.items)
            _write_index(repo_root, updated)
            rolled_back = len(replacements)
            for artifact_id, paths in cleanup_paths.items():
                cleanup_warning = False
                for path in paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        cleanup_warning = True
                results = [
                    item.model_copy(
                        update={
                            "status": "rolled_back",
                            "warnings": (
                                ("derived_cleanup_incomplete",) if cleanup_warning else ()
                            ),
                        }
                    )
                    if item.artifact_id == artifact_id
                    else item
                    for item in results
                ]
        return LegacyBodyMigrationResult(
            apply=False,
            rollback=True,
            candidates=len(candidates),
            eligible=len(replacements),
            migrated=0,
            rolled_back=rolled_back,
            failed=failed,
            skipped_shared=len(prior.items) - len(candidates),
            items=tuple(results),
        )


def backfill_report_fiscal_periods(
    repo_root: Path,
    *,
    tickers: set[str] | None = None,
    apply: bool = False,
) -> ReportFiscalPeriodBackfillResult:
    """Project exact report periods from checksum-verified immutable wrappers.

    The compact index is a rebuildable projection, while report wrappers and
    manifests are immutable evidence. Only one valid active quarter on the
    governed earnings selector is admissible; absent or conflicting values
    remain unresolved rather than being inferred from a date.
    """

    with hold_report_index(repo_root) if apply else nullcontext():
        normalized_tickers = (
            None if tickers is None else {value.strip().upper() for value in tickers}
        )
        prior = _read_index(repo_root)
        library_artifacts = latest_report_artifacts(
            load_report_artifact_index(repo_root), repo_root=repo_root
        )
        selected = [
            artifact
            for artifact in library_artifacts
            if normalized_tickers is None or artifact.ticker in normalized_tickers
        ]
        replacements: dict[str, ReportArtifactRef] = {}
        results: list[ReportFiscalPeriodBackfillItem] = []
        unresolved_reasons: dict[str, int] = {}
        failed = 0
        unresolved = 0
        skipped_existing = 0
        for artifact in selected:
            if artifact.fiscal_period_label is not None:
                skipped_existing += 1
                results.append(
                    ReportFiscalPeriodBackfillItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="skipped_existing",
                        fiscal_period_label=artifact.fiscal_period_label,
                        evidence=artifact.fiscal_period_evidence,
                    )
                )
                continue
            try:
                source_path = repo_root / artifact.standalone_path
                source_bytes = source_path.read_bytes()
                source_sha256 = hashlib.sha256(source_bytes).hexdigest()
                if source_sha256 != artifact.workspace_sha256:
                    raise ValueError("standalone report checksum mismatch")
                soup = BeautifulSoup(source_bytes, "html.parser")
                observed_values = tuple(
                    sorted(
                        {
                            value
                            for node in soup.select(_EARNINGS_PERIOD_SELECTOR)
                            if (value := str(node.get("data-quarter") or "").strip())
                            and _FISCAL_PERIOD_LABEL.fullmatch(value)
                        }
                    )
                )
                evidence = ReportFiscalPeriodEvidence(
                    source_path=artifact.standalone_path,
                    source_sha256=source_sha256,
                    observed_values=observed_values,
                )
                if len(observed_values) != 1:
                    unresolved += 1
                    reason = (
                        "no_active_earnings_selector"
                        if not observed_values
                        else "conflicting_active_selectors"
                    )
                    unresolved_reasons[reason] = unresolved_reasons.get(reason, 0) + 1
                    results.append(
                        ReportFiscalPeriodBackfillItem(
                            artifact_id=artifact.artifact_id,
                            ticker=artifact.ticker,
                            status="unresolved",
                            evidence=evidence,
                            unresolved_reason=reason,
                        )
                    )
                    continue
                fiscal_period_label = observed_values[0]
                replacements[artifact.artifact_id] = artifact.model_copy(
                    update={
                        "fiscal_period_label": fiscal_period_label,
                        "fiscal_period_evidence": evidence,
                    }
                )
                results.append(
                    ReportFiscalPeriodBackfillItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="eligible",
                        fiscal_period_label=fiscal_period_label,
                        evidence=evidence,
                    )
                )
            except (OSError, UnicodeError, ValueError) as exc:
                failed += 1
                results.append(
                    ReportFiscalPeriodBackfillItem(
                        artifact_id=artifact.artifact_id,
                        ticker=artifact.ticker,
                        status="failed",
                        error=str(exc),
                    )
                )

        applied = 0
        if apply and replacements and not failed:
            updated = tuple(replacements.get(item.artifact_id, item) for item in prior.items)
            _write_index(repo_root, updated)
            applied = len(replacements)
            results = [
                item.model_copy(update={"status": "applied"}) if item.status == "eligible" else item
                for item in results
            ]
        return ReportFiscalPeriodBackfillResult(
            apply=apply,
            candidates=len(selected) - skipped_existing,
            eligible=len(replacements),
            applied=applied,
            skipped_existing=skipped_existing,
            unresolved=unresolved,
            failed=failed,
            unresolved_reasons=unresolved_reasons,
            items=tuple(results),
        )


__all__ = [
    "CoverageRole",
    "LegacyBodyMigrationItem",
    "LegacyBodyMigrationResult",
    "ReaderMode",
    "ReconcileResult",
    "RenderedReportBody",
    "ReportArtifactIndex",
    "ReportArtifactRef",
    "ReportCaptureUnavailable",
    "ReportFiscalPeriodBackfillItem",
    "ReportFiscalPeriodBackfillResult",
    "ReportFiscalPeriodEvidence",
    "ReportInteractionManifest",
    "ReportModelBinding",
    "ReportPublicationApproval",
    "ReportPublicationConflictError",
    "ReportSectionRef",
    "backfill_report_fiscal_periods",
    "build_report_sync_status",
    "candidate_review_path",
    "latest_report_artifacts",
    "latest_report_artifacts_for_tickers",
    "load_report_artifact_index",
    "load_report_candidate_review",
    "load_report_capture_receipt",
    "load_report_file_capture_receipt",
    "migrate_legacy_report_bodies",
    "persist_report_artifact",
    "publish_report_candidate",
    "reconcile_legacy_workspace_reports",
    "report_artifact_index_path",
    "resolve_report_model_binding",
    "rollback_legacy_report_bodies",
    "save_report_candidate_sidecars",
]
