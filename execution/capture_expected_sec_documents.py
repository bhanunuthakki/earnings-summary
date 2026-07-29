"""Capture one bounded batch of sealed SEC expected documents into evidence.

Dry run is the default: it fetches and checkpoints raw responses under
``.tmp`` but does not mutate SQLite or the durable evidence blob root.  Use
``--apply`` with the same task id to atomically persist the checked batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import cast

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.sec_native_capture import (  # noqa: E402
    SecNativeCaptureHardStopError,
    SecNativeCaptureRequest,
    SecNativeCaptureResult,
    SessionLike,
    capture_expected_sec_documents,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sec_identity import sec_user_agent  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--inventory-key",
        action="append",
        required=True,
        help="Current completely sealed SEC inventory key; repeat for more than one",
    )
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "sec_native_capture",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-document-bytes", type=int, default=100_000_000)
    parser.add_argument(
        "--minimum-request-interval-seconds",
        type=float,
        default=0.25,
        help="Minimum pause between SEC requests (default: 0.25 seconds)",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    request = SecNativeCaptureRequest(
        inventory_keys=tuple(args.inventory_key),
        checkpoint_root=args.checkpoint_root,
        blob_root=args.blob_root,
        task_id=args.task_id,
        user_agent=sec_user_agent(),
        apply=args.apply,
        batch_size=args.batch_size,
        max_document_bytes=args.max_document_bytes,
        minimum_request_interval_seconds=args.minimum_request_interval_seconds,
    )
    write_sets = [
        "sec-edgar-network",
        f"sec-native-capture-checkpoint:{request.task_id}",
    ]
    if request.apply:
        write_sets.extend(
            (
                f"sqlite:{args.db.resolve()}",
                f"evidence-blobs:{request.blob_root.resolve()}",
            )
        )
    with JobLock(PROJECT_ROOT, "sec-native-capture", write_sets):
        role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
        try:
            _event(
                "sec_native_capture_started",
                task_id=request.task_id,
                mode="apply" if request.apply else "dry_run",
                inventory_count=len(request.inventory_keys),
            )
            with requests.Session() as session:
                result = capture_expected_sec_documents(
                    conn,
                    request,
                    session=cast(SessionLike, session),
                )
        except SecNativeCaptureHardStopError:
            _event("sec_native_capture_hard_stop", task_id=request.task_id)
            return 2
        finally:
            conn.close()
    _event(
        "sec_native_capture_completed",
        task_id=request.task_id,
        mode=result.mode,
        considered=result.considered,
        fetched=result.fetched,
        deferred=result.deferred,
        failed=result.failed,
    )
    sys.stdout.write(_command_output(result, request.checkpoint_root, request.task_id) + "\n")
    return 0


def _command_output(
    result: SecNativeCaptureResult,
    checkpoint_root: Path,
    task_id: str,
) -> str:
    payload = result.model_dump(mode="json")
    items = payload.pop("items")
    item_json = json.dumps(items, sort_keys=True, separators=(",", ":"))
    item_digest = hashlib.sha256(item_json.encode("utf-8")).hexdigest()
    result_root = checkpoint_root / task_id / "results"
    result_root.mkdir(parents=True, exist_ok=True)
    item_path = result_root / f"{item_digest}.json"
    if not item_path.exists():
        temporary = item_path.with_suffix(".json.tmp")
        temporary.write_text(item_json, encoding="utf-8")
        temporary.replace(item_path)
    payload["items_path"] = str(item_path)
    payload["item_count"] = len(items)
    return json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
