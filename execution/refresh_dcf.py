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
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import assumptions_doc  # noqa: E402
from dcf import equity_bridge as equity_bridge_mod  # noqa: E402
from dcf import live_price as live_price_mod  # noqa: E402
from dcf import persist as persist_mod  # noqa: E402
from dcf import redesign as redesign_mod  # noqa: E402
from dcf import reverse as reverse_mod  # noqa: E402
from dcf import universe as universe_mod  # noqa: E402
from dcf.provenance import DcfInputProvenance  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from ticker_validation import safe_ticker  # noqa: E402

DCF_DIR_NAME = "dcf"
CURRENCY_DEFAULT = "USD"
_BUILDER_SCRIPT = PROJECT_ROOT / "execution" / "build_redesigned_dcf.py"
_BANK_BUILDER = PROJECT_ROOT / "execution" / "build_bank_dcf.py"
_HOLDCO_BUILDER = PROJECT_ROOT / "execution" / "build_holdco_sotp.py"
_FINTECH_BUILDER = PROJECT_ROOT / "execution" / "build_fintech_sotp.py"
_PLATFORM_BUILDER = PROJECT_ROOT / "execution" / "build_nu_platform_dcf.py"
_MELI_SOTP_BUILDER = PROJECT_ROOT / "execution" / "build_meli_platform_dcf.py"
DCF_ENGINE_VERSION = "redesign_fcff_v1"

_DCF_SOURCE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("income_statement_quarterly.json", "income_statement"),
    ("balance_sheet_quarterly.json", "balance_sheet"),
    ("cash_flow_quarterly.json", "cash_flow"),
    ("product_segments_quarterly.json", "product_segments"),
    ("geo_segments_quarterly.json", "geographic_segments"),
    ("analyst_estimates_annual.json", "analyst_estimates"),
    ("profile.json", "company_profile"),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _source_file(
    path: Path,
    *,
    role: str,
    repo_root: Path,
) -> tuple[dict[str, object], datetime] | None:
    if not path.is_file():
        return None
    stat = path.stat()
    observed_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    try:
        display_path = str(path.relative_to(repo_root))
    except ValueError:
        display_path = str(path)
    return (
        {
            "role": role,
            "path": display_path.replace("\\", "/"),
            "sha256": _sha256_file(path),
            "bytes": stat.st_size,
            "observed_at": observed_at.isoformat(),
        },
        observed_at,
    )


def _primary_fact_observed_times(
    primary_fact_overlay: Mapping[str, object] | None,
) -> list[datetime]:
    """Return validated primary-document timestamps carried by overlay lineage."""
    if primary_fact_overlay is None:
        return []
    statements = primary_fact_overlay.get("statements")
    if not isinstance(statements, Mapping):
        return []
    observed: list[datetime] = []
    for statement_raw in cast("Mapping[str, object]", statements).values():
        if not isinstance(statement_raw, Mapping):
            continue
        statement = cast("Mapping[str, object]", statement_raw)
        applied = statement.get("applied")
        if not isinstance(applied, list):
            continue
        for lineage_raw in cast("list[object]", applied):
            if not isinstance(lineage_raw, Mapping):
                continue
            lineage = cast("Mapping[str, object]", lineage_raw)
            raw = lineage.get("as_of")
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            observed.append(
                parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            )
    return observed


