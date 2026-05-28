"""Refresh a ticker's DCF: seed/refresh workbook -> PV calc -> live-price -> persist.

The canonical workbook for each ticker lives at `dcf/<TICKER>.xlsx`. On each run:

  * Missing workbook -> `dcf.seeder` builds three sheets from scratch:
      Historicals  (program-owned, 20 quarters of FMP data)
      Forecast     (INPUTS auto-derived from this ticker's TTM history;
                    PROJECTED computed from inputs)
      Valuation    (year headers + FCF row + diluted-shares row, sized
                    so `workbook_reader` reads exactly what feeds the PV)

  * Existing workbook -> `dcf.refresher`:
      - rewrites Historicals from latest FMP
      - preserves the user's Forecast INPUTS edits
      - recomputes Forecast PROJECTED + Valuation from current INPUTS

The user's iteration loop: open the workbook in Excel, edit any Forecast
INPUT cell (yellow-filled), save, re-run refresh. The recomputed Valuation
feeds the PV calc and `dcf_runs` row that briefs read from.

Per-ticker WACC, MoS bar, and terminal multiple come from
`micro_thesis/holdings/<TICKER>.json`. Live price comes from
`data/historical/fmp/<TICKER>_profile.json`.

Usage:
    python execution/refresh_dcf.py --ticker META
    python execution/refresh_dcf.py --ticker META --workbook dcf/META.xlsx
    python execution/refresh_dcf.py --all-named  # all 12 named holdings with WACC
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import live_price as live_price_mod  # noqa: E402
from dcf import persist as persist_mod  # noqa: E402
from dcf import refresher as refresher_mod  # noqa: E402
from dcf import seeder as seeder_mod  # noqa: E402
from dcf import valuation as valuation_mod  # noqa: E402
from dcf import workbook_reader  # noqa: E402

DCF_DIR_NAME = "dcf"
FMP_QUARTERLY_DIR = Path("data") / "historical" / "fmp"
CURRENCY_DEFAULT = "USD"


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print(json.dumps({"event": "no_tickers", "detail": "nothing to refresh"}))
        return 0

    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    results: list[dict[str, object]] = []
    for ticker in tickers:
        result = _refresh_one(ticker, repo_root, db_path, args)
        results.append(result)
    print(json.dumps(results, indent=2, default=str))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to refresh")
    g.add_argument(
        "--all-named",
        action="store_true",
        help="Refresh all holdings JSONs that have a populated `wacc` field",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, dcf/, micro_thesis/. Default: this repo.",
    )
    p.add_argument(
        "--workbook",
        type=Path,
        default=None,
        help="Override workbook path. Default: dcf/<TICKER>.xlsx.",
    )
    p.add_argument(
        "--valuation-year",
        type=int,
        default=date.today().year,
        help="Cutoff year: > this is forecast, <= is actuals. Default: current calendar year.",
    )
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    if args.all_named:
        return _named_holdings_with_wacc(repo_root)
    return []


def _named_holdings_with_wacc(repo_root: Path) -> list[str]:
    """Return tickers whose holdings JSON has a non-null `wacc` (seeded names)."""
    out: list[str] = []
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    for path in sorted(holdings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data.get("wacc"), (int, float)):
            out.append(path.stem.upper())
    return out


def _refresh_one(
    ticker: str, repo_root: Path, db_path: Path, args: argparse.Namespace
) -> dict[str, object]:
    """Run the full refresh chain for one ticker. Returns a structured result."""
    from typing import cast

    holdings = _load_holdings(repo_root, ticker)
    if holdings is None:
        return {"ticker": ticker, "status": "skipped", "reason": "no holdings JSON"}

    workbook_path = _resolve_workbook(repo_root, ticker, args.workbook)
    fmp_dir = repo_root / FMP_QUARTERLY_DIR

    # Seed-or-refresh BEFORE the WACC / terminal-multiple gates, so an un-WACC'd
    # ticker still gets a workbook for the user to author a WACC against. For a
    # recently-IPO'd, S-1-anchored name with no FMP quarterly files, Historicals
    # are seeded from financial_facts (db_path enables that fallback).
    # Missing workbook → seed (derives Forecast INPUTS from the ticker's TTM
    # history). Existing workbook → refresh (preserves the user's Forecast INPUTS
    # edits, recomputes PROJECTED + Valuation from them, refreshes Historicals).
    seed_refresh: dict[str, object] = {}
    try:
        if workbook_path is None:
            new_path = repo_root / DCF_DIR_NAME / f"{ticker}.xlsx"
            seeder_mod.seed_workbook(
                ticker,
                fmp_quarterly_dir=fmp_dir,
                output_path=new_path,
                base_year=args.valuation_year,
                db_path=db_path,
            )
            workbook_path = new_path
            seed_refresh = {"workbook": "seeded"}
        else:
            try:
                refresh_result = refresher_mod.refresh_historicals(
                    workbook_path,
                    fmp_dir,
                    ticker=ticker,
                    base_year=args.valuation_year,
                    db_path=db_path,
                )
                seed_refresh = {
                    "workbook": "refreshed",
                    "historicals_cells": refresh_result.historicals_cells_written,
                    "y1_growth": refresh_result.forecast_inputs.y1_growth_pct,
                    "y1_op_margin": refresh_result.forecast_inputs.y1_operating_margin_pct,
                    "y5_op_margin": refresh_result.forecast_inputs.y5_operating_margin_pct,
                    "y1_capex_intensity": refresh_result.forecast_inputs.y1_capex_intensity_pct,
                    "y5_capex_intensity": refresh_result.forecast_inputs.y5_capex_intensity_pct,
                    "tax_rate": refresh_result.forecast_inputs.tax_rate_pct,
                }
            except refresher_mod.RefresherError as e:
                seed_refresh = {"workbook": "refresh_failed", "reason": str(e)}
    except seeder_mod.SeederError as e:
        return {
            "ticker": ticker,
            "status": "failed",
            "reason": f"seed: {e}",
            "workbook": str(workbook_path) if workbook_path else None,
        }

    # WACC gate — after seeding. The workbook now exists; the user authors WACC
    # into the holdings JSON next, then re-runs to get a valuation.
    wacc = holdings.get("wacc")
    if not isinstance(wacc, (int, float)):
        return {
            "ticker": ticker,
            "status": "skipped",
            "reason": "wacc not populated in holdings JSON",
            "workbook_path": str(workbook_path),
            **seed_refresh,
        }

    dcf_defaults_raw = holdings.get("dcf_defaults")
    dcf_defaults: dict[str, object] = {}
    if isinstance(dcf_defaults_raw, dict):
        dcf_defaults = cast("dict[str, object]", dcf_defaults_raw)
    terminal_multiple = dcf_defaults.get("terminal_multiple")
    if not isinstance(terminal_multiple, (int, float)):
        return {
            "ticker": ticker,
            "status": "skipped",
            "reason": "dcf_defaults.terminal_multiple not populated",
            "workbook_path": str(workbook_path),
            **seed_refresh,
        }
    mos_bar = holdings.get("mos_bar")
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    try:
        snapshot = workbook_reader.read_valuation(workbook_path, args.valuation_year)
    except workbook_reader.WorkbookReadError as e:
        return {
            "ticker": ticker,
            "status": "failed",
            "reason": str(e),
            "workbook": str(workbook_path),
            **seed_refresh,
        }

    fcf_stream = [snapshot.fcf_by_year[y] for y in snapshot.forecast_years[:5]]
    forecast_years_used = snapshot.forecast_years[:5]
    diluted_shares_M = snapshot.shares_by_year.get(snapshot.latest_actual_year)
    if diluted_shares_M is None:
        return {
            "ticker": ticker,
            "status": "failed",
            "reason": f"no diluted shares for {snapshot.latest_actual_year}",
            "workbook": str(workbook_path),
        }

    pv = valuation_mod.compute_pv_per_share(
        fcf_stream=fcf_stream,
        forecast_years=forecast_years_used,
        terminal_multiple=float(terminal_multiple),
        wacc=float(wacc),
        diluted_shares_M=diluted_shares_M,
    )

    live = live_price_mod.read_live_price(repo_root, ticker)
    over_under = (
        valuation_mod.over_under_pct(live.price, pv.fair_value_per_share)
        if live is not None
        else None
    )

    row = persist_mod.DcfRunRow(
        ticker=ticker,
        valuation_date=date.today(),
        horizon_years=len(forecast_years_used),
        wacc=float(wacc),
        npv=pv.enterprise_value,
        npv_per_share=pv.fair_value_per_share,
        shares_outstanding=diluted_shares_M * 1_000_000.0,
        currency=CURRENCY_DEFAULT,
        live_price=live.price if live else None,
        live_price_at=live.fetched_at if live else None,
        over_under_pct=over_under,
        mos_bar_used=mos_bar_f,
        assumption_snapshot_json=persist_mod.build_assumption_snapshot(
            fcf_stream=fcf_stream,
            forecast_years=forecast_years_used,
            wacc=float(wacc),
            terminal_multiple=float(terminal_multiple),
            diluted_shares_M=diluted_shares_M,
            workbook_path=str(workbook_path),
            pv_fcf_stream=pv.pv_fcf_stream,
            pv_terminal=pv.pv_terminal,
        ),
        notes=f"workbook={workbook_path.name}",
    )

    with sqlite3.connect(str(db_path)) as conn:
        persist_mod.upsert(conn, row)

    return {
        "ticker": ticker,
        "status": "ok",
        "workbook": str(workbook_path),
        "valuation_year": args.valuation_year,
        "forecast_years": forecast_years_used,
        "fair_value_per_share": pv.fair_value_per_share,
        "enterprise_value_M": pv.enterprise_value,
        "live_price": live.price if live else None,
        "over_under_pct": over_under,
        "mos_bar": mos_bar_f,
        **seed_refresh,
    }


def _load_holdings(repo_root: Path, ticker: str) -> dict[str, object] | None:
    from typing import cast

    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return cast("dict[str, object]", data)


def _resolve_workbook(repo_root: Path, ticker: str, override: Path | None) -> Path | None:
    """Resolve the workbook path: --workbook flag, then dcf/<TICKER>.xlsx.

    Returns None if neither is present — the caller treats that as the
    seed-needed branch and writes a fresh workbook at dcf/<TICKER>.xlsx.
    The examples/dcf/ directory holds read-only templates; never refresh in
    place there.
    """
    if override is not None:
        return override.resolve() if override.exists() else None
    primary = repo_root / DCF_DIR_NAME / f"{ticker.upper()}.xlsx"
    return primary if primary.exists() else None


if __name__ == "__main__":
    raise SystemExit(main())
