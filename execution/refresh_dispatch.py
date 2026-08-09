"""Per-ticker refresh dispatcher with stale-only mode.

Owns the canonical 6-step full-refresh chain (FMP fetch → transcripts →
IR-doc summarize → KPI extraction → SayDo pairs → report rebuild) as a
single Python entry point the dashboard and managed runtime can spawn and
stream output from.

Two modes:

    --mode full   : run every standard refresh step
    --mode stale  : skip FMP if pulled in the last 7 days. Cheaper steps
                    (transcripts / IR-summarize / KPI / SayDo / build)
                    always run because they're either idempotent skips
                    on a file-exists check or low-cost LLM calls.

Output is line-buffered to stdout so an SSE forwarder can pipe it through
to a browser drawer without buffering surprises:

    [dispatch] ticker=NU mode=stale stale_fmp_days=7
    [dispatch] step=fmp action=skip reason=fresh last_pulled=2026-05-11T01:02:14
    [dispatch] step=transcripts action=start
    ... (subprocess stdout) ...
    [dispatch] step=transcripts action=end rc=0
    ...
    [dispatch] all_done rc=0

Usage:
    python execution/refresh_dispatch.py --ticker NU
    python execution/refresh_dispatch.py --ticker NU --mode full
    python execution/refresh_dispatch.py --ticker NU --stale-fmp-days 3
    python execution/refresh_dispatch.py --ticker NU --plan-only   # print plan, no work
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.cadence_policy import STATEMENT_STALE_DAYS  # noqa: E402
from runtime.python_process import managed_python_argv  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

Mode = Literal["full", "stale"]

# Every step the dispatcher knows how to run, in canonical execution order.
# `build_report` stays last so it picks up whatever the earlier steps produced.
STEP_NAMES: tuple[str, ...] = (
    "fmp",
    "transcripts",
    "process_ir_docs",
    "news",
    "extract_kpis",
    "saydo",
    "dcf",
    "thesis_eval",
    "build_report",
)
# Default chain when no explicit --steps is given (the standard six-step refresh).
# news / dcf / thesis_eval are opt-in via --steps so a routine refresh isn't
# silently made heavier / more expensive.
DEFAULT_STEPS: tuple[str, ...] = (
    "fmp",
    "transcripts",
    "process_ir_docs",
    "extract_kpis",
    "saydo",
    "build_report",
)


def resolve_steps(steps: list[str] | None = None, skip: list[str] | None = None) -> list[str]:
    """Resolve which steps to run, in canonical order.

    `steps` selects an explicit subset (unknown names are dropped); when falsy,
    the default chain runs. `skip` removes names from whatever was selected.
    """
    if steps:
        wanted = {s.strip() for s in steps if s.strip()}
        selected = [s for s in STEP_NAMES if s in wanted]
    else:
        selected = list(DEFAULT_STEPS)
    if skip:
        skipset = {s.strip() for s in skip if s.strip()}
        selected = [s for s in selected if s not in skipset]
    return selected


# ---------------------------------------------------------------------------
# Plan: pure decision logic about what to run / skip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    ticker: str
    mode: Mode
    skip_fmp: bool
    skip_fmp_reason: str | None  # e.g. "fresh last_pulled=2026-05-11T01:02:14"
    force_budget_bypass: bool = False  # pass --force-budget-bypass to build_artifacts
    steps: tuple[str, ...] = ()  # resolved steps to run, in canonical order
    force: bool = False  # run FMP even if fresh (override the stale-skip)


def build_plan(
    *,
    ticker: str,
    mode: Mode,
    db_path: Path,
    stale_fmp_days: int = STATEMENT_STALE_DAYS,
    now: datetime | None = None,
    force_budget_bypass: bool = False,
    force: bool = False,
    steps: list[str] | None = None,
    skip_steps: list[str] | None = None,
) -> Plan:
    """Pure decision: given DB state, return what to run / skip.

    `now` is injectable for deterministic testing. `force` overrides the
    stale-skip so the FMP step runs regardless of freshness. `steps` /
    `skip_steps` narrow the chain (see `resolve_steps`).
    """
    resolved = tuple(resolve_steps(steps, skip_steps))
    if mode == "full" or force:
        return Plan(
            ticker=ticker,
            mode=mode,
            skip_fmp=False,
            skip_fmp_reason=None,
            force_budget_bypass=force_budget_bypass,
            steps=resolved,
            force=force,
        )

    now = now or datetime.now(UTC)
    last_pulled, reason = _check_fmp_freshness(
        db_path=db_path, ticker=ticker, stale_days=stale_fmp_days, now=now
    )
    return Plan(
        ticker=ticker,
        mode=mode,
        skip_fmp=last_pulled is not None,
        skip_fmp_reason=reason,
        force_budget_bypass=force_budget_bypass,
        steps=resolved,
        force=force,
    )


def _check_fmp_freshness(
    *, db_path: Path, ticker: str, stale_days: int, now: datetime
) -> tuple[datetime | None, str | None]:
    """Return (last_pulled_dt, reason_if_skip).

    If MAX(fmp_endpoint_status.last_pulled) is within `stale_days`, returns
    that timestamp + a "fresh" reason string. Otherwise (no rows or older
    than the threshold), returns (None, None) signaling "run the FMP step."
    """
    if not db_path.exists():
        return (None, None)
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT MAX(last_pulled) AS last_pulled FROM fmp_endpoint_status WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None or row["last_pulled"] is None:
        return (None, None)

    try:
        # SQLite stores naive ISO strings; treat as UTC for the freshness window.
        last_pulled = datetime.fromisoformat(str(row["last_pulled"]).replace(" ", "T"))
        if last_pulled.tzinfo is None:
            last_pulled = last_pulled.replace(tzinfo=UTC)
    except ValueError:
        return (None, None)

    threshold = now - timedelta(days=stale_days)
    if last_pulled >= threshold:
        return (last_pulled, f"fresh last_pulled={last_pulled.isoformat()}")
    return (None, None)


# ---------------------------------------------------------------------------
# Execution: run subprocesses per the plan
# ---------------------------------------------------------------------------


def execute(
    plan: Plan,
    *,
    project_root: Path = PROJECT_ROOT,
    out=sys.stdout,
    runner=None,
) -> int:
    """Run the chain per `plan`. Returns aggregate exit code (0 if all steps OK).

    `runner` is injectable for tests — default is `subprocess.run`. The signature
    matches `subprocess.run`: receives argv list, returns object with `.returncode`.
    """
    runner = runner or _default_runner
    steps = plan.steps or DEFAULT_STEPS
    _emit(out, f"[dispatch] ticker={plan.ticker} mode={plan.mode} steps={','.join(steps)}")

    # argv per step name — built once; only the selected ones run.
    builders = {
        "fmp": _argv_fmp(project_root, plan.ticker),
        "transcripts": _argv_transcripts(project_root, plan.ticker),
        "process_ir_docs": _argv_process_ir(project_root, plan.ticker),
        "news": _argv_news(project_root, plan.ticker),
        "extract_kpis": _argv_extract_kpis(project_root, plan.ticker),
        "saydo": _argv_saydo(project_root, plan.ticker),
        "dcf": _argv_dcf(project_root, plan.ticker),
        "thesis_eval": _argv_thesis_eval(project_root, plan.ticker),
        "build_report": _argv_build(
            project_root, plan.ticker, force_budget_bypass=plan.force_budget_bypass
        ),
    }

    rc_total = 0
    for name in steps:
        if name == "fmp" and plan.skip_fmp:
            _emit(out, f"[dispatch] step=fmp action=skip reason={plan.skip_fmp_reason}")
            continue
        _emit(out, f"[dispatch] step={name} action=start")
        result = runner(builders[name], out=out)
        rc = getattr(result, "returncode", 1)
        _emit(out, f"[dispatch] step={name} action=end rc={rc}")
        if rc != 0:
            rc_total = rc  # remember last failure; independent later steps still run

    _emit(out, f"[dispatch] all_done rc={rc_total}")
    return rc_total


def _default_runner(argv: list[str], *, out):
    """Run subprocess and forward stdout/stderr to `out`, line-buffered."""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        out.write(line)
        out.flush()
    proc.wait()
    return proc


def _argv_fmp(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "fetch_fmp_historical_data.py",
        "--ticker",
        ticker,
        "--limit",
        "12",
    )


def _argv_transcripts(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "backfill_transcripts.py",
        "--ticker",
        ticker,
    )


def _argv_process_ir(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "process_ir_documents.py",
        "--ticker",
        ticker,
    )


def _argv_extract_kpis(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "extract_kpis_from_summaries.py",
        "--ticker",
        ticker,
        "--source",
        "earnings",
        "--repo-root",
        str(project_root),
    )


def _argv_saydo(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "build_saydo_pairs.py",
        "--ticker",
        ticker,
        "--repo-root",
        str(project_root),
    )


def _argv_build(project_root: Path, ticker: str, *, force_budget_bypass: bool = False) -> list[str]:
    argv = managed_python_argv(
        project_root,
        project_root / "execution" / "build_artifacts.py",
        "--ticker",
        ticker,
        "--renderer",
        "workspace",
        "--enable-llm",
        "--repo-root",
        str(project_root),
    )
    if force_budget_bypass:
        argv.append("--force-budget-bypass")
    return argv


def _argv_news(project_root: Path, ticker: str) -> list[str]:
    # Default source=auto (FMP, WebSearch+Opus fallback per ticker on refusal).
    return managed_python_argv(
        project_root,
        project_root / "execution" / "fetch_news.py",
        "--tickers",
        ticker,
    )


def _argv_dcf(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "refresh_dcf.py",
        "--ticker",
        ticker,
    )


def _argv_thesis_eval(project_root: Path, ticker: str) -> list[str]:
    return managed_python_argv(
        project_root,
        project_root / "execution" / "run_thesis_evaluator.py",
        "--ticker",
        ticker,
    )


def _emit(out, line: str) -> None:
    out.write(line + "\n")
    out.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--mode", choices=("full", "stale"), default="stale")
    parser.add_argument("--stale-fmp-days", type=int, default=7)
    parser.add_argument(
        "--steps",
        help="Comma-separated subset of steps to run (default: the standard chain). "
        "Available: " + ",".join(STEP_NAMES),
    )
    parser.add_argument(
        "--skip-step",
        action="append",
        default=[],
        metavar="STEP",
        help="A step to skip; repeatable.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run the FMP step even if data is fresh (override the stale-skip).",
    )
    parser.add_argument("--plan-only", action="store_true", help="Print the plan as JSON and exit.")
    parser.add_argument(
        "--force-budget-bypass",
        action="store_true",
        help="Pass --force-budget-bypass through to build_artifacts (ignore LLM budget caps).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "data" / "portfolio.db",
        help="Path to portfolio.db (default: %(default)s)",
    )
    args = parser.parse_args()

    steps = [s for s in args.steps.split(",")] if args.steps else None
    plan = build_plan(
        ticker=args.ticker.upper(),
        mode=args.mode,
        db_path=args.db,
        stale_fmp_days=args.stale_fmp_days,
        force_budget_bypass=args.force_budget_bypass,
        force=args.force,
        steps=steps,
        skip_steps=args.skip_step or None,
    )

    if args.plan_only:
        print(json.dumps(asdict(plan), indent=2))
        return 0

    return execute(plan, project_root=PROJECT_ROOT)


if __name__ == "__main__":
    sys.exit(main())
