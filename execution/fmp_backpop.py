"""Diff-aware FMP bulk backpopulation — the owner's ~6-monthly paid-key window.

EDGAR (`execution/fetch_sec_xbrl.py`) is now the continuous free freshness
source for the ~45 statement line_items in `pipeline.sec_xbrl.TAG_LADDERS`.
FMP stays the periodic paid bulk refresh — this script builds a manifest for
`execution/save_fmp_data.py --manifest` that SKIPS the 6 (endpoint, period)
statement jobs EDGAR already covers well for a ticker, so the bounded call
budget goes to what EDGAR can never provide: segments, growth series, TTM,
ratios, analyst estimates, DCF, price history, peers, 10-K/10-Q JSON, etc.

Diff-aware selection (see `sec_covers_well`): a ticker's income-statement /
balance-sheet-statement / cashflow-statement (annual + quarter) FMP jobs are
skipped ONLY when EDGAR has a recent sec_xbrl fact for it AND there is no
unresolved `source_disagreement` validation issue. Any gap (no CIK, EDGAR
never ran, coverage stale, an active disagreement) falls back to fetching the
FMP statement too — never a blind skip.

Usage:
    python execution/fmp_backpop.py                        # dry-run: manifest + report only
    python execution/fmp_backpop.py --tickers NU,MELI       # scope to specific tickers
    python execution/fmp_backpop.py --apply --max-calls 5000   # fetch + index + extract

Bounded budget: `--apply` REQUIRES `--max-calls` (mirrors save_fmp_data's own
budget contract) — a ~6-monthly paid window should spend a number the owner
chose, not "whatever the manifest happens to total." Rate limit / v3-vs-stable
tier gating is controlled the same way as every other FMP script: set
FMP_TIER and (optionally) FMP_RATE_LIMIT_PER_SEC in the environment before
running.

Provenance stays correct without any special-casing: FMP rows insert through
the normal extractors (`extracted_by='fmp'`), and the tier-aware loader keeps
SEC facts winning disagreements — this script only decides which FMP calls
are worth spending budget on, not how the two sources are reconciled at read
time (see directives/fmp_backpop.md and directives/edgar_pipeline.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_paths import require_db_path
from run_lock import hold_run_lock
from runtime.job_runtime import JobLock
from runtime.python_process import managed_python_prefix
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))

import save_fmp_data as fmp_save

from models.companies import ListType
from pipeline.fmp_doc_index import index_fmp_files_for_ticker
from pipeline.queries import ANALYZED_LIST_TYPES, open_db
from pipeline.queries import tracked_companies_for_user as _tracked_for_user

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The 6 (endpoint, period) manifest keys EDGAR's TAG_LADDERS give SEC-sourced
# coverage for (income/balance/cashflow statements, annual + quarter). These
# are SKIP CANDIDATES only — `sec_covers_well` gates whether a given ticker
# actually qualifies to skip them this run.
SEC_OVERLAP_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        ("income-statement", "annual"),
        ("income-statement", "quarter"),
        ("balance-sheet-statement", "annual"),
        ("balance-sheet-statement", "quarter"),
        ("cashflow-statement", "annual"),
        ("cashflow-statement", "quarter"),
    }
)

# EDGAR coverage older than this is treated as stale (cron lapsed, or a name
# newly added to CIK_MAP hasn't had its first fetch yet) — re-include the FMP
# statement endpoints as a safety net rather than trusting an old fact.
SEC_FRESHNESS_DAYS = 400


@dataclass(frozen=True)
class TickerDecision:
    ticker: str
    list_type: str
    sec_covered: bool
    sec_fact_count: int
    has_disagreement: bool
    jobs_total: int
    jobs_included: int
    jobs_skipped_via_edgar: int


def sec_covers_well(
    conn: sqlite3.Connection, ticker: str, *, today: date | None = None
) -> tuple[bool, int, bool]:
    """(covered, fact_count, has_disagreement) for one ticker.

    `covered=True` means EDGAR is carrying this name's statement facts well
    enough that the FMP statement endpoints can be skipped this pass: at
    least one `sec_xbrl` fact within SEC_FRESHNESS_DAYS AND zero unresolved
    `source_disagreement` validation issues for the ticker.
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=SEC_FRESHNESS_DAYS)).isoformat()
    fact_count = conn.execute(
        "SELECT COUNT(*) FROM financial_facts "
        "WHERE ticker = ? AND extracted_by = 'sec_xbrl' AND date(period_end) >= date(?)",
        (ticker.upper(), cutoff),
    ).fetchone()[0]
    disagreement_count = conn.execute(
        "SELECT COUNT(*) FROM validation_issues "
        "WHERE ticker = ? AND rule = 'source_disagreement' AND resolved_at IS NULL",
        (ticker.upper(),),
    ).fetchone()[0]
    covered = int(fact_count) > 0 and int(disagreement_count) == 0
    return covered, int(fact_count), int(disagreement_count) > 0


