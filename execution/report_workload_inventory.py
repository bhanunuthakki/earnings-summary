"""Read-only workload inventory report (meta_eval_governance.md §1.1, PR1).

Renders the leverage-ranked per-purpose inventory from the ``llm_calls`` ledger —
the deterministic floor the Opus nominator will later read, and a replacement for
re-deriving ``cheapest_model_routing.md`` §4 by hand. Pure read; safe to run
anywhere, including against MAIN's ``data/portfolio.db``:

    python execution/report_workload_inventory.py --repo-root <MAIN>
    python execution/report_workload_inventory.py --db path/to/portfolio.db --since 30
    python execution/report_workload_inventory.py --repo-root <MAIN> --json

The ranking key is ``headroom_usd_30d`` = cost × current-tier downgrade headroom, so
a $112/mo purpose no longer ranks equal to a $0.30/mo one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.workload_inventory import (  # noqa: E402
    WINDOW_DAYS,
    build_workload_inventory,
    render_inventory_text,
    risk_note_for,
)


def _resolve_db(repo_root: Path, db_override: Path | None) -> Path:
    """Explicit ``--db`` wins; else ``<repo-root>/data/portfolio.db`` (data lives in
    MAIN, not the worktree — repo convention)."""
    if db_override is not None:
        return db_override
    return repo_root / "data" / "portfolio.db"


def _anon_footer(db_path: Path, since_days: int) -> str | None:
    """A one-line echo of ungoverned (purpose=NULL / unregistered) spend the inventory
    can't rank — reusing the Optimizer panel's alarm semantics. Best-effort."""
    try:
        from pipeline.model_eval_panel import load_anon_costs

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            anon = load_anon_costs(conn, window_days=since_days)
        finally:
            conn.close()
    except (ImportError, sqlite3.Error):
        return None
    if not anon:
        return None
    total = sum(a.cost_usd for a in anon)
    return (
        f"\n! {len(anon)} anonymous/unregistered line(s) - ${total:,.2f}/{since_days}d "
        "ungoverned (not rankable; see the Optimizer panel's anonymous-purpose alarm)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Data repo holding data/portfolio.db (default: this checkout)",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="Explicit portfolio.db path (overrides --repo-root)"
    )
    parser.add_argument(
        "--since",
        type=int,
        default=WINDOW_DAYS,
        help=f"Trailing window in days (default {WINDOW_DAYS})",
    )
    parser.add_argument("--max-rows", type=int, default=60, help="Rows to show (0 = all)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the text table")
    args = parser.parse_args()

    db_path: Path = args.db if args.db is not None else _resolve_db(args.repo_root, None)
    if not db_path.exists():
        sys.stderr.write(f"DB not found at {db_path}\n")
        return 2

    rows = build_workload_inventory(db_path, window_days=args.since)

    if args.json:
        payload = [{**dataclasses.asdict(r), "risk_note": risk_note_for(r.purpose)} for r in rows]
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(render_inventory_text(rows, window_days=args.since, max_rows=args.max_rows))
    footer = _anon_footer(db_path, args.since)
    if footer:
        print(footer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
