"""Run synthesis lenses. Each lens is a focused LLM prompt that reads across
sections to produce cross-cutting analytical content (thesis drift, 5-min
reread, bull case, reverse DCF, catalyst calendar, footnote anomalies,
cross-portfolio synthesis, etc.). See src/synthesis_lenses.py.

Output is cached via llm_artifacts so re-running with unchanged inputs is
free. force=True bypasses the cache.

Usage:
    # Single ticker, single lens
    python execution/run_lens.py --ticker GOOG --lens thesis_drift_qoq

    # Single ticker, all ticker-scoped lenses
    python execution/run_lens.py --ticker GOOG --all

    # Portfolio-scoped lenses (no ticker)
    python execution/run_lens.py --lens cross_portfolio_synthesis

    # All portfolio + a list of tickers
    python execution/run_lens.py --tickers GOOG,META,AMZN --all
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _sync_db_path(repo_root: Path) -> None:
    """Point the db module's DB_PATH at the caller's repo so the ledger writer
    + artifact store land their rows in the right DB. Mirrors the pattern in
    execution/build_artifacts.py:_sync_db_to_repo."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from synthesis_lenses import (  # noqa: E402
    LENSES,
    list_lenses_for_ticker,
    list_portfolio_lenses,
    run_lens,
)

log = logging.getLogger("run_lens")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=False)
    g.add_argument("--ticker", help="Single ticker to run lenses for.")
    g.add_argument("--tickers", help="Comma-separated tickers.")
    parser.add_argument(
        "--lens",
        choices=sorted(LENSES.keys()),
        help="Single lens to run. Omit + use --all to run all applicable lenses.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="With a ticker arg: run all ticker-scoped lenses. Without: run portfolio-scoped lenses.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass artifact cache and regenerate every lens.",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root containing data/."
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Point db module at the caller's repo so ledger writes land correctly.
    _sync_db_path(args.repo_root.resolve())

    tickers: list[str] = []
    if args.ticker:
        tickers = [args.ticker.upper()]
    elif args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    # Build the run plan: (ticker, lens_name) pairs
    plan: list[tuple[str | None, str]] = []
    if tickers:
        if args.lens:
            for t in tickers:
                plan.append((t, args.lens))
        elif args.all:
            for t in tickers:
                for ln in list_lenses_for_ticker():
                    plan.append((t, ln))
            # Also include portfolio lenses if --all and --tickers
            if args.tickers and not args.ticker:
                for ln in list_portfolio_lenses():
                    plan.append((None, ln))
        else:
            log.error({"event": "no_lens_or_all_specified"})
            return 2
    else:
        # No tickers — must be portfolio-scoped
        if args.lens:
            plan.append((None, args.lens))
        elif args.all:
            for ln in list_portfolio_lenses():
                plan.append((None, ln))
        else:
            log.error({"event": "no_ticker_no_lens"})
            return 2

    log.info({"event": "lens_plan", "n_runs": len(plan)})

    n_fresh = 0
    n_skipped = 0
    for ticker, lens_name in plan:
        lens = LENSES[lens_name]
        if lens.scope == "portfolio" and ticker is not None:
            continue  # don't run portfolio lenses with ticker context
        if lens.scope == "ticker" and ticker is None:
            continue

        log.info({"event": "lens_run_start", "ticker": ticker, "lens": lens_name})
        result = run_lens(
            lens, ticker=ticker, repo_root=args.repo_root, force=args.force
        )
        if result is None:
            log.warning({"event": "lens_run_skipped", "ticker": ticker, "lens": lens_name})
            n_skipped += 1
            continue
        # Cache vs fresh based on whether dirty or new (rough heuristic — both ok)
        log.info(
            {
                "event": "lens_run_done",
                "ticker": ticker,
                "lens": lens_name,
                "artifact_id": result.id,
                "chars": len(result.content_md or ""),
            }
        )
        n_fresh += 1

    print(
        f"\nLens run complete · {n_fresh} produced · {n_skipped} skipped (insufficient data)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
