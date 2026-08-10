"""Explicit maintenance CLI for governed session distillation.

Reads every idle/undistilled Ask thread and every landed/undistilled bridged
Claude-session transcript, runs each through
``synthesis.session_distill.run_session_distill`` (one structured LLM call
per session, deterministically grounded before anything lands), and reports
the aggregate tally. Belief revisions (tenet/stance) AUTO-ADOPT with a
one-tap Telegram Revert — see the module docstring for the owner-ruling
rationale.

``--postmortem-backfill`` is a separate, explicit one-time mode for drafting
every pending exit post-mortem in one operator-reviewed batch. Ordinary
session distillation never drafts post-mortems.

Usage:
    python execution/run_session_distill.py
    python execution/run_session_distill.py --dry-run   # list candidates, zero LLM
    python execution/run_session_distill.py --postmortem-backfill  # ONE-TIME: draft ALL
                                                                    # pending post-mortems

Exit status: 0 on a normal sweep (including "nothing to distil" and "some
candidates skipped groundless" — those are honest, non-error outcomes), 2 on
a hard stop (budget block / missing CLI — see ``llm.cli.is_hard_stop``), 3
when EVERY candidate session deferred transient (quota rule 3: defer + tally
+ retry next run — mirrors ``execution/run_calibration_scorecard.py``'s
exit-3 semantics).

This CLI is explicit maintenance only: it is not present in the scheduler
manifest. An ordinary run distils sessions only. Exit-postmortem drafting is
available solely through the explicit ``--postmortem-backfill`` mode.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("run_session_distill")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--db-path", type=Path, default=None, help="defaults to <repo-root>/data/portfolio.db"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list candidate sessions + counts only — zero LLM calls",
    )
    parser.add_argument(
        "--postmortem-backfill",
        action="store_true",
        help=(
            "draft ALL pending exit post-mortems in ONE batch + a single Telegram "
            "summary (the pre-existing all-NULL closed rows) instead of the nightly "
            "few-per-run pacing; runs ONLY this leg, not session distillation"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo_root = args.repo_root.resolve()
    db_path = args.db_path.resolve() if args.db_path else repo_root / "data" / "portfolio.db"

    from synthesis.session_distill import candidate_sessions

    if args.dry_run:
        refs = candidate_sessions(db_path)
        for ref in refs:
            print(
                f"candidate: source={ref.source} session_id={ref.session_id}",
                file=sys.stderr,
            )
        print(
            f"run_session_distill --dry-run: {len(refs)} candidate session(s), 0 LLM calls",
            file=sys.stderr,
        )
        return 0

    from llm.cli import is_hard_stop

    if args.postmortem_backfill:
        # ONE-TIME mode (B6): runs ONLY the post-mortem drafting leg, over the
        # FULL pending set, with a single batched Telegram summary -- not the
        # nightly few-per-run pacing, and never touches session distillation.
        from synthesis.exit_postmortem import run_postmortem_drafts

        try:
            pm_tally = run_postmortem_drafts(db_path, batch=True, repo_root=repo_root)
        except Exception as exc:
            if is_hard_stop(exc):
                log.error(
                    {
                        "event": "postmortem_backfill_hard_stop",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return 2
            raise
        print(f"run_session_distill --postmortem-backfill: {pm_tally}", file=sys.stderr)
        return 0

    from synthesis.session_distill import run_session_distill

    try:
        tally = run_session_distill(db_path, repo_root=repo_root)
    except Exception as exc:
        if is_hard_stop(exc):
            log.error(
                {"event": "session_distill_hard_stop", "error": f"{type(exc).__name__}: {exc}"}
            )
            return 2
        raise

    print(f"run_session_distill: {tally}", file=sys.stderr)

    sessions = tally.get("sessions", 0)
    deferred = tally.get("deferred_transient", 0)
    if sessions > 0 and deferred == sessions:
        # Quota rule 3: every candidate this run deferred transient (a busy
        # quota day) — nothing distilled, nothing lost (all retried next
        # sweep), but the caller should see a distinguishable non-zero exit
        # rather than a quiet 0 that reads as "swept, found nothing".
        log.error({"event": "session_distill_all_deferred", "tally": tally})
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
