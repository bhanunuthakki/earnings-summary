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

The portfolio DB lives in MAIN (``data/`` is gitignored), so when running from a
worktree pass ``--repo-root <MAIN>`` (or ``--db-path``). The companion tracker DB
is found at ``<repo-root>/../portfolio-tracker/portfolio.db`` unless
``--tracker-db`` overrides it.

Usage:
    python execution/sync_list_type_from_holdings.py                 # dry-run
    python execution/sync_list_type_from_holdings.py --apply
    python execution/sync_list_type_from_holdings.py --repo-root <MAIN> --apply
    python execution/sync_list_type_from_holdings.py --min-value 100 --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from identity import DEFAULT_USER_ID  # noqa: E402
from list_type_reconcile import (  # noqa: E402
    Reclassification,
    apply_reclassification,
    compute_reclassification,
    load_pins,
)


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _print_plan(plan: Reclassification, pins: dict[str, str], applied: bool) -> None:
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
        print(
            f"\n  [WARN] held > threshold but UNTRACKED ({len(plan.untracked_held)}) "
            "-- onboard manually:"
        )
        for ticker, sec_type, mv in plan.untracked_held:
            print(f"      {ticker:8}  {sec_type:8} held {_money(mv)}")
    if plan.unchanged_portfolio:
        print(
            f"\n  [OK] portfolio, correctly held ({len(plan.unchanged_portfolio)}): "
            f"{', '.join(plan.unchanged_portfolio)}"
        )

    if not plan.has_changes:
        print("\n  No changes -- list_type already matches holdings.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
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
        print(
            f"ERROR: portfolio DB not found at {db_path}. Pass --repo-root <MAIN> or --db-path.",
            file=sys.stderr,
        )
        return 1
    if not tracker_db.exists():
        print(
            f"ERROR: tracker DB not found at {tracker_db}. Pass --tracker-db. "
            "Holdings are required to reconcile list_type.",
            file=sys.stderr,
        )
        return 1

    pins = load_pins(repo_root)
    es_conn = sqlite3.connect(str(db_path))
    es_conn.row_factory = sqlite3.Row
    tracker_conn = sqlite3.connect(f"file:{tracker_db.as_posix()}?mode=ro", uri=True)
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

    _print_plan(plan, pins, applied=args.apply and plan.has_changes)

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
