"""Build approved discovery candidates into evaluation briefs (P5.4).

The execution half of "Discovery: queue, never auto-build": this script
only ever runs because the owner clicked Build (dashboard) or issued
``/discovery build`` (chat) — the approval IS the budget gate. Per ticker,
sequentially:

  1. candidate status → ``building`` (the queue shows progress live)
  2. tracked_companies.list_type → ``evaluation`` (direct UPDATE — NOT
     db.track_company, whose upsert fires its own fire-and-forget onboard
     subprocess; this script owns the sequencing) + processing_tier P2
  3. ``execution/onboard_ticker.py --ticker T --industry-template auto``
     (FMP fetch + quarterly refresh + transcript backfill)
  4. ``execution/build_artifacts.py --ticker T --flavor evaluation
     --enable-llm`` (~25 min + LLM spend per name — the cost the queue
     gated)
  5. status → ``built`` on success; on failure → back to ``queued`` (the
     owner re-triggers after looking) and the run CONTINUES with the next
     name — one bad ticker must not strand a bulk approval.

Everything prints to stdout so the dashboard's jobs SSE streams it
verbatim (the dispatch registry merges stderr into stdout).

Usage:
    python execution/discovery_build.py --tickers NVDA,WDC
    python execution/discovery_build.py --tickers WDC --user-id bhanu
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from discovery.store import list_candidates, set_status  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# A bulk approval is deliberately capped: each build is ~25 minutes + LLM
# spend, so ten names is already a ~4-hour, multi-dollar commitment — a
# bigger batch is almost certainly a misclick, not a decision.
MAX_BUILD_BATCH = 10

Runner = Callable[[list[str]], int]


def _default_runner(argv: list[str]) -> int:
    """Run a child step inheriting stdout/stderr (the job's SSE pipe)."""
    return subprocess.run(argv, check=False).returncode


def _emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def _candidate_id(db_path: Path, user_id: str, ticker: str) -> int | None:
    rows = list_candidates(user_id=user_id, status=None, db_path=db_path)
    for c in rows:
        if c.ticker == ticker:
            return c.id
    return None


def _promote_to_evaluation(db_path: Path, user_id: str, ticker: str) -> bool:
    """Restore and enter evaluation/P2, except an existing portfolio stays P1."""
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        row = conn.execute(
            "SELECT list_type FROM tracked_companies WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        ).fetchone()
        if row is None:
            return False
        if str(row[0]) == "portfolio":
            conn.execute(
                "UPDATE tracked_companies SET archived_at = NULL, processing_tier = 'P1', "
                "brief_dirty = 1 WHERE user_id = ? AND ticker = ?",
                (user_id, ticker),
            )
            conn.commit()
            return True
        conn.execute(
            "UPDATE tracked_companies SET list_type = 'evaluation', processing_tier = 'P2', "
            "brief_dirty = 1, archived_at = NULL "
            "WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        _emit("discovery_build_promote_failed", ticker=ticker, error=str(exc))
        return False
    finally:
        conn.close()


def build_one(
    ticker: str,
    *,
    repo_root: Path,
    user_id: str = DEFAULT_USER_ID,
    runner: Runner = _default_runner,
) -> bool:
    """One candidate through the full ladder. Returns True on a built brief."""
    symbol = ticker.strip().upper()
    db_path = repo_root / "data" / "portfolio.db"
    cand_id = _candidate_id(db_path, user_id, symbol)
    if cand_id is not None:
        set_status(cand_id, "building", db_path=db_path)
    _emit("discovery_build_start", ticker=symbol)

    ok = _promote_to_evaluation(db_path, user_id, symbol)
    steps: list[list[str]] = [
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(repo_root / "execution" / "onboard_ticker.py"),
            "--ticker",
            symbol,
            "--industry-template",
            "auto",
        ],
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(repo_root / "execution" / "build_artifacts.py"),
            "--ticker",
            symbol,
            "--flavor",
            "evaluation",
            "--enable-llm",
            "--repo-root",
            str(repo_root),
        ],
        # Investment Decision Card (PRD §8.1, P1.1) — every new evaluation
        # build ends with a card or an explicit failed step (never silently
        # skipped). Runs last so the deterministic inputs it reads (thesis,
        # DCF, candidate fit) are already fresh from the steps above.
        [
            *managed_python_prefix(PROJECT_ROOT),
            str(repo_root / "execution" / "build_investment_decision_card.py"),
            "--ticker",
            symbol,
            "--repo-root",
            str(repo_root),
        ],
    ]
    if ok:
        for argv in steps:
            _emit("discovery_build_step", ticker=symbol, argv=argv[1:3])
            code = runner(argv)
            if code != 0:
                _emit("discovery_build_step_failed", ticker=symbol, exit_code=code)
                ok = False
                break
    if cand_id is not None:
        set_status(cand_id, "built" if ok else "queued", db_path=db_path)
    _emit("discovery_build_done", ticker=symbol, ok=ok)
    return ok


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build approved discovery candidates")
    parser.add_argument("--tickers", required=True, help=f"comma-separated, max {MAX_BUILD_BATCH}")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    args = parser.parse_args(argv)

    tickers = [t.strip().upper() for t in str(args.tickers).split(",") if t.strip()]
    if not tickers:
        print("no tickers given", file=sys.stderr)
        return 2
    if len(tickers) > MAX_BUILD_BATCH:
        print(f"refusing {len(tickers)} tickers (max {MAX_BUILD_BATCH} per run)", file=sys.stderr)
        return 2

    repo_root = args.repo_root.resolve()
    failures = 0
    for ticker in tickers:
        if not build_one(ticker, repo_root=repo_root, user_id=args.user_id):
            failures += 1
    _emit("discovery_build_batch_done", total=len(tickers), failures=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
