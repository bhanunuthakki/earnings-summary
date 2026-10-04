"""Capture raw publisher authority surfaces and emit hash-bound IR evidence."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_paths import configured_db_path
from ir_pipeline.authority_capture import (
    IRAuthorityCaptureRequest,
    SessionLike,
    capture_ir_authority_surfaces,
)
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help="Strict closed JSON IRAuthorityCaptureRequest",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    conn: sqlite3.Connection | None = None
    try:
        request = IRAuthorityCaptureRequest.model_validate_json(
            args.request.read_text(encoding="utf-8")
        )
        if args.apply:
            if not os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip():
                raise ValueError("explicit configured database authority required for apply")
            if args.db.resolve() != configured_db_path(PROJECT_ROOT):
                raise ValueError("apply database differs from configured authority")
        _event(
            "ir_authority_capture_started",
            issuer_id=request.issuer_id,
            ticker=request.ticker,
            mode="apply" if args.apply else "dry_run",
            surface_count=len(request.surfaces),
        )
        role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
        write_sets = [
            "portfolio-db",
            f"evidence-blobs:{args.blob_root.resolve()}",
        ]
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "ir-authority-surface-capture",
                write_sets,
            ):
                conn = connect_sqlite(
                    args.db,
                    role=role,
                    schema_preflight=True,
                )
                with requests.Session() as session:
                    result = capture_ir_authority_surfaces(
                        conn,
                        request,
                        blob_root=args.blob_root,
                        apply=True,
                        session=cast(SessionLike, session),
                    )
        else:
            conn = connect_sqlite(
                args.db,
                role=role,
                schema_preflight=False,
            )
            with requests.Session() as session:
                result = capture_ir_authority_surfaces(
                    conn,
                    request,
                    blob_root=args.blob_root,
                    apply=False,
                    session=cast(SessionLike, session),
                )
    except Exception as exc:
        _event(
            "ir_authority_capture_failed",
            error_type=type(exc).__name__,
        )
        return 1
    finally:
        if conn is not None:
            conn.close()

    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "ir_authority_capture_completed",
        issuer_id=result.issuer_id,
        ticker=result.ticker,
        mode=result.mode,
        complete=result.complete,
        fetched=result.fetched,
        failed=result.failed,
        records_created=result.records_created,
        records_replayed=result.records_replayed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
