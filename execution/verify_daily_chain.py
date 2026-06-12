"""Dead-man post-flight: verify the morning pipeline ran and succeeded today.

Queries ``ingestion_runs`` for today's ``run_morning_pipeline`` directive,
writes ``.tmp/daily_chain_status.json`` with the verdict, and exits:

  0  at least one run completed with status=ok today
  1  no run found today, or the latest run failed / is still in_progress
  2  could not read the database

Called at the end of ``run_morning_pipeline.py`` so the status file is always
fresh after a pipeline run. Can also be run standalone (e.g. from a monitoring
script) to answer "did the morning pipeline run today?".
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
STATUS_FILE = PROJECT_ROOT / ".tmp" / "daily_chain_status.json"

# Directives that must run every day to constitute a healthy chain.
_REQUIRED_DIRECTIVES = ("run_morning_pipeline",)


def check(db_path: Path = DB_PATH) -> dict[str, object]:
    """Return a status dict describing today's chain run(s).

    Keys:
      verdict     "ok" | "missing" | "failed" | "db_error"
      runs_today  count of run_morning_pipeline rows started today
      latest_*    fields from the most recent row (absent when missing)
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cur = conn.execute(
                "SELECT run_id, started_at, ended_at, status, error_summary "
                "FROM ingestion_runs "
                "WHERE directive = 'run_morning_pipeline' "
                "  AND started_at >= ? "
                "ORDER BY started_at DESC",
                (today_start,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return {"verdict": "db_error", "error": str(exc), "runs_today": 0}

    if not rows:
        return {"verdict": "missing", "runs_today": 0}

    latest = rows[0]
    verdict = "ok" if latest["status"] == "ok" else "failed"
    return {
        "verdict": verdict,
        "runs_today": len(rows),
        "latest_run_id": latest["run_id"],
        "latest_status": latest["status"],
        "latest_started_at": str(latest["started_at"]),
        "latest_ended_at": str(latest["ended_at"]) if latest["ended_at"] else None,
        "error_summary": latest["error_summary"],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument(
        "--status-file",
        type=Path,
        default=STATUS_FILE,
        help="Path to write the JSON status artifact (default: .tmp/daily_chain_status.json).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress human-readable output; only exit code and status file matter.",
    )
    args = parser.parse_args(argv)

    result = check(args.db_path)

    args.status_file.parent.mkdir(parents=True, exist_ok=True)
    args.status_file.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    if not args.quiet:
        verdict = result.get("verdict", "unknown")
        if verdict == "ok":
            run_id = result.get("latest_run_id", "")
            print(f"✓  Morning pipeline ran successfully today ({run_id})\n")
        elif verdict == "missing":
            print(
                "✗  Morning pipeline has NOT run today — check the cron registration.\n",
                file=sys.stderr,
            )
        elif verdict == "db_error":
            print(
                f"✗  Could not read DB: {result.get('error', '')}\n",
                file=sys.stderr,
            )
        else:
            err = result.get("error_summary", "")
            run_id = result.get("latest_run_id", "")
            print(
                f"✗  Morning pipeline ran but FAILED today — {err}\n   Run ID: {run_id}\n",
                file=sys.stderr,
            )

    verdict = result.get("verdict", "unknown")
    if verdict == "db_error":
        return 2
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
