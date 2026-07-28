"""Audit sanity-flagged DCF runs (the trust gate, migration 0182).

Lists every ticker whose LATEST consolidated dcf_runs row is past the sanity
limit (|over_under_pct| > dcf.persist.SANITY_OVER_UNDER_LIMIT), cross-checked
against the FMP reported currency and the workbook FX persisted in the
assumption snapshot — so a unit/FX defect (the TSM class: non-USD reporter with
fx_to_usd=1.0) is distinguished from a genuinely stale/absurd model that needs
an assumptions review.

Read-only. Rerunnable. Human table on stdout (or --json for machine use);
structured events to stderr.

Usage:
    python execution/audit_dcf_outliers.py [--repo-root PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf.persist import SANITY_OVER_UNDER_LIMIT  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _reported_currency(repo_root: Path, ticker: str) -> str | None:
    p = repo_root / "data" / "historical" / "fmp" / f"{ticker}_income_statement_quarterly.json"
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        first = cast("dict[str, object]", rows[0])
        ccy = first.get("reportedCurrency")
        return str(ccy).upper() if isinstance(ccy, str) and ccy.strip() else None
    return None


def _snapshot_fx(snapshot_json: object) -> tuple[float | None, str | None]:
    """(fx_to_usd, reporting_currency) from an assumption snapshot, best-effort."""
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return None, None
    try:
        snap = json.loads(snapshot_json)
    except ValueError:
        return None, None
    if not isinstance(snap, dict):
        return None, None
    snap_d = cast("dict[str, object]", snap)
    fx = snap_d.get("fx_to_usd")
    ccy = snap_d.get("reporting_currency")
    return (
        float(fx) if isinstance(fx, (int, float)) else None,
        str(ccy) if isinstance(ccy, str) else None,
    )


def audit(repo_root: Path) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
        sanity_sel = "sanity_flag" if "sanity_flag" in cols else "NULL AS sanity_flag"
        latest_filter = "is_latest = 1" if "is_latest" in cols else "1=1"
        rows = conn.execute(
            f"""
            SELECT ticker, valuation_date, currency, npv_per_share, live_price,
                   over_under_pct, notes, assumption_snapshot_json, {sanity_sel}
            FROM dcf_runs
            WHERE {latest_filter}
              AND (segment_name IS NULL OR segment_name = '')
              AND over_under_pct IS NOT NULL
              AND ABS(over_under_pct) > ?
            ORDER BY ABS(over_under_pct) DESC
            """,
            (SANITY_OVER_UNDER_LIMIT,),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, object]] = []
    for r in rows:
        ticker = str(r["ticker"])
        fx, snap_ccy = _snapshot_fx(r["assumption_snapshot_json"])
        fmp_ccy = _reported_currency(repo_root, ticker)
        suspected_fx_defect = fmp_ccy not in (None, "USD") and (fx is None or fx == 1.0)
        out.append(
            {
                "ticker": ticker,
                "valuation_date": r["valuation_date"],
                "row_currency": r["currency"],
                "npv_per_share": r["npv_per_share"],
                "live_price": r["live_price"],
                "over_under_pct": r["over_under_pct"],
                "sanity_flag": r["sanity_flag"],
                "workbook_fx_to_usd": fx,
                "snapshot_reporting_currency": snap_ccy,
                "fmp_reported_currency": fmp_ccy,
                "suspected_fx_defect": suspected_fx_defect,
                "verdict": (
                    "FX/unit defect — rebuild workbook"
                    if suspected_fx_defect
                    else "model review — stale/aggressive assumptions"
                ),
            }
        )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    results = audit(args.repo_root.resolve())
    print(
        json.dumps({"event": "dcf_outlier_audit", "count": len(results)}),
        file=sys.stderr,
    )
    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0
    if not results:
        print(f"No latest runs past |over/under| > {SANITY_OVER_UNDER_LIMIT:.0%}. All clear.")
        return 0
    hdr = f"{'TICKER':<7} {'OVER/UNDER':>10} {'FV':>12} {'LIVE':>10} {'CCY':>4} {'FX':>7}  VERDICT"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        ou = float(r["over_under_pct"]) * 100  # type: ignore[arg-type]
        fv = r["npv_per_share"]
        live = r["live_price"]
        fx = r["workbook_fx_to_usd"]
        print(
            f"{r['ticker']:<7} {ou:>+9.0f}% "
            f"{(f'{fv:,.2f}' if isinstance(fv, (int, float)) else '—'):>12} "
            f"{(f'{live:,.2f}' if isinstance(live, (int, float)) else '—'):>10} "
            f"{(r['fmp_reported_currency'] or '?'):>4} "
            f"{(f'{fx:.3f}' if isinstance(fx, (int, float)) else '—'):>7}  {r['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
