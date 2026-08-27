"""Tier-aware lens runner — regenerate the lens set each tier is due for.

The scalability counterpart to `execution/run_lens.py`. Where `run_lens.py`
is the manual entry point ("run this lens for this ticker"), this script is
the cron-friendly automation: pick the cadence (daily / weekly / monthly),
look up which tickers + which lenses are due per the tier matrix, and
delegate each (ticker, lens) pair to `synthesis_lenses.run_lens`.

Per-tier lens scope (the fresh-review memo's tier matrix):

    P1 (portfolio)            → all ticker-scoped lenses + portfolio lens
    P2 (watchlist+evaluation) → five_min_reread + thesis_drift_qoq
    P3 (index_member, etc.)   → no scheduled LLM lenses

Cadence selects exactly one tier: daily → P1, weekly → P2, monthly → P3.
The monthly P3 plan is intentionally empty because screened/catalog names do
not receive scheduled LLM work.

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
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _sync_db_path(repo_root: Path) -> None:
    """Point the db module's DB_PATH at the caller's repo (mirrors run_lens.py)."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from llm.cli import is_hard_stop  # noqa: E402
from log_redact import redact  # noqa: E402
from models.companies import schedule_class_for_list_type  # noqa: E402
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
    "P3": [],
}

_PROTECTED_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_DEFERRED_EXIT = 75


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

    if args.max_plan_pairs > 0 and len(plan) > args.max_plan_pairs:
        log.error(
            {
                "event": "plan_too_large",
                "cadence": cadence,
                "n_runs": len(plan),
                "max_plan_pairs": args.max_plan_pairs,
            }
        )
        return 2

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

    stop_deadline = None
    if args.stop_before_local:
        if not args.window_opens_local:
            raise ValueError("--stop-before-local requires --window-opens-local")
        stop_deadline = _scheduled_window_deadline(
            datetime.now(_PROTECTED_TIME_ZONE),
            opens_at=args.window_opens_local,
            stops_at=args.stop_before_local,
        )
    n_fresh = 0
    n_cache_hits = 0
    n_skipped = 0
    n_deferred = 0
    n_deferred_transient = 0
    for pair_index, (tier, ticker, lens_name) in enumerate(plan):
        if stop_deadline is not None and datetime.now(_PROTECTED_TIME_ZONE) >= stop_deadline:
            n_deferred = len(plan) - pair_index
            log.warning(
                {
                    "event": "quota_window_deferred",
                    "cadence": cadence,
                    "deferred": n_deferred,
                    "stop_before_local": args.stop_before_local,
                }
            )
            break
        lens = LENSES.get(lens_name)
        if lens is None:
            log.warning({"event": "lens_not_found", "lens": lens_name})
            n_skipped += 1
            continue
        try:
            result = run_lens(
                lens,
                ticker=ticker if lens.scope == "ticker" else None,
                repo_root=repo_root,
                force=False,
            )
        except Exception as exc:
            if is_hard_stop(exc):
                log.error(
                    {
                        "event": "lens_hard_stop",
                        "tier": tier,
                        "ticker": ticker,
                        "lens": lens_name,
                        "error": redact(f"{type(exc).__name__}: {exc}"),
                    }
                )
                return 2
            n_deferred_transient += 1
            log.warning(
                {
                    "event": "lens_deferred_transient",
                    "tier": tier,
                    "ticker": ticker,
                    "lens": lens_name,
                    "error": redact(f"{type(exc).__name__}: {exc}"),
                }
            )
            continue
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
                "deferred": n_deferred,
                "deferred_transient": n_deferred_transient,
                "cache_hits_subset_of_produced": n_cache_hits,
            },
            indent=2,
        )
    )
    return _DEFERRED_EXIT if n_deferred or n_deferred_transient else 0


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
            rows = conn.execute(
                "SELECT UPPER(ticker) AS ticker, list_type "
                "FROM tracked_companies WHERE archived_at IS NULL"
            ).fetchall()
            tier_by_ticker = {
                r["ticker"]: schedule_class_for_list_type(str(r["list_type"])).value for r in rows
            }

    target_tier = {"daily": "P1", "weekly": "P2", "monthly": "P3"}[cadence]
    plan: list[tuple[str, str, str]] = []
    for lens_name in sorted(_tier_lens_set(target_tier)):
        due = set(tickers_due_for_lens_regen(repo_root, lens_name, cadence))
        for ticker in sorted(due):
            tier = tier_by_ticker.get(ticker.upper(), "P3")
            if tier != target_tier:
                continue
            plan.append((tier, ticker.upper(), lens_name))

    # Portfolio-scoped lens — only P1 cadence applies and only on the daily +
    # weekly ticks. Treat the portfolio lens as having a single sentinel
    # "ticker" for plan-tracking purposes.
    if target_tier == "P1" and _portfolio_synthesis_is_due(repo_root, cadence):
        for portfolio_lens in list_portfolio_lenses():
            plan.append(("P1", "__PORTFOLIO__", portfolio_lens))

    return plan


def _scheduled_window_deadline(datetime_now: datetime, *, opens_at: str, stops_at: str) -> datetime:
    """Return this cross-midnight window's stop, or now when outside it."""
    opens = time.fromisoformat(opens_at)
    stops = time.fromisoformat(stops_at)
    if opens <= stops:
        raise ValueError("scheduled quota window must cross midnight")
    current = datetime_now.timetz().replace(tzinfo=None)
    if current >= opens:
        stop_day = datetime_now.date() + timedelta(days=1)
    elif current < stops:
        stop_day = datetime_now.date()
    else:
        return datetime_now
    return datetime_now.replace(
        year=stop_day.year,
        month=stop_day.month,
        day=stop_day.day,
        hour=stops.hour,
        minute=stops.minute,
        second=stops.second,
        microsecond=0,
    )


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
        "--max-plan-pairs",
        type=int,
        default=0,
        help="Fail closed before dispatch when the full plan exceeds this size (0 = disabled).",
    )
    p.add_argument(
        "--stop-before-local",
        default="",
        help="Defer remaining pairs before this America/Los_Angeles HH:MM boundary.",
    )
    p.add_argument(
        "--window-opens-local",
        default="",
        help="Only dispatch inside the cross-midnight window beginning at this local HH:MM.",
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
