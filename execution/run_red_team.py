"""Run one monthly Red Team pass (directives/monthly_red_team.md Phase 2, PR5).

Generates one rotating-lens adversarial attack per held name (weight > 0.5%,
cash-likes and index ETFs excluded) plus the three cross-book passes
(factor-block detection, style drift, human-capital overlay), persists them to
``red_team_items``, and writes a standalone brief snapshot to
``.tmp/red_team_briefs/<run_key>.html`` (the live command-center panel always
reads the DB directly — this file is a convenience artifact, e.g. for
Telegram/email attachment in a later PR).

Idempotency key: ``red_team_{YYYY_MM}`` — a run with existing items for the
target month is a no-op unless ``--force`` is passed.

Per-item degrade: a transient LLM failure (parse failure or a transient call
error) defers that one item and tallies it; the run continues. A hard stop
(budget cap / missing CLI) halts the whole run loudly (exit 1).

Usage:
    python execution/run_red_team.py                       # current month
    python execution/run_red_team.py --month 2026-08
    python execution/run_red_team.py --dry-run              # zero LLM calls
    python execution/run_red_team.py --limit 3              # smoke test
    python execution/run_red_team.py --force                # re-run a month

Scheduling: First Saturday of the month, 10:00 America/Los_Angeles (clear of
the 04:00 pipeline / 03:00 monthly-prior / Sunday eval-rung windows — see
directives/llm_quota_scheduling.md). Windows Task Scheduler has no native
"Nth weekday of month" trigger, so the registered task (cron/red_team.task.xml)
fires every Saturday at 10:00 and this script itself no-ops unless it is the
FIRST Saturday of the month (``_is_first_saturday``) — see cron/
SETUP_WINDOWS_SCHEDULER.md for the one-line install command
(``schtasks /create ...``); this script never registers the task itself
(machine-level action, requires an admin shell — the repo's existing
convention for every other cron job, see verify below).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from clock import now_naive_utc  # noqa: E402
from redteam.brief import render_red_team_brief  # noqa: E402
from redteam.engine import RedTeamRunResult, run_red_team  # noqa: E402
from redteam.store import list_items_for_run  # noqa: E402


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}, default=str), file=sys.stderr)


def is_first_saturday(today: date | None = None) -> bool:
    """True iff ``today`` (default: today, local date) is the first Saturday
    of its month — the schtasks trigger fires every Saturday at 10:00; this
    guard is what makes it "first Saturday only" (Windows Task Scheduler has
    no native Nth-weekday trigger)."""
    d = today or now_naive_utc().date()
    return d.weekday() == 5 and d.day <= 7  # Saturday=5


def _write_brief_snapshot(repo_root: Path, run_key: str, db_path: Path) -> Path | None:
    items = list_items_for_run(db_path=db_path, run_key=run_key)
    if not items:
        return None
    html = render_red_team_brief(items, run_key=run_key)
    out_dir = repo_root / ".tmp" / "red_team_briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_key}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _write_dry_run_artifact(repo_root: Path, result: RedTeamRunResult) -> Path:
    out_dir = repo_root / ".tmp" / "red_team_briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result.run_key}_dry_run.json"
    out_path.write_text(
        json.dumps(
            {
                "run_key": result.run_key,
                "held_tickers": result.held_tickers,
                "tally": result.tally,
                "first_prompt_label": result.first_prompt_label,
                "first_prompt": result.first_prompt,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--month", default=None, help="YYYY-MM; default = current month")
    parser.add_argument(
        "--force", action="store_true", help="Re-run even if this month already has items"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Assemble evidence packs + prompts, print item counts + first prompt. ZERO LLM calls.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap held names processed (smoke tests)"
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "portfolio.db")
    parser.add_argument(
        "--skip-first-saturday-guard",
        action="store_true",
        help="Run even when today is not the first Saturday (manual/CI invocations).",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = args.db.resolve()

    # The scheduled task fires every Saturday; this is the "first Saturday
    # only" gate (see module docstring). A manual/smoke run should pass
    # --skip-first-saturday-guard or --dry-run rather than hit this no-op.
    if (
        not args.dry_run
        and not args.force
        and not args.skip_first_saturday_guard
        and not is_first_saturday()
    ):
        _log("red_team_skipped_not_first_saturday", today=str(now_naive_utc().date()))
        print(json.dumps({"skipped": "not_first_saturday"}))
        return 0

    _log(
        "red_team_run_start",
        month=args.month,
        force=args.force,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    try:
        result = run_red_team(
            repo_root=repo_root,
            db_path=db_path,
            month=args.month,
            force=args.force,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except Exception as exc:  # hard stop (budget/setup) — propagate loudly per is_hard_stop
        _log("red_team_run_hard_stop", error=f"{type(exc).__name__}: {exc}")
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1

    if result.already_done:
        _log("red_team_already_done", run_key=result.run_key)
        print(json.dumps({"run_key": result.run_key, "already_done": True}))
        return 0

    if args.dry_run:
        dry_run_path = _write_dry_run_artifact(repo_root, result)
        _log(
            "red_team_dry_run_done",
            run_key=result.run_key,
            held_tickers=len(result.held_tickers),
            tally=result.tally,
            artifact=str(dry_run_path),
        )
        print(
            json.dumps(
                {
                    "run_key": result.run_key,
                    "dry_run": True,
                    "held_tickers": len(result.held_tickers),
                    "tally": result.tally,
                    "dry_run_artifact": str(dry_run_path),
                },
                indent=2,
            )
        )
        return 0

    brief_path = _write_brief_snapshot(repo_root, result.run_key, db_path)
    _log(
        "red_team_run_done",
        run_key=result.run_key,
        tally=result.tally,
        brief_path=str(brief_path) if brief_path else None,
    )
    print(
        json.dumps(
            {
                "run_key": result.run_key,
                "held_tickers": len(result.held_tickers),
                "items_generated": len(result.item_ids),
                "tally": result.tally,
                "brief_path": str(brief_path) if brief_path else None,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