def build_manifest(
    conn: sqlite3.Connection, tickers: list[tuple[str, str]], *, today: date | None = None
) -> tuple[list[dict[str, str]], list[TickerDecision]]:
    """Return (manifest entries for save_fmp_data --manifest, per-ticker decisions).

    Every non-statement endpoint in `per_ticker_jobs` is always included —
    FMP is their only source, and this is the comprehensive periodic pass.
    Only the 6 `SEC_OVERLAP_KEYS` jobs are ever skipped, and only per-ticker
    when `sec_covers_well` says EDGAR has this name handled.
    """
    manifest: list[dict[str, str]] = []
    decisions: list[TickerDecision] = []
    for ticker, list_type in tickers:
        covered, fact_count, has_disagreement = sec_covers_well(conn, ticker, today=today)
        jobs = cast(
            "list[dict[str, object]]", fmp_save.per_ticker_jobs(ticker, list_type=list_type)
        )
        included = [
            j for j in jobs if not (covered and (j["path"], j["period"]) in SEC_OVERLAP_KEYS)
        ]
        manifest.extend(
            {
                "ticker": ticker,
                "endpoint": cast("str", j["path"]),
                "period": cast("str", j["period"]),
            }
            for j in included
        )
        decisions.append(
            TickerDecision(
                ticker=ticker,
                list_type=list_type,
                sec_covered=covered,
                sec_fact_count=fact_count,
                has_disagreement=has_disagreement,
                jobs_total=len(jobs),
                jobs_included=len(included),
                jobs_skipped_via_edgar=len(jobs) - len(included),
            )
        )
    return manifest, decisions


def _resolve_tickers(conn: sqlite3.Connection, explicit: list[str] | None) -> list[tuple[str, str]]:
    companies = _tracked_for_user(
        conn, list_types=frozenset(ListType) if explicit is not None else ANALYZED_LIST_TYPES
    )
    eligible = {c.ticker.upper(): c.list_type.value for c in companies}
    if explicit is not None:
        requested = list(dict.fromkeys(t.upper() for t in explicit))
        if not requested or any(t not in eligible for t in requested):
            raise ValueError("--tickers must name eligible tracked companies for the current user")
        return [(t, eligible[t]) for t in requested]
    return list(eligible.items())


def _run_fetch(
    manifest_path: Path,
    max_calls: int,
    repo_root: Path,
    db_path: Path,
    tickers: list[tuple[str, str]],
    receipt_path: Path,
    lock_proof: str,
) -> int:
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "save_fmp_data.py"),
        "--repo-root",
        str(repo_root),
        "--db",
        str(db_path),
        "--tickers",
        ",".join(t for t, _ in tickers),
        "--work-receipts",
        str(receipt_path),
        "--manifest",
        str(manifest_path),
        "--max-calls",
        str(max_calls),
    ]
    proc = subprocess.run(
        cmd,
        check=False,
        env={
            **os.environ,
            "EARNINGS_SUMMARY_DB_PATH": str(db_path),
            "EARNINGS_SUMMARY_JOB_LOCK_PROOF": lock_proof,
        },
    )
    return proc.returncode


