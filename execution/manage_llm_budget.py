"""Manage per-purpose LLM monthly budgets — list, set, month-end report.

The budget tables (migration 0052) carry the caps; this CLI is the
operator's surface for inspecting + adjusting them. Pairs with
`show_llm_spend.py` (raw spend) — this script focuses on cap utilization.

Usage:
    # List all purposes + caps + current-month spend
    python execution/manage_llm_budget.py --list

    # Update a single cap (USD)
    python execution/manage_llm_budget.py --set bear_case --cap 75

    # Flip a purpose to hard-block (raises LLMBudgetExceeded over cap)
    python execution/manage_llm_budget.py --set bear_case --hard-block

    # Back to soft cap (warn + proceed)
    python execution/manage_llm_budget.py --set bear_case --soft-cap

    # Month-end report (defaults to current month)
    python execution/manage_llm_budget.py --report
    python execution/manage_llm_budget.py --report --month 2026-04
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_budget import (  # noqa: E402
    list_budgets,
    month_report,
    set_cap,
)


def _fmt_usd(v: Decimal | float | None) -> str:
    if v is None:
        return "—"
    f = float(v)
    if f < 0.01:
        return f"${f:.4f}"
    return f"${f:.2f}"


def _fmt_pct(headroom_pct: float | None) -> str:
    """Convert headroom_pct (1.0 = none spent) to % spent display.

    Returns a colour-coded marker for tty users so over-cap rows pop:
      * "(OVER)" prefix when headroom < 0
      * "(WARN)" prefix when 0 <= headroom < 0.20
      * "(ok)"   prefix when headroom >= 0.20
    """
    if headroom_pct is None:
        return "—"
    pct_spent = 100.0 * (1.0 - headroom_pct)
    if headroom_pct < 0:
        return f"(OVER) {pct_spent:6.1f}%"
    if headroom_pct < 0.20:
        return f"(WARN) {pct_spent:6.1f}%"
    return f"(ok)   {pct_spent:6.1f}%"


def _cmd_list(db_path: Path) -> int:
    rows = list_budgets(db_path=db_path)
    if not rows:
        sys.stderr.write(
            f"No llm_budgets rows in {db_path}. Run `python -m alembic upgrade head` first.\n"
        )
        return 1
    now = datetime.now(UTC)
    print(f"=== LLM BUDGETS — {now.strftime('%Y-%m')} ===")
    print(f"  DB: {db_path}")
    print()
    print(
        f"  {'purpose':<32s} {'cap':>9s} {'spend':>9s} {'used':>16s} {'block':>5s}  {'updated_at'}"
    )
    for r in rows:
        # list_budgets returns dict[str, object] for JSON-boundary tolerance;
        # cast at the read site per the project's pyright convention.
        block = "HARD" if r["hard_block"] else "soft"
        spend = cast("Decimal", r["current_spend_usd"])
        cap = cast("Decimal", r["monthly_cap_usd"])
        headroom = cast("float", r["headroom_pct"])
        purpose = cast("str", r["purpose"])
        updated_at = cast("str", r["updated_at"])
        print(
            f"  {purpose:<32s} {_fmt_usd(cap):>9s} {_fmt_usd(spend):>9s} "
            f"{_fmt_pct(headroom):>16s} {block:>5s}  {updated_at}"
        )
    print()
    total_cap = sum(
        float(cast("Decimal", r["monthly_cap_usd"])) for r in rows if r["purpose"] != "__default__"
    )
    total_spend = sum(
        float(cast("Decimal", r["current_spend_usd"]))
        for r in rows
        if r["purpose"] != "__default__"
    )
    print(f"  Total budgeted (excl __default__): ${total_cap:,.2f}/mo")
    print(f"  Total spend this month:            ${total_spend:,.2f}")
    print(f"  Net headroom:                      ${total_cap - total_spend:,.2f}")
    return 0


def _cmd_set(db_path: Path, purpose: str, cap: float | None, hard_block: bool | None) -> int:
    """Update the cap and/or hard_block flag for `purpose`."""
    if cap is None and hard_block is None:
        sys.stderr.write("--set requires at least one of --cap, --hard-block, --soft-cap\n")
        return 2
    # Cap update goes through llm_budget.set_cap so the formatting is shared
    # with any other caller. hard_block toggling is direct SQL — it's a
    # one-column boolean update and doesn't merit a wrapper of its own.
    updated_any = False
    if cap is not None:
        if cap < 0:
            sys.stderr.write(f"--cap must be >= 0, got {cap}\n")
            return 2
        ok = set_cap(purpose, cap, db_path=db_path)
        if not ok:
            sys.stderr.write(
                f"No row for purpose={purpose!r} in llm_budgets — nothing updated.\n"
                "(Tip: spelling? See `--list` for valid purposes.)\n"
            )
            return 1
        print(f"Set cap for {purpose!r} ->{_fmt_usd(cap)}/mo")
        updated_any = True
    if hard_block is not None:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                UPDATE llm_budgets
                SET hard_block = ?, updated_at = ?
                WHERE purpose = ?
                """,
                (
                    1 if hard_block else 0,
                    datetime.now(UTC).isoformat(),
                    purpose,
                ),
            )
            conn.commit()
            if cur.rowcount == 0:
                sys.stderr.write(
                    f"No row for purpose={purpose!r} in llm_budgets — block flag unchanged.\n"
                )
                return 1 if not updated_any else 0
            print(
                f"Set hard_block for {purpose!r} ->"
                f"{'HARD (raise on cap)' if hard_block else 'soft (warn on cap)'}"
            )
        finally:
            conn.close()
    return 0


