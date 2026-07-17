"""Parity comparison logic: computed (kpi_facts) vs FMP's own cached ratios.

Structural note: docs/design/bottoms_up_metrics_engine.md section 4 lists six
files for `src/compute/metrics_engine/` and folds the comparison runner into
`tests/test_metrics_engine_parity.py` itself. This module is a deliberate,
minor structural addition beyond that list -- the comparison logic (read the
cached FMP JSON, look up the computed kpi_facts value, apply the tolerance
band) is needed by BOTH the pytest harness (synthetic fixtures only, per this
repo's testing convention -- no prod data at test time) and the
`execution/compute_derived_metrics.py --parity` CLI mode (a real, read-only
sweep against MAIN's cached data, per the mission instructions). Extracting
it here means the CLI and the test import the SAME comparison function
instead of duplicating it.

Field-name correction versus the design doc's sketch: the doc names the
cache files `{T}_ratios_ttm.json` / `{T}_key_metrics_ttm.json`; the files
`execution/save_fmp_data.py` actually writes are `{T}_financial_ratios_ttm.json`
/ `{T}_key_metrics_ttm.json` (verified directly against the real cache).

Grid-matched cache file, NOT always the "_ttm" one: verified against a real
parity run (MELI) that FMP's "_ttm" cache files are genuinely TTM-AGGREGATED
for flow-based ratios (margins) -- comparing a Phase-1 `period_grid="quarterly"`
margin formula (the single latest quarter) against FMP's TTM-summed margin is
an apples-to-oranges mismatch, not a bug. Fix: every `period_grid="quarterly"`
formula compares against the sibling `_quarterly` cache file's non-TTM field
(e.g. `grossProfitMargin`, not `grossProfitMarginTTM`) -- confirmed to exist
with the identical field name minus the TTM suffix. Only `roe`/`roa`
(`period_grid="ttm"`) keep the `_ttm` file, since a real trailing-4-quarter
sum is exactly what those two formulas compute. Balance-sheet-only ratios
(current/quick/cash/debt-to-equity) are point-in-time regardless of file, so
this only changes the flow-based comparisons.

Four Phase-1 metrics have no direct FMP equivalent and are excluded from the
mapping (documented, not silently skipped):
  - `fcf_margin` -- FMP's ratios/key-metrics files carry
    `freeCashFlowPerShare` and `revenuePerShare` separately, never a
    combined FCF-margin-over-revenue field.
  - `revenue_yoy` / `revenue_qoq` / `eps_diluted_yoy` -- these are
    sequential/YoY comparisons; FMP's growth files are quarterly point
    deltas on a different cadence/shape than a single snapshot comparison.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

from .io import latest_ttm_value
from .parity_known_mismatches import KNOWN_MISMATCHES, within_tolerance
from .registry import REGISTRY

# formula_key -> (cache file suffix, FMP field name, needs *100 to become a percent).
# Ratios in FMP's cached ratios/key-metrics files are fractions (0.06, not 6);
# Phase 1's percent-unit formulas store values as whole percents (6, not
# 0.06), so those pairs need the *100 rescale before comparison. File suffix
# matches the formula's own period_grid (see module docstring) -- "_quarterly"
# for every Phase-1 formula except roe/roa, which are genuinely "_ttm".
_FMP_FIELD_MAP: dict[str, tuple[str, str, bool]] = {
    "gross_margin": ("financial_ratios_quarterly", "grossProfitMargin", True),
    "operating_margin": ("financial_ratios_quarterly", "operatingProfitMargin", True),
    "net_margin": ("financial_ratios_quarterly", "netProfitMargin", True),
    "ebitda_margin": ("financial_ratios_quarterly", "ebitdaMargin", True),
    "current_ratio": ("financial_ratios_quarterly", "currentRatio", False),
    "quick_ratio": ("financial_ratios_quarterly", "quickRatio", False),
    "cash_ratio": ("financial_ratios_quarterly", "cashRatio", False),
    "debt_to_equity": ("financial_ratios_quarterly", "debtToEquityRatio", False),
    # period_grid="ttm" -- these two genuinely compare against FMP's TTM files.
    "roe": ("key_metrics_ttm", "returnOnEquityTTM", True),
    "roa": ("key_metrics_ttm", "returnOnAssetsTTM", True),
    "sbc_pct_revenue": ("key_metrics_quarterly", "stockBasedCompensationToRevenue", True),
}


class ParityOutcome:
    MATCH = "match"
    KNOWN_MISMATCH = "known_mismatch"
    FAIL = "fail"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class ParityResult:
    ticker: str
    formula_key: str
    computed: Decimal | None
    reference: Decimal | None
    outcome: str
    detail: str = ""


def load_fmp_reference(fmp_cache_dir: Path, ticker: str, formula_key: str) -> Decimal | None:
    """Read the FMP field mapped to `formula_key` from the cached TTM JSON.

    Returns None when the formula has no mapping (see module docstring) or
    the cache file/field/ticker row is absent -- never a guessed value.
    """
    mapping = _FMP_FIELD_MAP.get(formula_key)
    if mapping is None:
        return None
    suffix, field, needs_pct_scale = mapping
    path = fmp_cache_dir / f"{ticker.upper()}_{suffix}.json"
    if not path.exists():
        return None
    try:
        records: object = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(records, list) or not records:
        return None
    record_list = cast("list[object]", records)
    first = record_list[0]
    if not isinstance(first, dict) or field not in first:
        return None
    row = cast("dict[str, object]", first)
    raw = row[field]
    if not isinstance(raw, (int, float)):
        return None
    value = Decimal(str(raw))
    return value * Decimal(100) if needs_pct_scale else value


def compare_ticker(
    conn: sqlite3.Connection, fmp_cache_dir: Path, ticker: str
) -> list[ParityResult]:
    """Compare every mapped Phase-1 formula's computed value against FMP's
    own cached TTM figure for `ticker`. One ParityResult per mapped formula."""
    results: list[ParityResult] = []
    formula_keys = sorted(_FMP_FIELD_MAP)
    for formula_key in formula_keys:
        formula = next(
            (f for (key, _v), f in REGISTRY.items() if key == formula_key), None
        )
        if formula is None:
            continue
        computed = latest_ttm_value(conn, ticker, formula_key)
        reference = load_fmp_reference(fmp_cache_dir, ticker, formula_key)
        if computed is None or reference is None:
            results.append(
                ParityResult(
                    ticker=ticker,
                    formula_key=formula_key,
                    computed=computed,
                    reference=reference,
                    outcome=ParityOutcome.NO_DATA,
                    detail="missing computed value or FMP reference",
                )
            )
            continue
        if within_tolerance(formula.category, computed, reference):
            results.append(
                ParityResult(
                    ticker=ticker,
                    formula_key=formula_key,
                    computed=computed,
                    reference=reference,
                    outcome=ParityOutcome.MATCH,
                )
            )
            continue
        known_reason = KNOWN_MISMATCHES.get((ticker.upper(), formula_key))
        if known_reason is not None:
            results.append(
                ParityResult(
                    ticker=ticker,
                    formula_key=formula_key,
                    computed=computed,
                    reference=reference,
                    outcome=ParityOutcome.KNOWN_MISMATCH,
                    detail=known_reason,
                )
            )
            continue
        results.append(
            ParityResult(
                ticker=ticker,
                formula_key=formula_key,
                computed=computed,
                reference=reference,
                outcome=ParityOutcome.FAIL,
                detail=f"computed={computed} reference={reference} outside tolerance",
            )
        )
    return results
