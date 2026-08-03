"""Dead-man post-flight: verify the morning pipeline ran and succeeded today.

Queries ``ingestion_runs`` for today's ``run_morning_pipeline`` directive,
writes ``.tmp/daily_chain_status.json`` with the verdict, and exits:

  0  at least one run completed with status=ok today
  1  no run found today, or the latest run failed / is still in_progress
  2  could not read the database

Called at the end of ``run_morning_pipeline.py`` so the status file is always
fresh after a pipeline run. Can also be run standalone (e.g. from a monitoring
script) to answer "did the morning pipeline run today?".

It also carries the LLM cost ledger's dropped-write count (last 24h). Those
drops are best-effort losses the ledger cannot raise on, so without an active
surface they are visible only if someone opens the Cron Health tab. Folding
the count into this always-fresh, monitored status artifact — and printing a
``!!!`` marker when non-zero — makes them reach the same channels operators
already watch. The count never changes the exit code: a lost cost row is not
the same failure as the pipeline not running, and conflating them would muddy
the dead-man signal.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
STATUS_FILE = PROJECT_ROOT / ".tmp" / "daily_chain_status.json"

# Directives that must run every day to constitute a healthy chain.
_REQUIRED_DIRECTIVES = ("run_morning_pipeline",)


def _dropped_ledger_writes(db_path: Path) -> dict[str, object]:
    """Last-24h count of LLM cost-ledger rows the writer could not persist.

    Read from the durable counter beside the DB (``telemetry_health``). Best
    effort: a monitoring artifact must never be the thing that fails, so any
    problem reading the counter degrades to a zero count rather than raising.
    """
    try:
        from telemetry_health import DROPPED_LLM_LEDGER_WRITE, dropped_writes_since

        # The counter stamps UTC; use an aware cutoff so the 24h window is not
        # skewed by the machine's local offset (America/Los_Angeles here).
        dropped = dropped_writes_since(
            DROPPED_LLM_LEDGER_WRITE,
            db_path=db_path,
            since=datetime.now(UTC) - timedelta(hours=24),
        )
    except Exception:
        return {"dropped_ledger_writes_24h": 0, "dropped_ledger_last_error": None}
    if dropped is None:
        return {"dropped_ledger_writes_24h": 0, "dropped_ledger_last_error": None}
    return {
        "dropped_ledger_writes_24h": dropped.count,
        "dropped_ledger_last_error": dropped.last_error or None,
    }


def check(db_path: Path = DB_PATH) -> dict[str, object]:
    """Return a status dict describing today's chain run(s).

    Keys:
      verdict     "ok" | "missing" | "failed" | "db_error"
      runs_today  count of run_morning_pipeline rows started today
      latest_*    fields from the most recent row (absent when missing)
      dropped_ledger_writes_24h   count of lost LLM cost rows in the last 24h
      dropped_ledger_last_error   the most recent drop's error (or None)
    """
    dropped = _dropped_ledger_writes(db_path)
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
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
        return {"verdict": "db_error", "error": str(exc), "runs_today": 0, **dropped}

    if not rows:
        return {"verdict": "missing", "runs_today": 0, **dropped}

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
        **dropped,
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

    # The dropped-ledger alarm prints even under --quiet: the morning pipeline
    # calls this quietly, and suppressing the one line that says "cost rows are
    # being lost" would put it right back in the silent state this exists to
    # end. The `!!!` marker matches the pipeline's own failed-stage convention,
    # so a log scan surfaces it the same way.
    dropped_count = result.get("dropped_ledger_writes_24h", 0)
    if isinstance(dropped_count, int) and dropped_count > 0:
        last_error = result.get("dropped_ledger_last_error") or "(no detail)"
        sys.stderr.write(
            f"\n!!! [ledger_writes_dropped] {dropped_count} LLM cost row(s) lost in the "
            f"last 24h - most recent: {last_error}\n"
        )

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
