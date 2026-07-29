"""Materialise portfolio-fit per evaluation name for the cockpit Fit column.

Computes each evaluation candidate's fit to the held book (Marginal Sharpe ·
diversification · factor exposure · sector) off the daily price-chart cache plus
a live tracker fetch for the book's Sharpe / risk-free rate / sector weights /
growth tilt, and writes data/candidate_fit.json. The GET / render reads this
cache instead of running covariance-grade price reads per candidate on the boot
path — the pattern Stage 0d (cockpit fundamentals) established.

Called as Stage 0f of the morning pipeline (run_morning_pipeline.py), after the
weights cache (0c) and DCF re-price (0e) are fresh. Can also be run standalone.

Usage:
    python execution/refresh_candidate_fit.py
    python execution/refresh_candidate_fit.py --repo-root . --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from candidate_fit_cache import materialize_candidate_fit  # noqa: E402
from etf_score_cache import materialize_etf_scores  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("refresh_candidate_fit")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db, the price cache, and data/candidate_fit.json.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path (wins over --repo-root derivation).",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="Portfolio-tracker base URL (default: PORTFOLIO_TRACKER_API_URL or :8000).",
    )
    parser.add_argument(
        "--refresh-synthesis",
        action="store_true",
        help="Also refresh each evaluation ETF's role-in-portfolio one-pager "
        "(sha-gated LLM calls, ~60s each when inputs moved). OFF by default: "
        "Stage 0f runs on a 180s pure-math budget, and the workup payload's "
        "floats wiggle daily, so the sha gate re-bills most mornings — the "
        "2026-07-11 triggered run timed out the stage on exactly this. "
        "Syntheses refresh at onboard time, via build_etf_workup.py, or by "
        "passing this flag from a wider-budget context (weekly ETF refresh).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )

    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        n = materialize_candidate_fit(conn, repo_root, db_path=db_path, api_url=args.api_url)
        # ETF sibling (same Stage 0f budget, no new pipeline stage): the
        # fund-appropriate score for evaluation-list ETFs — see
        # pipeline/etf_score.py and directives/etf_data.md.
        n_etf = materialize_etf_scores(conn, repo_root)
        if args.refresh_synthesis:
            # Role-synthesis refresh (OPT-IN — see the flag's help): sha-gated,
            # per-item degrade per the scheduled-LLM policy — hard stops
            # propagate, transient failures log and continue.
            from etf_role_synthesis import generate_role_synthesis
            from etf_score_cache import evaluation_etf_tickers

            for etf_ticker in evaluation_etf_tickers(conn):
                _artifact_id, status = generate_role_synthesis(conn, repo_root, db_path, etf_ticker)
                log.info("etf_role_synthesis %s status=%s", etf_ticker, status)
    finally:
        conn.close()

    print(
        f"Candidate fit materialised · names={n} · etf_scores={n_etf} · "
        f"caches=data/candidate_fit.json,data/etf_score.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
