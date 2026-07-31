"""Generate pre-earnings briefs for in-window book names (single-purpose CLI).

Morning-pipeline leg (stage 1c) and ad-hoc tool. Scope, window, idempotency,
bounded refresh, and the per-item degrade posture all live in
``src/earnings_brief.py`` — this file is the thin typed entrypoint.

Exit codes: 0 = ran (tally on stdout, including all-skipped runs);
2 = hard stop (budget block / LLM setup) — loud, surfaced by the pipeline.

Structured JSON events go to stderr via logging; stdout carries ONLY the
final one-line JSON tally (never mixed).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from earnings_brief import BRIEF_WINDOW_DAYS, generate_all  # noqa: E402
from llm_client import is_hard_stop  # noqa: E402

log = logging.getLogger("generate_pre_earnings_briefs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=_REPO_ROOT / "data" / "portfolio.db")
    parser.add_argument(
        "--window-days",
        type=int,
        default=BRIEF_WINDOW_DAYS,
        help="generate for names reporting within this many days",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        default=None,
        help="restrict to specific ticker(s); repeatable",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="override 'today' (tests / backfills)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate even when a current artifact matches",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        tally = generate_all(
            args.db_path,
            _REPO_ROOT,
            today=args.as_of or datetime.now(UTC).date(),
            window_days=args.window_days,
            force=args.force,
            only_tickers=set(args.ticker) if args.ticker else None,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            log.error(
                {
                    "event": "pre_earnings_brief_hard_stop",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            )
            return 2
        raise

    print(json.dumps({"event": "pre_earnings_briefs_done", **tally}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
