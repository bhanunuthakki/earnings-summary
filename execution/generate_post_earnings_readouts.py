"""Generate persisted post-earnings readouts for portfolio names only.

This is the thin stage-1d entrypoint. Evaluation names are intentionally not
accepted by the scheduled lane; their only paid path is the explicit cockpit
POST action backed by ``earnings_readout.generate_for_ticker``.
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

from earnings_readout import generate_all  # noqa: E402
from llm_client import is_hard_stop  # noqa: E402

log = logging.getLogger("generate_post_earnings_readouts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=_REPO_ROOT / "data" / "portfolio.db")
    parser.add_argument(
        "--ticker",
        action="append",
        default=None,
        help="restrict the portfolio-only run to specific ticker(s); repeatable",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="override today for deterministic tests/backfills",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate even when the quarter artifact input hash matches",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        tally = generate_all(
            args.db_path,
            _REPO_ROOT,
            today=args.as_of or datetime.now(UTC).date(),
            force=args.force,
            only_tickers=set(args.ticker) if args.ticker else None,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            log.error(
                {
                    "event": "post_earnings_readout_hard_stop",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            )
            return 2
        raise
    print(json.dumps({"event": "post_earnings_readouts_done", **tally}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
