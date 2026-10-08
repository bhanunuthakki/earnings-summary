"""Requested-memo orchestration over the established operational entrypoints.

Each child owns its existing write boundary. The coordinator holds no database
writer lock while a child runs. It preserves a usable brief when an independent
source or downstream evidence gate is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from dcf.cashflow_refresh import PreparedCashflowDcfRequest, PreparedCashflowDcfResult
from dcf.input_evidence import SourceReadContext
from filings.sec_submissions_inventory import SEC_REGISTRATION_FINANCIAL_FORMS
from pipeline.sec_xbrl import resolve_companyfacts_cik
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_collection_target_in_connection,
)
from provenance.companyfacts_statement_continuation import (
    CompanyFactsStatementContinuationRequest,
    CompanyFactsStatementContinuationResult,
)
from provenance.financial_statement_admission import FinancialStatementContextReview
from provenance.immutable_artifact import (
    canonical_text_artifact_sha256,
    publish_bytes_no_clobber,
    publish_text_no_clobber,
)
from provenance.inventory_identity import resolve_sec_inventory_subject
from provenance.sec_filing_xbrl_ingest import FilingXbrlIngestResult
from provenance.sec_native_capture import SecNativeCaptureResult, load_captured_sec_filing_package
from report.artifacts import ReportArtifactRef, validate_report_artifact_path
from research.decision_brief import (
    DecisionBriefReadiness,
    MemoReaderBlock,
    assess_decision_brief,
    memo_reader_blocks,
    parse_memo_context_review,
    persist_decision_brief_readiness,
)
from runtime.job_runtime import JobDeadlineExceededError, run_captured_application_child
from runtime.python_process import managed_python_prefix
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DecisionBriefPreparationRequest(_Closed):
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,31}$")
    code_root: Path
    repo_root: Path
    database: Path
    source_state_root: Path | None = None
    apply: bool = False
    skip_fmp: bool = False
    enable_llm: bool = False
    companyfacts_reviews: tuple[Path, ...] = ()
    context_review: Path | None = None
    processor_installation: Path | None = None
    native_statement_reviews: Path | None = None
    valuation_request: Path | None = None
    valuation_request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    valuation_artifact: Path | None = None

    @field_validator("source_state_root")
    @classmethod
    def explicit_source_authority(cls, root: Path | None) -> Path | None:
        if root is not None:
            SourceReadContext.for_sec_state_root(root)
        return root


class PreparationStage(_Closed):
    stage: str
    status: Literal["completed", "blocked", "planned", "skipped"]
    reason_code: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None


class DecisionBriefPreparationReceipt(_Closed):
    schema_version: Literal["decision_brief_preparation.v1"] = "decision_brief_preparation.v1"
    ticker: str
    requested_at: datetime
    stages: tuple[PreparationStage, ...]
    artifact_manifest: str | None = None
    claim_inventory_path: str | None = None
    readiness: DecisionBriefReadiness | None = None
    status: Literal["planned", "delivered", "delivered_degraded", "blocked"]


class MemoClaimInventory(_Closed):
    schema_version: Literal["memo_claim_inventory.v1"] = "memo_claim_inventory.v1"
    artifact_id: str
    ticker: str
    body_sha256: str
    status: Literal["requires_analyst_review"] = "requires_analyst_review"
    decision_grade: Literal[False] = False
    blocks: tuple[MemoReaderBlock, ...]


def prepare_memo_claim_inventory(
    request: DecisionBriefPreparationRequest, artifact: ReportArtifactRef
) -> Path:
    if artifact.body_path is None:
        raise ValueError("retained memo body missing")
    body = request.repo_root / artifact.body_path
    validate_report_artifact_path(request.repo_root, body)
    raw = body.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact.body_sha256:
        raise ValueError("retained memo body changed")
    inventory = MemoClaimInventory(
        artifact_id=artifact.artifact_id,
        ticker=artifact.ticker,
        body_sha256=str(artifact.body_sha256),
        blocks=memo_reader_blocks(raw.decode("utf-8")),
    )
    payload = inventory.model_dump_json(indent=2) + "\n"
    path = (
        request.repo_root
        / "output/research"
        / artifact.ticker
        / "artifacts"
        / artifact.artifact_id
        / "review_inputs"
        / f"{canonical_text_artifact_sha256(payload)}.json"
    )
    validate_report_artifact_path(request.repo_root, path)
    publish_text_no_clobber(path.resolve(), payload)
    return path


CommandRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]


def run_command(command: tuple[str, ...], state_root: Path) -> subprocess.CompletedProcess[str]:
    return run_captured_application_child(list(command), cwd=state_root, timeout_seconds=900)


class _InventoryReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: Literal["apply", "dry_run"]
    ticker: str
    issuer_id: str
    complete: bool
    snapshot_id: str | None


class _PreflightReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["filing-xbrl-installation-preflight/v1"]
    status: Literal["ready_for_native_qualification", "unavailable", "rejected"]
    reason_code: str


class _OnboardingReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")
    event: Literal["onboard_sec_ingestion"]
    ticker: str
    run_id: str
    status: Literal["ok", "failed", "skipped"]
    rows_processed: int = Field(ge=0)


def _payload(
    output: str, script: str, request: DecisionBriefPreparationRequest, args: list[str]
) -> dict[str, object]:
    """Validate the existing child's receipt, including its issuer scope."""
    if script == "onboard_ticker.py":
        # This existing command also writes progress lines. Only its typed SEC
        # event or native single-flight suppression is a completion receipt.
        for line in reversed(output.splitlines()):
            try:
                value: object = json.loads(line)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            item = cast("dict[str, object]", value)
            if item.get("event") == "onboard_sec_ingestion":
                receipt = _OnboardingReceipt.model_validate(item)
                if receipt.ticker != request.ticker or receipt.status == "failed":
                    raise ValueError("onboarding receipt mismatch")
                return receipt.model_dump(mode="json")
            if item.get("status") in {"already_running", "already_done"} and all(
                isinstance(item.get(key), str) and item[key]
                for key in ("pipeline_key", "attempt_id")
            ):
                return item
        raise ValueError("onboarding receipt missing")
    value = json.loads(output)
    if script == "build_artifacts.py":
        items = TypeAdapter(list[dict[str, object]]).validate_python(value)
        selected = [item for item in items if item.get("ticker") == request.ticker]
        if len(selected) != 1 or not isinstance(selected[0].get("report_manifest"), str):
            raise ValueError("artifact receipt missing or ambiguous")
        return {"items": selected}
    if script == "sync_sec_filing_inventory.py":
        receipt = _InventoryReceipt.model_validate(value)
        if (
            receipt.ticker != request.ticker
            or receipt.mode != "apply"
            or not receipt.complete
            or not receipt.snapshot_id
        ):
            raise ValueError("inventory receipt incomplete")
        return receipt.model_dump(mode="json")
    if script == "capture_expected_sec_documents.py":
        if not isinstance(value, dict):
            raise ValueError("capture receipt missing")
        item = cast("dict[str, object]", value).copy()
        path = Path(str(item.pop("items_path")))
        path.resolve().relative_to(
            (
                request.repo_root
                / ".tmp/sec_native_capture"
                / args[args.index("--task-id") + 1]
                / "results"
            ).resolve()
        )
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise ValueError("capture item bytes changed")
        item_count = item.pop("item_count")
        item["items"] = json.loads(raw)
        receipt = SecNativeCaptureResult.model_validate(item)
        if (
            receipt.mode != "apply"
            or item_count != len(receipt.items)
            or receipt.task_id != args[args.index("--task-id") + 1]
        ):
            raise ValueError("capture receipt mismatch")
        if receipt.inventory_keys != (request_inventory_key(request),):
            raise ValueError("capture inventory scope mismatch")
        return receipt.model_dump(mode="json")
    if script == "ingest_sec_filing_xbrl.py":
        receipt = (
            _PreflightReceipt.model_validate(value)
            if "--preflight" in args
            else FilingXbrlIngestResult.model_validate(value)
        )
        if isinstance(receipt, FilingXbrlIngestResult) and (
            receipt.mode != "apply"
            or receipt.accession_number != args[args.index("--accession") + 1]
        ):
            raise ValueError("native accession receipt mismatch")
        if (
            isinstance(receipt, _PreflightReceipt)
            and receipt.status != "ready_for_native_qualification"
        ):
            raise ValueError("native processor not ready")
        return receipt.model_dump(mode="json")
    if script == "continue_companyfacts_statements.py":
        receipt = CompanyFactsStatementContinuationResult.model_validate(value)
        if receipt.mode != "apply":
            raise ValueError("statement receipt mode mismatch")
        return receipt.model_dump(mode="json")
    if script == "prepare_cashflow_dcf.py":
        receipt = PreparedCashflowDcfResult.model_validate(value)
        if receipt.mode != "apply" or receipt.ticker != request.ticker:
            raise ValueError("valuation receipt mismatch")
        return receipt.model_dump(mode="json")
    raise ValueError("unsupported child receipt")


