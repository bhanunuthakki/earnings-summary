"""Plan or apply bounded byte capture, fulltext extraction, and coverage continuation.

No source crawling, LLM calls, semantic admission, or completeness seals. Exit 2
means degraded or unfinished evidence, 75 means writer contention. Use the JSON
next_before_document_id to drain pages, then reconcile again from zero for retries.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

if __package__:
    from ._lib import PROJECT_ROOT, command_parser
else:
    from _lib import PROJECT_ROOT, command_parser
from pydantic import BaseModel, ConfigDict, Field

from operations.paths import configured_product_state_root, operations_runtime_directory
from provenance.document_evidence_pipeline import (
    DocumentEvidenceRequest,
    DocumentEvidenceResult,
    process_document_evidence,
)
from run_lock import RunLockHeldError, hold_run_lock
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    inherited_lock_is_valid,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class DocumentEvidenceResumeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    database_path: str
    repo_root: str
    ticker: str | None
    content_roots: tuple[str, ...]
    next_before_document_id: int = Field(ge=0)
    newest_document_id_seen: int = Field(ge=0)
    pending_document_ids: tuple[int, ...]
    result: DocumentEvidenceResult


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--ticker")
    parser.add_argument("--document-id", type=int)
    parser.add_argument("--before-document-id", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--content-root", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--resume", action="store_true", help="Apply a page and persist its exact receipt/cursor"
    )
    args = parser.parse_args(argv)
    if args.resume and (
        not args.apply
        or args.document_id is not None
        or args.before_document_id
        or args.batch_size < 2
    ):
        parser.error(
            "--resume requires --apply and batch-size >= 2; excludes --document-id/--before-document-id"
        )
    root = (
        args.repo_root.resolve() if args.repo_root else configured_product_state_root(PROJECT_ROOT)
    )
    request = DocumentEvidenceRequest(
        repo_root=root,
        ticker=args.ticker,
        document_id=args.document_id,
        before_document_id=args.before_document_id,
        batch_size=args.batch_size,
        content_roots=tuple(args.content_root),
        apply=args.apply,
    )
    receipt_path = operations_runtime_directory(root) / "document-evidence.latest.json"
    if request.apply:
        try:
            with ExitStack() as stack:
                inherited_database_lock = portfolio_db_path(
                    PROJECT_ROOT
                ).resolve() == args.db.resolve() and inherited_lock_is_valid(
                    PROJECT_ROOT, "portfolio-db"
                )
                if not inherited_database_lock:
                    stack.enter_context(
                        hold_run_lock(
                            args.db.resolve(), owner="process-document-evidence", timeout_s=0
                        )
                    )
                stack.enter_context(
                    JobLock(
                        root,
                        "process-document-evidence",
                        [f"sqlite:{args.db.resolve()}", f"artifact:{receipt_path}"],
                        wait_s=0,
                    )
                )
                return _run(args.db, request, receipt_path=receipt_path if args.resume else None)
        except (RunLockHeldError, JobAlreadyRunningError):
            sys.stderr.write('{"event":"document_evidence_locked"}\n')
            return 75
    return _run(args.db, request)


def _run(
    db_path: Path, request: DocumentEvidenceRequest, *, receipt_path: Path | None = None
) -> int:
    previous: DocumentEvidenceResumeReceipt | None = None
    if receipt_path is not None and receipt_path.exists():
        previous = DocumentEvidenceResumeReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
        if (
            previous.database_path != str(db_path.resolve())
            or previous.repo_root != str(request.repo_root.resolve())
            or previous.ticker != request.ticker
            or previous.content_roots
            != tuple(str(root.resolve()) for root in request.content_roots)
        ):
            raise ValueError("document evidence receipt scope mismatch")
        request = request.model_copy(
            update={"before_document_id": previous.next_before_document_id}
        )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db_path, role=role, schema_preflight=request.apply)
    try:
        if previous is None:
            result = process_document_evidence(conn, request)
            cursor = result.next_before_document_id if result.has_more else 0
            newest_seen = max((item.document_id for item in result.items), default=0)
        else:
            # Reserve half the bound for new arrivals and half for the historical
            # sweep. Ascending new IDs advance the watermark without losing a burst.
            fresh = process_document_evidence(
                conn,
                request.model_copy(
                    update={
                        "before_document_id": 0,
                        "newer_than_document_id": previous.newest_document_id_seen,
                        "batch_size": max(1, request.batch_size // 2),
                    }
                ),
            )
            newest_seen = max(
                (item.document_id for item in fresh.items), default=previous.newest_document_id_seen
            )
            remaining = request.batch_size - len(fresh.items)
            backlog = (
                process_document_evidence(
                    conn,
                    request.model_copy(
                        update={
                            "before_document_id": previous.next_before_document_id
                            or previous.newest_document_id_seen + 1,
                            "batch_size": remaining,
                        }
                    ),
                )
                if remaining
                else None
            )
            cursor = (
                (backlog.next_before_document_id if backlog.has_more else 0)
                if backlog
                else previous.next_before_document_id
            )
            result = _combine_results(fresh, backlog)
    finally:
        conn.close()
    if receipt_path is not None:
        pending = set(previous.pending_document_ids if previous else ())
        for item in result.items:
            if not item.degraded:
                pending.discard(item.document_id)
            else:
                pending.add(item.document_id)
        if pending:
            result.degraded = True
        receipt = DocumentEvidenceResumeReceipt(
            database_path=str(db_path.resolve()),
            repo_root=str(request.repo_root.resolve()),
            ticker=request.ticker,
            content_roots=tuple(str(root.resolve()) for root in request.content_roots),
            next_before_document_id=cursor,
            newest_document_id_seen=newest_seen,
            pending_document_ids=tuple(sorted(pending)),
            result=result,
        )
        _write_receipt(receipt_path, receipt)
    sys.stdout.write(result.model_dump_json() + "\n")
    return 2 if result.degraded else 0


def _combine_results(
    fresh: DocumentEvidenceResult, backlog: DocumentEvidenceResult | None
) -> DocumentEvidenceResult:
    if backlog is None:
        return fresh
    items = [*fresh.items, *backlog.items]
    return DocumentEvidenceResult(
        mode=fresh.mode,
        items=items,
        status_counts=dict(Counter(item.status for item in items)),
        captured=fresh.captured + backlog.captured,
        extracted=fresh.extracted + backlog.extracted,
        inventory_uninitialized=fresh.inventory_uninitialized + backlog.inventory_uninitialized,
        coverage_promotions_planned=fresh.coverage_promotions_planned
        + backlog.coverage_promotions_planned,
        coverage_promotions_created=fresh.coverage_promotions_created
        + backlog.coverage_promotions_created,
        has_more=fresh.has_more or backlog.has_more,
        next_before_document_id=backlog.next_before_document_id,
        findings=[*fresh.findings, *backlog.findings],
        degraded=fresh.degraded or backlog.degraded,
    )


def _write_receipt(path: Path, receipt: DocumentEvidenceResumeReceipt) -> None:
    """The cursor and complete batch receipt become visible atomically, after commits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(receipt.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
