"""Bootstrap SEC fund-series identities from regulator authority files.

Dry-run is the default. Apply mode captures the raw SEC registries as immutable
evidence and writes only through the append-only issuer/reporting registries.
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

from log_redact import redact  # noqa: E402
from provenance.issuer_registry_bootstrap import (  # noqa: E402
    SEC_MUTUAL_FUND_TICKERS_URL,
    HTTPSession,
    SecFundBootstrapRequest,
    SecFundBootstrapResult,
    SecFundRegistrantEvidence,
    bootstrap_sec_fund_registry,
    fetch_sec_company_tickers,
    target_sec_fund_registrant_ciks,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sec_identity import sec_user_agent  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fund-tickers-json", type=Path)
    source.add_argument("--fetch-sec-fund-tickers", action="store_true")
    parser.add_argument(
        "--registrant-json",
        action="append",
        default=[],
        metavar="CIK=PATH",
        help="Local SEC submissions JSON; repeat once per required registrant",
    )
    parser.add_argument(
        "--source-url",
        default=SEC_MUTUAL_FUND_TICKERS_URL,
    )
    parser.add_argument("--user-agent")
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _fetch_json(session: HTTPSession, url: str, user_agent: str) -> bytes:
    return fetch_sec_company_tickers(
        session,
        source_url=url,
        user_agent=user_agent,
    )


def _target_ciks(db: Path, raw_body: bytes) -> tuple[str, ...]:
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        return target_sec_fund_registrant_ciks(conn, raw_body=raw_body)
    finally:
        conn.close()


def _parse_local_registrants(
    values: list[str],
    *,
    required_ciks: tuple[str, ...],
) -> tuple[SecFundRegistrantEvidence, ...]:
    paths: dict[str, Path] = {}
    for value in values:
        cik, separator, raw_path = value.partition("=")
        if not separator or not cik.strip() or not raw_path.strip():
            raise ValueError("--registrant-json must use CIK=PATH")
        normalized = cik.strip().zfill(10)
        if normalized in paths:
            raise ValueError("--registrant-json repeats a CIK")
        paths[normalized] = Path(raw_path)
    missing = set(required_ciks) - set(paths)
    extra = set(paths) - set(required_ciks)
    if missing or extra:
        raise ValueError("local registrant files must exactly match targeted SEC registrants")
    return tuple(
        SecFundRegistrantEvidence(
            normalized_cik=cik,
            source_url=f"https://data.sec.gov/submissions/CIK{cik}.json",
            raw_body=paths[cik].read_bytes(),
        )
        for cik in required_ciks
    )


def _load_sources(
    args: argparse.Namespace,
) -> tuple[bytes, tuple[SecFundRegistrantEvidence, ...]]:
    user_agent = str(args.user_agent or sec_user_agent())
    if args.fetch_sec_fund_tickers:
        if args.registrant_json:
            raise ValueError("--registrant-json cannot be combined with live SEC fetching")
        with requests.Session() as session:
            raw_body = _fetch_json(
                cast(HTTPSession, session),
                str(args.source_url),
                user_agent,
            )
            required_ciks = _target_ciks(args.db, raw_body)
            registrants = tuple(
                SecFundRegistrantEvidence(
                    normalized_cik=cik,
                    source_url=(f"https://data.sec.gov/submissions/CIK{cik}.json"),
                    raw_body=_fetch_json(
                        cast(HTTPSession, session),
                        f"https://data.sec.gov/submissions/CIK{cik}.json",
                        user_agent,
                    ),
                )
                for cik in required_ciks
            )
            return raw_body, registrants
    raw_body = args.fund_tickers_json.read_bytes()
    required_ciks = _target_ciks(args.db, raw_body)
    return raw_body, _parse_local_registrants(
        args.registrant_json,
        required_ciks=required_ciks,
    )


def _run(
    args: argparse.Namespace,
    raw_body: bytes,
    registrants: tuple[SecFundRegistrantEvidence, ...],
) -> SecFundBootstrapResult:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(
        args.db,
        role=role,
        schema_preflight=bool(args.apply),
    )
    try:
        return bootstrap_sec_fund_registry(
            conn,
            raw_body=raw_body,
            request=SecFundBootstrapRequest(
                source_url=str(args.source_url),
                registrants=registrants,
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
            "sec_fund_registry_bootstrap_started",
            mode="apply" if args.apply else "dry_run",
            source=("sec_fetch" if args.fetch_sec_fund_tickers else "local_file"),
        )
        raw_body, registrants = _load_sources(args)
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "sec-fund-registry-bootstrap",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                result = _run(args, raw_body, registrants)
        else:
            result = _run(args, raw_body, registrants)
    except Exception as exc:
        _event(
            "sec_fund_registry_bootstrap_failed",
            mode="apply" if args.apply else "dry_run",
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    result_json = result.model_dump_json()
    _event(
        "sec_fund_registry_bootstrap_completed",
        mode=result.mode,
        selected_ticker_count=len(result.selected_tickers),
        records_created=result.records_created,
    )
    sys.stdout.write(result_json + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
