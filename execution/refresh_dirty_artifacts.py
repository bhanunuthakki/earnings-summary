"""Refresh dirty LLM artifacts. Drives the self-update loop.

The Phase 1.6 + 0043 trigger chain flips llm_artifacts.dirty=1 whenever
upstream facts change. The 0035 expires_at chain additionally surfaces any
artifact whose TTL (per-purpose, defined in llm_artifact_store._DEFAULT_TTL_DAYS)
has elapsed. This script:

  1. Reads up to --limit dirty/expired artifacts via drain_dirty().
  2. Groups by (ticker, purpose) and prints a refresh manifest (default mode).
  3. With --execute: invokes the purpose-specific regenerator script for each
     (ticker, command) pair, subprocess-isolated. Each regenerator writes a
     fresh artifact via llm_artifact_store.upsert (which clears dirty=0 and
     stamps a new expires_at), so the queue drains as the run progresses.
  4. With --max-cost-usd N: queries the llm_calls ledger for cost accrued
     since this run started; halts gracefully (exit 0) when the cap is
     reached so daily cron never blows the budget.

Designed for cron use:
  python execution/refresh_dirty_artifacts.py --execute --max-cost-usd 5

The --max-cost-usd safeguard short-circuits if cumulative cost in the run
exceeds the cap — protects the subscription / API spend.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_artifact_store import Artifact, drain_dirty  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("refresh_dirty_artifacts")


# Per-purpose regenerator command. Values are argv-list templates; the first
# `{ticker}` token (if present) is substituted with the dirty artifact's
# ticker at execution time. The dict serves two roles:
#   * Manifest mode prints the command line so an operator can pipe it.
#   * Execute mode subprocess-invokes the command with timeout + capture.
#
# Most purposes fold into `build_artifacts.py --enable-llm`, since the brief
# builder calls llm_client (which writes the artifact via upsert as a side
# effect of generating the section). Standalone extractors are preferred
# where they exist because they're cheaper per call than rebuilding the
# whole brief.
#
# If a purpose is missing from this mapping, the drain logs a warning and
# skips that artifact rather than crashing — graceful degradation as new
# purposes are added upstream.
_PURPOSE_TO_REGENERATOR: dict[str, list[str]] = {
    "bear_case": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--enable-llm",
    ],
    "qa_topics": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--enable-llm",
    ],
    "saydo_filter": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--enable-llm",
    ],
    "valuation_basis": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--enable-llm",
    ],
    "exec_comp_alignment": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--enable-llm",
    ],
    "company_description": [
        "python",
        "execution/extract_company_description.py",
        "--ticker",
        "{ticker}",
        "--refresh",
    ],
    "filing_intelligence": [
        "python",
        "execution/analyze_filing_intelligence.py",
        "--ticker",
        "{ticker}",
        "--refresh",
    ],
    "saydo_pair": [
        "python",
        "execution/build_saydo_pairs.py",
        "--ticker",
        "{ticker}",
        "--refresh",
    ],
    # Investment Decision Card (PRD §8.1, P1.1). generate_card is already
    # input-sha cache-keyed, so no --refresh/--force flag is needed here — a
    # dirty artifact means the inputs changed, which is exactly what
    # invalidates the cache key.
    "investment_decision_card": [
        "python",
        "execution/build_investment_decision_card.py",
        "--ticker",
        "{ticker}",
    ],
}


# Trigger/news-side LLM caches have NO standalone drain regenerator: recomputing
# them is a side effect of the daily trigger scan / news fetch (which already
# honor the dirty flag), and running those here would also fire alerts. So the
# drain classifies them as "refreshed by the daily scan" rather than warning
# "no_regenerator" (which reads like a missing-config bug). Keep in lockstep with
# the trigger/news family in llm_artifact_store.FACT_DEPENDENT_PURPOSES.
_DAILY_SCAN_PURPOSES: frozenset[str] = frozenset(
    {
        "earnings_tone_diff",
        "kpi_inflection_context",
        "saydo_due_context",
        "material_news_classification",
        "news_structuring",
    }
)


# Per-subprocess wall-clock cap. The brief builder typically runs in 60-120s
# per ticker with --enable-llm; 300s gives 2x+ headroom and keeps a stuck
# call from monopolizing the cron window.
_SUBPROCESS_TIMEOUT_S = 300


@dataclass(slots=True)
class _PendingJob:
    """One subprocess invocation to be run during --execute mode."""

    ticker: str
    purpose: str
    argv: list[str]


def _aggregate_breakdown(
    artifacts: list[Artifact],
) -> list[tuple[str, str, int]]:
    """Bucket Artifact rows into (ticker, purpose, count) for the manifest.

    Rows with no ticker (synthesis-scope artifacts) are filtered — the
    regenerator map is keyed on ticker substitution and there's nothing
    sensible to dispatch for portfolio-wide artifacts here.
    """
    counts: Counter[tuple[str, str]] = Counter()
    for art in artifacts:
        if not art.ticker:
            continue
        counts[(art.ticker, art.purpose)] += 1
    return [(ticker, purpose, count) for (ticker, purpose), count in sorted(counts.items())]


def _accrued_cost_usd(db_path: Path, since: datetime) -> float:
    """Sum of llm_calls.cost_estimate_usd for rows since `since`.

    Best-effort — returns 0.0 if the DB or table is missing. Matches the
    column / window pattern in execution/show_llm_spend.py so the cap and
    the dashboard always read the same number.
    """
    if not db_path.exists():
        return 0.0
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "llm_calls" not in tables:
                return 0.0
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_estimate_usd), 0.0) FROM llm_calls WHERE called_at >= ?",
                (since.isoformat(),),
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning({"event": "cost_query_failed", "error": str(exc)})
        return 0.0


def _build_pending_jobs(
    breakdown: list[tuple[str, str, int]],
) -> list[_PendingJob]:
    """Dedupe (ticker, argv) pairs from the dirty breakdown.

    One build_artifacts run regenerates multiple purposes for a ticker, so
    we collapse duplicates to avoid invoking the same subprocess twice in
    one drain. Unmapped purposes are logged + skipped here (the manifest
    print already warned, but skip is the load-bearing behavior).
    """
    seen: set[tuple[str, tuple[str, ...]]] = set()
    jobs: list[_PendingJob] = []
    for ticker, purpose, _count in breakdown:
        if not ticker:
            continue
        template = _PURPOSE_TO_REGENERATOR.get(purpose)
        if template is None:
            if purpose in _DAILY_SCAN_PURPOSES:
                # Expected: recomputed by the daily trigger scan / news fetch,
                # not the drain. Informational, not a missing-config warning.
                log.info({"event": "refreshed_by_daily_scan", "purpose": purpose, "ticker": ticker})
            else:
                log.warning({"event": "no_regenerator", "purpose": purpose, "ticker": ticker})
            continue
        argv = [tok.replace("{ticker}", ticker) for tok in template]
        key = (ticker, tuple(argv))
        if key in seen:
            continue
        seen.add(key)
        jobs.append(_PendingJob(ticker=ticker, purpose=purpose, argv=argv))
    return jobs


def _run_subprocess(job: _PendingJob, cwd: Path) -> dict[str, object]:
    """Invoke one regenerator. Captures exit + tail stderr; never raises."""
    try:
        proc = subprocess.run(
            job.argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ticker": job.ticker,
            "purpose": job.purpose,
            "exit_code": -1,
            "error": f"timeout after {_SUBPROCESS_TIMEOUT_S}s",
        }
    except OSError as exc:
        return {
            "ticker": job.ticker,
            "purpose": job.purpose,
            "exit_code": -1,
            "error": f"spawn failed: {exc}",
        }
    stderr_tail = ""
    if proc.returncode != 0 and proc.stderr:
        stderr_tail = proc.stderr[-400:]
    return {
        "ticker": job.ticker,
        "purpose": job.purpose,
        "exit_code": proc.returncode,
        "stderr_tail": stderr_tail,
    }


def _print_manifest(breakdown: list[tuple[str, str, int]], total: int) -> None:
    """Render the human-readable manifest to stdout. Same format the
    operator-piped runner has consumed since Phase 8."""
    by_ticker: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ticker, purpose, _count in breakdown:
        template = _PURPOSE_TO_REGENERATOR.get(purpose)
        if template is None:
            # Daily-scan purposes are not drain-regenerated, so they simply do
            # not appear in the drain manifest; only a genuinely unmapped
            # brief-side purpose is a config gap worth warning about.
            if purpose not in _DAILY_SCAN_PURPOSES:
                log.warning({"event": "no_regenerator", "purpose": purpose})
            continue
        if not ticker:
            continue
        argv = [tok.replace("{ticker}", ticker) for tok in template]
        by_ticker[ticker].append((purpose, " ".join(argv)))

    print(f"\n=== Dirty artifact refresh manifest ({total} rows) ===\n")
    for ticker, items in sorted(by_ticker.items()):
        purposes = sorted({p for p, _ in items})
        clis = sorted({c for _, c in items})
        print(f"# {ticker} ({', '.join(purposes)})")
        for c in clis:
            print(f"  {c}")
        print()


def _execute_jobs(
    jobs: list[_PendingJob],
    *,
    repo_root: Path,
    db_path: Path,
    max_cost_usd: float,
    run_started_at: datetime,
) -> int:
    """Drain `jobs` serially, halting if accrued cost crosses the cap.

    Exit semantics:
      * Cap hit: print "halted" line, return 0 (graceful — cron isn't
        supposed to alarm on this).
      * Job failures: logged, NOT raised. The drain continues so a single
        broken regenerator doesn't poison the rest of the queue.
      * All jobs complete: print summary, return 0.
    """
    ran = 0
    failed = 0
    for idx, job in enumerate(jobs):
        accrued = _accrued_cost_usd(db_path, since=run_started_at)
        if accrued >= max_cost_usd:
            remaining = len(jobs) - idx
            print(
                f"halted: cost cap reached at ${accrued:.2f}, {remaining} artifact(s) unprocessed"
            )
            log.info(
                {
                    "event": "drain_halted_cost_cap",
                    "accrued_cost_usd": accrued,
                    "cap_usd": max_cost_usd,
                    "unprocessed": remaining,
                }
            )
            print(
                "drain receipt: "
                + json.dumps(
                    {
                        "status": "deferred_cost_cap",
                        "run": ran,
                        "failed": failed,
                        "deferred": remaining,
                        "accrued_cost_usd": round(accrued, 4),
                        "cap_usd": max_cost_usd,
                    },
                    sort_keys=True,
                )
            )
            return 0
        log.info(
            {
                "event": "drain_invoke",
                "ticker": job.ticker,
                "purpose": job.purpose,
                "argv": job.argv,
                "accrued_cost_usd": round(accrued, 4),
            }
        )
        result = _run_subprocess(job, cwd=repo_root)
        exit_code = result.get("exit_code")
        if exit_code != 0:
            failed += 1
            log.warning({"event": "drain_subprocess_failed", **result})
        else:
            log.info({"event": "drain_subprocess_ok", **result})
        ran += 1

    final_cost = _accrued_cost_usd(db_path, since=run_started_at)
    print(
        f"drain complete: {ran} job(s) run ({failed} failed); "
        f"accrued ${final_cost:.2f} (cap ${max_cost_usd:.2f})"
    )
    status = "partial_failure" if failed else "complete"
    print(
        "drain receipt: "
        + json.dumps(
            {
                "status": status,
                "run": ran,
                "failed": failed,
                "deferred": 0,
                "accrued_cost_usd": round(final_cost, 4),
                "cap_usd": max_cost_usd,
            },
            sort_keys=True,
        )
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Max dirty rows to inspect.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--manifest-only",
        action="store_true",
        help=(
            "Print the refresh manifest and exit (default behavior). Don't "
            "invoke any LLM calls. Kept as an explicit flag for cron scripts "
            "that want to assert the default mode."
        ),
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Actually invoke the per-purpose regenerator scripts. Serial; "
            "subprocess-isolated; 300s wall-clock per call. Pairs with "
            "--max-cost-usd to bound spend."
        ),
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=5.0,
        help=(
            "Cap on cumulative LLM spend (USD) during this drain. Only "
            "consulted when --execute is set. Default $5.00 — sized for "
            "daily cron. Halts gracefully (exit 0) when the cap is reached."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = args.repo_root / "data" / "portfolio.db"

    artifacts = drain_dirty(limit=args.limit, db_path=db_path)
    breakdown = _aggregate_breakdown(artifacts)
    total = sum(count for _t, _p, count in breakdown)
    if total == 0:
        log.info({"event": "drain_idle", "total_dirty": 0})
        print("No dirty artifacts. Pipeline is fresh.")
        return 0

    log.info({"event": "drain_start", "total_dirty": total, "execute": args.execute})

    _print_manifest(breakdown, total)

    if not args.execute:
        print("# Re-run with --execute to fire the regenerators. Manifest-only run.")
        return 0

    jobs = _build_pending_jobs(breakdown)
    if not jobs:
        print("# No mappable jobs (every purpose was unmapped). Manifest-only effect.")
        return 0

    run_started_at = datetime.now(UTC)
    return _execute_jobs(
        jobs,
        repo_root=args.repo_root,
        db_path=db_path,
        max_cost_usd=args.max_cost_usd,
        run_started_at=run_started_at,
    )


if __name__ == "__main__":
    sys.exit(main())
