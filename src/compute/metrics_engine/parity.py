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

Phase 2 additions, grid-matched per this module's existing rule (period_grid
determines "_ttm" vs the plain non-TTM file, verified directly against real
MELI cache rows for every field below -- NOT assumed from the Phase 1
pattern): `roic_strict` -> `key_metrics_ttm.returnOnInvestedCapitalTTM`,
`roce` -> `key_metrics_ttm.returnOnCapitalEmployedTTM`, `net_debt_to_ebitda`
-> `key_metrics_ttm.netDebtToEBITDATTM`, `cash_conversion_cycle` ->
`key_metrics_ttm.cashConversionCycleTTM` (all four are period_grid="ttm"
formulas here, same TTM-grid reasoning as roe/roa; the first two are
fractions needing the *100 rescale, the latter two are already raw
multiples/day-counts, not rescaled). `asset_turnover` ->
`financial_ratios_ttm.assetTurnoverTTM`, `receivables_turnover` ->
`financial_ratios_ttm.receivablesTurnoverTTM`, `inventory_turnover` ->
`financial_ratios_ttm.inventoryTurnoverTTM` (also period_grid="ttm",
verified to exist as TTM-suffixed fields in the real `_financial_ratios_ttm`
cache, not just the quarterly one). `interest_coverage` ->
`financial_ratios_quarterly.interestCoverageRatio`, `bvps` ->
`financial_ratios_quarterly.bookValuePerShare`, `fcf_per_share` ->
`financial_ratios_quarterly.freeCashFlowPerShare` (these three are
period_grid="quarterly" formulas, so -- per the existing rule -- they
compare against the plain, non-TTM quarterly file/field, none rescaled).

Six Phase-2 metrics have no direct FMP equivalent and stay unmapped
(documented, not silently skipped): `roic_lease_adjusted` (FMP's own
`investedCapital` does not add back operating leases, so it is not the
"the lease-adjusted variant maps to FMP's own number" case -- unlike
net_debt's KNOWN_MISMATCHES precedent, there is no FMP field that even
attempts this definition), `net_debt_strict` / `net_debt_incl_lt_securities`
(FMP's key-metrics files publish `netDebtToEBITDA`, a ratio, but never a
raw net-debt dollar figure standalone), `eps_adjusted_ex_sbc` (FMP has no
SBC-adjusted EPS field), and `revenue_cagr_3y` / `ebitda_cagr_3y` (FMP's
cached files carry point-in-time snapshots, not a rolling 3-year CAGR).

Real-sweep findings (Phase 2 validation, --parity run against a scratch
copy of data/portfolio.db + the live data/historical/fmp/ cache, portfolio
+ evaluation scope, 48 tickers): a wide majority of "fail" outcomes trace
to two STRUCTURAL causes, neither a metrics_engine defect --

