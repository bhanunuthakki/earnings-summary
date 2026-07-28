"""Bootstrap the canonical issuer reporting registry from SEC authority data.

Dry-run is the default and opens SQLite read-only. Apply mode is serialized
with the portfolio database and evidence-blob write sets.
"""

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

from provenance.issuer_registry_bootstrap import (  # noqa: E402
    SEC_COMPANY_TICKERS_URL,
    BootstrapRequest,
    BootstrapResult,
    HTTPSession,
    bootstrap_issuer_reporting_registry,
    fetch_sec_company_tickers,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--company-tickers-json",
        type=Path,
        help="Strict local copy of SEC company_tickers.json",
    )
    source.add_argument(
        "--fetch-sec-company-tickers",
        action="store_true",
        help="Perform exactly one request to the SEC company-ticker endpoint",
    )
    parser.add_argument(
        "--source-url",
        default=SEC_COMPANY_TICKERS_URL,
        help="SEC company-ticker authority URL",
    )
    parser.add_argument(
        "--user-agent",
        help="SEC-declared User-Agent; required with --fetch-sec-company-tickers",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _load_body(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bytes:
    if args.fetch_sec_company_tickers:
        if not args.user_agent:
            parser.error("--fetch-sec-company-tickers requires --user-agent")
        with requests.Session() as session:
            return fetch_sec_company_tickers(
                cast(HTTPSession, session),
                source_url=str(args.source_url),
                user_agent=str(args.user_agent),
            )
    path = cast(Path, args.company_tickers_json)
    return path.read_bytes()


def _run(args: argparse.Namespace, raw_body: bytes) -> BootstrapResult:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(
        args.db,
        role=role,
        schema_preflight=bool(args.apply),
    )
    try:
        return bootstrap_issuer_reporting_registry(
            conn,
            raw_body=raw_body,
            request=BootstrapRequest(
                source_url=str(args.source_url),
                blob_root=args.blob_root,
                apply=bool(args.apply),
                recorded_at=datetime.now(UTC),
            ),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _event(
            "issuer_registry_bootstrap_started",
            mode="apply" if args.apply else "dry_run",
            source="sec_fetch" if args.fetch_sec_company_tickers else "local_file",
        )
        raw_body = _load_body(args, parser)
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "issuer-registry-bootstrap",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                result = _run(args, raw_body)
        else:
            result = _run(args, raw_body)
    except Exception as exc:
        _event(
            "issuer_registry_bootstrap_failed",
            mode="apply" if args.apply else "dry_run",
            error_type=type(exc).__name__,
        )
        return 1
    result_json = result.model_dump_json()
    _event(
        "issuer_registry_bootstrap_completed",
        mode=result.mode,
        selected_ticker_count=len(result.selected_tickers),
        records_created=result.records_created,
    )
    sys.stdout.write(result_json + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