def _reconcile_fetch_receipts(
    receipt_path: Path,
    manifest: list[dict[str, str]],
    repo_root: Path,
    started_at: datetime,
) -> dict[str, int | str]:
    """Verify exact attempted work and captured bytes; never infer completeness."""
    try:
        payload: object = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("FMP work receipts are missing or invalid") from exc
    if not isinstance(payload, list):
        raise ValueError("FMP work receipts must be a list")
    try:
        receipts = [
            fmp_save.FmpWorkReceipt.model_validate(row) for row in cast("list[object]", payload)
        ]
    except ValueError as exc:
        raise ValueError("FMP work receipts violate the typed contract") from exc
    expected = {(r["ticker"], r["endpoint"], r["period"]) for r in manifest}
    keys = [(r.ticker, r.endpoint, r.period) for r in receipts]
    if len(expected) != len(manifest) or len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("FMP receipts do not match the exact manifest")
    observed_at = datetime.now(UTC).replace(tzinfo=None)
    catalog = {
        ticker: {
            (job["path"], job["period"]): job["suffix"] for job in fmp_save.per_ticker_jobs(ticker)
        }
        for ticker in {row["ticker"] for row in manifest}
    }
    successes = 0
    empty = 0
    for receipt in receipts:
        suffix = catalog[receipt.ticker].get((receipt.endpoint, receipt.period))
        if suffix is None:
            raise ValueError("FMP receipt coordinates are absent from the endpoint catalog")
        if not started_at <= receipt.captured_at <= observed_at:
            raise ValueError("FMP work receipt capture time is outside this attempt")
        if receipt.outcome is fmp_save.FmpWorkReceiptOutcome.EMPTY:
            if receipt.http_status != 200:
                raise ValueError("FMP empty receipt requires HTTP 200")
            empty += 1
            continue
        if receipt.outcome is not fmp_save.FmpWorkReceiptOutcome.SUCCESS:
            raise ValueError(f"FMP work did not succeed: {receipt.outcome.value}")
        assert receipt.file_path is not None
        path = (repo_root / receipt.file_path).resolve()
        try:
            path.relative_to((repo_root / "data/historical/fmp").resolve())
        except ValueError as exc:
            raise ValueError("FMP receipt path is outside the retained FMP directory") from exc
        if (
            path
            != (repo_root / "data/historical/fmp" / f"{receipt.ticker}_{suffix}.json").resolve()
        ):
            raise ValueError("FMP receipt file does not match its catalog coordinates")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError("Retained FMP receipt bytes are unavailable") from exc
        if digest != receipt.content_sha256:
            raise ValueError("FMP receipt content hash does not match retained bytes")
        successes += 1
    return {
        "jobs_succeeded": successes,
        "jobs_unavailable": empty,
        "acquisition_status": "partial_unavailable" if empty else "manifest_verified",
        "archive_completeness": "unverified",
    }


def _run_index(conn: sqlite3.Connection, tickers: list[tuple[str, str]], repo_root: Path) -> int:
    total = 0
    for ticker, _ in tickers:
        total += index_fmp_files_for_ticker(conn, ticker, repo_root)
    return total


