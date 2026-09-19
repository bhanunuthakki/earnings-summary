"""Reconcile ``tracked_companies.list_type`` against the tracker's live holdings.

The rule (see :mod:`list_type_reconcile`): an operating company held over the
threshold is a ``portfolio`` name; a ``portfolio`` name not held over the
threshold demotes to ``evaluation`` (which keeps every brief/KPI — both tiers are
briefed). ETFs/funds/options/cash are never promoted. Names in
``data/portfolio_pins.json`` stay pinned to ``portfolio`` regardless of holdings.

Dry-run by default — it prints the diff and writes nothing. Pass ``--apply`` to
write, after which it re-syncs the issuer registry (the documented reconcile
behind raw ``list_type`` flips). Idempotent: a second ``--apply`` against the
same world is a no-op.

Untracked held equities remain review-gated. After inspecting the dry-run,
``--apply --onboard-untracked`` adds them through ``db.track_company`` so SEC
validation, issuer-registry sync, and the normal onboarding chain are preserved.
The scheduled morning run never passes this flag.

The portfolio DB lives in MAIN (``data/`` is gitignored), so when running from a
worktree pass ``--repo-root <MAIN>`` (or ``--db-path``). The companion tracker DB
is found at ``<repo-root>/../portfolio-tracker/portfolio.db`` unless
``--tracker-db`` overrides it.

Usage:
    python execution/sync_list_type_from_holdings.py                 # dry-run
    python execution/sync_list_type_from_holdings.py --apply
    python execution/sync_list_type_from_holdings.py --apply --onboard-untracked
    python execution/sync_list_type_from_holdings.py --repo-root <MAIN> --apply
    python execution/sync_list_type_from_holdings.py --min-value 100 --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import cast

try:
    from execution._lib import PROJECT_ROOT
except ModuleNotFoundError:  # managed script launch adds execution/ as the import root
    from _lib import PROJECT_ROOT

from identity import DEFAULT_USER_ID
from list_type_reconcile import (
    Reclassification,
    apply_reclassification,
    compute_reclassification,
    load_pins,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _money(v: float) -> str:
    return f"${v:,.0f}"


def print_plan(
    plan: Reclassification,
    pins: dict[str, str],
    applied: bool,
    *,
    onboarded_untracked: bool,
) -> None:
    # ASCII-only output — Windows consoles default to cp1252 and choke on glyphs.
    verb = "APPLIED" if applied else "DRY-RUN (no writes -- pass --apply)"
    print(f"\n=== list_type reconcile :: {verb} ===")

    if plan.promotions:
        print(f"\n  [+] promote -> portfolio ({len(plan.promotions)})")
        for ticker, frm, mv in plan.promotions:
            print(f"      {ticker:8} {frm:>10} -> portfolio   held {_money(mv)}")
    if plan.demotions:
        print(f"\n  [-] demote -> evaluation ({len(plan.demotions)})  [stays fully briefed]")
        for ticker, mv in plan.demotions:
            held = _money(mv) if mv > 0 else "not held"
            print(f"      {ticker:8}  portfolio -> evaluation   ({held})")
    if plan.pinned_kept:
        print(f"\n  [PIN] kept in portfolio via pin ({len(plan.pinned_kept)})")
        for ticker, mv in plan.pinned_kept:
            reason = pins.get(ticker, "")
            held = _money(mv) if mv > 0 else "not held"
            print(f"      {ticker:8}  ({held})  {reason}")
    if plan.untracked_held:
        if onboarded_untracked:
            print(f"\n  [+] onboard -> portfolio ({len(plan.untracked_held)})")
        else:
            print(f"\n  [WARN] held > threshold but UNTRACKED ({len(plan.untracked_held)})")
        for ticker, sec_type, mv in plan.untracked_held:
            print(f"      {ticker:8}  {sec_type:8} held {_money(mv)}")
        if not onboarded_untracked:
            print("      Review, then rerun with --apply --onboard-untracked to add them.")
    if plan.unchanged_portfolio:
        print(
            f"\n  [OK] portfolio, correctly held ({len(plan.unchanged_portfolio)}): "
            f"{', '.join(plan.unchanged_portfolio)}"
        )

    if plan.holdings_unavailable:
        print(
            "\n  [SKIP] tracker reported zero holdings (outage / empty sync) -- "
            "refusing to reclassify on no data. No changes."
        )
        return
    if not plan.has_changes and not onboarded_untracked:
        print("\n  No changes -- list_type already matches holdings.")


def onboard_untracked(plan: Reclassification, *, db_path: Path, user_id: str) -> int:
    """Add reviewed tracker holdings through the governed tracking entrypoint."""
    import db

    resolved_db_path = db_path.resolve()
    # db.track_company launches the onboarder as a child process. Keep the
    # explicit authority visible to both this process and that child.
    os.environ["EARNINGS_SUMMARY_DB_PATH"] = str(resolved_db_path)
    db.set_db_path(resolved_db_path)
    for ticker, _sec_type, _market_value in plan.untracked_held:
        # SEC validation replaces this ticker fallback with the official issuer
        # name when available; a failed lookup remains visibly unvalidated.
        db.track_company(ticker, ticker, "portfolio", user_id)
    return len(plan.untracked_held)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    parser.add_argument(
        "--onboard-untracked",
        action="store_true",
        help="With --apply, add reviewed held-but-untracked equities through db.track_company.",
    )
    parser.add_argument(
        "--min-value",
        type=float,
        default=100.0,
        help="Position market-value threshold in USD (default 100).",
    )
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root holding data/portfolio.db (MAIN when run from a worktree).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override portfolio DB path (wins over --repo-root).",
    )
    parser.add_argument(
        "--tracker-db", type=Path, default=None, help="Override companion tracker DB path."
    )
    args = parser.parse_args()
    if args.onboard_untracked and not args.apply:
        parser.error("--onboard-untracked requires --apply after reviewing the dry-run")

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    tracker_db: Path = (
        args.tracker_db
        if args.tracker_db is not None
        else repo_root.parent / "portfolio-tracker" / "portfolio.db"
    )

    if not db_path.exists():
        # The MAIN DB is the world we reconcile — its absence is a real failure
        # (exit 1 so a scheduled run surfaces it).
        print(
            f"ERROR: portfolio DB not found at {db_path}. Pass --repo-root <MAIN> or --db-path.",
            file=sys.stderr,
        )
        return 1
    if not tracker_db.exists():
        # No holdings to reconcile against → SOFT no-op (exit 0), never a hard
        # failure. Reclassifying on absent holdings would demote the whole book;
        # refusing is the safe move. Mirrors position_lifecycle's tracker-offline
        # degrade. The compute-side holdings_unavailable latch is the second line
        # of defence for an empty (but present) tracker DB.
        print(
            f"WARNING: tracker DB not found at {tracker_db} -- skipping list_type "
            "reconcile (no holdings to reconcile against; no changes made).",
            file=sys.stderr,
        )
        return 0

    pins = load_pins(repo_root)
    es_conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    es_conn.row_factory = sqlite3.Row
    tracker_conn = connect_sqlite(tracker_db, role=SQLiteConnectionRole.READ_ONLY)
    tracker_conn.row_factory = sqlite3.Row
    try:
        plan = compute_reclassification(
            es_conn=es_conn,
            tracker_conn=tracker_conn,
            pins=set(pins),
            min_value=args.min_value,
            user_id=args.user_id,
        )
        if args.apply and plan.has_changes:
            tally = apply_reclassification(es_conn=es_conn, plan=plan, user_id=args.user_id)
            print(f"Wrote {tally['promoted']} promotion(s), {tally['demoted']} demotion(s).")
    finally:
        es_conn.close()
        tracker_conn.close()

    onboarded_count = 0
    if args.apply and args.onboard_untracked and plan.untracked_held:
        onboarded_count = onboard_untracked(plan, db_path=db_path, user_id=args.user_id)

    print_plan(
        plan,
        pins,
        applied=args.apply and (plan.has_changes or onboarded_count > 0),
        onboarded_untracked=onboarded_count > 0,
    )

    # Re-sync the issuer registry after raw list_type flips (its docstring names
    # this as the reconcile that catches non-trigger list_type writes).
    if args.apply and plan.has_changes:
        try:
            import issuer_registry

            result = cast("dict[str, object]", issuer_registry.sync_all(repo_root, db_path=db_path))
            print(f"\nIssuer registry synced: {result}")
        except Exception as exc:  # pragma: no cover - best-effort, never fail the reconcile
            print(
                f"\nWARNING: issuer registry sync failed ({exc!r}); "
                "run execution/sync_issuer_registry.py manually.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
