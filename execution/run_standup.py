"""Proactive analyst standup — the initiating advisory rung (close_the_loops L9).

Watches four open loops (falsifiable decision conditions, stale journal
open-items, DCF assumption staleness, live position drift) and, when one trips,
composes a grounded, cited brief through the Ask engine, eval-gates it against
the ``ask_advisory_answer`` rubric (+ a standup ``--min-score`` floor), and —
if it clears the bar and the per-day / per-name rate limits — writes it into the
persistent "Analyst standup" ``ask_sessions`` thread the owner sees as a waiting
advisory message.

Wired as stage 1b of the morning pipeline (after the trigger sweep, so fresh
decision-condition state is in the DB); also runnable standalone:

    python execution/run_standup.py
    python execution/run_standup.py --dry-run            # compose+gate, no writes
    python execution/run_standup.py --max-per-day 1 --min-score 0.8
    python execution/run_standup.py --repo-root . --db-path /tmp/x.db

The two LLM legs (compose via ``claude -p``, the eval judge) make this a paid
rung; the rate limits + dedup keep it cheap, and ``--dry-run`` spends without
persisting. Exit 0 on a completed pass (including zero deliveries); exit 2 only
on a missing DB / unreadable rubric (configuration, not silence).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.rubric_judge import AUDIT_SPECS, load_rubric  # noqa: E402
from standup.compose import prod_events_fn  # noqa: E402
from standup.config import StandupConfig  # noqa: E402
from standup.run import run_standup  # noqa: E402

_PURPOSE = "ask_advisory_answer"


def _build_config(args: argparse.Namespace) -> StandupConfig:
    return StandupConfig(
        journal_stale_days=args.journal_stale_days,
        dcf_stale_days=args.dcf_stale_days,
        dcf_mispricing_pct=args.dcf_mispricing_pct,
        drift_min_pp=args.drift_min_pp,
        max_per_day=args.max_per_day,
        per_name_cooldown_days=args.cooldown_days,
        dedup_days=args.dedup_days,
        min_score=args.min_score,
        max_signals_considered=args.limit,
    )


def _force_utf8_stdio() -> None:
    """Composed briefs carry en/em dashes, arrows, and · separators. On Windows
    the default cp1252 stdout raises UnicodeEncodeError on those, which would
    make the morning-pipeline stage exit non-zero (counted as failed) AFTER a
    brief was already delivered. Force UTF-8 with errors='replace' so printing
    the brief never crashes the rung (mirrors the claude_cli.py snippet note)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--user-id", default=None, help="Defaults to identity.DEFAULT_USER_ID.")
    parser.add_argument("--dry-run", action="store_true", help="Compose + gate, persist nothing.")
    # rate limits / eval bar
    parser.add_argument("--max-per-day", type=int, default=3)
    parser.add_argument("--cooldown-days", type=int, default=3)
    parser.add_argument("--dedup-days", type=int, default=7)
    parser.add_argument("--min-score", type=float, default=0.75)
    parser.add_argument("--limit", type=int, default=25, help="Max signals composed per run.")
    # detection thresholds
    parser.add_argument("--journal-stale-days", type=int, default=30)
    parser.add_argument("--dcf-stale-days", type=int, default=45)
    parser.add_argument("--dcf-mispricing-pct", type=float, default=20.0)
    parser.add_argument("--drift-min-pp", type=float, default=3.0)
    args = parser.parse_args(argv)

    import db
    from identity import DEFAULT_USER_ID

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2
    # Sync the global so the compose + judge LLM cost rows land in THIS DB's
    # ledger (mirrors run_triggers.resolve_db_path).
    db.set_db_path(db_path)

    try:
        rubric = load_rubric(PROJECT_ROOT / AUDIT_SPECS[_PURPOSE].rubric_relpath, purpose=_PURPOSE)
    except ValueError as exc:
        sys.stderr.write(f"FATAL: rubric unloadable: {exc}\n")
        return 2

    report = run_standup(
        db_path=db_path,
        repo_root=repo_root,
        events_fn=prod_events_fn(db_path, repo_root),
        rubric=rubric,
        now=datetime.now(UTC).replace(tzinfo=None),
        user_id=args.user_id or DEFAULT_USER_ID,
        config=_build_config(args),
        dry_run=args.dry_run,
    )

    sys.stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
    for brief in report.briefs:
        tag = brief.ticker or "BOOK"
        sys.stdout.write(
            f"\n--- DELIVERED [{brief.kind} · {tag}] score={brief.score:.2f} "
            f"turn={brief.turn_id} ---\n{brief.headline}\n\n{brief.answer}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
