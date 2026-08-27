"""Resolve historical evidence after an SEC registrant changes ticker."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.issuer_registry_bootstrap import (  # noqa: E402
    SecCompanyTickerFetchError,
    SecFormerTickerRequest,
    SecFormerTickerResult,
    bootstrap_sec_former_ticker,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sec_identity import sec_user_agent  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def normalize_cik(value: object) -> str:
    normalized = str(value).strip().zfill(10)
    if len(normalized) != 10 or not normalized.isdigit():
        raise ValueError("CIK must contain at most ten decimal digits")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--former-ticker", required=True)
    parser.add_argument("--successor-ticker", required=True)
    parser.add_argument("--cik", required=True)
    parser.add_argument("--transition-date", type=date.fromisoformat, required=True)
    parser.add_argument("--transition-url", required=True)
    submissions = parser.add_mutually_exclusive_group(required=True)
    submissions.add_argument("--submissions-json", type=Path)
    submissions.add_argument("--fetch-sec-submissions", action="store_true")
    transition = parser.add_mutually_exclusive_group(required=True)
    transition.add_argument("--transition-document", type=Path)
    transition.add_argument("--fetch-transition-document", action="store_true")
    parser.add_argument("--user-agent")
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _fetch_sec(session: requests.Session, url: str, *, accept: str, user_agent: str) -> bytes:
    response = session.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=(10, 60),
    )
    if response.status_code in {401, 403}:
        raise SecCompanyTickerFetchError(
            f"SEC returned {response.status_code}; verify the declared User-Agent"
        )
    if response.status_code != 200:
        raise SecCompanyTickerFetchError(
            f"SEC authority request returned status {response.status_code}"
        )
    if not response.content:
        raise SecCompanyTickerFetchError("SEC authority request returned an empty body")
    return bytes(response.content)


def _load_sources(
    args: argparse.Namespace,
    *,
    submissions_url: str,
) -> tuple[bytes, bytes]:
    if not args.fetch_sec_submissions and not args.fetch_transition_document:
        return args.submissions_json.read_bytes(), args.transition_document.read_bytes()
    user_agent = str(args.user_agent or sec_user_agent())
    with requests.Session() as session:
        submissions = (
            _fetch_sec(
                session,
                submissions_url,
                accept="application/json",
                user_agent=user_agent,
            )
            if args.fetch_sec_submissions
            else args.submissions_json.read_bytes()
        )
        transition = (
            _fetch_sec(
                session,
                str(args.transition_url),
                accept="text/html",
                user_agent=user_agent,
            )
            if args.fetch_transition_document
            else args.transition_document.read_bytes()
        )
    return submissions, transition


def _run(
    args: argparse.Namespace,
    *,
    normalized_cik: str,
    submissions_url: str,
    submissions_raw_body: bytes,
    transition_raw_body: bytes,
) -> SecFormerTickerResult:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply))
    try:
        return bootstrap_sec_former_ticker(
            conn,
            request=SecFormerTickerRequest(
                former_ticker=str(args.former_ticker),
                successor_ticker=str(args.successor_ticker),
                normalized_cik=normalized_cik,
                transition_date=args.transition_date,
                submissions_source_url=submissions_url,
                submissions_raw_body=submissions_raw_body,
                transition_source_url=str(args.transition_url),
                transition_raw_body=transition_raw_body,
                blob_root=args.blob_root,
                apply=bool(args.apply),
                recorded_at=datetime.now(UTC),
            ),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    normalized_cik = normalize_cik(args.cik)
    submissions_url = f"https://data.sec.gov/submissions/CIK{normalized_cik}.json"
    try:
        _event(
            "sec_former_ticker_bootstrap_started",
            former_ticker=str(args.former_ticker).upper(),
            successor_ticker=str(args.successor_ticker).upper(),
            mode="apply" if args.apply else "dry_run",
        )
        submissions_raw_body, transition_raw_body = _load_sources(
            args,
            submissions_url=submissions_url,
        )
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "sec-former-ticker-bootstrap",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                result = _run(
                    args,
                    normalized_cik=normalized_cik,
                    submissions_url=submissions_url,
                    submissions_raw_body=submissions_raw_body,
                    transition_raw_body=transition_raw_body,
                )
        else:
            result = _run(
                args,
                normalized_cik=normalized_cik,
                submissions_url=submissions_url,
                submissions_raw_body=submissions_raw_body,
                transition_raw_body=transition_raw_body,
            )
    except Exception as exc:
        _event(
            "sec_former_ticker_bootstrap_failed",
            former_ticker=str(args.former_ticker).upper(),
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "sec_former_ticker_bootstrap_completed",
        former_ticker=result.former_ticker,
        successor_ticker=result.successor_ticker,
        mode=result.mode,
        records_created=result.records_created,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
