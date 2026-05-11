"""Refresh a ticker's DCF: read workbook → PV calc → live-price compare → persist.

The canonical workbook for each ticker lives at `dcf/<TICKER>.xlsx` under the
repo root. If absent (Phase 3 doesn't auto-seed), the CLI falls back to the
latest matching template in `examples/dcf/<TICKER>-*.xlsx`. Pass `--workbook
<path>` to override.

Per-ticker WACC, MoS bar, and terminal multiple come from
`micro_thesis/holdings/<TICKER>.json` (schema v2).

Live price comes from `data/historical/fmp/<TICKER>_profile.json` —
populated by the standard FMP refresh cron.

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
from dcf import valuation as valuation_mod  # noqa: E402
from dcf import workbook_reader  # noqa: E402

DCF_DIR_NAME = "dcf"
EXAMPLES_DIR_NAME = "examples/dcf"
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
        help="Override workbook path. Default: dcf/<TICKER>.xlsx with examples/dcf/ fallback.",
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
    holdings = _load_holdings(repo_root, ticker)
    if holdings is None:
        return {"ticker": ticker, "status": "skipped", "reason": "no holdings JSON"}

    wacc = holdings.get("wacc")
    if not isinstance(wacc, (int, float)):
        return {
            "ticker": ticker,
            "status": "skipped",
            "reason": "wacc not populated in holdings JSON",
        }
    from typing import cast

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
        }
    mos_bar = holdings.get("mos_bar")
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    workbook_path = _resolve_workbook(repo_root, ticker, args.workbook)
    if workbook_path is None:
        return {
            "ticker": ticker,
            "status": "skipped",
            "reason": f"no workbook at dcf/{ticker}.xlsx or examples/dcf/{ticker}-*.xlsx",
        }

    try:
        snapshot = workbook_reader.read_valuation(workbook_path, args.valuation_year)
    except workbook_reader.WorkbookReadError as e:
        return {
            "ticker": ticker,
            "status": "failed",
            "reason": str(e),
            "workbook": str(workbook_path),
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
    """Resolve the workbook path: --workbook flag, then dcf/<TICKER>.xlsx, then
    the latest examples/dcf/<TICKER>-*.xlsx fallback.
    """
    if override is not None:
        return override.resolve() if override.exists() else None
    primary = repo_root / DCF_DIR_NAME / f"{ticker.upper()}.xlsx"
    if primary.exists():
        return primary
    fallback_dir = repo_root / EXAMPLES_DIR_NAME
    if not fallback_dir.exists():
        return None
    matches = sorted(fallback_dir.glob(f"{ticker.upper()}-*.xlsx"))
    return matches[-1] if matches else None


if __name__ == "__main__":
    raise SystemExit(main())