def _run_extract(
    repo_root: Path, db_path: Path, tickers: list[tuple[str, str]], *, lock_proof: str | None = None
) -> int:
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "extract_facts.py"),
        "--repo-root",
        str(repo_root),
        "--db",
        str(db_path),
        "--tickers",
        ",".join(t for t, _ in tickers),
    ]
    child_env = {**os.environ, "EARNINGS_SUMMARY_DB_PATH": str(db_path)}
    if lock_proof is not None:
        child_env["EARNINGS_SUMMARY_JOB_LOCK_PROOF"] = lock_proof
    proc = subprocess.run(cmd, check=False, env=child_env)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", help="Comma-separated ticker subset (default: portfolio+watchlist+evaluation)"
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually fetch (save_fmp_data), index (fmp_doc_index), and extract "
        "(extract_facts --tickers). Without this flag, only the manifest + report are written.",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Bounded HTTP-attempt budget for this run. REQUIRED with --apply.",
    )
    args = parser.parse_args()

    if args.apply and args.max_calls is None:
        parser.error("--apply requires --max-calls (a ~6-monthly paid window must be bounded)")

    if args.max_calls is not None and args.max_calls <= 0:
        parser.error("--max-calls must be positive")
    try:
        db_path = require_db_path(args.db)
    except (RuntimeError, FileNotFoundError) as exc:
        parser.error(str(exc))
    repo_root = args.repo_root.expanduser().resolve()
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        explicit = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers is not None
            else None
        )
        try:
            tickers = _resolve_tickers(conn, explicit)
        except ValueError as exc:
            parser.error(str(exc))
        if not tickers:
            print(json.dumps({"tickers": 0, "status": "no_eligible_tickers"}))
            return 0
        manifest, decisions = build_manifest(conn, tickers)

        backpop_dir = repo_root / ".tmp" / "fmp_backpop"
        backpop_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        manifest_path = backpop_dir / f"manifest_{stamp}.json"
        receipt_path = manifest_path.with_name("receipts_" + manifest_path.name)
        manifest_path.write_text(json.dumps({"items": manifest}, indent=2), encoding="utf-8")

        total_jobs = sum(d.jobs_total for d in decisions)
        total_included = sum(d.jobs_included for d in decisions)
        total_skipped = sum(d.jobs_skipped_via_edgar for d in decisions)
        summary = {
            "tickers": len(decisions),
            "sec_covered_tickers": sum(1 for d in decisions if d.sec_covered),
            "tickers_with_disagreement": sum(1 for d in decisions if d.has_disagreement),
            "jobs_total_catalog": total_jobs,
            "jobs_included_in_manifest": total_included,
            "jobs_skipped_via_edgar": total_skipped,
            "manifest_path": str(manifest_path),
            "applied": False,
            "apply_requested": args.apply,
            "db_path": str(db_path),
            "repo_root": str(repo_root),
            "ticker_scope": [t for t, _ in tickers],
            "receipt_path": str(receipt_path),
        }
        print(json.dumps(summary, indent=2))
        if not args.apply:
            print(
                "\nDry run — no network calls made. Re-run with --apply --max-calls N to fetch.",
                file=sys.stderr,
            )
            return 0

        assert args.max_calls is not None
        # The retained-artifact owner spans capture and dependent processing.
        # The child validates this claim instead of acquiring the same lock.
        with JobLock(repo_root, "fmp-backpop", ["fmp-fetch-artifacts"], wait_s=0) as artifact_lock:
            started_at = datetime.now(UTC).replace(tzinfo=None)
            fetch_rc = _run_fetch(
                manifest_path,
                args.max_calls,
                repo_root,
                db_path,
                tickers,
                receipt_path,
                artifact_lock.inheritance_proof(),
            )
            if fetch_rc:
                print(
                    json.dumps(
                        {
                            "fetch_exit_code": fetch_rc,
                            "status": "fetch_failed",
                            "acquisition_status": "unverified",
                        }
                    )
                )
                return fetch_rc
            try:
                acquisition = _reconcile_fetch_receipts(
                    receipt_path, manifest, repo_root, started_at
                )
            except (OSError, ValueError) as exc:
                print(
                    json.dumps(
                        {
                            "status": "fetch_reconciliation_failed",
                            "reason": str(exc)
                            if isinstance(exc, ValueError)
                            else "receipt_unavailable",
                            "acquisition_status": "unverified",
                            "receipt_path": str(receipt_path),
                        }
                    )
                )
                return 1
            with hold_run_lock(db_path, owner="fmp_backpop"):
                writer = open_db(db_path)
                try:
                    indexed = _run_index(writer, tickers, repo_root)
                except Exception:
                    writer.rollback()
                    print(
                        json.dumps({"status": "index_failed", "acquisition_status": "unverified"})
                    )
                    raise
                finally:
                    writer.close()
                extract_rc = _run_extract(
                    repo_root, db_path, tickers, lock_proof=artifact_lock.inheritance_proof()
                )
        print(
            json.dumps(
                {
                    "fetch_exit_code": fetch_rc,
                    "applied": True,
                    "status": "extracted" if extract_rc == 0 else "extract_failed",
                    "documents_indexed": indexed,
                    "extract_exit_code": extract_rc,
                    **acquisition,
                    "index_scope": "all_retained_documents_for_exact_tickers",
                },
                indent=2,
            )
        )
        return extract_rc

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
