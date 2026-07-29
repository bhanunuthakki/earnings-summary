"""Tier-aware lens runner — regenerate the lens set each tier is due for.

The scalability counterpart to `execution/run_lens.py`. Where `run_lens.py`
is the manual entry point ("run this lens for this ticker"), this script is
the cron-friendly automation: pick the cadence (daily / weekly / monthly),
look up which tickers + which lenses are due per the tier matrix, and
delegate each (ticker, lens) pair to `synthesis_lenses.run_lens`.

Per-tier lens scope (the fresh-review memo's tier matrix):

    P1 (portfolio)            → all ticker-scoped lenses + portfolio lens
    P2 (watchlist+evaluation) → five_min_reread + thesis_drift_qoq
    P3 (index_member, etc.)   → five_min_reread

Cadence → tier matrix (which tiers actually fire on this tick):

    daily   P1 always; P2/P3 catch-up only if drifted past their cadence
    weekly  P1 weekly lens regen; P2 monthly catch-up; P3 quarterly catch-up
    monthly P1 monthly; P2 quarterly; P3 annual

The script is idempotent: `run_lens` dedups via the artifact-store
cache_inputs hash, so a re-run on identical inputs is free. `--dry-run`
lists the (ticker, lens) plan without invoking the LLM — useful for sanity-
checking what a freshly-installed cron will actually do.

Usage:
    python execution/run_due_lenses.py --dry-run
    python execution/run_due_lenses.py --cadence daily
    python execution/run_due_lenses.py --cadence weekly
    python execution/run_due_lenses.py --cadence monthly
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _sync_db_path(repo_root: Path) -> None:
    """Point the db module's DB_PATH at the caller's repo (mirrors run_lens.py)."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from pipeline.tier_runner import tickers_due_for_lens_regen  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from synthesis_lenses import (  # noqa: E402
    LENSES,
    list_lenses_for_ticker,
    list_portfolio_lenses,
    run_lens,
)

log = logging.getLogger("run_due_lenses")

Cadence = Literal["daily", "weekly", "monthly"]


# Per-tier lens set. P2 + P3 are deliberately narrow — five_min_reread is the
# action-oriented summary and thesis_drift_qoq lets the analyst see whether a
# watchlist name's quarter engaged with prior bear-case named-failure-modes.
# P1 gets the full ticker-scoped set (every lens applies to a held name).
_LENS_SET_BY_TIER: dict[str, list[str]] = {
    "P1": [],  # populated at runtime from list_lenses_for_ticker()
    "P2": ["five_min_reread", "thesis_drift_qoq"],
    "P3": ["five_min_reread"],
}


def _tier_lens_set(tier: str) -> list[str]:
    """Return the lens slug list applicable to a given tier."""
    if tier == "P1":
        return list_lenses_for_ticker()
    return _LENS_SET_BY_TIER.get(tier, [])


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cadence: Cadence = args.cadence
    plan = _build_plan(repo_root, cadence)

    log.info({"event": "plan_built", "cadence": cadence, "n_runs": len(plan)})

    if args.dry_run:
        # Compact dry-run output: counts per tier + first ~25 pairs as preview
        counts: dict[str, dict[str, int]] = {}
        for tier, _ticker, lens_name in plan:
            counts.setdefault(tier, {}).setdefault(lens_name, 0)
            counts[tier][lens_name] += 1
        preview = [{"tier": t, "ticker": tk, "lens": ln} for (t, tk, ln) in plan[:25]]
        print(
            json.dumps(
                {
                    "cadence": cadence,
                    "total_pairs": len(plan),
                    "counts_by_tier_and_lens": counts,
                    "preview": preview,
                },
                indent=2,
            )
        )
        return 0

    # Real run — apply the limit if set
    if args.limit > 0:
        plan = plan[: args.limit]

    n_fresh = 0
    n_cache_hits = 0
    n_skipped = 0
    for tier, ticker, lens_name in plan:
        lens = LENSES.get(lens_name)
        if lens is None:
            log.warning({"event": "lens_not_found", "lens": lens_name})
            n_skipped += 1
            continue
        result = run_lens(
            lens,
            ticker=ticker if lens.scope == "ticker" else None,
            repo_root=repo_root,
            force=False,
        )
        if result is None:
            n_skipped += 1
            log.info(
                {
                    "event": "lens_skipped",
                    "tier": tier,
                    "ticker": ticker,
                    "lens": lens_name,
                }
            )
            continue
        # run_lens returns the existing artifact unchanged on cache hit; the
        # artifact_store does the dedup. We can't trivially tell which, but
        # we log progress either way.
        n_fresh += 1
        log.info(
            {
                "event": "lens_done",
                "tier": tier,
                "ticker": ticker,
                "lens": lens_name,
                "artifact_id": result.id,
            }
        )

    print(
        json.dumps(
            {
                "cadence": cadence,
                "total_pairs": len(plan),
                "produced": n_fresh,
                "skipped": n_skipped,
                "cache_hits_subset_of_produced": n_cache_hits,
            },
            indent=2,
        )
    )
    return 0


