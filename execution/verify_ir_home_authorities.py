"""Verify reviewed publisher IR-home candidates against exact live bytes."""

from __future__ import annotations

try:  # direct script invocation
    from _lib import PROJECT_ROOT
except ImportError:  # pragma: no cover - test/import path fallback
    from execution._lib import PROJECT_ROOT

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import requests

from ir_pipeline.home_authority_batch import (
    IRHomeBatchRequest,
    IRHomeBatchResult,
    SessionLike,
    verify_ir_home_candidates,
)
from ir_pipeline.home_authority_registry import (
    IR_HOME_AUTHORITY_CANDIDATES,
    IRHomeAuthorityCandidate,
    candidate_for_ticker,
)
from log_redact import redact
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_DEFAULT_USER_AGENT = "earnings-summary IR authority verification"


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Reviewed candidate ticker; repeat to select multiple",
    )
    selection.add_argument("--all-candidates", action="store_true")
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--user-agent", default=_DEFAULT_USER_AGENT)
    parser.add_argument("--connect-timeout-seconds", type=int, default=10)
    parser.add_argument("--read-timeout-seconds", type=int, default=60)
    parser.add_argument("--max-body-bytes", type=int, default=10_000_000)
    parser.add_argument("--max-redirects", type=int, default=5)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--refresh-existing", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def select_candidates(tickers: tuple[str, ...]) -> tuple[IRHomeAuthorityCandidate, ...]:
    requested = {ticker.strip().upper() for ticker in tickers}
    resolved = {ticker: candidate_for_ticker(ticker) for ticker in requested}
    unknown = {ticker for ticker, candidate in resolved.items() if candidate is None}
    if unknown:
        raise ValueError("no reviewed IR-home candidate for: " + ", ".join(sorted(unknown)))
    primary_tickers = {candidate.ticker for candidate in resolved.values() if candidate is not None}
    return tuple(
        candidate
        for candidate in IR_HOME_AUTHORITY_CANDIDATES
        if candidate.ticker in primary_tickers
    )


def run_authority_verification(args: argparse.Namespace) -> IRHomeBatchResult:
    candidates = (
        IR_HOME_AUTHORITY_CANDIDATES
        if args.all_candidates
        else select_candidates(tuple(args.ticker))
    )
    request = IRHomeBatchRequest(
        candidates=candidates,
        blob_root=args.blob_root,
        apply=bool(args.apply),
        recorded_at=datetime.now(UTC),
        user_agent=str(args.user_agent),
        connect_timeout_seconds=int(args.connect_timeout_seconds),
        read_timeout_seconds=int(args.read_timeout_seconds),
        max_body_bytes=int(args.max_body_bytes),
        max_redirects=int(args.max_redirects),
        max_workers=int(args.max_workers),
        refresh_existing=bool(args.refresh_existing),
    )
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn: sqlite3.Connection = connect_sqlite(
        args.db,
        role=role,
        schema_preflight=bool(args.apply),
    )
    try:
        result = verify_ir_home_candidates(
            conn,
            request=request,
            session_factory=_session_factory,
        )
        if args.apply:
            conn.commit()
        return result
    except Exception:
        if args.apply:
            conn.rollback()
        raise
    finally:
        conn.close()


def _session_factory() -> SessionLike:
    return cast(SessionLike, requests.Session())


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = "apply" if args.apply else "dry_run"
    try:
        _event(
            "ir_home_authority_verification_started",
            mode=mode,
            selection="all" if args.all_candidates else sorted(args.ticker),
            refresh_existing=bool(args.refresh_existing),
        )
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "ir-home-authority-verification",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                result = run_authority_verification(args)
        else:
            result = run_authority_verification(args)
    except Exception as exc:
        _event(
            "ir_home_authority_verification_failed",
            mode=mode,
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1

    failed = sum(item.outcome == "failed" for item in result.items)
    verified = sum(item.outcome == "verified" for item in result.items)
    skipped = len(result.items) - failed - verified
    records_created = sum(item.records_created for item in result.items)
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "ir_home_authority_verification_completed",
        mode=result.mode,
        candidate_count=len(result.items),
        verified=verified,
        skipped=skipped,
        failed=failed,
        records_created=records_created,
    )
    if failed and not args.allow_partial:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