def _cmd_report(db_path: Path, month: str | None) -> int:
    rows = month_report(month=month, db_path=db_path)
    label = month or datetime.now(UTC).strftime("%Y-%m")
    if not rows:
        print(f"=== LLM BUDGET REPORT — {label} ===")
        print(f"  No llm_budgets data in {db_path} (or no calls this month yet).")
        return 0
    print(f"=== LLM BUDGET REPORT — {label} ===")
    print(f"  DB: {db_path}")
    print()
    print(f"  {'purpose':<32s} {'cap':>9s} {'spend':>9s} {'calls':>6s} {'used':>16s} {'block'}")
    total_spend = Decimal("0")
    total_capped = Decimal("0")
    for r in rows:
        # month_report returns dict[str, object]; cast at the read site.
        purpose = cast("str", r["purpose"])
        spend = cast("Decimal", r["spend_usd"])
        cap_obj = r["cap_usd"]  # may be None when purpose is unbudgeted
        calls = cast("int", r["calls"])
        headroom_obj = r["headroom_pct"]
        block_flag = r["hard_block"]
        block = "—" if block_flag is None else ("HARD" if block_flag else "soft")
        # Exclude __default__ from the totals — it's the fallback cap for
        # purposes with no row of their own, not a real spend bucket.
        if purpose != "__default__":
            total_spend += spend
        cap_for_fmt: Decimal | None = cap_obj if isinstance(cap_obj, Decimal) else None
        if cap_for_fmt is not None and purpose != "__default__":
            total_capped += cap_for_fmt
        headroom_for_fmt = headroom_obj if isinstance(headroom_obj, float) else None
        print(
            f"  {purpose:<32s} {_fmt_usd(cap_for_fmt):>9s} {_fmt_usd(spend):>9s} "
            f"{calls:>6d} "
            f"{_fmt_pct(headroom_for_fmt):>16s} {block}"
        )
    print()
    print(f"  Total spend this month (excl __default__): {_fmt_usd(total_spend)}")
    print(f"  Total budgeted (excl __default__):         {_fmt_usd(total_capped)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout so em-dashes and the OK/WARN markers render on
    # Windows consoles (whose default cp1252 codepage breaks on U+2014).
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        with suppress(OSError):
            reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
        help="Path to portfolio.db (default: this project's DB).",
    )
    parser.add_argument("--list", action="store_true", help="List all budgets")
    parser.add_argument(
        "--set",
        dest="set_purpose",
        default=None,
        help="Purpose to update (use with --cap and/or --hard-block/--soft-cap)",
    )
    parser.add_argument("--cap", type=float, default=None, help="New monthly cap (USD)")
    block_group = parser.add_mutually_exclusive_group()
    block_group.add_argument(
        "--hard-block",
        dest="hard_block",
        action="store_const",
        const=True,
        help="Flip purpose to hard-block (raise LLMBudgetExceeded over cap)",
    )
    block_group.add_argument(
        "--soft-cap",
        dest="hard_block",
        action="store_const",
        const=False,
        help="Flip purpose to soft cap (warn + proceed over cap)",
    )
    parser.add_argument("--report", action="store_true", help="Month-end report")
    parser.add_argument(
        "--month", default=None, help="YYYY-MM for --report (default: current month)"
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        sys.stderr.write(f"DB not found at {db_path}\n")
        return 2

    if args.list:
        return _cmd_list(db_path)
    if args.set_purpose:
        return _cmd_set(
            db_path,
            args.set_purpose,
            args.cap,
            args.hard_block,
        )
    if args.report:
        return _cmd_report(db_path, args.month)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
