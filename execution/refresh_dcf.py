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
    `dcf.redesign.read_inputs`/`value` -> `dcf.valuation.compute_valuation`, and
    upserts the `dcf_runs` row that briefs read from;
  - recomputes the Bull/Bear scenario fair values (Dashboard SCENARIOS deltas)
    and the WACC x exit-multiple Sensitivity grid from the same inputs, rewrites
    those static cells post-inject, and stores the scenario range in
    `assumption_snapshot_json` (BASE remains `npv_per_share`/`over_under_pct`).

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
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import assumptions_doc  # noqa: E402
from dcf import live_price as live_price_mod  # noqa: E402
from dcf import persist as persist_mod  # noqa: E402
from dcf import redesign as redesign_mod  # noqa: E402
from dcf import reverse as reverse_mod  # noqa: E402
from dcf import universe as universe_mod  # noqa: E402
from ticker_validation import safe_ticker  # noqa: E402

DCF_DIR_NAME = "dcf"
CURRENCY_DEFAULT = "USD"
_BUILDER_SCRIPT = PROJECT_ROOT / "execution" / "build_redesigned_dcf.py"
_BANK_BUILDER = PROJECT_ROOT / "execution" / "build_bank_dcf.py"
_HOLDCO_BUILDER = PROJECT_ROOT / "execution" / "build_holdco_sotp.py"
_FINTECH_BUILDER = PROJECT_ROOT / "execution" / "build_fintech_sotp.py"
_PLATFORM_BUILDER = PROJECT_ROOT / "execution" / "build_nu_platform_dcf.py"
_MELI_SOTP_BUILDER = PROJECT_ROOT / "execution" / "build_meli_platform_dcf.py"


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
      - "holdco_sotp"         -> the capital-allocator SOTP model (`_refresh_holdco`).
      - "fintech_sotp"        -> the fintech segment SOTP (`_refresh_fintech_sotp`).
      - "platform_dcf"        -> the customer-driven platform DCF (`_refresh_platform`).
      - "meli_platform_sotp"  -> the MELI Commerce/Fintech SOTP (`_refresh_meli_sotp`).
      - "new"/"none"/unknown  -> skip, surfacing any Opus-proposed new-model spec.
    """
    model, suggestion = _valuation_model(repo_root, ticker)
    if model == "bank_excess_return":
        return _refresh_bank(ticker, repo_root)
    if model == "holdco_sotp":
        return _refresh_holdco(ticker, repo_root)
    if model == "fintech_sotp":
        return _refresh_fintech_sotp(ticker, repo_root)
    if model == "platform_dcf":
        return _refresh_platform(ticker, repo_root)
    if model == "meli_platform_sotp":
        return _refresh_meli_sotp(ticker, repo_root)
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


def _refresh_fintech_sotp(ticker: str, repo_root: Path) -> dict[str, object]:
    """Build the fintech segment sum-of-the-parts model
    (``execution/build_fintech_sotp.py``) to ``dcf/<T>.xlsx`` — a hybrid that
    values a fintech's lending, fee/deposit, and tech-platform segments separately
    (the right lens when a single bank or FCFF model would crush the non-credit
    franchises). The builder computes the value-of-record and upserts ``dcf_runs``
    itself, like the bank/holdco/FCFF builders."""
    t = ticker.upper()
    dest = repo_root / DCF_DIR_NAME / f"{t}.xlsx"
    env = dict(os.environ, DCF_TICKER=t, DCF_REPO_ROOT=str(repo_root), DCF_DEST=str(dest))
    proc = subprocess.run(
        [sys.executable, str(_FINTECH_BUILDER)],
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
        return {"ticker": t, "status": "failed", "format": "fintech_sotp", "reason": reason}
    return {
        "ticker": t,
        "status": "ok",
        "format": "fintech_sotp",
        "workbook": str(dest),
        "result": line,
    }


def _refresh_platform(ticker: str, repo_root: Path) -> dict[str, object]:
    """Build the customer-driven platform DCF (``execution/build_nu_platform_dcf.py``)
    to ``dcf/<T>.xlsx`` — values a fintech as a customer-acquisition + monetization
    platform (customers x ARPAC -> Credit/Float/Fee gross profit -> FCFE), the right
    lens when a credit-book model under-credits the non-credit franchises and the
    user-growth flywheel. The builder computes the value-of-record and upserts
    ``dcf_runs`` itself, like the bank/holdco/fintech/FCFF builders."""
    t = ticker.upper()
    dest = repo_root / DCF_DIR_NAME / f"{t}.xlsx"
    env = dict(os.environ, DCF_TICKER=t, DCF_REPO_ROOT=str(repo_root), DCF_DEST=str(dest))
    proc = subprocess.run(
        [sys.executable, str(_PLATFORM_BUILDER)],
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
        return {"ticker": t, "status": "failed", "format": "platform_dcf", "reason": reason}
    return {
        "ticker": t,
        "status": "ok",
        "format": "platform_dcf",
        "workbook": str(dest),
        "result": line,
    }


def _refresh_meli_sotp(ticker: str, repo_root: Path) -> dict[str, object]:
    """Build the MELI sum-of-the-parts platform DCF
    (``execution/build_meli_platform_dcf.py``) to ``dcf/<T>.xlsx`` — values
    Commerce + Fintech-payments as operating FCFF (@ WACC) and the Mercado Pago
    credit book as a separate excess-return/FCFE piece (@ Ke), summed to NAV. The
    right lens for a hybrid where one FCFF model can't hold both a capital-light
    marketplace and a capital-consuming lending book. The builder computes the
    value-of-record and upserts ``dcf_runs`` itself, like the platform/bank
    builders."""
    t = ticker.upper()
    dest = repo_root / DCF_DIR_NAME / f"{t}.xlsx"
    env = dict(os.environ, DCF_TICKER=t, DCF_REPO_ROOT=str(repo_root), DCF_DEST=str(dest))
    proc = subprocess.run(
        [sys.executable, str(_MELI_SOTP_BUILDER)],
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
        return {"ticker": t, "status": "failed", "format": "meli_platform_sotp", "reason": reason}
    return {
        "ticker": t,
        "status": "ok",
        "format": "meli_platform_sotp",
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


def _scenario_prior_snapshot(
    inp: redesign_mod.RedesignInputs, meta: dict[str, object] | None
) -> dict[str, object]:
    """The ``scenario_prior`` block for the assumption snapshot: the weights of
    record (the workbook cells, so owner edits are reflected) plus the LLM
    rationale/provenance from the assumptions JSON. ``dcf.scenario_reward`` reads
    the weights (PR E); the card renders the rationale."""
    default_w = (
        redesign_mod.DEFAULT_SCENARIO_WEIGHTS["bull"],
        redesign_mod.DEFAULT_SCENARIO_WEIGHTS["base"],
        redesign_mod.DEFAULT_SCENARIO_WEIGHTS["bear"],
    )
    is_global = all(
        abs(a - b) < 1e-9
        for a, b in zip((inp.weight_bull, inp.weight_base, inp.weight_bear), default_w, strict=True)
    )
    rationale = ""
    set_by = "global" if is_global else "owner"
    as_of = ""
    if isinstance(meta, dict):
        r = meta.get("rationale")
        rationale = r if isinstance(r, str) else ""
        sb = meta.get("set_by")
        set_by = sb if isinstance(sb, str) and sb else set_by
        ao = meta.get("as_of")
        as_of = ao if isinstance(ao, str) else ""
    return {
        "weights": {"bull": inp.weight_bull, "base": inp.weight_base, "bear": inp.weight_bear},
        "rationale": rationale,
        "set_by": set_by,
        "as_of": as_of,
    }


def _redesign_snapshot(
    rv: redesign_mod.RedesignValuation,
    workbook_path: str,
    *,
    scenarios: redesign_mod.ScenarioValues | None = None,
    inp: redesign_mod.RedesignInputs | None = None,
    scenario_prior_meta: dict[str, object] | None = None,
) -> str:
    """Serialize the redesigned-DCF inputs/outputs into the assumption snapshot.

    ``scenarios`` adds the Bull/Bear fair values (and the deltas that produced
    them) — BASE stays the row's ``npv_per_share``/``over_under_pct`` (the 0076
    convention untouched); Bull/Bear live only here, for the valuation card and
    any risk/reward consumer.
    """
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
    if scenarios is not None and inp is not None:
        payload["scenarios"] = {
            "base": {"fair_value_per_share_usd": scenarios.base},
            "bull": {
                "fair_value_per_share_usd": scenarios.bull,
                "deltas": dataclasses.asdict(inp.bull_deltas),
            },
            "bear": {
                "fair_value_per_share_usd": scenarios.bear,
                "deltas": dataclasses.asdict(inp.bear_deltas),
            },
        }
    # Reverse-DCF: the market-implied assumption set at the workbook's current
    # price, persisted alongside (never recomputed on render). Solved from the
    # same injected inputs, so it tracks the user's preserved edits. Absent when
    # there's no usable price / base value (solve_priced_in returns None).
    if inp is not None:
        priced_in = reverse_mod.solve_priced_in(inp)
        if priced_in is not None:
            payload["priced_in"] = priced_in.to_snapshot_dict()
    # Per-name scenario prior: the weights of record (workbook) + the LLM rationale
    # (assumptions JSON). scenario_reward consumes the weights; the card renders the
    # rationale. Persisted always so a consumer never recomputes on read.
    if inp is not None:
        payload["scenario_prior"] = _scenario_prior_snapshot(inp, scenario_prior_meta)
    return json.dumps(payload, indent=2)


@dataclasses.dataclass(frozen=True)
class SyncResult:
    """Outcome of one workbook→assumptions-JSON sync. ``status`` is 'synced'
    (existing block updated), 'created' (no JSON existed — one was created
    from the workbook), or 'failed' (with ``detail``); persisted to
    ``dcf_runs.assumptions_sync_status`` so a broken mirror is durable and
    visible, not a bool lost in stdout."""

    status: str
    detail: str | None = None

    def as_status_text(self) -> str:
        return f"{self.status}: {self.detail}" if self.detail else self.status


def _apply_inputs_to_block(rd: dict[str, object], inp: redesign_mod.RedesignInputs) -> None:
    """Write the workbook's numeric assumptions into a redesign block. Updates
    ONLY the numeric fields — segment growth, op margins, tax, exit
    multiple/basis, terminal method/g, capex-da, the WACC drivers
    (beta/rf/ERP/cost-of-debt) and the scenario offsets; the Opus
    ``narrative``/``reasoning``, the model flags (``dcf_applicable``/
    ``business_model``/``valuation_model``) and the ``opus_baseline``
    provenance snapshot are never touched."""
    rd["segments"] = {
        seg: {
            "near_term_growth": inp.near_growth_by_segment[seg],
            "terminal_growth": inp.terminal_growth_by_segment[seg],
        }
        for seg in inp.segments
    }
    rd["near_term_op_margin"] = inp.near_op_margin
    rd["terminal_op_margin"] = inp.terminal_op_margin
    rd["tax_rate"] = inp.tax_rate
    rd["terminal_method"] = inp.terminal_method
    rd["exit_basis"] = inp.terminal_basis
    rd["exit_multiple"] = inp.exit_multiple
    rd["terminal_growth_g"] = inp.terminal_growth_g
    rd["terminal_capex_da"] = inp.terminal_capex_da
    rd["beta"] = inp.beta
    # risk_free_rate + equity_risk_premium are GLOBAL now (global_dcf_assumptions,
    # migration 0112) — edited once in the dashboard's Global DCF assumptions panel,
    # not per name. Deliberately NOT mirrored back into the per-name block, so the
    # global governs every FCFF name and a from-scratch build reads it from the
    # store (build_redesigned_dcf: `_opus.get("risk_free_rate", _g.risk_free_rate)`).
    # A name can still pin its own value by hand; the sync just won't re-add it.
    # cost_of_debt + tax_rate stay per-name (company-specific).
    rd["cost_of_debt"] = inp.cost_of_debt
    # Scenario offsets mirror too, so a from-scratch build (which seeds the
    # Bull/Bear columns from this block) reproduces edited scenarios.
    rd["scenario_bull"] = dataclasses.asdict(inp.bull_deltas)
    rd["scenario_bear"] = dataclasses.asdict(inp.bear_deltas)
    _mirror_scenario_prior_weights(rd, inp)


def _mirror_scenario_prior_weights(rd: dict[str, object], inp: redesign_mod.RedesignInputs) -> None:
    """Mirror the workbook's scenario probability weights into the redesign block's
    ``scenario_prior`` sub-block so a from-scratch build reproduces them.

    The LLM ``rationale``/``as_of`` are PRESERVED (they are narrative, like the Opus
    reasoning). Provenance stays honest: when the owner has edited the weights away
    from the stored ones, ``set_by`` flips ``llm`` -> ``owner`` so the card never
    attributes an owner's split to the LLM. A block is only written when there is a
    real prior on file (an existing block, or weights the owner moved off the
    symmetric default) — the JSON stays clean for names with no per-name prior."""
    existing_sp = rd.get("scenario_prior")
    sp_block: dict[str, object] = (
        dict(cast("dict[str, object]", existing_sp)) if isinstance(existing_sp, dict) else {}
    )
    new_w = (inp.weight_bull, inp.weight_base, inp.weight_bear)
    default_w = (
        redesign_mod.DEFAULT_SCENARIO_WEIGHTS["bull"],
        redesign_mod.DEFAULT_SCENARIO_WEIGHTS["base"],
        redesign_mod.DEFAULT_SCENARIO_WEIGHTS["bear"],
    )
    is_global = all(abs(a - b) < 1e-9 for a, b in zip(new_w, default_w, strict=True))
    if not sp_block and is_global:
        return  # no per-name prior on file and weights are the default — keep clean

    def _wf(key: str) -> float | None:
        v = sp_block.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    old_w = (_wf("bull_weight"), _wf("base_weight"), _wf("bear_weight"))
    had_old = any(x is not None for x in old_w)
    changed = had_old and any(
        o is None or abs(o - n) > 1e-6 for o, n in zip(old_w, new_w, strict=True)
    )
    sp_block["bull_weight"] = inp.weight_bull
    sp_block["base_weight"] = inp.weight_base
    sp_block["bear_weight"] = inp.weight_bear
    if changed and sp_block.get("set_by") == "llm":
        sp_block["set_by"] = "owner"
    elif "set_by" not in sp_block:
        sp_block["set_by"] = "global" if is_global else "owner"
    rd["scenario_prior"] = sp_block


def sync_assumptions_json(
    repo_root: Path, ticker: str, inp: redesign_mod.RedesignInputs
) -> SyncResult:
    """Mirror the workbook's edited numeric assumptions back into
    ``data/dcf_assumptions/<T>.json["redesign"]`` — the from-scratch-build default.

    Without this, an ``import``/refresh preserves edits in the workbook + ``dcf_runs``
    but leaves the JSON stale, so a ``build_all_redesigned_dcf`` (which builds purely
    from the JSON) would silently revert them. The contract is LOUD in both
    directions (no silent False):

    * no assumptions file → one is **created** with a ``redesign`` block from the
      workbook (``set_by: "sync"`` marks it as workbook-derived, not an Opus
      pass), closing the revert hole for the ~43 workbooks that never had one;
    * a file with no ``redesign`` block gets one (same marker);
    * an unreadable file or a failed write returns ``failed`` with the detail —
      the caller persists it to ``dcf_runs`` and warns on stderr.
    """
    path = repo_root / "data" / "dcf_assumptions" / f"{ticker.upper()}.json"
    created = not path.exists()
    data: dict[str, object] = {}
    if not created:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return SyncResult("failed", f"unreadable assumptions JSON: {e}")
        if not isinstance(raw, dict):
            return SyncResult("failed", "assumptions JSON is not an object")
        data = cast("dict[str, object]", raw)
    block = data.get("redesign")
    rd: dict[str, object]
    if isinstance(block, dict):
        rd = cast("dict[str, object]", block)
    else:
        if not created and block is not None:
            return SyncResult("failed", "redesign key exists but is not an object")
        # No block to sync into — create one from the workbook so a from-scratch
        # build reproduces the user's current inputs instead of reverting them.
        created = True
        rd = {"set_by": "sync", "created_at": date.today().isoformat()}
        data["redesign"] = rd
    _apply_inputs_to_block(rd, inp)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return SyncResult("failed", f"write failed: {e}")
    return SyncResult("created" if created else "synced")


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
        inp = redesign_mod.read_inputs(tmp)
        rv = redesign_mod.value(inp) if inp is not None else None
    except redesign_mod.RedesignError as e:
        _unlink(tmp)
        return {"ticker": ticker, "status": "failed", "reason": str(e)}
    if inp is None or rv is None:
        _unlink(tmp)
        return {"ticker": ticker, "status": "failed", "reason": "rebuilt workbook not redesign"}

    # Rewrite the Python-computed static cells (Dashboard scenario fair values +
    # the Sensitivity grid) from the INJECTED inputs — the builder wrote them
    # from its from-scratch defaults, which the user's preserved edits supersede.
    scenarios = redesign_mod.write_computed_outputs(tmp, inp)

    # Assumption provenance from the same injected inputs: reconcile the
    # override ledger against the Opus baseline, rewrite the Assumptions sheet
    # + yellow-cell comments. A corrupt assumptions JSON must not block the
    # valuation refresh, but it surfaces in the result + stderr, never silently.
    assumptions_path = repo_root / "data" / "dcf_assumptions" / f"{ticker}.json"
    provenance: dict[str, object]
    try:
        provenance = {
            "status": "ok",
            "sources": assumptions_doc.write_provenance(
                tmp, inp, assumptions_path, ticker=ticker, update_ledger=True
            ),
        }
    except assumptions_doc.ProvenanceError as e:
        provenance = {"status": "error", "detail": str(e)}
        sys.stderr.write(f"WARNING: assumption provenance for {ticker} failed: {e}\n")

    os.replace(tmp, dest)

    # Mirror the now-deployed assumptions back to the from-scratch default so a
    # build_all_redesigned_dcf can't silently revert user edits (single source of
    # truth). A failure must not block the valuation refresh (the edits are
    # already safe in the workbook + dcf_runs) but it is LOUD: persisted onto
    # the dcf_runs row, surfaced in the result, and warned to stderr.
    sync = sync_assumptions_json(repo_root, ticker, inp)
    if sync.status == "failed":
        sys.stderr.write(f"WARNING: assumptions sync for {ticker} failed: {sync.detail}\n")

    holdings = _load_holdings(repo_root, ticker)
    mos_bar = holdings.get("mos_bar") if holdings else None
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    fair_value = rv.value_per_share_usd
    # over/under is undefined for a non-positive fair value (a forecast whose
    # assumptions imply negative FCF) — the central derivation returns None
    # rather than crash (the #291 guard). upsert() re-derives the same value
    # for the persisted row; this local copy only feeds the result payload.
    over_under = persist_mod.derive_over_under(live.price if live else None, fair_value)

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
        mos_bar_used=mos_bar_f,
        assumption_snapshot_json=_redesign_snapshot(
            rv,
            str(dest),
            scenarios=scenarios,
            inp=inp,
            scenario_prior_meta=_load_scenario_prior_meta(repo_root, ticker),
        ),
        notes=f"workbook={dest.name} (redesigned)",
        assumptions_sync_status=sync.as_status_text(),
        assumptions_synced_at=datetime.now(UTC).replace(tzinfo=None),
    )
    with sqlite3.connect(str(db_path)) as conn:
        persist_mod.upsert(conn, row)

    return {
        "ticker": ticker,
        "status": "ok",
        "workbook": str(dest),
        "format": "redesign",
        "assumptions_sync": dataclasses.asdict(sync),
        "assumption_provenance": provenance,
        "valuation_year": valuation_year,
        "fair_value_per_share": fair_value,
        "fair_value_bull": scenarios.bull,
        "fair_value_bear": scenarios.bear,
        "enterprise_value_M": rv.operating_value_usd_m,
        "live_price": live.price if live else None,
        "over_under_pct": over_under,
        "mos_bar": mos_bar_f,
        "wacc": rv.wacc,
        "terminal_method": rv.terminal_method,
        "inputs_preserved": captured is not None,
    }


def _prior_live_price(db_path: Path, ticker: str) -> tuple[float | None, datetime | None]:
    """The prior dcf_runs row's ``(live_price, live_price_at)`` for a ticker, or
    ``(None, None)``. An in-app edit changes the model, not the market quote —
    ``apply_edits`` carries the last-fetched price forward rather than faking a
    fresh one (price-leg freshness is a separate concern)."""
    if not db_path.exists():
        return None, None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT live_price, live_price_at FROM dcf_runs WHERE ticker = ?", (ticker,)
            ).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    price = float(row["live_price"]) if row["live_price"] is not None else None
    at_raw = row["live_price_at"]
    at: datetime | None = None
    if isinstance(at_raw, datetime):
        at = at_raw
    elif isinstance(at_raw, str) and at_raw:
        with contextlib.suppress(ValueError):
            at = datetime.fromisoformat(at_raw)
    return price, at


def apply_edits(
    ticker: str,
    repo_root: Path,
    db_path: Path,
    inp_edited: redesign_mod.RedesignInputs,
) -> dict[str, object]:
    """Persist in-app DCF assumption edits WITHOUT an FMP rebuild.

    The non-rebuild sibling of :func:`_refresh_redesign` — the in-app
    modify→save loop behind ``POST /api/dcf/save``. It writes the edited yellow
    Dashboard cells onto the live workbook, re-reads (so WACC is re-derived from
    the saved CAPM drivers — the inputs of record), recomputes the
    value-of-record + scenarios + sensitivity, reconciles the override ledger
    against the IMMUTABLE Opus baseline (``write_provenance`` never overwrites
    it), mirrors the edits into the from-scratch default, and upserts
    ``dcf_runs``. The market quote is preserved from the prior run. Returns a
    result dict shaped like ``_refresh_redesign``'s.
    """
    try:
        ticker = safe_ticker(ticker)
    except ValueError:
        # Defense in depth: the web routes 400 a bad ticker, but apply_edits is
        # also a CLI/internal entry point and ``ticker`` names the workbook path.
        return {"ticker": str(ticker), "status": "failed", "reason": "invalid ticker"}
    dest = repo_root / DCF_DIR_NAME / f"{ticker}.xlsx"
    if not redesign_mod.is_redesign_format(dest):
        return {"ticker": ticker, "status": "failed", "reason": "no redesigned workbook to edit"}

    # Write the edited input cells onto the live workbook (no FMP rebuild, no
    # price change), then re-read: WACC re-derives from the saved CAPM drivers.
    redesign_mod.inject_dashboard(
        dest, redesign_mod.capture_from_inputs(inp_edited), current_price=None
    )
    try:
        inp = redesign_mod.read_inputs(dest)
        rv = redesign_mod.value(inp) if inp is not None else None
    except redesign_mod.RedesignError as e:
        return {"ticker": ticker, "status": "failed", "reason": str(e)}
    if inp is None or rv is None:
        return {"ticker": ticker, "status": "failed", "reason": "workbook not redesign after edit"}

    scenarios = redesign_mod.write_computed_outputs(dest, inp)

    assumptions_path = repo_root / "data" / "dcf_assumptions" / f"{ticker}.json"
    provenance: dict[str, object]
    try:
        provenance = {
            "status": "ok",
            "sources": assumptions_doc.write_provenance(
                dest, inp, assumptions_path, ticker=ticker, update_ledger=True
            ),
        }
    except assumptions_doc.ProvenanceError as e:
        provenance = {"status": "error", "detail": str(e)}
        sys.stderr.write(f"WARNING: assumption provenance for {ticker} failed: {e}\n")

    sync = sync_assumptions_json(repo_root, ticker, inp)
    if sync.status == "failed":
        sys.stderr.write(f"WARNING: assumptions sync for {ticker} failed: {sync.detail}\n")

    holdings = _load_holdings(repo_root, ticker)
    mos_bar = holdings.get("mos_bar") if holdings else None
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    prior_price, prior_price_at = _prior_live_price(db_path, ticker)
    live_price = prior_price if prior_price is not None else inp.current_price
    fair_value = rv.value_per_share_usd
    over_under = persist_mod.derive_over_under(live_price, fair_value)

    row = persist_mod.DcfRunRow(
        ticker=ticker,
        valuation_date=date.today(),
        horizon_years=redesign_mod.N_FC,
        wacc=rv.wacc,
        npv=rv.operating_value_usd_m,
        npv_per_share=fair_value,
        shares_outstanding=rv.diluted_shares_m * 1_000_000.0,
        currency=CURRENCY_DEFAULT,
        live_price=live_price,
        live_price_at=prior_price_at,
        mos_bar_used=mos_bar_f,
        assumption_snapshot_json=_redesign_snapshot(
            rv,
            str(dest),
            scenarios=scenarios,
            inp=inp,
            scenario_prior_meta=_load_scenario_prior_meta(repo_root, ticker),
        ),
        notes=f"workbook={dest.name} (redesigned; in-app edit)",
        assumptions_sync_status=sync.as_status_text(),
        assumptions_synced_at=datetime.now(UTC).replace(tzinfo=None),
    )
    with sqlite3.connect(str(db_path)) as conn:
        persist_mod.upsert(conn, row)

    return {
        "ticker": ticker,
        "status": "ok",
        "workbook": str(dest),
        "format": "redesign",
        "assumptions_sync": dataclasses.asdict(sync),
        "assumption_provenance": provenance,
        "fair_value_per_share": fair_value,
        "fair_value_bull": scenarios.bull,
        "fair_value_bear": scenarios.bear,
        "enterprise_value_M": rv.operating_value_usd_m,
        "live_price": live_price,
        "over_under_pct": over_under,
        "mos_bar": mos_bar_f,
        "wacc": rv.wacc,
        "terminal_method": rv.terminal_method,
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


def _load_scenario_prior_meta(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """The ``redesign.scenario_prior`` sub-block from ``data/dcf_assumptions/<T>.json``
    (the LLM rationale + provenance), or None. Read AFTER ``sync_assumptions_json``,
    so it reflects any owner-edit reconciliation the sync just wrote."""
    path = repo_root / "data" / "dcf_assumptions" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    block = cast("dict[str, object]", raw).get("redesign")
    if not isinstance(block, dict):
        return None
    sp = cast("dict[str, object]", block).get("scenario_prior")
    return cast("dict[str, object]", sp) if isinstance(sp, dict) else None


if __name__ == "__main__":
    raise SystemExit(main())
