"""Expire stale research-queue wonderings so pending counts stay trustworthy.

B7 (2026-07-19 program overhaul) rewrote this from a single blanket
"older than N days -> expired" sweep into a TWO-PHASE expiry that never
drops a wondering silently:

  1. The weekly packet's "expiring research" section
     (``pipeline.weekly_packet._send_expiring_research``) surfaces every
     about-to-expire 'proposed' task as its own card (Run (~$x) / -> session
     / Drop) and stamps ``packeted_at`` + bumps ``unanswered_weeks`` on the
     task row each week it stays proposed.
  2. THIS sweep only expires a task when EITHER:
       (a) it has gone through a SECOND unanswered packet week
           (``unanswered_weeks >= 2`` in the task's repurposed meta JSON —
           see ``research.proposals`` module docstring for the ``run_id``
           repurposing), or
       (b) it has NEVER been packeted at all and is older than ``--days``
           (the safety net for a stretch where the weekly-packet job itself
           was down/misconfigured — without this floor a task could sit
           forever if the packet never ran).
     A packet-ACKNOWLEDGED drop (the owner tapping "Drop" on the card) expires
     a task IMMEDIATELY via ``capture.research_notify``'s ``rx:drop``
     dispatch — by the time this sweep runs, that task is already
     ``status='expired'`` and excluded by the ``status='proposed'`` filter.

Before this rewrite: the 2026-07 adversarial pass found 13 of 17 wonderings
ever detected sat as ``proposed`` and silently expired with the old blanket
sweep — nobody ever SAW them before they vanished. ``--resurrect-expired``
is the one-time burst-triage aid for those 13: a READ-ONLY preview (never
mutates status — an unsupervised bulk un-expire of 13 rows with no per-row
review is exactly the kind of write this flag deliberately does NOT do; run
it, read the list, handle each manually) of every currently-``expired`` task,
formatted as a single packet-shaped section.

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
from research.proposals import ResearchTask, list_tasks, set_task_status
from schema_compat import require_current_for_write
from user_state._db import open_conn


def _log(event: dict[str, object]) -> None:
    print(json.dumps(event), file=sys.stderr)


def _expiring(tasks: list[ResearchTask], *, cutoff: str) -> list[tuple[ResearchTask, str]]:
    """Pure decision function (no I/O): which 'proposed' tasks are due to
    expire, and why. Split out from :func:`expire_stale_tasks` so the
    two-phase rule is unit-testable without a DB round trip."""
    out: list[tuple[ResearchTask, str]] = []
    for task in tasks:
        weeks_raw = task.meta.get("unanswered_weeks")
        weeks = int(weeks_raw) if isinstance(weeks_raw, int) else 0
        packeted_at = task.meta.get("packeted_at")
        if weeks >= 2:
            out.append((task, "second_unanswered_week"))
        elif not packeted_at and task.created_at and task.created_at < cutoff:
            out.append((task, "never_packeted_stale"))
    return out


def expire_stale_tasks(*, days: int, apply: bool, db_path: Path | None = None) -> list[int]:
    """Return the ids of tasks due to expire (and expire them when
    ``apply``). ``created_at`` is the repo's naive-UTC ISO text, so a
    lexicographic compare against a naive-UTC cutoff is exact (matches the
    store convention used throughout ``research.proposals``)."""
    if apply:
        preflight_conn = open_conn(db_path)
        try:
            require_current_for_write(preflight_conn)
        finally:
            preflight_conn.close()
    cutoff = (now_naive_utc() - timedelta(days=days)).isoformat()
    tasks = list_tasks(status="proposed", db_path=db_path)
    expiring = _expiring(tasks, cutoff=cutoff)
    for task, reason in expiring:
        _log(
            {
                "event": "stale_research_task",
                "task_id": task.id,
                "ticker": task.ticker,
                "created_at": task.created_at,
                "claim": task.claim[:120],
                "reason": reason,
                "action": "expire" if apply else "would_expire",
            }
        )
        if apply:
            set_task_status(task.id, "expired", db_path=db_path)
    return [task.id for task, _ in expiring]


def resurrect_expired_preview(*, db_path: Path | None = None) -> list[ResearchTask]:
    """The ``--resurrect-expired`` one-time burst-triage aid: every currently
    ``expired`` task, unfiltered. READ-ONLY — see module docstring for why
    this never mutates status."""
    return list_tasks(status="expired", db_path=db_path)


def render_resurrect_section(tasks: list[ResearchTask]) -> str:
    """Format the expired backlog as one packet-shaped section — the SAME
    'ticker - claim' head the live expiring-research cards use
    (``pipeline.weekly_packet.expiring_research_message_text``), so a burst
    triage session reads like an oversized version of the weekly card."""
    if not tasks:
        return "No expired research tasks on file."
    lines = [f"Expired research - {len(tasks)} for one-time burst triage:"]
    for task in tasks:
        head = f"{task.ticker + ' - ' if task.ticker else ''}{task.claim.strip()}"
        created = task.created_at[:10] if task.created_at else "?"
        lines.append(f"- #{task.id} {head} (created {created})")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=21,
        help=(
            "expire a NEVER-packeted 'proposed' task older than this many days "
            "(the safety-net floor; default 21 -- roughly 3 unwarned weeks). A "
            "task the weekly packet HAS surfaced expires on its own two-week "
            "unanswered rule instead, regardless of --days."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the expiry (default is a dry run)"
    )
    parser.add_argument("--db-path", type=Path, default=None, help="override the portfolio DB")
    parser.add_argument(
        "--resurrect-expired",
        action="store_true",
        help=(
            "READ-ONLY: print every currently-expired task as a one-time "
            "burst-triage packet section, then exit (never mutates status, "
            "and ignores --apply/--days)."
        ),
    )
    args = parser.parse_args()

    if args.resurrect_expired:
        tasks = resurrect_expired_preview(db_path=args.db_path)
        print(render_resurrect_section(tasks))
        return 0

    ids = expire_stale_tasks(days=args.days, apply=args.apply, db_path=args.db_path)
    mode = "expired" if args.apply else "would expire (dry run; pass --apply)"
    print(f"{len(ids)} stale proposed research task(s) {mode}: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