1. **Cache staleness, not a computation bug.** The "_ttm" cache files
   (`key_metrics_ttm.json`, `financial_ratios_ttm.json`) carry exactly ONE
   row with no `date` field -- there is no way to verify it reflects the
   SAME reporting period as our latest `kpi_facts` row. Verified directly:
   AVGO's newest computed gross_margin (period_end 2026-05-03) has no
   counterpart yet in the cached `financial_ratios_quarterly.json` (whose
   newest row is 2026-02-01) -- comparing period-aligned rows instead
   (2025-11-01 computed vs the cache's 2025-11-02 row) gives 67.99 vs
   67.99333..., an exact match. `load_fmp_reference`/`compare_ticker`
   inherited this "latest vs latest, no date check" design unchanged from
   Phase 1 -- a genuine harness gap (feeds a period-aware comparison as a
   tracked follow-up), not something Phase 2 introduced or should silently
   patch over with per-ticker KNOWN_MISMATCHES guesses.
2. **net_debt_to_ebitda's documented strict-convention divergence, now
   visible at scale.** For near-zero-net-debt (net-cash) names the strict
   vs FMP-inclusive net-debt definitions can differ enough to flip the
   sign of net_debt_to_ebitda entirely (e.g. GOOG: -0.23 computed vs +0.24
   reference) -- exactly the AAPL-scale ambiguity registry.py's
   NET_DEBT_TO_EBITDA method_notes already name, just showing up broadly
   because most of the roster runs net-cash.

Neither finding produced a NEW verified (ticker, formula_key) reason
confident enough to add to KNOWN_MISMATCHES without over-claiming
certainty per-ticker (staleness and the convention gap are entangled in
any single observation) -- see that module's docstring: "an entry only
after confirming the divergence is a documented method choice... never as
a way to silence an unexplained failure." Left for the parity-harness
follow-up (period-aware comparison) to disentangle properly.

Phase 3 additions (valuation), field names verified directly against a real
MELI `_financial_ratios_ttm.json` / `_key_metrics_ttm.json` cache row (not
guessed): `pe_ttm` -> `financial_ratios_ttm.priceToEarningsRatioTTM`,
`ps_ttm` -> `financial_ratios_ttm.priceToSalesRatioTTM`, `pb` ->
`financial_ratios_ttm.priceToBookRatioTTM` (all three already raw multiples,
not rescaled). `ev_ebitda` -> `key_metrics_ttm.evToEBITDATTM`, `ev_sales` ->
`key_metrics_ttm.evToSalesTTM` (also raw multiples). `fcf_yield` ->
`key_metrics_ttm.freeCashFlowYieldTTM`, `earnings_yield` -> `key_metrics_ttm.
earningsYieldTTM` (both fractions -- 0.133, not 13.3 -- rescaled *100 to
match this engine's percent-unit convention). `enterprise_value_strict` ->
`key_metrics_ttm.enterpriseValueTTM` (a raw dollar figure; FMP's own EV may
include minority interest/preferred equity this engine's v1 omits per
registry.ENTERPRISE_VALUE_STRICT's method_notes -- a per-ticker divergence
traced to that omission gets a KNOWN_MISMATCHES entry once confirmed, not
pre-emptively).

Every Phase-3 mapping is expected to diverge from the reference by MORE than
Phase 1/2's comparisons even when both sides are "correct": FMP's cached
ratio was computed against ITS OWN last-cycle price snapshot, while this
engine's side is a live quote fetched at parity-run time -- the two prices
can genuinely differ by a trading day or more. This is exactly what the
+/-5% valuation tolerance band (parity_known_mismatches._VALUATION_BAND)
already exists to absorb; a fail outside that band is still a real
regression, not price-snapshot noise.
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
    # Phase 2 -- see module docstring for the per-field verification notes.
    # period_grid="ttm" formulas -> the "_ttm" file, TTM-suffixed field.
    "roic_strict": ("key_metrics_ttm", "returnOnInvestedCapitalTTM", True),
    "roce": ("key_metrics_ttm", "returnOnCapitalEmployedTTM", True),
    "net_debt_to_ebitda": ("key_metrics_ttm", "netDebtToEBITDATTM", False),
    "cash_conversion_cycle": ("key_metrics_ttm", "cashConversionCycleTTM", False),
    "asset_turnover": ("financial_ratios_ttm", "assetTurnoverTTM", False),
    "receivables_turnover": ("financial_ratios_ttm", "receivablesTurnoverTTM", False),
    "inventory_turnover": ("financial_ratios_ttm", "inventoryTurnoverTTM", False),
    # period_grid="quarterly" formulas -> the plain non-TTM file/field.
    "interest_coverage": ("financial_ratios_quarterly", "interestCoverageRatio", False),
    "bvps": ("financial_ratios_quarterly", "bookValuePerShare", False),
    "fcf_per_share": ("financial_ratios_quarterly", "freeCashFlowPerShare", False),
    # Phase 3 -- valuation, all period_grid="ttm" -> the "_ttm" file/field
    # convention (see module docstring for the real-cache-verified fields).
    "pe_ttm": ("financial_ratios_ttm", "priceToEarningsRatioTTM", False),
    "ps_ttm": ("financial_ratios_ttm", "priceToSalesRatioTTM", False),
    "pb": ("financial_ratios_ttm", "priceToBookRatioTTM", False),
    "ev_ebitda": ("key_metrics_ttm", "evToEBITDATTM", False),
    "ev_sales": ("key_metrics_ttm", "evToSalesTTM", False),
    "fcf_yield": ("key_metrics_ttm", "freeCashFlowYieldTTM", True),
    "earnings_yield": ("key_metrics_ttm", "earningsYieldTTM", True),
    "enterprise_value_strict": ("key_metrics_ttm", "enterpriseValueTTM", False),
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
        formula = next((f for (key, _v), f in REGISTRY.items() if key == formula_key), None)
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
        if within_tolerance(formula.category, computed, reference, unit=formula.unit):
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
