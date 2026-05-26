"""One-off: re-derive the auto-FCF-decomposition DCF for the 12 named tickers
and emit a markdown table. Reads FMP data from the main repo's data/ dir,
holdings JSON from there too, but does NOT touch the main repo's dcf/
workbooks or DB — every workbook is written to a temp dir.

Run from the worktree:
    python scratch/backfill_dcf_decomposition.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
MAIN_REPO = WORKTREE.parents[2]
sys.path.insert(0, str(WORKTREE / "src"))

from dcf import forecast as forecast_mod  # noqa: E402
from dcf import seeder as seeder_mod  # noqa: E402
from dcf import valuation as valuation_mod  # noqa: E402
from dcf import workbook_reader  # noqa: E402

TICKERS = [
    "AMZN", "GOOG", "META", "MELI", "NU", "NVO",
    "NOW", "WIX", "RBRK", "VEEV", "BN", "LLY",
]
FMP_DIR = MAIN_REPO / "data" / "historical" / "fmp"
HOLDINGS_DIR = MAIN_REPO / "micro_thesis" / "holdings"
BASE_YEAR = 2026


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _holdings(ticker: str) -> dict[str, object] | None:
    path = HOLDINGS_DIR / f"{ticker}.json"
    if not path.exists():
        return None
    raw = _load_json(path)
    return raw if isinstance(raw, dict) else None  # type: ignore[return-value]


def _live_price(ticker: str) -> float | None:
    path = FMP_DIR / f"{ticker}_profile.json"
    if not path.exists():
        return None
    raw = _load_json(path)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        v = raw[0].get("price")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _row_for(ticker: str, workdir: Path) -> dict[str, object]:
    holdings = _holdings(ticker)
    if holdings is None:
        return {"ticker": ticker, "error": "no holdings JSON"}

    wacc = holdings.get("wacc")
    dcf_defaults_raw = holdings.get("dcf_defaults")
    dcf_defaults = dcf_defaults_raw if isinstance(dcf_defaults_raw, dict) else {}
    terminal_multiple = dcf_defaults.get("terminal_multiple")
    if not isinstance(wacc, (int, float)) or not isinstance(terminal_multiple, (int, float)):
        return {"ticker": ticker, "error": "missing wacc or terminal_multiple"}

    workbook = workdir / f"{ticker}.xlsx"
    try:
        seeder_mod.seed_workbook(
            ticker,
            fmp_quarterly_dir=FMP_DIR,
            output_path=workbook,
            base_year=BASE_YEAR,
            force=True,
        )
    except seeder_mod.SeederError as e:
        return {"ticker": ticker, "error": f"seed failed: {e}"}

    import openpyxl  # noqa: E402
    wb = openpyxl.load_workbook(str(workbook), data_only=True)
    inputs = forecast_mod.read_inputs_from_sheet(wb[seeder_mod.FORECAST_SHEET])
    wb.close()
    projections = forecast_mod.compute_projections(inputs, BASE_YEAR)
    snap = workbook_reader.read_valuation(workbook, valuation_year=BASE_YEAR)
    fcf_stream = [snap.fcf_by_year[y] for y in snap.forecast_years[:5]]
    forecast_years_used = snap.forecast_years[:5]
    shares = snap.shares_by_year.get(snap.latest_actual_year, inputs.diluted_shares_M)
    pv = valuation_mod.compute_pv_per_share(
        fcf_stream=fcf_stream,
        forecast_years=forecast_years_used,
        terminal_multiple=float(terminal_multiple),
        wacc=float(wacc),
        diluted_shares_M=shares,
    )
    live = _live_price(ticker)
    return {
        "ticker": ticker,
        "ttm_rev_M": round(inputs.base_revenue_M),
        "y1_growth": inputs.y1_growth_pct,
        "y1_op_margin": inputs.y1_operating_margin_pct,
        "y5_op_margin": inputs.y5_operating_margin_pct,
        "y1_capex_int": inputs.y1_capex_intensity_pct,
        "y5_capex_int": inputs.y5_capex_intensity_pct,
        "tax_rate": inputs.tax_rate_pct,
        "y1_fcf_M": round(projections.fcf_M[0]),
        "y5_fcf_M": round(projections.fcf_M[-1]),
        "fair_value": pv.fair_value_per_share,
        "live_price": live,
    }


def _fmt_pct(v: object) -> str:
    return f"{float(v) * 100:.1f}%" if isinstance(v, (int, float)) else "-"


def _fmt_int(v: object) -> str:
    return f"{int(v):,}" if isinstance(v, (int, float)) else "-"


def _fmt_dollar(v: object) -> str:
    return f"${float(v):,.2f}" if isinstance(v, (int, float)) else "-"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        rows = [_row_for(t, workdir) for t in TICKERS]

    print()
    print("| Ticker | TTM Rev ($M) | Y1 Growth | Y1 OpM | Y5 OpM | Y1 Capex | Y5 Capex | Tax | Y1 FCF ($M) | Y5 FCF ($M) | Auto FV/share | Live |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        if "error" in r:
            print(f"| {r['ticker']} | — | — | — | — | — | — | — | — | — | **{r['error']}** | — |")
            continue
        print(
            f"| {r['ticker']} | {_fmt_int(r['ttm_rev_M'])} | "
            f"{_fmt_pct(r['y1_growth'])} | "
            f"{_fmt_pct(r['y1_op_margin'])} | "
            f"{_fmt_pct(r['y5_op_margin'])} | "
            f"{_fmt_pct(r['y1_capex_int'])} | "
            f"{_fmt_pct(r['y5_capex_int'])} | "
            f"{_fmt_pct(r['tax_rate'])} | "
            f"{_fmt_int(r['y1_fcf_M'])} | "
            f"{_fmt_int(r['y5_fcf_M'])} | "
            f"{_fmt_dollar(r['fair_value'])} | "
            f"{_fmt_dollar(r['live_price'])} |"
        )

    # Verdict
    positive_y5 = sum(
        1 for r in rows
        if isinstance(r.get("y5_fcf_M"), (int, float)) and float(r["y5_fcf_M"]) > 0
    )
    print()
    print(f"**Y5 FCF positive: {positive_y5} / {len(rows)}**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
