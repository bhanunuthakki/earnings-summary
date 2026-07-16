"""Expire stale research-queue wonderings so pending counts stay trustworthy.

A ``proposed`` research task nobody ran for ``--days`` days is dead weight: it
inflates the Ledger's pending badges forever (the 2026-07 adversarial pass
found 14 tasks parked since the 2026-07-02 demo day), which teaches the owner
the counts are meaningless. This sweeps them to ``status='expired'`` — they
drop out of the open-wonderings list but the row (and its note backlink)
survives for the record.

Dry-run by default; ``--apply`` writes. Idempotent: an expired task never
matches again.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clock import now_naive_utc
from research.proposals import set_task_status
from user_state._db import open_conn


def _log(event: dict[str, object]) -> None:
    print(json.dumps(event), file=sys.stderr)


def expire_stale_tasks(*, days: int, apply: bool, db_path: Path | None = None) -> list[int]:
    """Return the ids of stale ``proposed`` tasks (and expire them when
    ``apply``). Staleness = created more than ``days`` days ago and never run.
    ``created_at`` is the repo's naive-UTC ISO text, so a lexicographic compare
    against a naive-UTC cutoff is exact."""
    cutoff = (now_naive_utc() - timedelta(days=days)).isoformat()
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, ticker, claim, created_at FROM research_tasks "
            "WHERE status = 'proposed' AND created_at < ? ORDER BY id",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        _log(
            {
                "event": "stale_research_task",
                "task_id": int(row["id"]),
                "ticker": row["ticker"],
                "created_at": str(row["created_at"]),
                "claim": str(row["claim"] or "")[:120],
                "action": "expire" if apply else "would_expire",
            }
        )
        if apply:
            set_task_status(int(row["id"]), "expired", db_path=db_path)
    return [int(row["id"]) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=10, help="expire 'proposed' tasks older than this (default 10)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the expiry (default is a dry run)"
    )
    parser.add_argument("--db-path", type=Path, default=None, help="override the portfolio DB")
    args = parser.parse_args()
    ids = expire_stale_tasks(days=args.days, apply=args.apply, db_path=args.db_path)
    mode = "expired" if args.apply else "would expire (dry run; pass --apply)"
    print(f"{len(ids)} stale proposed research task(s) {mode}: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