def _build_plan(repo_root: Path, cadence: Cadence) -> list[tuple[str, str, str]]:
    """Return the run plan as a list of (tier, ticker, lens_name).

    For ticker-scoped lenses: ticker is the actual ticker.
    For portfolio-scoped lenses (cross_portfolio_synthesis): ticker is the
    sentinel "__PORTFOLIO__" — handled by main() to call run_lens with
    ticker=None.
    """
    # We need each ticker's tier so we can decide what lenses apply, but
    # tickers_due_for_lens_regen is per-lens. Strategy: walk per (tier, lens),
    # call tickers_due_for_lens_regen once per lens, then intersect with the
    # ticker's actual tier via tracked_companies lookup.
    db_path = repo_root / "data" / "portfolio.db"
    tier_by_ticker: dict[str, str] = {}
    if db_path.exists():
        with connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY) as conn:
            conn.row_factory = sqlite3.Row
            if _has_processing_tier_column(conn):
                rows = conn.execute(
                    "SELECT UPPER(ticker) AS ticker, processing_tier "
                    "FROM tracked_companies WHERE archived_at IS NULL"
                ).fetchall()
                tier_by_ticker = {r["ticker"]: (r["processing_tier"] or "P3").upper() for r in rows}

    plan: list[tuple[str, str, str]] = []
    # Collect unique lens slugs across tiers (P1 = full set; P2/P3 are subsets).
    seen_lenses: set[str] = set()
    for tier in ("P1", "P2", "P3"):
        for lens_name in _tier_lens_set(tier):
            seen_lenses.add(lens_name)

    for lens_name in sorted(seen_lenses):
        due = set(tickers_due_for_lens_regen(repo_root, lens_name, cadence))
        for ticker in sorted(due):
            tier = tier_by_ticker.get(ticker.upper(), "P3")
            applicable_lenses = _tier_lens_set(tier)
            if lens_name not in applicable_lenses:
                continue
            plan.append((tier, ticker.upper(), lens_name))

    # Portfolio-scoped lens — only P1 cadence applies and only on the daily +
    # weekly ticks. Treat the portfolio lens as having a single sentinel
    # "ticker" for plan-tracking purposes.
    if cadence in ("daily", "weekly") and _portfolio_synthesis_is_due(repo_root, cadence):
        for portfolio_lens in list_portfolio_lenses():
            plan.append(("P1", "__PORTFOLIO__", portfolio_lens))

    return plan


def _portfolio_synthesis_is_due(repo_root: Path, cadence: Cadence) -> bool:
    """The portfolio-scoped lens runs weekly for P1 — fire on daily + weekly ticks.

    The daily cron runs every day; the cross-portfolio synthesis is expensive
    so it's gated to weekly cadence (Sunday-equivalent). On `cadence='weekly'`
    we always fire. On `cadence='daily'` we fire only when the last cached
    portfolio synthesis is older than 7 days.
    """
    from datetime import datetime, timedelta

    if cadence == "monthly":
        return False
    if cadence == "weekly":
        return True
    # Daily: only if drifted past 7d
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return False
    with connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        if row is None:
            return True
        latest = conn.execute(
            """
            SELECT MAX(generated_at) AS g FROM llm_artifacts
            WHERE purpose = 'lens:cross_portfolio_synthesis'
              AND scope = 'portfolio'
              AND superseded_by_id IS NULL
            """
        ).fetchone()
    if not latest or not latest["g"]:
        return True
    try:
        last_dt = datetime.fromisoformat(str(latest["g"]).replace("Z", ""))
        if last_dt.tzinfo is not None:
            last_dt = last_dt.replace(tzinfo=None)
    except ValueError:
        return True
    return (datetime.now() - last_dt) >= timedelta(days=7)


def _has_processing_tier_column(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("PRAGMA table_info(tracked_companies)")
    rows: list[tuple[object, ...]] = cur.fetchall()
    return any(row[1] == "processing_tier" for row in rows)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--cadence",
        choices=("daily", "weekly", "monthly"),
        default="daily",
        help="Which cron tick is calling — selects the tier-vs-age matrix row.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the (tier, ticker, lens) plan without invoking the LLM.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If > 0, cap the number of (ticker, lens) pairs run this cadence tick.",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(main())