def build_dcf_provenance(
    *,
    ticker: str,
    repo_root: Path,
    workbook_path: Path,
    input_payload: Mapping[str, object],
    assumption_snapshot_json: str,
    live_price: float | None,
    live_price_at: datetime | None,
    live_price_source: str | None,
    mos_bar: float | None,
    primary_fact_overlay: Mapping[str, object] | None = None,
    equity_bridge_receipt: Mapping[str, object] | None = None,
    country_risk_context: Mapping[str, object] | None = None,
) -> DcfInputProvenance:
    """Build reproducible lineage for the effective DCF input set."""
    ticker = ticker.upper()
    source_specs: list[tuple[Path, str]] = [
        *[
            (
                repo_root / "data" / "historical" / "fmp" / f"{ticker}_{suffix}",
                role,
            )
            for suffix, role in _DCF_SOURCE_SUFFIXES
        ],
        (
            repo_root / "data" / "dcf_assumptions" / f"{ticker}.json",
            "owner_assumptions",
        ),
        (
            repo_root / "micro_thesis" / "holdings" / f"{ticker}.json",
            "holding_policy",
        ),
    ]
    sources: list[dict[str, object]] = []
    observed_times: list[datetime] = []
    for path, role in source_specs:
        source = _source_file(path, role=role, repo_root=repo_root)
        if source is not None:
            detail, observed_at = source
            sources.append(detail)
            observed_times.append(observed_at)

    country_source = (
        country_risk_context.get("source_record") if country_risk_context is not None else None
    )
    if _valid_builder_source_record(country_source):
        source_detail = dict(cast("Mapping[str, object]", country_source))
        sources.append(source_detail)
        raw_observed_at = source_detail.get("observed_at")
        if isinstance(raw_observed_at, str):
            try:
                parsed = datetime.fromisoformat(raw_observed_at.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                observed_times.append(
                    parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
                )

    workbook_sha256 = _sha256_file(workbook_path)
    workbook_source = _source_file(
        workbook_path,
        role="calculation_workbook",
        repo_root=repo_root,
    )
    if workbook_source is not None:
        workbook_detail, _generated_at = workbook_source
        sources.append(workbook_detail)

    normalized_live_at: datetime | None = None
    if live_price_at is not None:
        normalized_live_at = (
            live_price_at.replace(tzinfo=UTC)
            if live_price_at.tzinfo is None
            else live_price_at.astimezone(UTC)
        )
        observed_times.append(normalized_live_at)
    observed_times.extend(_primary_fact_observed_times(primary_fact_overlay))
    market_price: dict[str, object] = {
        "price": live_price,
        "observed_at": _iso_utc(normalized_live_at),
        "source": live_price_source,
    }
    canonical_inputs: dict[str, object] = {
        "engine_version": DCF_ENGINE_VERSION,
        "ticker": ticker,
        "valuation_inputs": dict(input_payload),
        "assumption_snapshot": json.loads(assumption_snapshot_json),
        "market_price": market_price,
        "mos_bar": mos_bar,
        "workbook_sha256": workbook_sha256,
        "primary_fact_overlay": dict(primary_fact_overlay) if primary_fact_overlay else None,
        "equity_bridge_receipt": (dict(equity_bridge_receipt) if equity_bridge_receipt else None),
        "country_risk_context": (
            dict(country_risk_context) if country_risk_context is not None else None
        ),
    }
    canonical = json.dumps(
        canonical_inputs,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    input_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return DcfInputProvenance(
        input_sha256=input_sha256,
        workbook_sha256=workbook_sha256,
        engine_version=DCF_ENGINE_VERSION,
        inputs_as_of=max(observed_times, default=datetime(1970, 1, 1, tzinfo=UTC)),
        detail={
            "market_price": market_price,
            "sources": sources,
            "primary_fact_overlay": dict(primary_fact_overlay) if primary_fact_overlay else None,
            "equity_bridge_receipt": (
                dict(equity_bridge_receipt) if equity_bridge_receipt else None
            ),
            "country_risk_context": (
                dict(country_risk_context) if country_risk_context is not None else None
            ),
            "ticker": ticker,
            "inputs_as_of_status": "observed" if observed_times else "unavailable",
        },
    )


_PRIMARY_OVERLAY_STATEMENTS = frozenset({"income", "balance", "cash_flow"})


def primary_fact_overlay_from_builder(
    stderr: str, *, expected_ticker: str | None = None
) -> dict[str, object]:
    """Aggregate complete, ticker-matched builder overlay receipts truthfully."""
    statements: dict[str, object] = {}
    reasons: set[str] = set()
    for line in stderr.splitlines():
        try:
            payload_raw: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload_raw, dict):
            continue
        payload = cast("dict[str, object]", payload_raw)
        if payload.get("event") != "dcf_primary_fact_overlay":
            continue
        receipt_ticker = payload.get("ticker")
        if expected_ticker is not None and (
            not isinstance(receipt_ticker, str) or receipt_ticker.upper() != expected_ticker.upper()
        ):
            reasons.add("ticker_mismatch")
            continue
        statement = payload.get("statement")
        if not isinstance(statement, str) or statement not in _PRIMARY_OVERLAY_STATEMENTS:
            reasons.add("unexpected_statement_receipt")
            continue
        if statement in statements:
            reasons.add("duplicate_statement_receipt")
            continue
        statements[statement] = {
            key: value
            for key, value in payload.items()
            if key not in {"event", "ticker", "statement"}
        }
    if not statements:
        if reasons:
            return {"status": "degraded", "statements": {}, "reasons": sorted(reasons)}
        return {"status": "unavailable", "reason": "builder emitted no overlay receipt"}
    if frozenset(statements) != _PRIMARY_OVERLAY_STATEMENTS:
        reasons.add("missing_statement_receipts")
    for statement in statements.values():
        if not isinstance(statement, Mapping) or statement.get("status") != "ok":
            reasons.add("statement_degraded")
    return {
        "status": "degraded" if reasons else "ok",
        "statements": statements,
        "reasons": sorted(reasons),
    }


def equity_bridge_context_from_builder(
    stderr: str, *, expected_ticker: str
) -> dict[str, object] | None:
    """Return the builder's one exact model-input bridge context, or fail closed."""
    matches: list[dict[str, object]] = []
    for line in stderr.splitlines():
        try:
            payload_raw: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload_raw, dict):
            continue
        payload = cast("dict[str, object]", payload_raw)
        if payload.get("event") != "dcf_equity_bridge_context":
            continue
        if payload.get("schema_version") != "dcf_equity_bridge_context.v2":
            continue
        receipt_ticker = payload.get("ticker")
        if not isinstance(receipt_ticker, str) or receipt_ticker.upper() != expected_ticker.upper():
            continue
        matches.append({key: value for key, value in payload.items() if key != "event"})
    return matches[0] if len(matches) == 1 else None


