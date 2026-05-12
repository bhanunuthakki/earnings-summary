"""Daily worker: drain the brief_dirty queue, refresh DCFs, regenerate briefs.

Runs after the earnings_calendar_watcher (which populates `expected_earnings`)
and the existing FMP/SEC fetch crons (which write to financial_facts via the
canonical extractors). The fact-table writes flip
`tracked_companies.brief_dirty = 1` via the SQL triggers from migration 0026.

For each dirty ticker, this worker runs the synthesize → publish slice of
the canonical pipeline:

  thesis evaluator  → writes a fresh thesis_evaluations row
  match_commitments → fills Say-Do outcomes for periods that just landed
  refresh_dcf       → re-reads the DCF workbook, recomputes PV / over-under
  build_artifacts   → regenerates the brief (HTML / MD / JSON / xlsx)

Then clears brief_dirty. Each step is run as a subprocess so a failure in
one ticker doesn't poison the worker's Python state.

Usage:
    python execution/daily_fetch_and_brief.py                   # all dirty tickers
    python execution/daily_fetch_and_brief.py --ticker META     # force-refresh one
    python execution/daily_fetch_and_brief.py --enable-llm      # populate §8/§9 LLM sections
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        tickers = _resolve_tickers(conn, args)

    if not tickers:
        print(json.dumps({"event": "no_dirty_tickers"}))
        return 0

    results: list[dict[str, object]] = []
    for ticker in tickers:
        result = _refresh_one_ticker(ticker, repo_root, db_path, args)
        results.append(result)

    print(
        json.dumps(
            {"started_at": datetime.now(timezone.utc).isoformat(), "tickers": results}, indent=2
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ticker", help="Force-refresh a specific ticker, ignoring brief_dirty")
    g.add_argument(
        "--all-tracked", action="store_true", help="Refresh every tracked ticker (heavy)"
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    p.add_argument(
        "--enable-llm",
        action="store_true",
        help="Pass --enable-llm to build_artifacts (populates §8 recent_developments + §9 bear_case)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If > 0, cap the number of tickers refreshed this run (useful for testing).",
    )
    return p.parse_args()


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    """Tickers to refresh this run. Explicit > all-tracked > brief_dirty queue."""
    cursor = conn.cursor()
    if args.ticker:
        return [args.ticker.upper()]
    if args.all_tracked:
        cursor.execute(
            f"SELECT ticker FROM tracked_companies "
            f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} "
            f"AND (archived_at IS NULL) "
            f"ORDER BY ticker"
        )
    else:
        cursor.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE brief_dirty = 1 AND (archived_at IS NULL) "
            "ORDER BY ticker"
        )
    rows = cursor.fetchall()
    tickers = [str(r["ticker"]).upper() for r in rows]
    if args.limit > 0:
        tickers = tickers[: args.limit]
    return tickers


def _refresh_one_ticker(
    ticker: str, repo_root: Path, db_path: Path, args: argparse.Namespace
) -> dict[str, object]:
    """Run the synth-and-publish chain for one ticker; clear dirty flag on success."""
    stages: list[dict[str, object]] = []

    stages.append(
        _run_step(
            "thesis_evaluator",
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "run_thesis_evaluator.py"),
                "--ticker",
                ticker,
            ],
            cwd=repo_root,
        )
    )
    stages.append(
        _run_step(
            "match_commitments",
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "match_commitments.py"),
                "--ticker",
                ticker,
            ],
            cwd=repo_root,
        )
    )
    stages.append(
        _run_step(
            "refresh_dcf",
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "refresh_dcf.py"),
                "--ticker",
                ticker,
                "--repo-root",
                str(repo_root),
            ],
            cwd=repo_root,
        )
    )
    build_cmd = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "build_artifacts.py"),
        "--ticker",
        ticker,
        "--repo-root",
        str(repo_root),
        "--allow-untracked",
    ]
    if args.enable_llm:
        build_cmd.append("--enable-llm")
    stages.append(_run_step("build_artifacts", build_cmd, cwd=repo_root))

    overall_ok = all(s["exit_code"] == 0 for s in stages)
    if overall_ok:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE tracked_companies SET brief_dirty = 0 WHERE ticker = ?",
                (ticker,),
            )
            conn.commit()
    return {"ticker": ticker, "status": "ok" if overall_ok else "failed", "stages": stages}


def _run_step(name: str, cmd: list[str], cwd: Path) -> dict[str, object]:
    """Run one step. Captures exit code + truncated stderr; never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "exit_code": -1, "error": "timeout after 600s"}
    except OSError as e:
        return {"name": name, "exit_code": -1, "error": f"spawn failed: {e}"}
    return {
        "name": name,
        "exit_code": proc.returncode,
        "stderr_tail": _tail(proc.stderr, 400) if proc.returncode != 0 else "",
    }


def _tail(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return "…" + s[-n:]


if __name__ == "__main__":
    raise SystemExit(main())
