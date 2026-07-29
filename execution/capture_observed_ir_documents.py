"""Capture one bounded batch of raw IR documents from sealed discovery evidence.

Dry run is the default. It fetches and checkpoints bytes under ``.tmp`` but
does not mutate SQLite or the durable content-addressed blob root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.authority import PublisherEndpointRule  # noqa: E402
from ir_pipeline.evidence_capture import (  # noqa: E402
    IRDocumentCaptureHardStopError,
    IRDocumentCaptureRequest,
    SessionLike,
    capture_observed_ir_documents,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _publisher_rule(value: str) -> PublisherEndpointRule:
    host, separator, path_prefix = value.partition("/")
    return PublisherEndpointRule(
        host=host,
        path_prefix="/" + path_prefix if separator else "/",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inventory-key", action="append", required=True)
    parser.add_argument(
        "--publisher-file-endpoint",
        action="append",
        default=[],
        metavar="HOST[/PATH_PREFIX]",
        help="Explicit publisher CDN/file endpoint authorization; repeat as needed",
    )
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "ir_evidence_capture",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-document-bytes", type=int, default=100_000_000)
    parser.add_argument("--max-redirects", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    request = IRDocumentCaptureRequest(
        inventory_keys=tuple(args.inventory_key),
        publisher_file_rules=tuple(
            _publisher_rule(value) for value in args.publisher_file_endpoint
        ),
        checkpoint_root=args.checkpoint_root,
        blob_root=args.blob_root,
        task_id=args.task_id,
        user_agent=args.user_agent,
        apply=args.apply,
        batch_size=args.batch_size,
        max_document_bytes=args.max_document_bytes,
        max_redirects=args.max_redirects,
    )
    write_sets = [f"ir-evidence-capture-checkpoint:{request.task_id}"]
    if request.apply:
        write_sets.extend(
            (
                f"sqlite:{args.db.resolve()}",
                f"evidence-blobs:{request.blob_root.resolve()}",
            )
        )
    with JobLock(PROJECT_ROOT, "ir-evidence-capture", write_sets):
        role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(
            args.db,
            role=role,
            schema_preflight=request.apply,
        )
        try:
            _event(
                "ir_evidence_capture_started",
                task_id=request.task_id,
                mode="apply" if request.apply else "dry_run",
                inventory_count=len(request.inventory_keys),
            )
            with requests.Session() as session:
                result = capture_observed_ir_documents(
                    conn,
                    request,
                    session=cast(SessionLike, session),
                )
        except IRDocumentCaptureHardStopError:
            _event(
                "ir_evidence_capture_hard_stop",
                task_id=request.task_id,
            )
            return 2
        except Exception as exc:
            _event(
                "ir_evidence_capture_failed",
                task_id=request.task_id,
                error_type=type(exc).__name__,
            )
            return 1
        finally:
            conn.close()
    _event(
        "ir_evidence_capture_completed",
        task_id=request.task_id,
        mode=result.mode,
        considered=result.considered,
        fetched=result.fetched,
        deferred=result.deferred,
        failed=result.failed,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