def country_risk_context_from_builder(
    stderr: str, *, expected_ticker: str
) -> dict[str, object] | None:
    """Return one structurally valid country-risk context, or fail closed."""
    matches: list[dict[str, object]] = []
    for line in stderr.splitlines():
        try:
            payload_raw: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload_raw, dict):
            continue
        payload = cast("dict[str, object]", payload_raw)
        if payload.get("event") != "dcf_country_risk_context":
            continue
        if payload.get("schema_version") != "dcf_country_risk_context.v1":
            continue
        receipt_ticker = payload.get("ticker")
        if not isinstance(receipt_ticker, str) or receipt_ticker.upper() != expected_ticker.upper():
            continue
        authority = payload.get("authority")
        premium = payload.get("premium")
        source_record = payload.get("source_record")
        if authority not in {
            "owner_override",
            "preserved_dashboard_override",
            "systematic_default_zero",
            "systematic_geo",
        }:
            continue
        if isinstance(premium, bool) or not isinstance(premium, (int, float)):
            continue
        if source_record is not None and not _valid_builder_source_record(source_record):
            continue
        if (
            authority in {"owner_override", "preserved_dashboard_override"}
            and source_record is not None
        ):
            continue
        if authority == "systematic_geo" and source_record is None:
            continue
        if authority == "systematic_default_zero" and (
            source_record is not None or float(premium) != 0.0
        ):
            continue
        matches.append({key: value for key, value in payload.items() if key != "event"})
    return matches[0] if len(matches) == 1 else None


