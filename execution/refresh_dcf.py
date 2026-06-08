"""Refresh a ticker's redesigned DCF: rebuild workbook -> value -> live-price -> persist.

The canonical workbook for each ticker is the redesigned 9-sheet
`dcf/<TICKER>.xlsx` (Cover/Dashboard/Color Code/WACC/Model/Financials/Consensus/
Valuation/Monte Carlo). On each run, `refresh_one`:

  - re-runs `execution/build_redesigned_dcf.py` to rebuild every sheet
    (Financials/segments/Consensus/WACC/Model/Valuation/Monte Carlo) from the
    latest FMP, regenerating all formula links and current price;
  - PRESERVES the user-owned Dashboard inputs (the yellow assumption cells) by
    capturing them first and re-injecting them after the rebuild (only current
    price is refreshed, from the live quote);
  - recomputes the value-of-record in Python from those Dashboard inputs + the
    Financials actuals (openpyxl can't evaluate the formulas offline), via
    `dcf.redesign.read_and_value` -> `dcf.valuation.compute_valuation`, and
    upserts the `dcf_runs` row that briefs read from.

Names Opus flagged `dcf_applicable=false` (banks/insurers/asset-managers) are
skipped, matching the builder's SKIP.

The user's iteration loop: open the workbook in Sheets/Excel, edit any yellow
Dashboard cell, save, re-run refresh. Per-ticker MoS bar comes from
`micro_thesis/holdings/<TICKER>.json`; WACC and the terminal multiple live in the
workbook's Dashboard. Live price comes from the multi-source stack
(`sources.price`).

Usage:
    python execution/refresh_dcf.py --ticker META
    python execution/refresh_dcf.py --ticker META --workbook dcf/META.xlsx
    python execution/refresh_dcf.py --all-named  # every DCF-maintained name (portfolio + evaluation)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import live_price as live_price_mod  # noqa: E402
from dcf import persist as persist_mod  # noqa: E402
from dcf import redesign as redesign_mod  # noqa: E402
from dcf import universe as universe_mod  # noqa: E402
from dcf import valuation as valuation_mod  # noqa: E402

DCF_DIR_NAME = "dcf"
CURRENCY_DEFAULT = "USD"
_BUILDER_SCRIPT = PROJECT_ROOT / "execution" / "build_redesigned_dcf.py"
_BANK_BUILDER = PROJECT_ROOT / "execution" / "build_bank_dcf.py"
_HOLDCO_BUILDER = PROJECT_ROOT / "execution" / "build_holdco_sotp.py"


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
        result = refresh_one(
            ticker,
            repo_root,
            db_path,
            workbook_override=args.workbook,
            valuation_year=args.valuation_year,
        )
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
        help="Refresh every DCF-maintained name: portfolio + evaluation tracked "
        "companies (plus any legacy holdings JSON carrying a `wacc`)",
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
        return dcf_maintained_universe(repo_root)
    return []


def dcf_maintained_universe(repo_root: Path) -> list[str]:
    """The names a DCF is maintained for (what ``--all-named`` resolves to): every
    briefed-list ticker (portfolio + evaluation) from the DB, unioned with any
    legacy holdings JSON that still carries a hand-seeded ``wacc``. The redesigned
    builder computes its own WACC, so evaluation-list names qualify without a
    seeded ``wacc`` — first-class alongside portfolio names. Non-applicable
    financials self-skip in ``refresh_one``.
    """
    names = set(universe_mod.dcf_universe(repo_root))
    names.update(_named_holdings_with_wacc(repo_root))
    return sorted(names)


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


def refresh_one(
    ticker: str,
    repo_root: Path,
    db_path: Path,
    *,
    workbook_override: Path | None = None,
    valuation_year: int,
) -> dict[str, object]:
    """Refresh one ticker's redesigned DCF. Returns a structured result dict.

    Public so the Google-Sheets re-ingest (`execution/dcf_sheets.py import`) and
    the dashboard `/actions/dcf-import` can drive the same recompute + `dcf_runs`
    upsert.

    Dispatches on the ticker's `valuation_model` (see `_valuation_model`):
      - "fcff_dcf"            -> the redesigned FCFF DCF (`_refresh_redesign`).
      - "bank_excess_return"  -> the equity-side bank model (`_refresh_bank`).
      - "holdco_sotp"         -> the sum-of-the-parts model (`_refresh_holdco`).
      - "new"/"none"/unknown  -> skip, surfacing any Opus-proposed new-model spec.
    """
    model, suggestion = _valuation_model(repo_root, ticker)
    if model == "bank_excess_return":
        return _refresh_bank(ticker, repo_root)
    if model == "holdco_sotp":
        return _refresh_holdco(ticker, repo_root)
    if model != "fcff_dcf":
        # "new" (Opus proposed an archetype the pipeline doesn't have yet), "none",
        # or an unknown model string — no template to run.
        reason = f"no valuation template ({model})"
        if suggestion:
            reason += f" — SUGGESTS: {suggestion}"
        return {
            "ticker": ticker.upper(),
            "status": "skipped",
            "reason": reason,
            "valuation_model": model,
        }

    dest = (
        workbook_override.resolve()
        if workbook_override is not None
        else repo_root / DCF_DIR_NAME / f"{ticker.upper()}.xlsx"
    )
    return _refresh_redesign(ticker, repo_root, db_path, dest=dest, valuation_year=valuation_year)


def _dcf_not_applicable(repo_root: Path, ticker: str) -> str | None:
    """Return a reason if Opus flagged this name `dcf_applicable=false` (a
    bank/insurer/asset-manager an FCFF DCF can't value), else None.

    Mirrors the builder, which prints SKIP and writes no workbook for these.
    """
    path = repo_root / "data" / "dcf_assumptions" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    redesign_obj = cast("dict[str, object]", raw).get("redesign")
    if not isinstance(redesign_obj, dict):
        return None
    redesign_data = cast("dict[str, object]", redesign_obj)
    if redesign_data.get("dcf_applicable") is False:
        bm = redesign_data.get("business_model")
        return bm if isinstance(bm, str) else "not applicable"
    return None


def _valuation_model(repo_root: Path, ticker: str) -> tuple[str, str | None]:
    """The valuation archetype to run for `ticker`, plus any Opus 'new-model'
    suggestion. Resolution order:
      1. holdings ``valuation_model`` override (committed, user-owned — wins),
      2. the Opus determination in ``data/dcf_assumptions/<T>.json["redesign"]``,
      3. a backward-compat heuristic (bank -> bank model; other dcf_applicable=false
         -> "none"; else "fcff_dcf").
    Returns ``(model, suggestion)`` — model is "fcff_dcf" / "bank_excess_return" /
    "holdco_sotp" / "new" / "none" (or any explicit string the user/Opus set).
    """
    t = ticker.upper()
    hp = repo_root / "micro_thesis" / "holdings" / f"{t}.json"
    if hp.exists():
        try:
            h: object = json.loads(hp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            h = None
        if isinstance(h, dict):
            vm = cast("dict[str, object]", h).get("valuation_model")
            if isinstance(vm, str) and vm:
                return vm, None
    ap = repo_root / "data" / "dcf_assumptions" / f"{t}.json"
    if ap.exists():
        try:
            raw: object = json.loads(ap.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            rd = cast("dict[str, object]", raw).get("redesign")
            if isinstance(rd, dict):
                rdd = cast("dict[str, object]", rd)
                vm = rdd.get("valuation_model")
                if isinstance(vm, str) and vm:
                    sugg = rdd.get("valuation_model_suggestion")
                    return vm, (sugg if isinstance(sugg, str) and vm == "new" else None)
    na = _dcf_not_applicable(repo_root, t)
    if na == "bank":
        return "bank_excess_return", None
    if na is not None:
        # a non-bank financial (asset_manager/insurer/...) with no explicit
        # valuation_model yet: no template — surface the business model as the reason.
        return na, None
    return "fcff_dcf", None


def _refresh_bank(ticker: str, repo_root: Path) -> dict[str, object]:
    """Build the equity-side bank credit model (``execution/build_bank_dcf.py``)
    to ``dcf/<T>.xlsx``. The builder computes the value-of-record and upserts
    ``dcf_runs`` itself, so this just drives it env-style like the FCFF builder."""
    t = ticker.upper()
    dest = repo_root / DCF_DIR_NAME / f"{t}.xlsx"
    env = dict(os.environ, DCF_TICKER=t, DCF_REPO_ROOT=str(repo_root), DCF_DEST=str(dest))
    proc = subprocess.run(
        [sys.executable, str(_BANK_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None:
        reason = (proc.stderr.strip().splitlines() or [""])[-1][:160]
        return {"ticker": t, "status": "failed", "format": "bank", "reason": reason}
    return {"ticker": t, "status": "ok", "format": "bank", "workbook": str(dest), "result": line}


def _refresh_holdco(ticker: str, repo_root: Path) -> dict[str, object]:
    """Build the sum-of-the-parts holdco model (``execution/build_holdco_sotp.py``)
    to ``dcf/<T>.xlsx``; the builder computes the value-of-record and upserts
    ``dcf_runs`` itself, like the bank/FCFF builders."""
    t = ticker.upper()
    dest = repo_root / DCF_DIR_NAME / f"{t}.xlsx"
    env = dict(os.environ, DCF_TICKER=t, DCF_REPO_ROOT=str(repo_root), DCF_DEST=str(dest))
    proc = subprocess.run(
        [sys.executable, str(_HOLDCO_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None:
        reason = (proc.stderr.strip().splitlines() or [""])[-1][:160]
        return {"ticker": t, "status": "failed", "format": "holdco_sotp", "reason": reason}
    return {
        "ticker": t,
        "status": "ok",
        "format": "holdco_sotp",
        "workbook": str(dest),
        "result": line,
    }


def _run_builder(ticker: str, repo_root: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    """Run the redesigned-DCF builder for one ticker, writing to `dest`.

    Env-driven exactly like `build_all_redesigned_dcf.py`: the builder rebuilds
    every sheet from the FMP data under `repo_root`.
    """
    env = dict(
        os.environ,
        DCF_TICKER=ticker.upper(),
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(dest),
    )
    return subprocess.run(
        [sys.executable, str(_BUILDER_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _redesign_snapshot(rv: redesign_mod.RedesignValuation, workbook_path: str) -> str:
    """Serialize the redesigned-DCF inputs/outputs into the assumption snapshot."""
    payload: dict[str, object] = {
        "workbook_path": workbook_path,
        "format": "redesign",
        "wacc": rv.wacc,
        "terminal_method": rv.terminal_method,
        "terminal_basis": rv.terminal_basis,
        "exit_multiple": rv.exit_multiple,
        "fx_to_usd": rv.fx_to_usd,
        "diluted_shares_M": rv.diluted_shares_m,
        "cash_M": rv.cash_m,
        "total_debt_M": rv.total_debt_m,
        "value_per_share_usd": rv.value_per_share_usd,
        "value_per_share_reporting": rv.value_per_share_reporting,
        "valuation_fcf_M": rv.fcff_stream_m,
        "forecast_revenue_M": rv.forecast_revenue_m,
    }
    return json.dumps(payload, indent=2)


def _refresh_redesign(
    ticker: str,
    repo_root: Path,
    db_path: Path,
    *,
    dest: Path,
    valuation_year: int,
) -> dict[str, object]:
    """Rebuild the redesigned workbook from the latest FMP, preserve the user's
    Dashboard inputs, recompute the value-of-record, and upsert `dcf_runs`.

    Edit-preservation: the Dashboard yellow cells are captured before the rebuild
    and re-injected after, so re-pulling actuals never clobbers the user's
    assumptions; only current price is refreshed from the live quote.
    """
    ticker = ticker.upper()
    live = live_price_mod.read_live_price(repo_root, ticker)

    captured = redesign_mod.capture_dashboard(dest) if dest.exists() else None

    # Build to a sibling temp file so a build failure never corrupts the user's
    # existing workbook; only a clean build is swapped into place.
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = _run_builder(ticker, repo_root, tmp)
    except OSError as e:
        return {"ticker": ticker, "status": "failed", "reason": f"builder spawn failed: {e}"}

    result_line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(("RESULT", "SKIP"))), None
    )
    if result_line is not None and result_line.startswith("SKIP"):
        _unlink(tmp)
        # Surface the builder's own reason (SKIP\t<T>\t<reason>\t<detail>). This
        # branch only fires for data-insufficiency SKIPs — true dcf_applicable=false
        # names return earlier via `_dcf_not_applicable`, before the builder runs.
        skip_parts = result_line.split("\t")
        skip_reason = skip_parts[2] if len(skip_parts) > 2 else "dcf not applicable"
        return {
            "ticker": ticker,
            "status": "skipped",
            "reason": f"builder SKIP ({skip_reason})",
        }
    if result_line is None or proc.returncode != 0 or not tmp.exists():
        _unlink(tmp)
        tail = (proc.stderr.strip().splitlines() or [""])[-1][:200]
        return {"ticker": ticker, "status": "failed", "reason": f"builder: {tail or 'no RESULT'}"}

    redesign_mod.inject_dashboard(tmp, captured, current_price=live.price if live else None)

    try:
        rv = redesign_mod.read_and_value(tmp)
    except redesign_mod.RedesignError as e:
        _unlink(tmp)
        return {"ticker": ticker, "status": "failed", "reason": str(e)}
    if rv is None:
        _unlink(tmp)
        return {"ticker": ticker, "status": "failed", "reason": "rebuilt workbook not redesign"}

    os.replace(tmp, dest)

    holdings = _load_holdings(repo_root, ticker)
    mos_bar = holdings.get("mos_bar") if holdings else None
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    fair_value = rv.value_per_share_usd
    # over/under is undefined for a non-positive fair value (a forecast whose
    # assumptions imply negative FCF) — persist the value, leave over_under None
    # rather than crash (the #291 guard).
    over_under = (
        valuation_mod.over_under_pct(live.price, fair_value)
        if (live is not None and fair_value > 0)
        else None
    )

    row = persist_mod.DcfRunRow(
        ticker=ticker,
        valuation_date=date.today(),
        horizon_years=redesign_mod.N_FC,
        wacc=rv.wacc,
        npv=rv.operating_value_usd_m,
        npv_per_share=fair_value,
        shares_outstanding=rv.diluted_shares_m * 1_000_000.0,
        currency=CURRENCY_DEFAULT,
        live_price=live.price if live else None,
        live_price_at=live.fetched_at if live else None,
        over_under_pct=over_under,
        mos_bar_used=mos_bar_f,
        assumption_snapshot_json=_redesign_snapshot(rv, str(dest)),
        notes=f"workbook={dest.name} (redesigned)",
    )
    with sqlite3.connect(str(db_path)) as conn:
        persist_mod.upsert(conn, row)

    return {
        "ticker": ticker,
        "status": "ok",
        "workbook": str(dest),
        "format": "redesign",
        "valuation_year": valuation_year,
        "fair_value_per_share": fair_value,
        "enterprise_value_M": rv.operating_value_usd_m,
        "live_price": live.price if live else None,
        "over_under_pct": over_under,
        "mos_bar": mos_bar_f,
        "wacc": rv.wacc,
        "terminal_method": rv.terminal_method,
        "inputs_preserved": captured is not None,
    }


def _load_holdings(repo_root: Path, ticker: str) -> dict[str, object] | None:
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


if __name__ == "__main__":
    raise SystemExit(main())
