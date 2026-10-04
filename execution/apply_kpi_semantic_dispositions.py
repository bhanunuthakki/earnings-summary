"""Dry-run or apply an independently authorized KPI disposition manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))

from apply_kpi_semantic_refresh import (
    RepairBlockedError as DispositionBlockedError,
)
from apply_kpi_semantic_refresh import (
    emit_repair_phase_diagnostics,
    validate_repair_review_preconditions,
)
from apply_kpi_semantic_refresh import (
    repair_database_authority as _disposition_database,
)
from apply_kpi_semantic_refresh import (
    repair_lock_root as _lock_root,
)
from apply_kpi_semantic_refresh import (
    schema_revision as _schema_revision,
)
from apply_kpi_semantic_refresh import (
    validate_external_repair_evidence as validate_disposition_external_evidence,
)
from backup_restore_readiness_receipt import (
    BackupRestoreReadinessReceipt,
    validate_receipt_for_source,
)
from fetch_windows_review_bundle import (
    WindowsReviewPins,
    identity_sha256,
)

from operations.kpi_repair_receipts import (
    KpiDispositionAttemptReceipt,
    KpiDispositionJudgeReceipt,
    judge_qualification_is_current,
    repair_executor_code_sha256,
    seal_disposition_attempt,
)
from operations.review_bundle import (
    OperationsReviewBundle,
    database_lineage_identity,
    review_code_identity,
)
from pipeline.kpi_semantic_dispositions import (
    KpiSemanticDispositionManifest,
    KpiSemanticDispositionResult,
    apply_kpi_semantic_disposition_manifest,
    validate_legacy_disposition_replay,
)
from pipeline.queries import open_db
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_WINDOWS_STATE_ROOT = Path(
    os.environ.get("EARNINGS_SUMMARY_STATE_ROOT")
    or Path.home() / ".gemini" / "antigravity" / "scratch" / "earnings-summary"
)

OPERATIONS_GOVERNANCE_DISPOSITION = "no_surface_change_internal_append_only_kpi_dispositions"
OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT = (
    "src/operations/registry.py:OperationsRegistry",
    "src/pipeline/operations_panel.py:visible_surface_dispositions",
    "src/operations/review_bundle.py:ReviewKpiCensus",
)
_SHA256 = r"^[0-9a-f]{64}$"


class KpiDispositionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["passed", "applied", "replayed", "blocked", "failed"]
    receipt_path: str
    receipt_sha256: str = Field(pattern=_SHA256)
    blocker_codes: tuple[str, ...]


def _write_content_addressed(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = payload.rstrip() + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise DispositionBlockedError("content_addressed_receipt_conflict")
        return
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _write_latest(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(payload.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def _artifact_sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return hashlib.sha256(b"unreadable-artifact").hexdigest()


def _validate_apply_authority(
    *, db_path: Path, repo_root: Path, receipt_root: Path, review_bundle: OperationsReviewBundle
) -> None:
    if sys.platform != "win32":
        raise DispositionBlockedError("apply_requires_windows_authority")
    canonical_db = CANONICAL_WINDOWS_STATE_ROOT / "data" / "portfolio.db"
    canonical_receipts = CANONICAL_WINDOWS_STATE_ROOT / "data" / "operations" / "kpi_dispositions"
    if db_path.resolve() != canonical_db.resolve():
        raise DispositionBlockedError("apply_database_is_not_canonical_windows_authority")
    if repo_root.resolve() != PROJECT_ROOT.resolve():
        raise DispositionBlockedError("apply_report_configuration_root_is_not_running_code")
    if receipt_root.resolve() != canonical_receipts.resolve():
        raise DispositionBlockedError("apply_receipt_root_is_not_canonical_operations_surface")
    if (
        identity_sha256(review_code_identity(PROJECT_ROOT))
        != review_bundle.identity.code_instance_sha256
    ):
        raise DispositionBlockedError("apply_code_identity_mismatch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--review-bundle", type=Path, required=True)
    parser.add_argument("--trusted-review-pins", type=Path, required=True)
    parser.add_argument("--backup-restore-receipt", type=Path, required=True)
    parser.add_argument("--judge-receipt", type=Path)
    parser.add_argument("--dry-run-receipt", type=Path)
    parser.add_argument("--approved-manifest-sha256")
    parser.add_argument("--max-review-age-seconds", type=int, default=1200)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def _load_authority_artifacts(
    args: argparse.Namespace,
) -> tuple[
    KpiSemanticDispositionManifest,
    OperationsReviewBundle,
    WindowsReviewPins,
    BackupRestoreReadinessReceipt,
]:
    return (
        KpiSemanticDispositionManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        ),
        OperationsReviewBundle.model_validate_json(args.review_bundle.read_text(encoding="utf-8")),
        WindowsReviewPins.model_validate_json(args.trusted_review_pins.read_text(encoding="utf-8")),
        BackupRestoreReadinessReceipt.model_validate_json(
            args.backup_restore_receipt.read_text(encoding="utf-8")
        ),
    )


def _failed_input_receipt(
    *,
    args: argparse.Namespace,
    started: datetime,
    attempt_id: str,
    mode: str,
    code_sha: str,
    error: Exception,
) -> KpiDispositionAttemptReceipt:
    return seal_disposition_attempt(
        attempt_id=attempt_id,
        logical_idempotency_key_sha256=_artifact_sha(args.manifest),
        manifest_sha256=_artifact_sha(args.manifest),
        review_bundle_sha256=_artifact_sha(args.review_bundle),
        backup_restore_evidence_id=_artifact_sha(args.backup_restore_receipt),
        executor_code_sha256=code_sha,
        mode=mode,
        state="failed",
        started_at=started,
        completed_at=datetime.now(UTC),
        validated_fact_dispositions=0,
        validated_reference_dispositions=0,
        inserted_context_rows=0,
        replayed_context_rows=0,
        inserted_reference_rows=0,
        replayed_reference_rows=0,
        blocker_codes=(f"invalid_input_{type(error).__name__}",),
    )


def publish_disposition_receipt(
    *, receipt_root: Path, receipt: KpiDispositionAttemptReceipt
) -> KpiDispositionSummary:
    receipt_path = receipt_root / "attempts" / f"{receipt.attempt_id}.json"
    _write_content_addressed(receipt_path, receipt.model_dump_json(indent=2))
    _write_latest(receipt_root / "latest.json", receipt.model_dump_json(indent=2))
    return KpiDispositionSummary(
        attempt_id=receipt.attempt_id,
        state=receipt.state,
        receipt_path=str(receipt_path),
        receipt_sha256=receipt.content_sha256,
        blocker_codes=receipt.blocker_codes,
    )


def judge_authorizes(
    *,
    dry_run: KpiDispositionAttemptReceipt,
    judge: KpiDispositionJudgeReceipt,
    manifest_sha: str,
    manifest: KpiSemanticDispositionManifest,
    executor_code_sha: str,
    now: datetime | None = None,
) -> bool:
    return bool(
        dry_run.mode == "dry_run"
        and dry_run.state == "passed"
        and dry_run.manifest_sha256 == manifest_sha
        and dry_run.review_bundle_sha256 == manifest.review_bundle_sha256
        and dry_run.backup_restore_evidence_id == manifest.backup_restore_evidence_id
        and dry_run.executor_code_sha256 == executor_code_sha
        and judge.dry_run_receipt_sha256 == dry_run.content_sha256
        and judge.verdict == "PASS"
        and judge.evidence_tier == "J3"
        and judge.manifest_sha256 == manifest_sha
        and judge.review_bundle_sha256 == manifest.review_bundle_sha256
        and judge.executor_code_sha256 == executor_code_sha
        and judge.purpose == "kpi_semantic_disposition"
        and judge_qualification_is_current(judge, now=now if now is not None else datetime.now(UTC))
    )


def _database_identity_matches(
    conn: sqlite3.Connection,
    *,
    manifest: KpiSemanticDispositionManifest,
    review_bundle: OperationsReviewBundle,
) -> bool:
    database_sha = hashlib.sha256(database_lineage_identity(conn).encode("utf-8")).hexdigest()
    return bool(
        database_sha == manifest.expected_database_instance_sha256
        and database_sha == review_bundle.identity.database_instance_sha256
    )


def recover_committed_disposition(
    *,
    db_path: Path,
    manifest: KpiSemanticDispositionManifest,
    manifest_sha: str,
    logical_key_sha: str,
    executor_code_sha: str,
    review_bundle: OperationsReviewBundle,
) -> KpiSemanticDispositionResult | None:
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        if _schema_revision(conn) != manifest.expected_schema_revision:
            raise DispositionBlockedError("database_schema_revision_changed")
        if not _database_identity_matches(conn, manifest=manifest, review_bundle=review_bundle):
            raise DispositionBlockedError("database_lineage_identity_changed")
        row = conn.execute(
            "SELECT logical_idempotency_key_sha256,review_bundle_sha256,"
            "backup_restore_evidence_id,executor_code_sha256,fact_disposition_count,"
            "reference_disposition_count,inserted_context_rows,inserted_reference_rows "
            "FROM kpi_semantic_disposition_commits WHERE manifest_sha256=?",
            (manifest_sha,),
        ).fetchone()
        if row is not None and manifest.schema_version == "kpi_semantic_dispositions.v2":
            try:
                validate_legacy_disposition_replay(conn, manifest=manifest)
            except ValueError as exc:
                raise DispositionBlockedError("committed_legacy_disposition_head_changed") from exc
    finally:
        conn.close()
    if row is None:
        return None
    expected = (
        logical_key_sha,
        manifest.review_bundle_sha256,
        manifest.backup_restore_evidence_id,
        executor_code_sha,
        len(manifest.fact_dispositions),
        len(manifest.report_reference_dispositions),
    )
    if tuple(row[:6]) != expected:
        raise DispositionBlockedError("committed_disposition_binding_mismatch")
    return KpiSemanticDispositionResult(
        inserted_context_rows=0,
        replayed_context_rows=len(manifest.fact_dispositions),
        inserted_reference_rows=0,
        replayed_reference_rows=len(manifest.report_reference_dispositions),
    )


def _record_disposition_commit(
    conn: sqlite3.Connection,
    *,
    manifest: KpiSemanticDispositionManifest,
    manifest_sha: str,
    logical_key_sha: str,
    executor_code_sha: str,
    result: KpiSemanticDispositionResult,
) -> None:
    conn.execute(
        "INSERT INTO kpi_semantic_disposition_commits "
        "(manifest_sha256,logical_idempotency_key_sha256,review_bundle_sha256,"
        "backup_restore_evidence_id,executor_code_sha256,fact_disposition_count,"
        "reference_disposition_count,inserted_context_rows,inserted_reference_rows,"
        "committed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            manifest_sha,
            logical_key_sha,
            manifest.review_bundle_sha256,
            manifest.backup_restore_evidence_id,
            executor_code_sha,
            len(manifest.fact_dispositions),
            len(manifest.report_reference_dispositions),
            result.inserted_context_rows,
            result.inserted_reference_rows,
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        ),
    )


def execute_disposition_transaction(
    *,
    db_path: Path,
    repo_root: Path,
    manifest: KpiSemanticDispositionManifest,
    manifest_sha: str,
    logical_key_sha: str,
    executor_code_sha: str,
    review_bundle: OperationsReviewBundle,
    apply: bool,
    validate_current_authority: Callable[[], None],
    max_review_age: timedelta = timedelta(minutes=20),
) -> KpiSemanticDispositionResult:
    transaction_started = perf_counter()
    conn = open_db(db_path)
    try:
        if _schema_revision(conn) != manifest.expected_schema_revision:
            raise DispositionBlockedError("database_schema_revision_changed")
        if not _database_identity_matches(conn, manifest=manifest, review_bundle=review_bundle):
            raise DispositionBlockedError("database_lineage_identity_changed")
        validate_current_authority()
        conn.execute("BEGIN IMMEDIATE")
        result = apply_kpi_semantic_disposition_manifest(
            conn, repo_root=repo_root, manifest=manifest
        )
        if apply:
            _record_disposition_commit(
                conn,
                manifest=manifest,
                manifest_sha=manifest_sha,
                logical_key_sha=logical_key_sha,
                executor_code_sha=executor_code_sha,
                result=result,
            )
        checks_started = perf_counter()
        try:
            if tuple(str(row[0]) for row in conn.execute("PRAGMA integrity_check")) != (
                "ok",
            ) or tuple(conn.execute("PRAGMA foreign_key_check")):
                raise DispositionBlockedError("post_disposition_database_invalid")
            validate_current_authority()
        finally:
            emit_repair_phase_diagnostics(
                phase="post_write_checks",
                started_monotonic=checks_started,
                review_bundle=review_bundle,
                max_review_age=max_review_age,
                now=datetime.now(UTC),
            )
        # Diagnostics can block on their output sink. Fence the actual
        # transaction boundary after that work, not before it.
        validate_current_authority()
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        emit_repair_phase_diagnostics(
            phase="transaction_validation",
            started_monotonic=transaction_started,
            review_bundle=review_bundle,
            max_review_age=max_review_age,
            now=datetime.now(UTC),
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = datetime.now(UTC)
    attempt_id = uuid4().hex
    mode: Literal["dry_run", "apply"] = "apply" if args.apply else "dry_run"
    receipt_root = args.receipt_root.resolve()
    executor_code_sha = repair_executor_code_sha256(PROJECT_ROOT)
    try:
        manifest, review_bundle, trusted_pins, backup = _load_authority_artifacts(args)
    except Exception as exc:
        receipt = _failed_input_receipt(
            args=args,
            started=started,
            attempt_id=attempt_id,
            mode=mode,
            code_sha=executor_code_sha,
            error=exc,
        )
        print(
            publish_disposition_receipt(
                receipt_root=receipt_root, receipt=receipt
            ).model_dump_json()
        )
        return 2

    manifest_sha = manifest.content_sha256()
    logical_key_sha = hashlib.sha256(manifest.logical_idempotency_key.encode()).hexdigest()
    blocker_codes: tuple[str, ...] = ()
    result = KpiSemanticDispositionResult(
        inserted_context_rows=0,
        replayed_context_rows=0,
        inserted_reference_rows=0,
        replayed_reference_rows=0,
    )
    state: Literal["passed", "applied", "replayed", "blocked", "failed"] = "failed"
    dry_run: KpiDispositionAttemptReceipt | None = None
    judge: KpiDispositionJudgeReceipt | None = None
    authority_checked_at = started

    def validate_current_authority() -> None:
        nonlocal authority_checked_at
        now = datetime.now(UTC)
        if manifest.knowledge_at > now + timedelta(minutes=5):
            raise DispositionBlockedError("manifest_knowledge_at_from_future")
        validate_repair_review_preconditions(
            manifest=manifest,
            review_bundle=review_bundle,
            trusted_pins=trusted_pins,
            backup=backup,
            now=now,
            max_review_age=timedelta(seconds=args.max_review_age_seconds),
            not_before=authority_checked_at,
        )
        if args.apply and (
            dry_run is None
            or judge is None
            or not judge_authorizes(
                dry_run=dry_run,
                judge=judge,
                manifest_sha=manifest_sha,
                manifest=manifest,
                executor_code_sha=executor_code_sha,
                now=now,
            )
        ):
            raise DispositionBlockedError("judge_receipt_not_authorizing")
        authority_checked_at = now

    try:
        if args.user_id != manifest.user_id:
            raise DispositionBlockedError("manifest_user_identity_mismatch")
        if args.repo_root.resolve() != PROJECT_ROOT.resolve():
            raise DispositionBlockedError("report_configuration_root_is_not_running_code")
        if args.max_review_age_seconds <= 0:
            raise DispositionBlockedError("invalid_review_age")
        if not args.apply:
            validate_current_authority()
        if args.apply:
            _validate_apply_authority(
                db_path=args.db,
                repo_root=args.repo_root,
                receipt_root=receipt_root,
                review_bundle=review_bundle,
            )
            if args.approved_manifest_sha256 != manifest_sha:
                raise DispositionBlockedError("owner_manifest_approval_mismatch")
            if args.dry_run_receipt is None:
                raise DispositionBlockedError("dry_run_receipt_missing")
            if args.judge_receipt is None:
                raise DispositionBlockedError("judge_receipt_missing")
            dry_run = KpiDispositionAttemptReceipt.model_validate_json(
                args.dry_run_receipt.read_text(encoding="utf-8")
            )
            judge = KpiDispositionJudgeReceipt.model_validate_json(
                args.judge_receipt.read_text(encoding="utf-8")
            )
            judge_checked_at = datetime.now(UTC)
            if judge_checked_at < authority_checked_at:
                raise DispositionBlockedError("repair_clock_moved_backwards")
            if not judge_authorizes(
                dry_run=dry_run,
                judge=judge,
                manifest_sha=manifest_sha,
                manifest=manifest,
                executor_code_sha=executor_code_sha,
                now=judge_checked_at,
            ):
                raise DispositionBlockedError("judge_receipt_not_authorizing")
            authority_checked_at = judge_checked_at
            validate_current_authority()
        with JobLock(
            _lock_root(args.db),
            "kpi-semantic-dispositions",
            ["portfolio-db"],
            wait_s=0,
        ):
            recovered = (
                recover_committed_disposition(
                    db_path=args.db,
                    manifest=manifest,
                    manifest_sha=manifest_sha,
                    logical_key_sha=logical_key_sha,
                    executor_code_sha=executor_code_sha,
                    review_bundle=review_bundle,
                )
                if args.apply
                else None
            )
            if recovered is not None:
                validate_current_authority()
                # A committed repair changed the live contents. Preserve the
                # exact immutable rollback proof without demanding that the
                # source still equal its pre-repair snapshot.
                reasons = validate_receipt_for_source(
                    backup,
                    source_db=args.db,
                    source_revision=manifest.expected_schema_revision,
                    require_current_identity=False,
                )
                if reasons:
                    raise DispositionBlockedError(reasons[0])
                validate_current_authority()
                result = recovered
                state = "replayed"
            else:
                authority_checked_at = validate_disposition_external_evidence(
                    manifest=manifest,
                    db_path=args.db,
                    review_bundle=review_bundle,
                    trusted_pins=trusted_pins,
                    backup=backup,
                    now=datetime.now(UTC),
                    max_review_age=timedelta(seconds=args.max_review_age_seconds),
                    not_before=authority_checked_at,
                )
                validate_current_authority()
                clone_started = perf_counter()
                with _disposition_database(
                    live_db=args.db, backup=backup, apply=args.apply
                ) as work_db:
                    emit_repair_phase_diagnostics(
                        phase="clone_hash",
                        started_monotonic=clone_started,
                        review_bundle=review_bundle,
                        max_review_age=timedelta(seconds=args.max_review_age_seconds),
                        now=datetime.now(UTC),
                    )
                    result = execute_disposition_transaction(
                        db_path=work_db,
                        repo_root=args.repo_root,
                        manifest=manifest,
                        manifest_sha=manifest_sha,
                        logical_key_sha=logical_key_sha,
                        executor_code_sha=executor_code_sha,
                        review_bundle=review_bundle,
                        apply=args.apply,
                        validate_current_authority=validate_current_authority,
                        max_review_age=timedelta(seconds=args.max_review_age_seconds),
                    )
                state = "applied" if args.apply else "passed"
    except JobAlreadyRunningError:
        blocker_codes = ("portfolio_db_lock_contended",)
        state = "blocked"
    except DispositionBlockedError as exc:
        blocker_codes = (exc.code,)
        state = "blocked"
    except Exception as exc:
        blocker_codes = (f"unexpected_{type(exc).__name__}",)
        state = "failed"

    receipt = seal_disposition_attempt(
        attempt_id=attempt_id,
        logical_idempotency_key_sha256=logical_key_sha,
        manifest_sha256=manifest_sha,
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256=executor_code_sha,
        mode=mode,
        state=state,
        started_at=started,
        completed_at=datetime.now(UTC),
        validated_fact_dispositions=(
            len(manifest.fact_dispositions) if state in {"passed", "applied", "replayed"} else 0
        ),
        validated_reference_dispositions=(
            len(manifest.report_reference_dispositions)
            if state in {"passed", "applied", "replayed"}
            else 0
        ),
        inserted_context_rows=result.inserted_context_rows,
        replayed_context_rows=result.replayed_context_rows,
        inserted_reference_rows=result.inserted_reference_rows,
        replayed_reference_rows=result.replayed_reference_rows,
        blocker_codes=blocker_codes,
    )
    publication_started = perf_counter()
    summary = publish_disposition_receipt(receipt_root=receipt_root, receipt=receipt)
    emit_repair_phase_diagnostics(
        phase="receipt_publication",
        started_monotonic=publication_started,
        review_bundle=review_bundle,
        max_review_age=timedelta(seconds=args.max_review_age_seconds),
        now=datetime.now(UTC),
    )
    sys.stderr.write(
        json.dumps(
            {
                "event": "kpi_semantic_dispositions_completed",
                "attempt_id": attempt_id,
                "state": state,
                "receipt_sha256": receipt.content_sha256,
                "blocker_codes": blocker_codes,
            },
            sort_keys=True,
        )
        + "\n"
    )
    print(summary.model_dump_json())
    return 0 if state in {"passed", "applied", "replayed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