def _valid_builder_source_record(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    source = cast("Mapping[str, object]", value)
    path = source.get("path")
    digest = source.get("sha256")
    byte_size = source.get("bytes")
    observed_at = source.get("observed_at")
    if not isinstance(observed_at, str):
        return False
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        source.get("role") == "geographic_revenue"
        and isinstance(path, str)
        and bool(path.strip())
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdefABCDEF" for character in digest)
        and isinstance(byte_size, int)
        and not isinstance(byte_size, bool)
        and byte_size >= 0
        and source.get("influences_calculation") is True
        and source.get("selection") in {"annual_latest_fiscal_year", "quarterly_latest_four"}
    )


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
        help="Refresh every active DCF-maintained name: portfolio + evaluation",
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
    active briefed-list ticker (portfolio + evaluation) from the DB. Files and
    legacy WACC seeds are inputs, never membership authority. Non-applicable
    financials self-skip in ``refresh_one``.
    """
    return universe_mod.dcf_universe(repo_root)


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
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    _unlink(tmp)
    env = dict(
        os.environ,
        DCF_TICKER=t,
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(tmp),
        DCF_PROMOTE_DEST=str(dest),
    )
    proc = subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(_BANK_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None or "dcf_runs=ok" not in line or not dest.is_file():
        _unlink(tmp)
        reason = (
            (proc.stderr.strip().splitlines() or [""])[-1][:160]
            if line is None
            else "builder did not atomically persist and promote the DCF"
        )
        return {"ticker": t, "status": "failed", "format": "bank", "reason": reason}
    return {"ticker": t, "status": "ok", "format": "bank", "workbook": str(dest), "result": line}


def _refresh_holdco(ticker: str, repo_root: Path) -> dict[str, object]:
    """Build the sum-of-the-parts holdco model (``execution/build_holdco_sotp.py``)
    to ``dcf/<T>.xlsx``; the builder computes the value-of-record and upserts
    ``dcf_runs`` itself, like the bank/FCFF builders."""
    t = ticker.upper()
    dest = repo_root / DCF_DIR_NAME / f"{t}.xlsx"
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    _unlink(tmp)
    env = dict(
        os.environ,
        DCF_TICKER=t,
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(tmp),
        DCF_PROMOTE_DEST=str(dest),
        DCF_OWNER_INPUTS_DEST=str(dest),
    )
    proc = subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(_HOLDCO_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None or "dcf_runs=ok" not in line or not dest.is_file():
        _unlink(tmp)
        reason = (
            (proc.stderr.strip().splitlines() or [""])[-1][:160]
            if line is None
            else "builder did not atomically persist and promote the DCF"
        )
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
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    _unlink(tmp)
    env = dict(
        os.environ,
        DCF_TICKER=t,
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(tmp),
        DCF_PROMOTE_DEST=str(dest),
    )
    proc = subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(_FINTECH_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None or "dcf_runs=ok" not in line or not dest.is_file():
        _unlink(tmp)
        reason = (
            (proc.stderr.strip().splitlines() or [""])[-1][:160]
            if line is None
            else "builder did not atomically persist and promote the DCF"
        )
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
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    _unlink(tmp)
    env = dict(
        os.environ,
        DCF_TICKER=t,
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(tmp),
        DCF_PROMOTE_DEST=str(dest),
    )
    proc = subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(_PLATFORM_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None or "dcf_runs=ok" not in line or not dest.is_file():
        _unlink(tmp)
        reason = (
            (proc.stderr.strip().splitlines() or [""])[-1][:160]
            if line is None
            else "builder did not atomically persist and promote the DCF"
        )
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
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    _unlink(tmp)
    env = dict(
        os.environ,
        DCF_TICKER=t,
        DCF_REPO_ROOT=str(repo_root),
        DCF_DEST=str(tmp),
        DCF_PROMOTE_DEST=str(dest),
    )
    proc = subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(_MELI_SOTP_BUILDER)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    if line is None or "dcf_runs=ok" not in line or not dest.is_file():
        _unlink(tmp)
        reason = (
            (proc.stderr.strip().splitlines() or [""])[-1][:160]
            if line is None
            else "builder did not atomically persist and promote the DCF"
        )
        return {"ticker": t, "status": "failed", "format": "meli_platform_sotp", "reason": reason}
    return {
        "ticker": t,
        "status": "ok",
        "format": "meli_platform_sotp",
        "workbook": str(dest),
        "result": line,
    }


def _run_builder(
    ticker: str,
    repo_root: Path,
    dest: Path,
    *,
    country_risk_override: float | None = None,
) -> subprocess.CompletedProcess[str]:
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
    if country_risk_override is not None:
        env["DCF_COUNTRY_RISK_OVERRIDE"] = str(country_risk_override)
    return subprocess.run(
        [*managed_python_prefix(PROJECT_ROOT), str(_BUILDER_SCRIPT)],
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


def _stage_assumptions(path: Path) -> Path:
    """Copy an existing assumptions mirror so provenance writes are reversible."""
    staged = path.with_name(f"{path.stem}.rebuild{path.suffix}")
    _unlink(staged)
    if not path.exists():
        # The caller may let sync create this file only after the promotion
        # gate clears; never point a preflight provenance write at the live path.
        return staged
    try:
        shutil.copy2(path, staged)
    except OSError:
        _unlink(staged)
        raise
    return staged


def _swap_staged(path: Path, staged: Path) -> Path | None:
    """Swap one staged file and return a rollback backup, if one existed."""
    if not staged.is_file():
        return None
    backup = path.with_name(f"{path.stem}.rollback.{os.getpid()}{path.suffix}")
    if backup.exists():
        raise OSError(f"rollback path already exists: {backup}")
    had_original = path.is_file()
    if had_original:
        os.replace(path, backup)
    try:
        os.replace(staged, path)
    except Exception:
        if had_original and backup.is_file():
            os.replace(backup, path)
        raise
    return backup if had_original else None


def _restore_swap(path: Path, backup: Path | None) -> None:
    """Restore a file after a post-swap persistence failure."""
    if backup is None:
        _unlink(path)
        return
    _unlink(path)
    if backup.is_file():
        os.replace(backup, path)


def _blocked_promotion_result(
    ticker: str,
    decision: persist_mod.DcfPromotionDecision,
    *,
    workbook: Path | None = None,
) -> dict[str, object]:
    """Return candidate evidence without implying a durable/current write."""
    result: dict[str, object] = {
        "ticker": ticker.upper(),
        "status": "blocked",
        "reason": decision.reason,
        "promotion": decision.as_dict(),
        "candidate_evidence": dict(decision.candidate_evidence),
        "dcf_run_persisted": False,
    }
    if workbook is not None:
        result["workbook"] = str(workbook)
    return result


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


def _reported_currency(repo_root: Path, ticker: str) -> str | None:
    """The name's FMP reported currency, from the same cache the builder reads.

    None when the cache file is missing/unreadable — the FX guard then stays
    quiet (a USD reporter with no cache must not fail its refresh)."""
    p = repo_root / "data" / "historical" / "fmp" / f"{ticker}_income_statement_quarterly.json"
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        row = cast("dict[str, object]", rows[0])
        ccy = row.get("reportedCurrency")
        return str(ccy).upper() if isinstance(ccy, str) and ccy.strip() else None
    return None


def _unconverted_fx_reason(repo_root: Path, ticker: str, fx_to_usd: float) -> str | None:
    """Belt-and-braces FX guard at the persist seam (the TSM defect class).

    The workbook's ×FX multiplier is the FX source of truth; a non-USD reporter
    whose workbook carries ×1.0 was built before the builder's unknown-currency
    fail-loud (or hand-edited wrong) and would persist a local-currency fair
    value stamped USD. Returns the failure reason, or None when consistent."""
    ccy = _reported_currency(repo_root, ticker)
    if ccy not in (None, "USD") and fx_to_usd == 1.0:
        return (
            f"unconverted workbook: reported currency {ccy} but fx_to_usd=1.0 — "
            "rebuild the workbook (builder now fails loud on unknown currencies)"
        )
    return None


def _redesign_snapshot(
    rv: redesign_mod.RedesignValuation,
    workbook_path: str,
    *,
    scenarios: redesign_mod.ScenarioValues | None = None,
    inp: redesign_mod.RedesignInputs | None = None,
    scenario_prior_meta: dict[str, object] | None = None,
    holdings: Mapping[str, object] | None = None,
    reporting_currency: str | None = None,
) -> str:
    """Serialize the redesigned-DCF inputs/outputs into the assumption snapshot.

    ``scenarios`` adds the Bull/Bear fair values (and the deltas that produced
    them) — BASE stays the row's ``npv_per_share``/``over_under_pct`` (the 0076
    convention untouched); Bull/Bear live only here, for the valuation card and
    any risk/reward consumer. ``holdings`` (the already-loaded ``micro_thesis/
    holdings/<T>.json`` dict, Monthly Red Team Phase 1 guard 3) feeds the bear
    leg's ``provenance`` classification — ``"seed" | "thesis" | "owner"`` — so a
    consumer (``dcf.scenario_reward.parse_scenario_bear_provenance``, the bear-
    realism lint) can tell a generic mild-disappointment bear apart from one the
    analyst actually calibrated.
    """
    payload: dict[str, object] = {
        "workbook_path": workbook_path,
        "format": "redesign",
        "reporting_currency": reporting_currency,
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
                "provenance": redesign_mod.classify_bear_provenance(inp.bear_deltas, holdings),
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

    holdings = _load_holdings(repo_root, ticker)
    captured = redesign_mod.capture_dashboard(dest) if dest.exists() else None
    # Guard 3 (Monthly Red Team): an UNTOUCHED BEAR_SEED Bear column is a labeled
    # fallback, not an owner edit — don't let the capture→inject loop re-inject it
    # over a freshly thesis-seeded Bear column when the holdings JSON names a
    # thesis-calibrated bear. Owner-edited bears are preserved unconditionally.
    captured = redesign_mod.strip_unedited_seed_bear(captured, holdings)

    # Build to a sibling temp file so a build failure never corrupts the user's
    # existing workbook; only a clean build is swapped into place.
    tmp = dest.parent / f"{dest.stem}.rebuild.xlsx"
    assumptions_path = repo_root / "data" / "dcf_assumptions" / f"{ticker}.json"
    staged_assumptions = _stage_assumptions(assumptions_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = _run_builder(
            ticker,
            repo_root,
            tmp,
            country_risk_override=(
                captured.scalars.get(redesign_mod.COUNTRY_RISK_PREMIUM_ROW)
                if captured is not None
                else None
            ),
        )
    except OSError as e:
        return {"ticker": ticker, "status": "failed", "reason": f"builder spawn failed: {e}"}

    result_line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(("RESULT", "SKIP"))), None
    )
    if result_line is not None and result_line.startswith("SKIP"):
        _unlink(tmp)
        _unlink(staged_assumptions)
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
        _unlink(staged_assumptions)
        tail = (proc.stderr.strip().splitlines() or [""])[-1][:200]
        return {"ticker": ticker, "status": "failed", "reason": f"builder: {tail or 'no RESULT'}"}

    primary_fact_overlay = primary_fact_overlay_from_builder(proc.stderr, expected_ticker=ticker)
    equity_bridge_context = equity_bridge_context_from_builder(proc.stderr, expected_ticker=ticker)
    country_risk_context = country_risk_context_from_builder(proc.stderr, expected_ticker=ticker)
    redesign_mod.inject_dashboard(tmp, captured, current_price=live.price if live else None)

    try:
        inp = redesign_mod.read_inputs(tmp)
        rv = redesign_mod.value(inp) if inp is not None else None
    except redesign_mod.RedesignError as e:
        _unlink(tmp)
        _unlink(staged_assumptions)
        return {"ticker": ticker, "status": "failed", "reason": str(e)}
    if inp is None or rv is None:
        _unlink(tmp)
        _unlink(staged_assumptions)
        return {"ticker": ticker, "status": "failed", "reason": "rebuilt workbook not redesign"}

    # Rewrite the Python-computed static cells (Dashboard scenario fair values +
    # the Sensitivity grid) from the INJECTED inputs — the builder wrote them
    # from its from-scratch defaults, which the user's preserved edits supersede.
    scenarios = redesign_mod.write_computed_outputs(tmp, inp)

    # Assumption provenance from the same injected inputs: reconcile the
    # override ledger against the Opus baseline, rewrite the Assumptions sheet
    # + yellow-cell comments. A corrupt assumptions JSON must not block the
    # valuation refresh, but it surfaces in the result + stderr, never silently.
    provenance: dict[str, object]
    try:
        provenance = {
            "status": "ok",
            "sources": assumptions_doc.write_provenance(
                tmp,
                inp,
                staged_assumptions,
                ticker=ticker,
                update_ledger=True,
            ),
        }
    except assumptions_doc.ProvenanceError as e:
        provenance = {"status": "error", "detail": str(e)}
        sys.stderr.write(f"WARNING: assumption provenance for {ticker} failed: {e}\n")

    mos_bar = holdings.get("mos_bar") if holdings else None
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    fx_reason = _unconverted_fx_reason(repo_root, ticker, rv.fx_to_usd)
    if fx_reason is not None:
        _unlink(tmp)
        _unlink(staged_assumptions)
        sys.stderr.write(f"WARNING: {ticker}: {fx_reason}\n")
        return {"ticker": ticker, "status": "failed", "reason": fx_reason}

    fair_value = rv.value_per_share_usd
    # over/under is undefined for a non-positive fair value (a forecast whose
    # assumptions imply negative FCF) — the central derivation returns None
    # rather than crash (the #291 guard). upsert() re-derives the same value
    # for the persisted row; this local copy only feeds the result payload.
    over_under = persist_mod.derive_over_under(live.price if live else None, fair_value)
    assumption_snapshot = _redesign_snapshot(
        rv,
        str(dest),
        reporting_currency=_reported_currency(repo_root, ticker),
        scenarios=scenarios,
        inp=inp,
        scenario_prior_meta=_load_scenario_prior_meta(repo_root, ticker),
        holdings=holdings,
    )
    equity_bridge_receipt = equity_bridge_mod.build_equity_bridge_receipt(
        ticker=ticker,
        operating_value_usd_m=rv.operating_value_usd_m,
        cash_m=rv.cash_m,
        total_debt_m=rv.total_debt_m,
        diluted_shares_m=rv.diluted_shares_m,
        fx_to_usd=rv.fx_to_usd,
        value_per_share_usd=fair_value,
        reporting_currency=_reported_currency(repo_root, ticker),
        primary_fact_overlay=primary_fact_overlay,
        bridge_context=equity_bridge_context,
    )
    input_provenance = build_dcf_provenance(
        ticker=ticker,
        repo_root=repo_root,
        workbook_path=tmp,
        input_payload=inp.to_dict(),
        assumption_snapshot_json=assumption_snapshot,
        live_price=live.price if live else None,
        live_price_at=live.fetched_at if live else None,
        live_price_source=getattr(live, "source_name", None) if live else None,
        mos_bar=mos_bar_f,
        primary_fact_overlay=primary_fact_overlay,
        equity_bridge_receipt=equity_bridge_receipt.to_dict(),
        country_risk_context=country_risk_context,
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
        mos_bar_used=mos_bar_f,
        assumption_snapshot_json=assumption_snapshot,
        notes=f"workbook={dest.name} (redesigned)",
        assumptions_sync_status=None,
        assumptions_synced_at=None,
        provenance=input_provenance,
    )
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True) as conn:
        # Hold the writer lock across the read-only preflight and the final
        # upsert so a competing refresh cannot change the current run between
        # the decision and persistence.
        conn.execute("BEGIN IMMEDIATE")
        decision = persist_mod.check_promotion(conn, row)
        if not decision.allowed:
            _unlink(tmp)
            _unlink(staged_assumptions)
            return _blocked_promotion_result(ticker, decision, workbook=dest)
        # The preflight is read-only. Swap both artifacts only after it clears;
        # the persistence chokepoint repeats the same gate before committing.
        workbook_backup: Path | None = None
        assumptions_backup: Path | None = None
        workbook_swapped = False
        assumptions_swapped = False
        try:
            workbook_backup = _swap_staged(dest, tmp)
            workbook_swapped = True
            assumptions_backup = _swap_staged(assumptions_path, staged_assumptions)
            assumptions_swapped = True
            sync = sync_assumptions_json(repo_root, ticker, inp)
            if sync.status == "failed":
                sys.stderr.write(f"WARNING: assumptions sync for {ticker} failed: {sync.detail}\n")
            row = dataclasses.replace(
                row,
                assumptions_sync_status=sync.as_status_text(),
                assumptions_synced_at=datetime.now(UTC).replace(tzinfo=None),
            )
            persisted = persist_mod.upsert(conn, row)
        except Exception:
            if assumptions_swapped:
                _restore_swap(assumptions_path, assumptions_backup)
            if workbook_swapped:
                _restore_swap(dest, workbook_backup)
            raise
        finally:
            if assumptions_backup is not None:
                _unlink(assumptions_backup)
            if workbook_backup is not None:
                _unlink(workbook_backup)

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
        "dcf_run_persisted": persisted,
        "input_sha256": input_provenance.input_sha256,
        "primary_fact_overlay_status": primary_fact_overlay.get("status"),
        "primary_fact_overlay_reasons": primary_fact_overlay.get("reasons", []),
        "equity_bridge_status": equity_bridge_receipt.status,
        "equity_bridge_reasons": list(equity_bridge_receipt.reasons),
    }


def _prior_live_price(db_path: Path, ticker: str) -> tuple[float | None, datetime | None]:
    """The prior dcf_runs row's ``(live_price, live_price_at)`` for a ticker, or
    ``(None, None)``. An in-app edit changes the model, not the market quote —
    ``apply_edits`` carries the last-fetched price forward rather than faking a
    fresh one (price-leg freshness is a separate concern)."""
    if not db_path.exists():
        return None, None
    try:
        with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
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


def _prior_dcf_provenance_detail(db_path: Path, ticker: str) -> dict[str, object] | None:
    """Recover the ticker-matched current generic DCF provenance envelope."""
    if not db_path.exists():
        return None
    try:
        with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(dcf_runs)")}
            if "provenance_json" not in columns:
                return None
            has_latest = "is_latest" in columns
            has_segment = "segment_name" in columns
            has_created_at = "created_at" in columns
            if has_latest and has_segment and has_created_at:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                      AND COALESCE(is_latest, 1) = 1
                      AND COALESCE(segment_name, '') = ''
                    ORDER BY created_at DESC, id DESC LIMIT 1
                """
            elif has_latest and has_segment:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                      AND COALESCE(is_latest, 1) = 1
                      AND COALESCE(segment_name, '') = ''
                    ORDER BY id DESC LIMIT 1
                """
            elif has_latest and has_created_at:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                      AND COALESCE(is_latest, 1) = 1
                    ORDER BY created_at DESC, id DESC LIMIT 1
                """
            elif has_latest:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                      AND COALESCE(is_latest, 1) = 1
                    ORDER BY id DESC LIMIT 1
                """
            elif has_segment and has_created_at:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                      AND COALESCE(segment_name, '') = ''
                    ORDER BY created_at DESC, id DESC LIMIT 1
                """
            elif has_segment:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                      AND COALESCE(segment_name, '') = ''
                    ORDER BY id DESC LIMIT 1
                """
            elif has_created_at:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                    ORDER BY created_at DESC, id DESC LIMIT 1
                """
            else:
                query = """
                    SELECT provenance_json FROM dcf_runs
                    WHERE UPPER(ticker) = UPPER(?)
                    ORDER BY id DESC LIMIT 1
                """
            row = conn.execute(query, (ticker,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    try:
        detail_raw: object = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return None
    if not isinstance(detail_raw, dict):
        return None
    detail = cast("dict[str, object]", detail_raw)
    recorded_ticker = detail.get("ticker")
    if not isinstance(recorded_ticker, str) or recorded_ticker.upper() != ticker.upper():
        return None
    return dict(detail)


def prior_primary_fact_overlay(db_path: Path, ticker: str) -> dict[str, object] | None:
    """Recover validated primary lineage from the current generic DCF run."""
    detail = _prior_dcf_provenance_detail(db_path, ticker)
    if detail is None:
        return None
    overlay_raw = detail.get("primary_fact_overlay")
    if not isinstance(overlay_raw, dict):
        return None
    overlay = cast("dict[str, object]", overlay_raw)
    if overlay.get("status") not in {"ok", "degraded", "unavailable"}:
        return None
    statements = overlay.get("statements")
    if statements is not None and not isinstance(statements, dict):
        return None
    return dict(overlay)


def prior_equity_bridge_context(db_path: Path, ticker: str) -> dict[str, object] | None:
    """Recover the exact model-input context from the prior bridge receipt."""
    detail = _prior_dcf_provenance_detail(db_path, ticker)
    if detail is None:
        return None
    receipt = detail.get("equity_bridge_receipt")
    if not isinstance(receipt, dict):
        return None
    context = cast("dict[str, object]", receipt).get("bridge_context")
    if not isinstance(context, dict):
        return None
    typed_context = cast("dict[str, object]", context)
    if typed_context.get("schema_version") != "dcf_equity_bridge_context.v2":
        return None
    recorded_ticker = typed_context.get("ticker")
    if not isinstance(recorded_ticker, str) or recorded_ticker.upper() != ticker.upper():
        return None
    return dict(typed_context)


def prior_country_risk_context(db_path: Path, ticker: str) -> dict[str, object] | None:
    """Recover the country-risk authority and exact geo receipt for edits."""
    detail = _prior_dcf_provenance_detail(db_path, ticker)
    if detail is None:
        return None
    context = detail.get("country_risk_context")
    if not isinstance(context, dict):
        return None
    typed_context = cast("dict[str, object]", context)
    if typed_context.get("schema_version") != "dcf_country_risk_context.v1":
        return None
    recorded_ticker = typed_context.get("ticker")
    if not isinstance(recorded_ticker, str) or recorded_ticker.upper() != ticker.upper():
        return None
    authority = typed_context.get("authority")
    source_record = typed_context.get("source_record")
    if authority not in {
        "owner_override",
        "preserved_dashboard_override",
        "systematic_default_zero",
        "systematic_geo",
    }:
        return None
    if source_record is not None and not _valid_builder_source_record(source_record):
        return None
    if (
        authority in {"owner_override", "preserved_dashboard_override"}
        and source_record is not None
    ):
        return None
    premium = typed_context.get("premium")
    if isinstance(premium, bool) or not isinstance(premium, (int, float)):
        return None
    if authority == "systematic_geo" and source_record is None:
        return None
    if authority == "systematic_default_zero" and (
        source_record is not None or float(premium) != 0.0
    ):
        return None
    return dict(typed_context)


def country_risk_context_for_edit(
    prior_context: Mapping[str, object] | None,
    *,
    ticker: str,
    effective_premium: float,
) -> dict[str, object]:
    """Retain exact systematic lineage only while the saved CRP is unchanged."""
    prior_premium = prior_context.get("premium") if prior_context is not None else None
    if (
        prior_context is not None
        and not isinstance(prior_premium, bool)
        and isinstance(prior_premium, (int, float))
        and float(prior_premium) == effective_premium
    ):
        return dict(prior_context)
    return {
        "schema_version": "dcf_country_risk_context.v1",
        "ticker": ticker.upper(),
        "premium": effective_premium,
        "authority": "owner_override",
        "source_record": None,
    }


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

    # Keep the live workbook and assumptions mirror untouched until the typed
    # promotion gate clears the fully computed candidate.
    staged_dest = dest.with_name(f"{dest.stem}.edit.xlsx")
    _unlink(staged_dest)
    try:
        shutil.copy2(dest, staged_dest)
    except OSError as e:
        return {"ticker": ticker, "status": "failed", "reason": f"stage workbook failed: {e}"}
    assumptions_path = repo_root / "data" / "dcf_assumptions" / f"{ticker}.json"
    staged_assumptions = _stage_assumptions(assumptions_path)

    # Write the edited input cells onto the live workbook (no FMP rebuild, no
    # price change), then re-read: WACC re-derives from the saved CAPM drivers.
    redesign_mod.inject_dashboard(
        staged_dest, redesign_mod.capture_from_inputs(inp_edited), current_price=None
    )
    try:
        inp = redesign_mod.read_inputs(staged_dest)
        rv = redesign_mod.value(inp) if inp is not None else None
    except redesign_mod.RedesignError as e:
        _unlink(staged_dest)
        _unlink(staged_assumptions)
        return {"ticker": ticker, "status": "failed", "reason": str(e)}
    if inp is None or rv is None:
        _unlink(staged_dest)
        _unlink(staged_assumptions)
        return {"ticker": ticker, "status": "failed", "reason": "workbook not redesign after edit"}

    scenarios = redesign_mod.write_computed_outputs(staged_dest, inp)

    provenance: dict[str, object]
    try:
        provenance = {
            "status": "ok",
            "sources": assumptions_doc.write_provenance(
                staged_dest,
                inp,
                staged_assumptions,
                ticker=ticker,
                update_ledger=True,
            ),
        }
    except assumptions_doc.ProvenanceError as e:
        provenance = {"status": "error", "detail": str(e)}
        sys.stderr.write(f"WARNING: assumption provenance for {ticker} failed: {e}\n")

    holdings = _load_holdings(repo_root, ticker)
    mos_bar = holdings.get("mos_bar") if holdings else None
    mos_bar_f = float(mos_bar) if isinstance(mos_bar, (int, float)) else None

    prior_price, prior_price_at = _prior_live_price(db_path, ticker)
    live_price = prior_price if prior_price is not None else inp.current_price

    fx_reason = _unconverted_fx_reason(repo_root, ticker, rv.fx_to_usd)
    if fx_reason is not None:
        _unlink(staged_dest)
        _unlink(staged_assumptions)
        sys.stderr.write(f"WARNING: {ticker}: {fx_reason}\n")
        return {"ticker": ticker, "status": "failed", "reason": fx_reason}

    fair_value = rv.value_per_share_usd
    over_under = persist_mod.derive_over_under(live_price, fair_value)
    assumption_snapshot = _redesign_snapshot(
        rv,
        str(dest),
        reporting_currency=_reported_currency(repo_root, ticker),
        scenarios=scenarios,
        inp=inp,
        scenario_prior_meta=_load_scenario_prior_meta(repo_root, ticker),
        holdings=holdings,
    )
    prior_overlay = prior_primary_fact_overlay(db_path, ticker)
    prior_bridge_context = prior_equity_bridge_context(db_path, ticker)
    prior_country_context = prior_country_risk_context(db_path, ticker)
    effective_country_context = country_risk_context_for_edit(
        prior_country_context,
        ticker=ticker,
        effective_premium=inp.country_risk_premium,
    )
    equity_bridge_receipt = equity_bridge_mod.build_equity_bridge_receipt(
        ticker=ticker,
        operating_value_usd_m=rv.operating_value_usd_m,
        cash_m=rv.cash_m,
        total_debt_m=rv.total_debt_m,
        diluted_shares_m=rv.diluted_shares_m,
        fx_to_usd=rv.fx_to_usd,
        value_per_share_usd=fair_value,
        reporting_currency=_reported_currency(repo_root, ticker),
        primary_fact_overlay=prior_overlay,
        bridge_context=prior_bridge_context,
    )
    input_provenance = build_dcf_provenance(
        ticker=ticker,
        repo_root=repo_root,
        workbook_path=staged_dest,
        input_payload=inp.to_dict(),
        assumption_snapshot_json=assumption_snapshot,
        live_price=live_price,
        live_price_at=prior_price_at,
        live_price_source="prior_dcf_run",
        mos_bar=mos_bar_f,
        primary_fact_overlay=prior_overlay,
        equity_bridge_receipt=equity_bridge_receipt.to_dict(),
        country_risk_context=effective_country_context,
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
        live_price=live_price,
        live_price_at=prior_price_at,
        mos_bar_used=mos_bar_f,
        assumption_snapshot_json=assumption_snapshot,
        notes=f"workbook={dest.name} (redesigned; in-app edit)",
        assumptions_sync_status=None,
        assumptions_synced_at=None,
        provenance=input_provenance,
    )
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        decision = persist_mod.check_promotion(conn, row)
        if not decision.allowed:
            _unlink(staged_dest)
            _unlink(staged_assumptions)
            return _blocked_promotion_result(ticker, decision, workbook=dest)
        workbook_backup = None
        assumptions_backup = None
        workbook_swapped = False
        assumptions_swapped = False
        try:
            workbook_backup = _swap_staged(dest, staged_dest)
            workbook_swapped = True
            assumptions_backup = _swap_staged(assumptions_path, staged_assumptions)
            assumptions_swapped = True
            sync = sync_assumptions_json(repo_root, ticker, inp)
            if sync.status == "failed":
                sys.stderr.write(f"WARNING: assumptions sync for {ticker} failed: {sync.detail}\n")
            row = dataclasses.replace(
                row,
                assumptions_sync_status=sync.as_status_text(),
                assumptions_synced_at=datetime.now(UTC).replace(tzinfo=None),
            )
            persisted = persist_mod.upsert(conn, row)
        except Exception:
            if assumptions_swapped:
                _restore_swap(assumptions_path, assumptions_backup)
            if workbook_swapped:
                _restore_swap(dest, workbook_backup)
            raise
        finally:
            if assumptions_backup is not None:
                _unlink(assumptions_backup)
            if workbook_backup is not None:
                _unlink(workbook_backup)

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
        "dcf_run_persisted": persisted,
        "input_sha256": input_provenance.input_sha256,
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
