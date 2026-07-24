"""Refresh the C3 business-factor taxonomy loadings (src/risk_factors.py).

For every active portfolio holding, grounds its C2 disclosed revenue mix
(geography + product) and its ``micro_thesis/holdings/<T>.json`` thesis onto
a small controlled taxonomy of business drivers ("Brazil consumer credit",
"digital ad spend", "GLP-1/obesity reimbursement", ... — see
``risk_factors.TAXONOMY``) via one governed LLM call per name, then persists
the loadings into ``business_factor_exposures`` (append-only with
owner-edit supremacy — see that migration's and ``persist_exposures``'s
docstrings).

Cached in ``llm_artifacts`` (purpose ``business_factor_taxonomy``, scope
``ticker``) keyed by a per-name mix+thesis input hash — a re-run is a no-op
(no LLM spend) for any name whose disclosed mix and thesis file are
unchanged since the last run. Low-frequency by design (weekly is enough; see
``directives/llm_quota_scheduling.md``'s registered window for this
purpose) — run on demand or from a slow cron, NOT the daily morning
pipeline. The Risk tab's "Business-factor exposure" section only ever reads
the persisted table back; it never triggers a call itself.

Usage:
    python execution/refresh_business_factors.py
    python execution/refresh_business_factors.py --dry-run   # list candidates, zero LLM
    python execution/refresh_business_factors.py --repo-root . --db-path /tmp/x.db

Exit status: 0 on a normal sweep (including "nothing to refresh" — an honest,
non-error outcome), 2 on a hard stop (budget block / missing CLI — see
``llm.cli.is_hard_stop``), 3 when EVERY candidate ticker deferred transient
(quota rule 3: defer + tally + retry next run — mirrors
``execution/run_session_distill.py``'s exit-3 semantics).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("refresh_business_factors")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db and micro_thesis/holdings/.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path (wins over --repo-root derivation).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list candidate tickers only — zero LLM calls",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )

    from risk_factors import portfolio_tickers

    if args.dry_run:
        tickers = portfolio_tickers(db_path)
        for t in tickers:
            print(f"candidate: {t}", file=sys.stderr)
        print(
            f"refresh_business_factors --dry-run: {len(tickers)} candidate ticker(s), 0 LLM calls",
            file=sys.stderr,
        )
        return 0

    from llm.cli import is_hard_stop
    from risk_factors import refresh_all

    try:
        counts = refresh_all(db_path, repo_root)
    except Exception as exc:
        if is_hard_stop(exc):
            log.error(
                {
                    "event": "business_factor_refresh_hard_stop",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return 2
        raise

    print(f"refresh_business_factors: {counts}", file=sys.stderr)

    tickers_n = counts.get("tickers", 0)
    deferred = counts.get("deferred_transient", 0)
    if tickers_n > 0 and deferred == tickers_n:
        log.error({"event": "business_factor_refresh_all_deferred", "counts": counts})
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
