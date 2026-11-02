"""Generate a retained summary for one exact registered transcript version."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from alias_manager import resolve_ticker
from db_paths import db_path_context, require_db_path
from pipeline.issuer_document_inventory import (
    IssuerDocumentInventoryError,
    resolve_registered_document_period,
)
from pipeline.transcript_summary import (
    TranscriptSummaryVerifier,
    generate_transcript_summary,
)
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_TRANSCRIPT_TYPES = ("transcript", "ir_transcript", "earnings_call_transcript")


def process_document(
    document_version_id: str,
    *,
    db_path: Path,
    state_root: Path,
    verifier: TranscriptSummaryVerifier,
) -> dict[str, object]:
    """Process one explicitly selected canonical transcript version."""
    result = generate_transcript_summary(
        document_version_id,
        db_path=db_path,
        state_root=state_root,
        verifier=verifier,
    )
    return {
        "status": result.status,
        "document_version_id": result.document_version_id,
        "artifact_id": result.artifact_id,
        "projection_path": (
            None if result.projection_path is None else str(result.projection_path)
        ),
    }


def select_registered_transcripts(
    db_path: Path,
    *,
    ticker: str | None,
    all_tickers: bool,
    fiscal_year: int | None,
    fiscal_quarter: int | None,
) -> list[str]:
    """Select current registered transcript versions and prove their fiscal keys."""
    if (ticker is None and not all_tickers) or (ticker is not None and all_tickers):
        raise ValueError("select exactly one of --ticker or --all")
    if (fiscal_year is None) != (fiscal_quarter is None):
        raise ValueError("--year and --quarter must be supplied together")
    if fiscal_year is not None and not 1900 <= fiscal_year <= 2200:
        raise ValueError("--year must be between 1900 and 2200")
    if fiscal_quarter is not None and fiscal_quarter not in {1, 2, 3, 4}:
        raise ValueError("--quarter must be 1, 2, 3, or 4")
    if ticker is None:
        canonical_ticker = None
    else:
        with db_path_context(db_path):
            canonical_ticker = resolve_ticker(ticker).upper()
        if not canonical_ticker:
            raise ValueError("--ticker is empty")

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
        query = (
            "SELECT current.document_version_id,current.ticker "
            "FROM v_evidence_document_current AS current "
            "JOIN v_evidence_document_versions_canonical AS canonical "
            "ON canonical.document_version_id=current.document_version_id "
            "WHERE lower(current.document_type) IN (?,?,?)"
        )
        params: list[object] = list(_TRANSCRIPT_TYPES)
        if canonical_ticker is not None:
            query += " AND current.ticker=?"
            params.append(canonical_ticker)
        query += " ORDER BY current.ticker,current.period_end,current.document_version_id"
        rows = conn.execute(query, tuple(params)).fetchall()
        selected: list[str] = []
        for row in rows:
            document_version_id, document_ticker = str(row[0]), str(row[1])
            if canonical_ticker is not None and document_ticker != canonical_ticker:
                continue
            try:
                registered = resolve_registered_document_period(
                    conn,
                    document_version_id,
                )
            except IssuerDocumentInventoryError as exc:
                raise IssuerDocumentInventoryError(
                    f"selection_deferred:{document_version_id}:{exc.reason_code}"
                ) from exc
            if (fiscal_year is None or registered.request.fiscal_year == fiscal_year) and (
                fiscal_quarter is None or registered.request.fiscal_quarter == fiscal_quarter
            ):
                selected.append(document_version_id)
        return selected


def run_for_ticker(
    ticker: str,
    quarter: str | int | None = None,
    year: str | int | None = None,
    *,
    db_path: Path,
    state_root: Path,
    verifier: TranscriptSummaryVerifier,
) -> int:
    """Process exact current transcript versions selected by fiscal coordinates."""
    fiscal_quarter = _parse_quarter(quarter)
    fiscal_year = None if year is None else int(year)
    document_version_ids = select_registered_transcripts(
        db_path,
        ticker=ticker,
        all_tickers=False,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
    )
    results = [
        process_document(
            document_version_id,
            db_path=db_path,
            state_root=state_root,
            verifier=verifier,
        )
        for document_version_id in document_version_ids
    ]
    print(json.dumps({"ticker": ticker, "results": results}, sort_keys=True))
    return int(any(result["status"] == "failed" for result in results))


def _parse_quarter(value: str | int | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text.startswith("Q"):
        text = text[1:]
    quarter = int(text)
    if quarter not in {1, 2, 3, 4}:
        raise ValueError("quarter must be 1, 2, 3, or 4")
    return quarter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate transcript summaries from exact registered document versions."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--document-version-id")
    selection.add_argument("--ticker")
    selection.add_argument("--all", action="store_true", dest="all_tickers")
    parser.add_argument("--quarter")
    parser.add_argument("--year", type=int)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    database = require_db_path(args.db)
    state_root = args.state_root.expanduser().resolve(strict=True)
    load_project_env(repo_root)

    try:
        from pipeline.transcript_summary_verification import (
            TranscriptSummaryVerifier as AuditAdapter,
        )

        verifier = AuditAdapter(db_path=database, state_root=state_root)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked_qualified_verifier_unavailable",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "document_version_id": args.document_version_id,
                },
                sort_keys=True,
            )
        )
        return 2

    try:
        if args.document_version_id is not None:
            if args.year is not None or args.quarter is not None:
                raise ValueError("--year and --quarter apply only to --ticker or --all")
            document_version_ids = [args.document_version_id]
        else:
            document_version_ids = select_registered_transcripts(
                database,
                ticker=args.ticker,
                all_tickers=args.all_tickers,
                fiscal_year=args.year,
                fiscal_quarter=_parse_quarter(args.quarter),
            )
        results = [
            process_document(
                document_version_id,
                db_path=database,
                state_root=state_root,
                verifier=verifier,
            )
            for document_version_id in document_version_ids
        ]
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "document_version_id": args.document_version_id,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "complete", "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
