"""Resolve a delisted SEC issuer from its Form 15 submissions authority file."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.issuer_registry_bootstrap import (  # noqa: E402
    HTTPSession,
    SecHistoricalIssuerRequest,
    SecHistoricalIssuerResult,
    bootstrap_sec_historical_issuer,
    fetch_sec_company_tickers,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sec_identity import sec_user_agent  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--cik", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--submissions-json", type=Path)
    source.add_argument("--fetch-sec-submissions", action="store_true")
    parser.add_argument("--user-agent")
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _load_body(args: argparse.Namespace, source_url: str) -> bytes:
    if not args.fetch_sec_submissions:
        return args.submissions_json.read_bytes()
    with requests.Session() as session:
        return fetch_sec_company_tickers(
            cast(HTTPSession, session),
            source_url=source_url,
            user_agent=str(args.user_agent or sec_user_agent()),
        )


def _run(
    args: argparse.Namespace,
    raw_body: bytes,
    source_url: str,
) -> SecHistoricalIssuerResult:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(
        args.db,
        role=role,
        schema_preflight=bool(args.apply),
    )
    try:
        return bootstrap_sec_historical_issuer(
            conn,
            request=SecHistoricalIssuerRequest(
                ticker=str(args.ticker),
                normalized_cik=str(args.cik),
                source_url=source_url,
                raw_body=raw_body,
                blob_root=args.blob_root,
                apply=bool(args.apply),
                recorded_at=datetime.now(UTC),
            ),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    normalized_cik = str(args.cik).strip().zfill(10)
    source_url = f"https://data.sec.gov/submissions/CIK{normalized_cik}.json"
    try:
        _event(
            "sec_historical_issuer_bootstrap_started",
            ticker=str(args.ticker).upper(),
            mode="apply" if args.apply else "dry_run",
        )
        raw_body = _load_body(args, source_url)
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "sec-historical-issuer-bootstrap",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                result = _run(args, raw_body, source_url)
        else:
            result = _run(args, raw_body, source_url)
    except Exception as exc:
        _event(
            "sec_historical_issuer_bootstrap_failed",
            ticker=str(args.ticker).upper(),
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "sec_historical_issuer_bootstrap_completed",
        ticker=result.ticker,
        mode=result.mode,
        records_created=result.records_created,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