def request_inventory_key(request: DecisionBriefPreparationRequest) -> str:
    conn = connect_sqlite(request.database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        now = datetime.now(UTC)
        cik = resolve_companyfacts_cik(conn, request.ticker, knowledge_at=now)
        subject = resolve_sec_inventory_subject(
            conn, ticker=request.ticker, cik=cik, knowledge_at=now
        )
        return f"{subject.issuer_id}:sec-submissions"
    finally:
        conn.close()


def _context_scope(
    conn: sqlite3.Connection, review: FinancialStatementContextReview, issuer_id: str
) -> None:
    row = conn.execute(
        "SELECT issuer_id FROM v_evidence_document_versions_canonical WHERE document_version_id=?",
        (review.document_version_id,),
    ).fetchone()
    if review.issuer_id != issuer_id or row is None or str(row[0]) != issuer_id:
        raise ValueError("statement source issuer mismatch")


def _review_scope(
    conn: sqlite3.Connection, request: DecisionBriefPreparationRequest, issuer_id: str
) -> tuple[tuple[str, ...], tuple[FinancialStatementContextReview, ...]]:
    payloads: list[str] = []
    reviews: tuple[FinancialStatementContextReview, ...] = ()
    for path in request.companyfacts_reviews:
        payload = path.read_bytes().decode("utf-8")
        review = CompanyFactsStatementContinuationRequest.model_validate_json(payload)
        payloads.append(payload)
        for item in review.facts:
            _context_scope(conn, item.context, issuer_id)
    if request.native_statement_reviews is not None:
        reviews = TypeAdapter(tuple[FinancialStatementContextReview, ...]).validate_json(
            request.native_statement_reviews.read_bytes()
        )
        for item in reviews:
            _context_scope(conn, item, issuer_id)
    if request.valuation_request is not None:
        raw = request.valuation_request.read_bytes()
        valuation = PreparedCashflowDcfRequest.model_validate_json(raw)
        if valuation.model_inputs.ticker != request.ticker:
            raise ValueError("valuation ticker mismatch")
        if hashlib.sha256(raw).hexdigest() != request.valuation_request_sha256:
            raise ValueError("valuation commitment mismatch")
    return tuple(payloads), reviews


def prepare_native_review_file(
    request: DecisionBriefPreparationRequest,
    reviews: tuple[FinancialStatementContextReview, ...],
    document_id: str,
) -> Path | None:
    selected = tuple(item for item in reviews if item.document_version_id == document_id)
    if not selected:
        return None
    payload = TypeAdapter(tuple[FinancialStatementContextReview, ...]).dump_json(selected).decode()
    path = (
        request.repo_root
        / ".tmp/decision_brief"
        / request.ticker
        / "native_reviews"
        / f"{canonical_text_artifact_sha256(payload)}.json"
    )
    publish_text_no_clobber(path, payload)
    return path


def _prepare_decision_brief(
    request: DecisionBriefPreparationRequest,
    *,
    runner: CommandRunner = run_command,
) -> DecisionBriefPreparationReceipt:
    now = datetime.now(UTC)
    stages: list[PreparationStage] = []
    prefix = managed_python_prefix(request.code_root)

    def execute(stage: str, script: str, args: list[str]) -> dict[str, object]:
        command = tuple([*prefix, str(request.code_root / "execution" / script), *args])
        if not request.apply:
            stages.append(
                PreparationStage(
                    stage=stage,
                    status="planned",
                    reason_code="explicit_apply_required",
                    command=command,
                )
            )
            return {}
        try:
            result = runner(command, request.repo_root)
        except (subprocess.TimeoutExpired, JobDeadlineExceededError):
            stages.append(
                PreparationStage(
                    stage=stage,
                    status="blocked",
                    reason_code="stage_deadline_exceeded",
                    command=command,
                )
            )
            return {}
        except ValueError:
            stages.append(
                PreparationStage(
                    stage=stage,
                    status="blocked",
                    reason_code="stage_output_contract_failed",
                    command=command,
                )
            )
            return {}
        except OSError:
            stages.append(
                PreparationStage(
                    stage=stage,
                    status="blocked",
                    reason_code="stage_process_unavailable",
                    command=command,
                )
            )
            return {}
        if result.returncode == 0:
            try:
                output = _payload(result.stdout, script, request, args)
            except (ValueError, KeyError, OSError):
                stages.append(
                    PreparationStage(
                        stage=stage,
                        status="blocked",
                        reason_code="stage_output_contract_failed",
                        command=command,
                        exit_code=0,
                    )
                )
                return {}
        else:
            try:
                value: object = json.loads(result.stdout)
                output = cast("dict[str, object]", value) if isinstance(value, dict) else {}
            except ValueError:
                output = {}
        suppression = output.get("status") in {"already_running", "already_done"}
        raw_reason = output.get("reason_code") if not suppression else output.get("status")
        # Only a bounded machine code is accepted from child output. Raw
        # provider errors, credentials and source payloads are not journaled.
        reason = (
            str(raw_reason)
            if isinstance(raw_reason, str)
            and raw_reason.replace("_", "").isalnum()
            and len(raw_reason) <= 128
            else "stage_completed"
            if result.returncode == 0
            else "stage_failed"
        )
        stages.append(
            PreparationStage(
                stage=stage,
                status="skipped"
                if suppression
                else "completed"
                if result.returncode == 0
                else "blocked",
                reason_code=reason,
                command=command,
                exit_code=result.returncode,
            )
        )
        return output

    source_args = [
        "--ticker",
        request.ticker,
        "--db",
        str(request.database),
        "--project-root",
        str(request.repo_root),
    ]
    if request.skip_fmp:
        source_args.append("--skip-fmp")
    if not request.enable_llm:
        source_args.append("--skip-llm")
    execute("source_onboarding", "onboard_ticker.py", source_args)
    if not request.apply:
        execute("native_preflight", "ingest_sec_filing_xbrl.py", ["--preflight"])
        execute(
            "full_memo",
            "build_artifacts.py",
            [
                "--ticker",
                request.ticker,
                "--db-path",
                str(request.database),
                "--repo-root",
                str(request.repo_root),
                "--flavor",
                "evaluation",
            ],
        )
        return DecisionBriefPreparationReceipt(
            ticker=request.ticker, requested_at=now, stages=tuple(stages), status="planned"
        )

    conn = connect_sqlite(request.database, role=SQLiteConnectionRole.READ_ONLY)
    inventory_key: str | None = None
    cik: str | None = None
    revision = 1
    fresh_inventory = False
    accessions: tuple[str, ...] = ()
    reviews_allowed = False
    companyfacts_payloads: tuple[str, ...] = ()
    native_reviews: tuple[FinancialStatementContextReview, ...] = ()
    try:
        policy = authorize_collection_target_in_connection(
            conn,
            request.ticker,
            requested=True,
            source=CollectionSource.SEC,
            artifact_kind=ArtifactKind.FILING_PACKAGE,
        )
        if not policy.allowed:
            stages.append(
                PreparationStage(
                    stage="sec_identity", status="blocked", reason_code=policy.status.value
                )
            )
        else:
            cik = resolve_companyfacts_cik(conn, request.ticker, knowledge_at=now)
            subject = resolve_sec_inventory_subject(
                conn, ticker=request.ticker, cik=cik, knowledge_at=now
            )
            inventory_key = f"{subject.issuer_id}:sec-submissions"
            try:
                companyfacts_payloads, native_reviews = _review_scope(
                    conn, request, subject.issuer_id
                )
                reviews_allowed = True
            except (ValueError, OSError):
                stages.append(
                    PreparationStage(
                        stage="review_scope",
                        status="blocked",
                        reason_code="requested_issuer_review_scope_invalid",
                    )
                )
            latest = conn.execute(
                "SELECT revision,outcome,authoritative,completed_at FROM source_inventory_snapshots WHERE inventory_key=? ORDER BY revision DESC LIMIT 1",
                (inventory_key,),
            ).fetchone()
            if latest is not None:
                revision = int(latest[0]) + 1
                completed = datetime.fromisoformat(str(latest[3]))
                completed = (
                    completed.replace(tzinfo=UTC)
                    if completed.tzinfo is None
                    else completed.astimezone(UTC)
                )
                fresh_inventory = (
                    str(latest[1]) == "succeeded"
                    and bool(latest[2])
                    and timedelta(0) <= now - completed <= timedelta(hours=24)
                )
    except (ValueError, RuntimeError, sqlite3.Error):
        inventory_key = None
        stages.append(
            PreparationStage(
                stage="sec_identity",
                status="blocked",
                reason_code="canonical_sec_identity_unavailable",
            )
        )
    finally:
        conn.close()
    if inventory_key is not None and cik is not None:
        if not fresh_inventory:
            execute(
                "sec_inventory",
                "sync_sec_filing_inventory.py",
                [
                    "--ticker",
                    request.ticker,
                    "--cik",
                    cik,
                    "--revision",
                    str(revision),
                    "--db",
                    str(request.database),
                    "--blob-root",
                    str(request.repo_root / "data/evidence/blobs"),
                    "--package-checkpoint-root",
                    str(request.repo_root / ".tmp/sec_filing_package_inventory"),
                    "--contract-failure-root",
                    str(request.repo_root / ".tmp/sec_inventory_contract_failures"),
                    "--apply",
                ],
            )
        else:
            stages.append(
                PreparationStage(
                    stage="sec_inventory",
                    status="skipped",
                    reason_code="current_authoritative_inventory_retained",
                )
            )
        capture_args = [
            "--db",
            str(request.database),
            "--inventory-key",
            inventory_key,
            "--checkpoint-root",
            str(request.repo_root / ".tmp/sec_native_capture"),
            "--blob-root",
            str(request.repo_root / "data/evidence/blobs"),
            "--task-id",
            f"memo-{request.ticker}-{now.date().isoformat()}",
            "--batch-size",
            "250",
            "--apply",
        ]
        seen_documents: set[str] = set()
        for batch in range(8):
            capture = execute(
                "sec_capture" if batch == 0 else f"sec_capture:{batch + 1}",
                "capture_expected_sec_documents.py",
                capture_args,
            )
            if not capture:
                break
            if capture["deferred"] or capture["failed"]:
                stages.append(
                    PreparationStage(
                        stage="sec_capture_closure",
                        status="blocked",
                        reason_code="sec_capture_pending_or_failed",
                    )
                )
                break
            if not capture["has_more"]:
                break
            receipt = SecNativeCaptureResult.model_validate(capture)
            documents = {
                item.document_version_id for item in receipt.items if item.document_version_id
            }
            if receipt.records_created == 0 or not documents - seen_documents:
                stages.append(
                    PreparationStage(
                        stage="sec_capture_closure",
                        status="blocked",
                        reason_code="sec_capture_no_progress",
                    )
                )
                break
            seen_documents.update(documents)
        else:
            stages.append(
                PreparationStage(
                    stage="sec_capture_closure",
                    status="blocked",
                    reason_code="sec_capture_batch_limit_reached",
                )
            )
        conn = connect_sqlite(request.database, role=SQLiteConnectionRole.READ_ONLY)
        try:
            accessions = tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT accession_number FROM expected_documents WHERE snapshot_id=(SELECT snapshot_id FROM source_inventory_snapshots WHERE inventory_key=? ORDER BY revision DESC LIMIT 1) AND form_type IN (SELECT value FROM json_each(?)) AND accession_number IS NOT NULL ORDER BY accession_number",
                    (
                        inventory_key,
                        json.dumps(
                            [
                                "10-K",
                                "10-K/A",
                                "10-Q",
                                "10-Q/A",
                                "20-F",
                                "20-F/A",
                                "40-F",
                                "40-F/A",
                                *sorted(SEC_REGISTRATION_FINANCIAL_FORMS),
                            ]
                        ),
                    ),
                )
            )
        finally:
            conn.close()
    preflight_args = ["--preflight"]
    if request.processor_installation:
        preflight_args.extend(["--installation", str(request.processor_installation)])
    preflight = execute("native_preflight", "ingest_sec_filing_xbrl.py", preflight_args)
    if preflight.get("status") == "ready_for_native_qualification" and inventory_key and cik:
        for accession in accessions:
            native_args = [
                "--db",
                str(request.database),
                "--inventory-key",
                inventory_key,
                "--cik",
                cik,
                "--accession",
                accession,
                "--apply",
            ]
            if request.processor_installation:
                native_args.extend(["--installation", str(request.processor_installation)])
            # Reviews are accession scoped by the native command. A request
            # must contain reviews for this exact source, never global scope.
            conn = connect_sqlite(request.database, role=SQLiteConnectionRole.READ_ONLY)
            try:
                members = load_captured_sec_filing_package(
                    conn, inventory_key=inventory_key, accession_number=accession
                )
            except (ValueError, RuntimeError, sqlite3.Error):
                stages.append(
                    PreparationStage(
                        stage=f"native_extract:{accession}",
                        status="blocked",
                        reason_code="captured_accession_not_complete",
                    )
                )
                continue
            finally:
                conn.close()
            if reviews_allowed:
                path = prepare_native_review_file(
                    request, native_reviews, members[0].document_version_id
                )
                if path is not None:
                    native_args.extend(["--statement-context-reviews", str(path)])
            execute(f"native_extract:{accession}", "ingest_sec_filing_xbrl.py", native_args)
    for payload in companyfacts_payloads if reviews_allowed else ():
        reviews = (
            request.repo_root
            / ".tmp/decision_brief"
            / request.ticker
            / "companyfacts_reviews"
            / f"{hashlib.sha256(payload.encode()).hexdigest()}.json"
        )
        publish_bytes_no_clobber(reviews, payload.encode())
        execute(
            "statement_admission",
            "continue_companyfacts_statements.py",
            [
                "--db",
                str(request.database),
                "--repo-root",
                str(request.repo_root),
                "--request",
                str(reviews),
                "--apply",
            ],
        )
    if request.valuation_request is not None and reviews_allowed:
        if request.valuation_request_sha256 is None or request.valuation_artifact is None:
            stages.append(
                PreparationStage(
                    stage="valuation",
                    status="blocked",
                    reason_code="prepared_valuation_commitment_missing",
                )
            )
        else:
            execute(
                "valuation",
                "prepare_cashflow_dcf.py",
                [
                    "--db",
                    str(request.database),
                    "--repo-root",
                    str(request.repo_root),
                    *(
                        ["--state-root", str(request.source_state_root)]
                        if request.source_state_root is not None
                        else []
                    ),
                    "--request",
                    str(request.valuation_request),
                    "--request-sha256",
                    request.valuation_request_sha256,
                    "--artifact",
                    str(request.valuation_artifact),
                    "--apply",
                ],
            )
    else:
        stages.append(
            PreparationStage(
                stage="valuation", status="skipped", reason_code="no_new_prepared_analyst_valuation"
            )
        )
    build_args = [
        "--ticker",
        request.ticker,
        "--db-path",
        str(request.database),
        "--repo-root",
        str(request.repo_root),
        "--flavor",
        "evaluation",
    ]
    if request.enable_llm:
        build_args.append("--enable-llm")
    # Rendering remains independent of an absent thesis or an unavailable
    # native processor. The receipt will retain every unresolved gate.
    build_output = execute("full_memo", "build_artifacts.py", build_args)
    # Use the exact child's manifest reference, including an idempotently
    # retained artifact. Never guess a date or select an unrelated old report.
    artifact: ReportArtifactRef | None = None
    items = build_output.get("items")
    try:
        if stages[-1].status == "completed" and isinstance(items, list):
            checked_items = cast("list[dict[str, object]]", items)
            manifest = Path(str(checked_items[0]["report_manifest"]))
            validate_report_artifact_path(request.repo_root, manifest)
            candidate = ReportArtifactRef.model_validate_json(manifest.read_bytes())
            if candidate.ticker != request.ticker:
                raise ValueError("returned artifact ticker mismatch")
            if (request.repo_root / candidate.manifest_path).resolve() != manifest.resolve():
                raise ValueError("returned artifact manifest mismatch")
            artifact = candidate
    except (ValueError, OSError, KeyError):
        stages.append(
            PreparationStage(
                stage="artifact_reconstruction",
                status="blocked",
                reason_code="returned_artifact_invalid",
            )
        )
    readiness = None
    claim_inventory_path = None
    if artifact is not None:
        try:
            claim_inventory_path = (
                prepare_memo_claim_inventory(request, artifact)
                .relative_to(request.repo_root)
                .as_posix()
            )
        except (ValueError, OSError):
            stages.append(
                PreparationStage(
                    stage="memo_claim_inventory",
                    status="blocked",
                    reason_code="memo_claim_inventory_unavailable",
                )
            )
        context = None
        if request.context_review is not None:
            try:
                context = parse_memo_context_review(request.context_review.read_bytes())
            except (ValueError, OSError):
                stages.append(
                    PreparationStage(
                        stage="memo_context_review",
                        status="blocked",
                        reason_code="memo_context_review_invalid",
                    )
                )
        try:
            conn = connect_sqlite(request.database, role=SQLiteConnectionRole.READ_ONLY)
            try:
                readiness = assess_decision_brief(
                    conn,
                    repo_root=request.repo_root,
                    artifact=artifact,
                    as_of=datetime.now(UTC),
                    context_review=context,
                    source_context=SourceReadContext.for_sec_state_root(request.source_state_root)
                    if request.source_state_root is not None
                    else None,
                )
            finally:
                conn.close()
            persist_decision_brief_readiness(request.repo_root, readiness)
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            readiness = None
            stages.append(
                PreparationStage(
                    stage="memo_readiness",
                    status="blocked",
                    reason_code="memo_readiness_reconstruction_failed",
                )
            )
    return DecisionBriefPreparationReceipt(
        ticker=request.ticker,
        requested_at=now,
        stages=tuple(stages),
        artifact_manifest=artifact.manifest_path if artifact else None,
        claim_inventory_path=claim_inventory_path,
        readiness=readiness,
        status="delivered"
        if readiness
        and readiness.decision_grade
        and not any(stage.status == "blocked" for stage in stages)
        else "delivered_degraded"
        if artifact
        else "blocked",
    )


def prepare_decision_brief(
    request: DecisionBriefPreparationRequest,
    *,
    runner: CommandRunner = run_command,
) -> DecisionBriefPreparationReceipt:
    """Persist a recovery receipt even when a source or file boundary fails."""
    try:
        receipt = _prepare_decision_brief(request, runner=runner)
    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        receipt = DecisionBriefPreparationReceipt(
            ticker=request.ticker,
            requested_at=datetime.now(UTC),
            status="blocked",
            stages=(
                PreparationStage(
                    stage="preparation", status="blocked", reason_code="preparation_boundary_failed"
                ),
            ),
        )
    if request.apply:
        payload = receipt.model_dump_json(indent=2) + "\n"
        digest = canonical_text_artifact_sha256(payload)
        publish_text_no_clobber(
            request.repo_root
            / ".tmp/decision_brief"
            / request.ticker
            / "requests"
            / f"{digest}.json",
            payload,
        )
    return receipt
