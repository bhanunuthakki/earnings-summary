"""Per-ticker refresh dispatcher with stale-only mode.

Wraps the 6-step chain from full_refresh.bat (FMP fetch → transcripts →
IR-doc summarize → KPI extraction → SayDo pairs → report rebuild) into a
single Python entry point the dashboard can spawn as a subprocess and
stream output from.

Two modes:

    --mode full   : run every step (matches full_refresh.bat)
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

Mode = Literal["full", "stale"]


# ---------------------------------------------------------------------------
# Plan: pure decision logic about what to run / skip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    ticker: str
    mode: Mode
    skip_fmp: bool
    skip_fmp_reason: str | None  # e.g. "fresh last_pulled=2026-05-11T01:02:14"


def build_plan(
    *,
    ticker: str,
    mode: Mode,
    db_path: Path,
    stale_fmp_days: int = 7,
    now: datetime | None = None,
) -> Plan:
    """Pure decision: given DB state, return what to skip.

    `now` is injectable for deterministic testing.
    """
    if mode == "full":
        return Plan(ticker=ticker, mode=mode, skip_fmp=False, skip_fmp_reason=None)

    now = now or datetime.now(UTC)
    last_pulled, reason = _check_fmp_freshness(
        db_path=db_path, ticker=ticker, stale_days=stale_fmp_days, now=now
    )
    return Plan(ticker=ticker, mode=mode, skip_fmp=last_pulled is not None, skip_fmp_reason=reason)


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
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT MAX(last_pulled) AS last_pulled FROM fmp_endpoint_status "
            "WHERE ticker = ?",
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
    _emit(out, f"[dispatch] ticker={plan.ticker} mode={plan.mode}")

    rc_total = 0
    steps = [
        ("fmp", _argv_fmp(project_root, plan.ticker), plan.skip_fmp, plan.skip_fmp_reason),
        ("transcripts", _argv_transcripts(project_root, plan.ticker), False, None),
        ("process_ir_docs", _argv_process_ir(project_root, plan.ticker), False, None),
        ("extract_kpis", _argv_extract_kpis(project_root, plan.ticker), False, None),
        ("saydo", _argv_saydo(project_root, plan.ticker), False, None),
        ("build_report", _argv_build(project_root, plan.ticker), False, None),
    ]

    for name, argv, skip, skip_reason in steps:
        if skip:
            _emit(out, f"[dispatch] step={name} action=skip reason={skip_reason}")
            continue
        _emit(out, f"[dispatch] step={name} action=start")
        result = runner(argv, out=out)
        rc = getattr(result, "returncode", 1)
        _emit(out, f"[dispatch] step={name} action=end rc={rc}")
        if rc != 0:
            rc_total = rc  # remember last failure; chain continues per full_refresh.bat semantics

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
    return [
        sys.executable,
        str(project_root / "execution" / "fetch_fmp_historical_data.py"),
        "--ticker", ticker, "--limit", "12",
    ]


def _argv_transcripts(project_root: Path, ticker: str) -> list[str]:
    return [
        sys.executable,
        str(project_root / "execution" / "backfill_transcripts.py"),
        "--ticker", ticker,
    ]


def _argv_process_ir(project_root: Path, ticker: str) -> list[str]:
    return [
        sys.executable,
        str(project_root / "execution" / "process_ir_documents.py"),
        "--ticker", ticker,
    ]


def _argv_extract_kpis(project_root: Path, ticker: str) -> list[str]:
    return [
        sys.executable,
        str(project_root / "execution" / "extract_kpis_from_summaries.py"),
        "--ticker", ticker,
        "--source", "earnings",
        "--repo-root", str(project_root),
    ]


def _argv_saydo(project_root: Path, ticker: str) -> list[str]:
    return [
        sys.executable,
        str(project_root / "execution" / "build_saydo_pairs.py"),
        "--ticker", ticker,
        "--repo-root", str(project_root),
    ]


def _argv_build(project_root: Path, ticker: str) -> list[str]:
    return [
        sys.executable,
        str(project_root / "execution" / "build_artifacts.py"),
        "--ticker", ticker,
        "--renderer", "workspace",
        "--enable-llm",
        "--repo-root", str(project_root),
    ]


def _emit(out, line: str) -> None:
    out.write(line + "\n")
    out.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--mode", choices=("full", "stale"), default="stale")
    parser.add_argument("--stale-fmp-days", type=int, default=7)
    parser.add_argument("--plan-only", action="store_true", help="Print the plan as JSON and exit.")
    parser.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "data" / "portfolio.db",
        help="Path to portfolio.db (default: %(default)s)",
    )
    args = parser.parse_args()

    plan = build_plan(
        ticker=args.ticker.upper(),
        mode=args.mode,
        db_path=args.db,
        stale_fmp_days=args.stale_fmp_days,
    )

    if args.plan_only:
        print(json.dumps(asdict(plan), indent=2))
        return 0

    return execute(plan, project_root=PROJECT_ROOT)


if __name__ == "__main__":
    sys.exit(main())
