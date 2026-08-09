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
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.python_process import managed_python_prefix  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import save_fmp_data as fmp_save  # noqa: E402

from pipeline.fmp_doc_index import index_fmp_files_for_ticker  # noqa: E402
from pipeline.queries import ANALYZED_LIST_TYPES, open_db  # noqa: E402
from pipeline.queries import tracked_companies_for_user as _tracked_for_user  # noqa: E402

BACKPOP_DIR = PROJECT_ROOT / ".tmp" / "fmp_backpop"

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
    if explicit:
        placeholders = ",".join("?" for _ in explicit)
        rows = conn.execute(
            f"SELECT ticker, list_type FROM tracked_companies WHERE ticker IN ({placeholders})",
            [t.upper() for t in explicit],
        ).fetchall()
        found = {cast("str", r[0]).upper(): cast("str", r[1]) for r in rows}
        return [(t.upper(), found.get(t.upper(), "none")) for t in explicit]
    companies = _tracked_for_user(conn, list_types=ANALYZED_LIST_TYPES)
    return [(c.ticker.upper(), c.list_type.value) for c in companies]


def _run_fetch(manifest_path: Path, max_calls: int) -> int:
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "save_fmp_data.py"),
        "--manifest",
        str(manifest_path),
        "--max-calls",
        str(max_calls),
    ]
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


def _run_index(conn: sqlite3.Connection, tickers: list[tuple[str, str]]) -> int:
    total = 0
    for ticker, _ in tickers:
        total += index_fmp_files_for_ticker(conn, ticker, PROJECT_ROOT)
    return total


def _run_extract() -> int:
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "extract_facts.py"),
        "--all",
    ]
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tickers", help="Comma-separated ticker subset (default: portfolio+watchlist+evaluation)"
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually fetch (save_fmp_data), index (fmp_doc_index), and extract "
        "(extract_facts --all). Without this flag, only the manifest + report are written.",
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

    conn = open_db(args.db)
    try:
        explicit = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers
            else None
        )
        tickers = _resolve_tickers(conn, explicit)
        manifest, decisions = build_manifest(conn, tickers)

        BACKPOP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        manifest_path = BACKPOP_DIR / f"manifest_{stamp}.json"
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
            "manifest_path": str(manifest_path.relative_to(PROJECT_ROOT)),
            "applied": args.apply,
        }
        print(json.dumps(summary, indent=2))
        if not args.apply:
            print(
                "\nDry run — no network calls made. Re-run with --apply --max-calls N to fetch.",
                file=sys.stderr,
            )
            return 0

        assert args.max_calls is not None
        fetch_rc = _run_fetch(manifest_path, args.max_calls)
        indexed = _run_index(conn, tickers)
        extract_rc = _run_extract()
        print(
            json.dumps(
                {
                    "fetch_exit_code": fetch_rc,
                    "documents_indexed": indexed,
                    "extract_exit_code": extract_rc,
                },
                indent=2,
            )
        )
        return fetch_rc or extract_rc
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
