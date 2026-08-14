"""Refresh dirty LLM artifacts. Drives the self-update loop.

The Phase 1.6 + 0043 trigger chain flips llm_artifacts.dirty=1 whenever
upstream facts change. The 0035 expires_at chain additionally surfaces any
artifact whose TTL (per-purpose, defined in llm_artifact_store._DEFAULT_TTL_DAYS)
has elapsed. This script:

  1. Reads up to --limit dirty/expired artifacts via drain_dirty().
  2. Groups by (ticker, purpose) and prints a refresh manifest (default mode).
  3. With --execute: invokes the purpose-specific regenerator script for each
     (ticker, command) pair, subprocess-isolated. Legacy native JSON caches are
     then schema-validated and projected through llm_artifact_store.upsert;
     native or direct-store producers must satisfy the exact queued rows.
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
from log_redact import redact  # noqa: E402
from native_artifact_projection import (  # noqa: E402
    PROJECTABLE_NATIVE_PURPOSES,
    NativeArtifactProjectionError,
    project_native_artifact,
)
from runtime.python_process import managed_python_argv  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("refresh_dirty_artifacts")


# Per-purpose regenerator command. Values are argv-list templates; the first
# `{ticker}` token (if present) is substituted with the dirty artifact's
# ticker at execution time. The dict serves two roles:
#   * Manifest mode prints the command line so an operator can pipe it.
#   * Execute mode subprocess-invokes the command with timeout + capture.
#
# Report-side purposes use `build_artifacts.py --regenerate-purpose`, which
# exits after the named producer and does not render or invoke unrelated LLM
# sections. Standalone extractors remain preferred where they already exist.
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
        "--regenerate-purpose",
        "bear_case",
    ],
    "qa_topics": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--regenerate-purpose",
        "qa_topics",
    ],
    "saydo_filter": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--regenerate-purpose",
        "saydo_filter",
    ],
    "valuation_basis": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--regenerate-purpose",
        "valuation_basis",
    ],
    "exec_comp_alignment": [
        "python",
        "execution/build_artifacts.py",
        "--ticker",
        "{ticker}",
        "--regenerate-purpose",
        "exec_comp_alignment",
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


@dataclass(frozen=True, slots=True)
class _ArtifactObligation:
    """One exact dirty/expired row a regenerator must resolve."""

    artifact_id: int
    purpose: str
    scope: str
    fiscal_period: str | None
    was_expired: bool


@dataclass(slots=True)
class _PendingJob:
    """One subprocess invocation and every queued row it is expected to resolve."""

    ticker: str
    purposes: list[str]
    obligations: list[_ArtifactObligation]
    argv: list[str]
    queued_at: datetime


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


@dataclass(frozen=True, slots=True)
class _CostAvailable:
    cost_usd: float


@dataclass(frozen=True, slots=True)
class _CostUnavailable:
    error: str


_CostQueryResult = _CostAvailable | _CostUnavailable


def _accrued_cost_usd(db_path: Path, since: datetime) -> _CostQueryResult:
    """Read spend since `since`; unavailability is explicit and never zero-valued."""
    if not db_path.exists():
        return _CostUnavailable("portfolio database is missing")
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "llm_calls" not in tables:
                return _CostUnavailable("llm_calls cost ledger is missing")
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_estimate_usd), 0.0) FROM llm_calls WHERE called_at >= ?",
                (since.isoformat(),),
            ).fetchone()
            return _CostAvailable(float(row[0]) if row and row[0] is not None else 0.0)
        finally:
            conn.close()
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        return _CostUnavailable(redact(f"{type(exc).__name__}: {exc}"))


def _build_pending_jobs(
    artifacts: list[Artifact],
    *,
    queued_at: datetime,
) -> list[_PendingJob]:
    """Dedupe subprocesses while retaining every exact row they must refresh."""
    jobs: list[_PendingJob] = []
    job_indexes: dict[tuple[str, tuple[str, ...]], int] = {}
    classified_without_job: set[tuple[str, str]] = set()
    for art in sorted(artifacts, key=lambda item: (item.ticker or "", item.purpose, item.id)):
        ticker = art.ticker
        if not ticker:
            continue
        purpose = art.purpose
        template = _PURPOSE_TO_REGENERATOR.get(purpose)
        if template is None:
            classification_key = (ticker, purpose)
            if classification_key in classified_without_job:
                continue
            classified_without_job.add(classification_key)
            if purpose in _DAILY_SCAN_PURPOSES:
                log.info({"event": "refreshed_by_daily_scan", "purpose": purpose, "ticker": ticker})
            else:
                log.warning({"event": "no_regenerator", "purpose": purpose, "ticker": ticker})
            continue
        argv = [tok.replace("{ticker}", ticker) for tok in template]
        key = (ticker, tuple(argv))
        expires_at = art.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        obligation = _ArtifactObligation(
            artifact_id=art.id,
            purpose=purpose,
            scope=art.scope,
            fiscal_period=art.fiscal_period,
            was_expired=expires_at is not None and expires_at < queued_at,
        )
        existing_index = job_indexes.get(key)
        if existing_index is not None:
            existing = jobs[existing_index]
            if purpose not in existing.purposes:
                existing.purposes.append(purpose)
            existing.obligations.append(obligation)
            continue
        job_indexes[key] = len(jobs)
        jobs.append(
            _PendingJob(
                ticker=ticker,
                purposes=[purpose],
                obligations=[obligation],
                argv=argv,
                queued_at=queued_at,
            )
        )
    return jobs


def _managed_job_argv(job: _PendingJob, cwd: Path) -> list[str]:
    """Route an internal Python regenerator through the verified SQLite bootstrap."""
    if len(job.argv) < 2 or job.argv[0].casefold() not in {"python", "python.exe"}:
        raise ValueError("dirty-artifact regenerators must be repository Python scripts")
    return managed_python_argv(cwd, job.argv[1], *job.argv[2:])


def _run_subprocess(job: _PendingJob, cwd: Path) -> dict[str, object]:
    """Invoke one regenerator. Captures exit + tail stderr; never raises."""
    try:
        proc = subprocess.run(
            _managed_job_argv(job, cwd),
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
            "purpose": job.purposes[0],
            "purposes": job.purposes,
            "exit_code": -1,
            "error": f"timeout after {_SUBPROCESS_TIMEOUT_S}s",
        }
    except OSError as exc:
        return {
            "ticker": job.ticker,
            "purpose": job.purposes[0],
            "purposes": job.purposes,
            "exit_code": -1,
            "error": redact(f"spawn failed: {exc}"),
        }
    stderr_tail = ""
    if proc.returncode != 0 and proc.stderr:
        stderr_tail = redact(proc.stderr)[-400:]
    return {
        "ticker": job.ticker,
        "purpose": job.purposes[0],
        "purposes": job.purposes,
        "exit_code": proc.returncode,
        "stderr_tail": stderr_tail,
    }


def _project_native_job(
    job: _PendingJob,
    *,
    repo_root: Path,
    db_path: Path,
) -> str | None:
    """Project every freshly written native cache owned by this exact job."""
    grouped: dict[tuple[str, str, str | None], list[int]] = defaultdict(list)
    for obligation in job.obligations:
        if obligation.purpose in PROJECTABLE_NATIVE_PURPOSES:
            grouped[(obligation.purpose, obligation.scope, obligation.fiscal_period)].append(
                obligation.artifact_id
            )
    for (purpose, scope, fiscal_period), obligation_ids in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], item[0][1], item[0][2] or ""),
    ):
        try:
            project_native_artifact(
                ticker=job.ticker,
                purpose=purpose,
                repo_root=repo_root,
                db_path=db_path,
                queued_at=job.queued_at,
                scope=scope,
                fiscal_period=fiscal_period,
                obligation_ids=tuple(sorted(obligation_ids)),
            )
        except NativeArtifactProjectionError as exc:
            return redact(f"{purpose}: {exc}")
    return None


@dataclass(frozen=True, slots=True)
class _ProgressCheck:
    """Result of verifying exact queued rows after a child exits zero."""

    satisfied: bool
    unresolved_artifact_ids: tuple[int, ...]
    error: str | None = None


def _check_job_progress(
    job: _PendingJob,
    *,
    db_path: Path,
    checked_at: datetime,
) -> _ProgressCheck:
    """Fail closed unless every exact queued row was cleared or superseded."""
    obligation_by_id = {item.artifact_id: item for item in job.obligations}
    if not obligation_by_id:
        return _ProgressCheck(False, (), "job has no artifact obligations")
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.row_factory = sqlite3.Row
            rows = [
                row
                for artifact_id in sorted(obligation_by_id)
                if (
                    row := conn.execute(
                        """
                        SELECT id, superseded_by_id, dirty, expires_at
                        FROM llm_artifacts
                        WHERE id = ?
                        """,
                        (artifact_id,),
                    ).fetchone()
                )
                is not None
            ]
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return _ProgressCheck(
            False,
            tuple(sorted(obligation_by_id)),
            redact(f"{type(exc).__name__}: {exc}"),
        )

    row_by_id = {int(row["id"]): row for row in rows}
    unresolved: list[int] = []
    for artifact_id, obligation in sorted(obligation_by_id.items()):
        row = row_by_id.get(artifact_id)
        if row is None:
            unresolved.append(artifact_id)
            continue
        if row["superseded_by_id"] is not None:
            continue
        if bool(row["dirty"]):
            unresolved.append(artifact_id)
            continue
        if not obligation.was_expired:
            continue
        raw_expires_at = row["expires_at"]
        if raw_expires_at is None:
            unresolved.append(artifact_id)
            continue
        try:
            expires_at = datetime.fromisoformat(str(raw_expires_at))
        except ValueError:
            unresolved.append(artifact_id)
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= checked_at:
            unresolved.append(artifact_id)

    unresolved_ids = tuple(unresolved)
    return _ProgressCheck(not unresolved_ids, unresolved_ids)


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


def _halt_for_unavailable_cost_ledger(
    unavailable: _CostUnavailable,
    *,
    ran: int,
    failed: int,
    no_progress: int,
    deferred: int,
    cap_usd: float,
) -> int:
    """Emit one redacted failure receipt when spend enforcement is unavailable."""
    safe_error = redact(unavailable.error)
    log.warning({"event": "drain_cost_ledger_unavailable", "error": safe_error})
    print("halted: cost ledger unavailable; no further regenerators were started")
    print(
        "drain receipt: "
        + json.dumps(
            {
                "status": "partial_failure_cost_ledger" if failed else "blocked_cost_ledger",
                "run": ran,
                "failed": failed,
                "no_progress": no_progress,
                "deferred": deferred,
                "accrued_cost_usd": None,
                "cap_usd": cap_usd,
                "error": safe_error,
            },
            sort_keys=True,
        )
    )
    return 1


def _execute_jobs(
    jobs: list[_PendingJob],
    *,
    repo_root: Path,
    db_path: Path,
    max_cost_usd: float,
    run_started_at: datetime,
) -> int:
    """Drain jobs serially; queue progress and spend accounting both fail closed."""
    ran = 0
    failed = 0
    no_progress = 0
    for idx, job in enumerate(jobs):
        cost_result = _accrued_cost_usd(db_path, since=run_started_at)
        if isinstance(cost_result, _CostUnavailable):
            return _halt_for_unavailable_cost_ledger(
                cost_result,
                ran=ran,
                failed=failed,
                no_progress=no_progress,
                deferred=len(jobs) - idx,
                cap_usd=max_cost_usd,
            )
        accrued = cost_result.cost_usd
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
                        "status": ("partial_failure_cost_cap" if failed else "deferred_cost_cap"),
                        "run": ran,
                        "failed": failed,
                        "no_progress": no_progress,
                        "deferred": remaining,
                        "accrued_cost_usd": round(accrued, 4),
                        "cap_usd": max_cost_usd,
                    },
                    sort_keys=True,
                )
            )
            return 1 if failed else 0
        log.info(
            {
                "event": "drain_invoke",
                "ticker": job.ticker,
                "purpose": job.purposes[0],
                "purposes": job.purposes,
                "argv": _managed_job_argv(job, repo_root),
                "accrued_cost_usd": round(accrued, 4),
            }
        )
        result = _run_subprocess(job, cwd=repo_root)
        exit_code = result.get("exit_code")
        if exit_code != 0:
            failed += 1
            log.warning({"event": "drain_subprocess_failed", **result})
        else:
            projection_error = _project_native_job(
                job,
                repo_root=repo_root,
                db_path=db_path,
            )
            progress = _check_job_progress(
                job,
                db_path=db_path,
                checked_at=datetime.now(UTC),
            )
            if projection_error is not None or not progress.satisfied:
                failed += 1
                no_progress += 1
                log.warning(
                    {
                        "event": "drain_no_progress",
                        **result,
                        "obligation_ids": [item.artifact_id for item in job.obligations],
                        "unresolved_artifact_ids": list(progress.unresolved_artifact_ids),
                        "verification_error": projection_error or progress.error,
                    }
                )
            else:
                log.info({"event": "drain_subprocess_ok", **result})
        ran += 1

    final_cost_result = _accrued_cost_usd(db_path, since=run_started_at)
    if isinstance(final_cost_result, _CostUnavailable):
        return _halt_for_unavailable_cost_ledger(
            final_cost_result,
            ran=ran,
            failed=failed,
            no_progress=no_progress,
            deferred=0,
            cap_usd=max_cost_usd,
        )
    final_cost = final_cost_result.cost_usd
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
                "no_progress": no_progress,
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
    queue_checked_at = datetime.now(UTC)

    artifacts = drain_dirty(limit=args.limit, db_path=db_path, now=queue_checked_at)
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

    jobs = _build_pending_jobs(artifacts, queued_at=queue_checked_at)
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
