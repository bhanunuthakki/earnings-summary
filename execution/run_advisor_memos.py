"""Run advisor memos (master build P2.3): next-dollar and/or swap checks.

The single entry point for every way a memo run starts:

* **Dashboard button** — the Memos panel POSTs ``/actions/advisor-memo``,
  which streams this script through the jobs SSE machinery.
* **Monthly cron** — ``cron/monthly_advisor_memos.task.xml`` runs
  ``--kind all`` on the 1st of each month.
* **Manual** — run it directly.

Usage:
    python execution/run_advisor_memos.py --kind next_dollar
    python execution/run_advisor_memos.py --kind swap_checks --margin-pp 20
    python execution/run_advisor_memos.py --kind all --repo-root <path>

Exit status: 0 when every attempted memo persisted (including the
nothing-cleared-the-bar swap outcome — that is a healthy result, not a
failure), 1 when any attempt was skipped on a transient LLM failure, 2 on a
hard stop (budget block / missing CLI — see llm.cli.is_hard_stop).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("run_advisor_memos")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        choices=("next_dollar", "swap_checks", "all"),
        required=True,
        help="which memo run to execute",
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=None, help="defaults to identity.DEFAULT_USER_ID")
    parser.add_argument(
        "--margin-pp",
        type=float,
        default=15.0,
        help="swap-discipline clearing bar in percentage points of DCF upside",
    )
    parser.add_argument(
        "--max-memos", type=int, default=2, help="max swap memos (LLM calls) per run"
    )
    parser.add_argument(
        "--force-budget-bypass",
        action="store_true",
        help="skip the per-purpose LLM budget check for this run",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    repo_root = args.repo_root.resolve()

    from advisor.context import build_advisor_context
    from advisor.memos import generate_next_dollar_memo, run_swap_checks
    from identity import DEFAULT_USER_ID
    from llm.cli import is_hard_stop

    user_id = args.user_id or DEFAULT_USER_ID
    failures = 0
    try:
        # One context fetch serves both kinds (tracker + DB round-trips are
        # the slow part; the memos only differ in prompts).
        ctx = build_advisor_context(repo_root, user_id=user_id)
        print(
            f"context: {len(ctx.audit_rows)} holdings · "
            f"{len(ctx.candidates_val)} external candidates with DCFs · "
            f"tracker {'up' if ctx.live.available else 'OFFLINE'}",
            flush=True,
        )
        if args.kind in ("next_dollar", "all"):
            result = generate_next_dollar_memo(
                repo_root,
                user_id=user_id,
                ctx=ctx,
                force_budget_bypass=args.force_budget_bypass,
            )
            if result.ok:
                print(f"next_dollar: memo #{result.memo_id} — {result.title}", flush=True)
            else:
                failures += 1
                print(f"next_dollar: SKIPPED — {result.skipped_reason}", flush=True)
        if args.kind in ("swap_checks", "all"):
            results = run_swap_checks(
                repo_root,
                user_id=user_id,
                margin_pp=args.margin_pp,
                max_memos=args.max_memos,
                ctx=ctx,
                force_budget_bypass=args.force_budget_bypass,
            )
            if not results:
                print(
                    f"swap_checks: no pair cleared the {args.margin_pp:.0f}pp bar "
                    "(healthy outcome — discipline held)",
                    flush=True,
                )
            for r in results:
                if r.ok:
                    print(
                        f"swap_check: memo #{r.memo_id} — {r.ticker} vs {r.counter_ticker}",
                        flush=True,
                    )
                else:
                    failures += 1
                    print(
                        f"swap_check: SKIPPED {r.ticker} vs {r.counter_ticker} — "
                        f"{r.skipped_reason}",
                        flush=True,
                    )
    except Exception as exc:
        if is_hard_stop(exc):
            print(f"HARD STOP: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 2
        raise
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
