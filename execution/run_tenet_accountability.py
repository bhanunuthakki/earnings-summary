"""execution/run_tenet_accountability.py — the weekly Sat 09:00 Tenet-accountability
pass (B5, 2026-07-19 program overhaul).

For every ``current`` Worldview Tenet, deterministically gathers the owner's
own decisions made since the Tenet's ``as_of`` (plus best-effort position
alpha), then runs each through
``synthesis.tenet_accountability.run_accountability`` (one structured Sonnet
call per Tenet WITH evidence — a Tenet nobody has acted on since costs zero
LLM calls), persisting the verdict onto the Tenet's own row so the Worldview
panel can show a receipts chip and the nag governor can raise at most one
``tenet_challenge`` moment per week.

Usage:
    python execution/run_tenet_accountability.py
    python execution/run_tenet_accountability.py --dry-run   # list current tenets, zero LLM

Exit status: 0 on a normal sweep (including "no current tenets" and "some
tenets skipped, nothing to judge" — both honest, non-error outcomes), 2 on a
hard stop (budget block / missing CLI — see ``llm.cli.is_hard_stop``), 3 when
EVERY tenet this run deferred transient (quota rule 3: defer + tally + retry
next run — mirrors ``execution/run_session_distill.py``'s exit-3 semantics).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("run_tenet_accountability")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--db-path", type=Path, default=None, help="defaults to <repo-root>/data/portfolio.db"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list current tenets only — zero LLM calls",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo_root = args.repo_root.resolve()
    db_path = args.db_path.resolve() if args.db_path else repo_root / "data" / "portfolio.db"

    from synthesis.tenets import list_tenets

    if args.dry_run:
        tenets = list_tenets(status="current", db_path=db_path)
        for t in tenets:
            print(
                f"candidate: tenet_id={t.id} scope_key={t.scope_key} as_of={t.as_of}",
                file=sys.stderr,
            )
        print(
            f"run_tenet_accountability --dry-run: {len(tenets)} current tenet(s), 0 LLM calls",
            file=sys.stderr,
        )
        return 0

    from llm.cli import is_hard_stop
    from synthesis.tenet_accountability import run_accountability

    try:
        tally = run_accountability(db_path)
    except Exception as exc:
        if is_hard_stop(exc):
            log.error(
                {
                    "event": "tenet_accountability_hard_stop",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return 2
        raise

    print(f"run_tenet_accountability: {tally}", file=sys.stderr)

    tenets = tally.get("tenets", 0)
    deferred = tally.get("deferred_transient", 0)
    if tenets > 0 and deferred == tenets:
        # Quota rule 3: every current Tenet this run deferred transient (a
        # busy quota day) — nothing assessed, nothing lost (all retried next
        # week's sweep), but the caller should see a distinguishable non-zero
        # exit rather than a quiet 0 that reads as "swept, found nothing".
        log.error({"event": "tenet_accountability_all_deferred", "tally": tally})
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
