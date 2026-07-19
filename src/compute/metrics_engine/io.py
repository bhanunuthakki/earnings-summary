"""The only module in this package touching sqlite3.Connection.

Resolves canonical inputs for a (ticker, period_end, fiscal_period_type)
cell from `financial_facts`, calls `engine.compute`, and persists the
outcome: a successful value lands in `kpi_facts` (via
`pipeline.kpi_persistence.find_or_create_kpi_definition` +
`pipeline.restatement_detector.insert_kpi_with_restatement_detection` --
the same existing pattern `compute.fmp_derived_kpis` uses), and EVERY
attempt -- success, "not applicable to this business model", or any other
not-computable outcome -- lands a row in `metric_computation_attempts` so
"why is there no value here" is always answerable from a row, never
inferred from absence (docs/design/bottoms_up_metrics_engine.md section 3).

Deviation from the doc's io.py sketch re: `_dedupe_by_calendar_quarter`:
this module's own SQL already tier-ranks (source_quality_tier, id) and
groups by exact `period_end` before bucketing into calendar quarters (the
same one-winner-per-exact-period_end contract
`compute.fmp_derived_kpis._fetch_quarterly_facts` uses) -- so by the time
rows reach `_dedupe_by_calendar_quarter`, there is normally nothing left to
coalesce. It is still invoked directly (not reimplemented) as the
documented safety net against the rare case of two distinct period_ends
landing in the same calendar quarter.

Phase 3 (valuation): MARKET_CAP/PRICE have no financial_facts equivalent --
`_resolve_valuation_spot` fetches a live price via `sources.price.
read_live_price` (the SAME multi-source stack `src/dcf/reprice.py` and
`research_cockpit.profile_quote` already use, reused rather than a new
fetcher) and derives market_cap bottoms-up (price * the latest quarter's
weighted_avg_shares_diluted). Computed ONLY at the latest calendar quarter
(`_VALUATION_FORMULA_KEYS`) -- a live price has no historical equivalent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from models.companies import AccountingStandard, BusinessModelClass
from models.documents import SourceQualityTier, SourceType
from models.facts import DerivedInputRef, DerivedRef
from pipeline import locators
from pipeline.confidence import score_confidence
from pipeline.kpi_persistence import find_or_create_kpi_definition
from pipeline.restatement_detector import insert_kpi_with_restatement_detection
from report.sections._common import calendar_quarter_key

# Reused directly per docs/design/bottoms_up_metrics_engine.md section 0 --
# the module docstring above explains why this private helper (not just
# calendar_quarter_key) is still needed. Scoped suppression, not a blanket
# type: ignore: this is the one documented, intentional cross-module reuse
# of a private symbol.
from report.sections.financials import (
    _dedupe_by_calendar_quarter,  # pyright: ignore[reportPrivateUsage]
)
from sources.price import LivePrice, read_live_price
from timeseries.loaders import reader_tier_rank_sql

from .applicability import applicable_formulas
from .engine import ComputedValue, NotComputable, compute
from .inputs import CanonicalConcept, resolve_concept
from .registry import ENGINE_VERSION, REGISTRY, FormulaDef, ReasonCode, all_latest

# Concepts whose financial_facts value is a FLOW over the reporting period
# (income-statement / cash-flow lines) -- summed across the trailing 4
# calendar quarters for a period_grid="ttm" formula. Everything else is
# treated as a point-in-time STOCK concept (latest quarter's value; see
# docs/design/bottoms_up_metrics_engine.md section 2 "TTM" note).
_FLOW_CONCEPTS: frozenset[CanonicalConcept] = frozenset(
    {
        CanonicalConcept.REVENUE,
        CanonicalConcept.GROSS_PROFIT,
        CanonicalConcept.OPERATING_INCOME,
        CanonicalConcept.NET_INCOME,
        CanonicalConcept.COST_OF_REVENUE,
        CanonicalConcept.STOCK_BASED_COMPENSATION,
        CanonicalConcept.INCOME_TAX_EXPENSE,
        CanonicalConcept.PRETAX_INCOME,
        CanonicalConcept.INTEREST_EXPENSE,
        CanonicalConcept.FREE_CASH_FLOW,
        CanonicalConcept.OPERATING_CASH_FLOW,
        CanonicalConcept.CAPITAL_EXPENDITURE,
        CanonicalConcept.DEPRECIATION_AND_AMORTIZATION,
        CanonicalConcept.EPS_BASIC,
        CanonicalConcept.EPS_DILUTED,
    }
)

# Same-concept prior-period offset each growth formula compares against.
# For the quarterly-cadence formulas this is in calendar quarters (4 back =
# same calendar quarter one year prior, never a fiscal-label comparison --
# the section 0 landmine); for the two period_grid="fy" CAGR formulas
# (Phase 2) this is in fiscal years within the SEPARATE annual-cells list
# (see _build_annual_cells) -- 3 back per docs/design/
# bottoms_up_metrics_engine.md section 1's "3 full FY gaps" requirement.
_PRIOR_OFFSET: dict[str, int] = {
    "revenue_yoy": 4,
    "eps_diluted_yoy": 4,
    "revenue_qoq": 1,
    "revenue_cagr_3y": 3,
    "ebitda_cagr_3y": 3,
}

# TTM window gap guard: reject a trailing-4-quarter window spanning more
# than this many days (mirrors fmp_derived_kpis.py's existing ROE guard).
_TTM_MAX_SPAN_DAYS = 400

# Phase 2: FY-cadence CAGR span guard -- "requires 3 full FY gaps, no
# interpolation across a stub year" (docs/design/
# bottoms_up_metrics_engine.md section 1). Positional indexing into
# _build_annual_cells' sorted-by-period_end list would silently treat a
# missing fiscal year as "3 years apart" if we didn't also check the actual
# calendar-day span -- this guard rejects that case explicitly rather than
# producing a mislabeled CAGR. +/-45 days of slack absorbs ordinary
# reporting-date drift (leap years, a shifted fiscal year-end).
_FY_CAGR_MIN_SPAN_DAYS = 3 * 365 - 45
_FY_CAGR_MAX_SPAN_DAYS = 3 * 365 + 45

# Phase 2: formula_key -> the subset of its required_inputs that resolve as
# a 2-point AVERAGE (mean of the TTM window's first and last quarter-end
# balances) rather than a TTM flow-sum or a period-end point-in-time value.
# Concept-level (not formula-level) because the SAME concept is treated
# differently by different formulas -- e.g. TOTAL_ASSETS is period-end for
# roa but averaged for asset_turnover (docs/design/
# bottoms_up_metrics_engine.md section 1's asset_turnover/receivables_turnover
# method notes).
_AVERAGE_STOCK_FORMULAS: dict[str, frozenset[CanonicalConcept]] = {
    "asset_turnover": frozenset({CanonicalConcept.TOTAL_ASSETS}),
    "receivables_turnover": frozenset({CanonicalConcept.ACCOUNTS_RECEIVABLE}),
    "inventory_turnover": frozenset({CanonicalConcept.INVENTORY}),
    "cash_conversion_cycle": frozenset(
        {
            CanonicalConcept.INVENTORY,
            CanonicalConcept.ACCOUNTS_RECEIVABLE,
            CanonicalConcept.ACCOUNTS_PAYABLE,
        }
    ),
}


# Phase 3: a price-reader is (repo_root, ticker) -> LivePrice | None. Defaults
# to `sources.price.read_live_price` -- the SAME multi-source stack
# src/dcf/reprice.py and research_cockpit.profile_quote already use, per the
# mandate to reuse the existing live-price path rather than add a new
# fetcher. Injected in tests to avoid the network (mirrors reprice.py's own
# PriceReader alias).
PriceReader = Callable[[Path, str], LivePrice | None]

# One resolved input's lineage: (concept, as-of date, doc_id). doc_id is None
# for a live-price-sourced input (MARKET_CAP/PRICE) -- there is no
# financial_facts document backing a vendor quote, unlike every other
# concept this engine resolves.
LineageEntry = tuple[CanonicalConcept, datetime, int | None]

# Phase 3: formula_keys whose formula consumes MARKET_CAP/PRICE. These are
# computed ONLY at the LATEST calendar quarter (compute_for_ticker skips
# every older cell entirely -- not even a not_computable row, since a spot
# valuation was never attempted for a past date): a live price has no
# historical equivalent without a separate historical price-series fetch,
# which is out of scope per the "reuse the existing live-price path, do not
# add a new fetcher" mandate (docs/design/bottoms_up_metrics_engine.md
# section 6 Phase 3).
_VALUATION_FORMULA_KEYS: frozenset[str] = frozenset(
    {
        "enterprise_value_strict",
        "pe_ttm",
        "ps_ttm",
        "pb",
        "ev_ebitda",
        "ev_sales",
        "fcf_yield",
        "earnings_yield",
    }
)

# Concepts with no financial_facts equivalent -- resolved only via
# _resolve_valuation_spot's live-price fetch, never via resolve_concept's
# per-standard field mapping. Excluded from compute_for_ticker's
# MISSING_INPUT_MAPPING check so a valuation formula is never rejected
# before it even reaches the per-cell compute path (resolve_concept
# correctly returns None for these under EVERY standard -- that is the
# documented state, not a gap to report).
_LIVE_PRICE_CONCEPTS: frozenset[CanonicalConcept] = frozenset(
    {CanonicalConcept.MARKET_CAP, CanonicalConcept.PRICE}
)


# Threaded live-price prefetch -- mirrors src/dcf/reprice.py's
# PRICE_FETCH_WORKERS/PER_TICKER_TIMEOUT_S exactly (same yfinance-bound
# per-ticker cost, same bounded-concurrency shape).
PRICE_FETCH_WORKERS = 8
PRICE_FETCH_TIMEOUT_S = 15.0


def prefetch_live_prices(
    repo_root: Path,
    tickers: list[str],
    *,
    price_reader: PriceReader = read_live_price,
    workers: int = PRICE_FETCH_WORKERS,
    timeout_s: float = PRICE_FETCH_TIMEOUT_S,
    log: Callable[..., None] | None = None,
) -> dict[str, LivePrice | None]:
    """Fetch every ticker's live price under bounded concurrency, ONCE, so a
    multi-ticker compute sweep never blocks on a serial network call per
    ticker (worst case ceil(n/workers)*timeout instead of n*timeout).

    Mirrors ``src/dcf/reprice.py``'s ``_fetch_prices`` contract exactly:
    submit-all-then-collect, per-future timeout, abandon-and-move-on on a
    stuck ticker (the worker thread leaks until interpreter exit --
    acceptable for a once-daily batch), ``shutdown(wait=False)`` so an
    abandoned future can never hang this function itself. A ticker whose
    fetch times out or raises maps to None -- its valuation formulas then
    resolve MISSING_INPUT, same as any other absent input.

    ``log`` (optional) receives ``log(event_name, ticker=..., ...)`` for the
    timeout/error paths -- the CLI passes its JSON-line stderr logger.
    """
    from concurrent.futures import Future, ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeoutError

    out: dict[str, LivePrice | None] = {}
    if not tickers:
        return out
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="metrics-engine-price")
    try:
        futures: dict[Future[LivePrice | None], str] = {
            executor.submit(price_reader, repo_root, ticker): ticker for ticker in tickers
        }
        for future, ticker in futures.items():
            try:
                out[ticker] = future.result(timeout=timeout_s)
            except FutureTimeoutError:
                if log is not None:
                    log("price_prefetch_timeout", ticker=ticker, timeout_s=timeout_s)
                out[ticker] = None
            except Exception as exc:
                if log is not None:
                    log("price_prefetch_error", ticker=ticker, error=str(exc)[:200])
                out[ticker] = None
    finally:
        executor.shutdown(wait=False)
    return out


def cached_price_reader(price: LivePrice | None) -> PriceReader:
    """Wrap an already-prefetched LivePrice as a PriceReader so
    ``compute_for_ticker`` reads the cached quote instead of fetching."""

    def _reader(_repo_root: Path, _ticker: str) -> LivePrice | None:
        return price

    return _reader


def _resolve_valuation_spot(
    price_reader: PriceReader,
    repo_root: Path,
    ticker: str,
    latest_cell: QuarterCell,
) -> tuple[dict[CanonicalConcept, Decimal], datetime | None, str | None]:
    """PRICE + MARKET_CAP for the LATEST calendar quarter, from a live price fetch.

    market_cap is computed bottoms-up (live price * the latest quarter's
    weighted_avg_shares_diluted) rather than read from the cached
    historical_market_cap.json row docs/design/bottoms_up_metrics_engine.md
    section 1 names -- see registry.py's `_MARKET_CAP_METHOD_NOTE` for the
    full justification (keeps the price and market-cap legs internally
    consistent, since a cached market-cap row can be up to a day stale
    relative to a live quote).

    Returns ({}, None, None) on any miss (no live price, or no diluted share
    count to multiply by) -- every valuation formula then resolves
    MISSING_INPUT for MARKET_CAP/PRICE, same as any other absent input.
    """
    shares = latest_cell.values.get(CanonicalConcept.WEIGHTED_AVG_SHARES_DILUTED)
    if shares is None or shares <= 0:
        return {}, None, None
    live = price_reader(repo_root, ticker)
    if live is None:
        return {}, None, None
    price = Decimal(str(live.price))
    market_cap = price * shares
    return (
        {CanonicalConcept.PRICE: price, CanonicalConcept.MARKET_CAP: market_cap},
        live.fetched_at,
        live.source_name,
    )


@dataclass(frozen=True)
class QuarterCell:
    """One ticker's resolved concepts for one calendar quarter."""

    period_end: datetime
    fiscal_period_type: str
    values: dict[CanonicalConcept, Decimal]
    doc_ids: dict[CanonicalConcept, int]


@dataclass(frozen=True)
class TickerComputeSummary:
    """Per-ticker outcome tally for one compute_for_ticker() run."""

    ticker: str
    business_model: BusinessModelClass
    accounting_standard: AccountingStandard
    quarters_seen: int
    attempts_written: int
    computed_ok: int
    not_computable: int
    skipped_unchanged: int


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def resolve_classification(
    conn: sqlite3.Connection, ticker: str
) -> tuple[BusinessModelClass, AccountingStandard]:
    """Read tracked_companies.business_model_class/accounting_standard for `ticker`.

    Schema-defensive (alembic 0163/0164): falls back to the default
    (operating_company, us_gaap) when the columns are absent (a synthetic
    test fixture) or the ticker has no tracked_companies row.
    """
    has_bmc = _table_has_column(conn, "tracked_companies", "business_model_class")
    has_std = _table_has_column(conn, "tracked_companies", "accounting_standard")
    if not has_bmc and not has_std:
        return BusinessModelClass.OPERATING_COMPANY, AccountingStandard.US_GAAP
    cols: list[str] = []
    if has_bmc:
        cols.append("business_model_class")
    if has_std:
        cols.append("accounting_standard")
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM tracked_companies WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    ).fetchone()
    if row is None:
        return BusinessModelClass.OPERATING_COMPANY, AccountingStandard.US_GAAP
    business_model = (
        BusinessModelClass(row["business_model_class"])
        if has_bmc and row["business_model_class"]
        else BusinessModelClass.OPERATING_COMPANY
    )
    accounting_standard = (
        AccountingStandard(row["accounting_standard"])
        if has_std and row["accounting_standard"]
        else AccountingStandard.US_GAAP
    )
    return business_model, accounting_standard


def upsert_formula_definitions(conn: sqlite3.Connection) -> dict[tuple[str, int], int]:
    """Find-or-create a formula_definitions row per REGISTRY entry.

    Mirrors `pipeline.kpi_persistence.find_or_create_kpi_definition`'s
    idempotency: a (formula_key, version) pair that already exists is
    looked up, not re-inserted. Returns {(formula_key, version): row id}.
    """
    out: dict[tuple[str, int], int] = {}
    for (key, version), formula in REGISTRY.items():
        existing = conn.execute(
            "SELECT id FROM formula_definitions WHERE formula_key = ? AND version = ?",
            (key, version),
        ).fetchone()
        if existing is not None:
            out[(key, version)] = int(existing["id"])
            continue
        cur = conn.execute(
            "INSERT INTO formula_definitions "
            "(formula_key, version, category, display_formula, method_notes, "
            " period_grid, unit, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                formula.formula_key,
                formula.version,
                formula.category.value,
                formula.display_formula,
                formula.method_notes,
                formula.period_grid,
                formula.unit.value,
                datetime.now(),
            ),
        )
        out[(key, version)] = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.commit()
    return out


def _build_quarter_cells(
    conn: sqlite3.Connection, ticker: str, standard: AccountingStandard
) -> list[QuarterCell]:
    """Read every Phase-1 concept's financial_facts rows for `ticker`, tier-ranked
    per exact period_end, then bucket into one QuarterCell per calendar quarter.

    Period discovery is deliberately UNRESTRICTED by which concepts happen to
    map for `standard` (any line_item counts): an IFRS ticker in Phase 1 has
    zero mapped concepts (IFRS_FIELD_MAP is empty), but it still reported
    real quarters -- those quarters must exist as QuarterCells (with empty
    `values`/`doc_ids`) so `compute_for_ticker` can write a
    `MISSING_INPUT_MAPPING` metric_computation_attempts row for each one.
    Without this, an IFRS ticker would get NO cells at all and therefore NO
    attempts rows whatsoever -- silently reproducing the exact "absence is
    ambiguous" failure mode the whole engine exists to close (docs/design/
    bottoms_up_metrics_engine.md section 3).
    """
    period_rows = conn.execute(
        """
        SELECT DISTINCT period_end, fiscal_period_type
        FROM financial_facts
        WHERE ticker = ? AND fiscal_period_type IN ('Q1','Q2','Q3','Q4')
        """,
        (ticker.upper(),),
    ).fetchall()
    if not period_rows:
        return []

    all_concepts = tuple(CanonicalConcept)
    concept_to_field = {c: resolve_concept(standard, c) for c in all_concepts}
    field_to_concept = {f: c for c, f in concept_to_field.items() if f is not None}
    fields = sorted(field_to_concept)

    grouped: dict[tuple[datetime, str], dict[str, object]] = {}
    for row in period_rows:
        pe_raw = row["period_end"]
        pe = datetime.fromisoformat(pe_raw) if isinstance(pe_raw, str) else pe_raw
        key = (pe, row["fiscal_period_type"])
        grouped[key] = {"period_end": pe, "fiscal_period_type": row["fiscal_period_type"]}

    if fields:
        tier_rank = reader_tier_rank_sql(conn)
        placeholders = ",".join("?" * len(fields))
        cur = conn.execute(
            f"""
            SELECT ff.period_end, ff.fiscal_period_type, ff.line_item, ff.value, ff.source_doc_id
            FROM financial_facts ff
            JOIN documents d ON d.id = ff.source_doc_id
            WHERE ff.ticker = ?
              AND d.file_path NOT LIKE '%_ttm.json'
              AND d.file_path NOT LIKE '%_annual.json'
              AND ff.line_item IN ({placeholders})
              AND ff.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
            ORDER BY ff.period_end ASC, {tier_rank} ASC, ff.id ASC
            """,
            (ticker.upper(), *fields),
        )
        for row in cur.fetchall():
            pe_raw = row["period_end"]
            pe = datetime.fromisoformat(pe_raw) if isinstance(pe_raw, str) else pe_raw
            key = (pe, row["fiscal_period_type"])
            bucket = grouped.setdefault(
                key, {"period_end": pe, "fiscal_period_type": row["fiscal_period_type"]}
            )
            field_name = str(row["line_item"])
            bucket[field_name] = Decimal(str(row["value"]))
            bucket[f"__doc__{field_name}"] = int(row["source_doc_id"])

    raw_rows = [grouped[k] for k in sorted(grouped, key=lambda k: k[0])]
    deduped_rows = _dedupe_by_calendar_quarter(raw_rows) if raw_rows else []

    cells: list[QuarterCell] = []
    for row in deduped_rows:
        pe_raw = row["period_end"]
        pe = pe_raw if isinstance(pe_raw, datetime) else datetime.fromisoformat(str(pe_raw)[:19])
        values: dict[CanonicalConcept, Decimal] = {}
        doc_ids: dict[CanonicalConcept, int] = {}
        for field_name, concept in field_to_concept.items():
            v = row.get(field_name)
            if isinstance(v, Decimal):
                values[concept] = v
                doc_id = row.get(f"__doc__{field_name}")
                if isinstance(doc_id, int):
                    doc_ids[concept] = doc_id
        cells.append(
            QuarterCell(
                period_end=pe,
                fiscal_period_type=str(row.get("fiscal_period_type") or ""),
                values=values,
                doc_ids=doc_ids,
            )
        )
    cells.sort(key=lambda c: calendar_quarter_key(c.period_end))
    return cells


def _build_annual_cells(
    conn: sqlite3.Connection, ticker: str, standard: AccountingStandard
) -> list[QuarterCell]:
    """Read every Phase-2 concept's financial_facts rows for `ticker` at
    fiscal_period_type='FY', tier-ranked per exact period_end, one
    QuarterCell per fiscal year -- the annual-cadence sibling of
    _build_quarter_cells, feeding the two period_grid="fy" CAGR formulas
    (revenue_cagr_3y, ebitda_cagr_3y).

    Deliberately does NOT exclude '%_annual.json'-sourced rows (unlike
    _build_quarter_cells' filter, verified directly against a real ticker:
    MELI's FY revenue rows are sourced from
    `MELI_income_statement_annual.json`, exactly the file that filter would
    drop -- reusing it here would silently starve every FMP-sourced FY row).
    Still excludes '%_ttm.json' -- a trailing-twelve-month aggregate is not
    the same thing as a fixed fiscal year, even for a calendar-year filer.

    No calendar-quarter dedup step: an FY row's period_end IS the fiscal
    year-end date already, so tier-ranked-per-exact-period_end is the full
    dedup this grid needs (mirrors the same period discovery being
    unrestricted-by-mapping rationale as _build_quarter_cells, for the same
    IFRS-ticker-still-needs-a-metric_computation_attempts-row reason).
    """
    period_rows = conn.execute(
        """
        SELECT DISTINCT period_end, fiscal_period_type
        FROM financial_facts
        WHERE ticker = ? AND fiscal_period_type = 'FY'
        """,
        (ticker.upper(),),
    ).fetchall()
    if not period_rows:
        return []

    all_concepts = tuple(CanonicalConcept)
    concept_to_field = {c: resolve_concept(standard, c) for c in all_concepts}
    field_to_concept = {f: c for c, f in concept_to_field.items() if f is not None}
    fields = sorted(field_to_concept)

    grouped: dict[datetime, dict[str, object]] = {}
    for row in period_rows:
        pe_raw = row["period_end"]
        pe = datetime.fromisoformat(pe_raw) if isinstance(pe_raw, str) else pe_raw
        grouped[pe] = {"period_end": pe, "fiscal_period_type": "FY"}

    if fields:
        tier_rank = reader_tier_rank_sql(conn)
        placeholders = ",".join("?" * len(fields))
        cur = conn.execute(
            f"""
            SELECT ff.period_end, ff.line_item, ff.value, ff.source_doc_id
            FROM financial_facts ff
            JOIN documents d ON d.id = ff.source_doc_id
            WHERE ff.ticker = ?
              AND d.file_path NOT LIKE '%_ttm.json'
              AND ff.line_item IN ({placeholders})
              AND ff.fiscal_period_type = 'FY'
            ORDER BY ff.period_end ASC, {tier_rank} ASC, ff.id ASC
            """,
            (ticker.upper(), *fields),
        )
        for row in cur.fetchall():
            pe_raw = row["period_end"]
            pe = datetime.fromisoformat(pe_raw) if isinstance(pe_raw, str) else pe_raw
            bucket = grouped.setdefault(pe, {"period_end": pe, "fiscal_period_type": "FY"})
            field_name = str(row["line_item"])
            bucket[field_name] = Decimal(str(row["value"]))
            bucket[f"__doc__{field_name}"] = int(row["source_doc_id"])

    cells: list[QuarterCell] = []
    for pe in sorted(grouped):
        row = grouped[pe]
        values: dict[CanonicalConcept, Decimal] = {}
        doc_ids: dict[CanonicalConcept, int] = {}
        for field_name, concept in field_to_concept.items():
            v = row.get(field_name)
            if isinstance(v, Decimal):
                values[concept] = v
                doc_id = row.get(f"__doc__{field_name}")
                if isinstance(doc_id, int):
                    doc_ids[concept] = doc_id
        cells.append(
            QuarterCell(period_end=pe, fiscal_period_type="FY", values=values, doc_ids=doc_ids)
        )
    return cells


def _resolve_ttm_sum(
    cells: list[QuarterCell], idx: int, concept: CanonicalConcept
) -> tuple[Decimal | None, list[tuple[datetime, int]]]:
    """Sum `concept` over cells[idx-3:idx+1], requiring 4 quarters within
    _TTM_MAX_SPAN_DAYS. Returns (sum_or_None, [(period_end, doc_id), ...])."""
    if idx < 3:
        return None, []
    window = cells[idx - 3 : idx + 1]
    if (window[-1].period_end - window[0].period_end).days > _TTM_MAX_SPAN_DAYS:
        return None, []
    total = Decimal(0)
    provenance: list[tuple[datetime, int]] = []
    for cell in window:
        v = cell.values.get(concept)
        if v is None:
            return None, []
        total += v
        doc_id = cell.doc_ids.get(concept)
        if doc_id is not None:
            provenance.append((cell.period_end, doc_id))
    return total, provenance


def _resolve_ttm_average(
    cells: list[QuarterCell], idx: int, concept: CanonicalConcept
) -> tuple[Decimal | None, list[tuple[datetime, int]]]:
    """Average `concept` over the TTM window's first and last quarter-end
    balances (cells[idx-3] and cells[idx]) -- "average of period start/end"
    per docs/design/bottoms_up_metrics_engine.md section 1's
    asset_turnover/receivables_turnover/inventory_turnover method notes.
    Same 4-quarter-window / _TTM_MAX_SPAN_DAYS gap guard as _resolve_ttm_sum
    so the average always spans the SAME window the sibling flow-sum
    (revenue_ttm, cost_of_revenue_ttm) uses. Returns
    (average_or_None, [(period_end, doc_id), ...] for both endpoints)."""
    if idx < 3:
        return None, []
    window_start = cells[idx - 3]
    window_end = cells[idx]
    if (window_end.period_end - window_start.period_end).days > _TTM_MAX_SPAN_DAYS:
        return None, []
    start_value = window_start.values.get(concept)
    end_value = window_end.values.get(concept)
    if start_value is None or end_value is None:
        return None, []
    provenance: list[tuple[datetime, int]] = []
    for cell in (window_start, window_end):
        doc_id = cell.doc_ids.get(concept)
        if doc_id is not None:
            provenance.append((cell.period_end, doc_id))
    return (start_value + end_value) / Decimal(2), provenance


def _resolve_inputs(
    cells: list[QuarterCell], idx: int, formula: FormulaDef
) -> tuple[
    dict[CanonicalConcept, Decimal | None],
    dict[CanonicalConcept, Decimal | None] | None,
    list[LineageEntry],
]:
    """Build (resolved_inputs, prior_inputs, lineage_refs) for one formula at one cell.

    `cells` is whichever grid matches `formula.period_grid` -- the
    calendar-quarter list for "quarterly"/"ttm" formulas, or the annual
    (fiscal_period_type='FY') list from _build_annual_cells for "fy"
    formulas (compute_for_ticker picks the right one before calling this).

    lineage_refs is a flat list of (concept, period_end, doc_id) covering
    every input actually used (including TTM window legs) -- the raw
    material for computed_from's inputs[] list.
    """
    cell = cells[idx]
    concepts = tuple(formula.required_inputs) + tuple(formula.optional_inputs)
    resolved: dict[CanonicalConcept, Decimal | None] = {}
    lineage: list[LineageEntry] = []
    average_concepts = _AVERAGE_STOCK_FORMULAS.get(formula.formula_key, frozenset())

    if formula.period_grid == "ttm":
        for concept in concepts:
            if concept in _FLOW_CONCEPTS:
                total, provenance = _resolve_ttm_sum(cells, idx, concept)
                resolved[concept] = total
                for pe, doc_id in provenance:
                    lineage.append((concept, pe, doc_id))
            elif concept in average_concepts:
                average, provenance = _resolve_ttm_average(cells, idx, concept)
                resolved[concept] = average
                for pe, doc_id in provenance:
                    lineage.append((concept, pe, doc_id))
            else:
                resolved[concept] = cell.values.get(concept)
                doc_id = cell.doc_ids.get(concept)
                if doc_id is not None:
                    lineage.append((concept, cell.period_end, doc_id))
        return resolved, None, lineage

    # quarterly grid, and the fy grid (period-end read directly from the
    # cell -- an FY row already IS the full-year flow/stock value, no
    # summing needed; only the prior-period lookback below differs by grid).
    for concept in concepts:
        resolved[concept] = cell.values.get(concept)
        doc_id = cell.doc_ids.get(concept)
        if doc_id is not None:
            lineage.append((concept, cell.period_end, doc_id))

    prior_offset = _PRIOR_OFFSET.get(formula.formula_key)
    if prior_offset is None:
        return resolved, None, lineage

    prior_idx = idx - prior_offset
    if prior_idx < 0:
        return resolved, None, lineage
    prior_cell = cells[prior_idx]

    if formula.period_grid == "fy":
        span_days = (cell.period_end - prior_cell.period_end).days
        if not (_FY_CAGR_MIN_SPAN_DAYS <= span_days <= _FY_CAGR_MAX_SPAN_DAYS):
            # A missing fiscal year would otherwise let positional indexing
            # silently compare non-adjacent years as if they were exactly 3
            # apart -- reject rather than mislabel the CAGR window.
            return resolved, None, lineage

    prior: dict[CanonicalConcept, Decimal | None] = {}
    for concept in concepts:
        prior[concept] = prior_cell.values.get(concept)
        doc_id = prior_cell.doc_ids.get(concept)
        if doc_id is not None:
            lineage.append((concept, prior_cell.period_end, doc_id))
    return resolved, prior, lineage


def _input_fingerprint(
    lineage: list[LineageEntry],
) -> str:
    """sha256 of the sorted (concept, period_end, doc_id) tuples that fed a
    computation -- the recompute-skip key (docs/design/
    bottoms_up_metrics_engine.md section 2 "Recompute triggers")."""
    parts = sorted(f"{c.value}|{pe.date().isoformat()}|{doc_id}" for c, pe, doc_id in lineage)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _existing_attempt(
    conn: sqlite3.Connection,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    formula_id: int,
) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT input_fingerprint, engine_version FROM metric_computation_attempts "
        "WHERE ticker = ? AND period_end = ? AND fiscal_period_type = ? AND formula_id = ?",
        (ticker.upper(), period_end, fiscal_period_type, formula_id),
    ).fetchone()
    if row is None:
        return None
    return str(row["input_fingerprint"]), str(row["engine_version"])


def _lineage_json(formula: FormulaDef, lineage: list[LineageEntry]) -> str:
    return json.dumps(
        {
            "display": formula.display_formula,
            "inputs": [
                {
                    # Phase 3: a live-price-sourced input (MARKET_CAP/PRICE)
                    # carries doc_id=None -- there is no financial_facts
                    # document behind a vendor quote, so it is refed
                    # "vendor_field" (models.facts.VendorFieldRef's category)
                    # rather than "financial_fact". period_end doubles as the
                    # quote's as-of date for these entries -- see
                    # registry._MARKET_CAP_METHOD_NOTE's staleness contract.
                    "ref": "financial_fact" if doc_id is not None else "vendor_field",
                    "item": concept.value,
                    "period_end": pe.date().isoformat(),
                    "doc_id": doc_id,
                }
                for concept, pe, doc_id in lineage
            ],
        },
        separators=(",", ":"),
    )


def _persist_attempt(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    formula: FormulaDef,
    formula_id: int,
    result: ComputedValue | NotComputable,
    lineage: list[LineageEntry],
) -> None:
    fingerprint = _input_fingerprint(lineage)
    kpi_fact_id: int | None = None
    reason_code: str | None = None
    reason_detail: str | None = None

    if isinstance(result, ComputedValue):
        status = "ok"
        kpi_def_id = find_or_create_kpi_definition(
            conn,
            ticker=ticker,
            name=formula.formula_key,
            unit=formula.unit,
            primary_source=SourceType.FMP,
        )
        confidence = score_confidence(
            tier=SourceQualityTier.FMP_NORMALIZED, extracted_by="metrics_engine"
        )
        # Phase 3: a live-price-sourced input (MARKET_CAP/PRICE) carries
        # doc_id=None (no financial_facts document backs a vendor quote) --
        # anchor on the first input that DOES have a real document instead of
        # naively taking lineage[0], so a valuation formula's kpi_facts row
        # still points at a genuine filing. Falls back to the pre-existing 0
        # sentinel only when lineage is entirely empty (or entirely
        # vendor-sourced, which cannot happen today: every valuation formula
        # also requires at least one financial_facts-backed concept).
        anchor_doc_id = next((doc_id for _, _, doc_id in lineage if doc_id is not None), 0)
        # Canonical `derived` locator (docs/design/provenance_clickthrough.md
        # §1.5/§3.2, bottoms_up_metrics_engine.md §3): the SAME lineage data
        # `_lineage_json` already assembles for `computed_from`, promoted into
        # the typed locator union so the Phase C formula-tree peek can walk
        # it without a retrofit. `computed_from` is still written alongside
        # (unchanged JSON shape) for existing readers
        # (ui.source_chip._lineage_rows) -- one underlying lineage, two
        # representations during the transition, not two divergent formats:
        # `locator.derived` is the canonical read path going forward: a
        # pre-existing row lacking `locator` (written by PR #904, before this
        # kind existed) is lazily upgraded on read via
        # `pipeline.locators.derived_locator_from_computed_from` rather than
        # backfilled.
        derived_loc = locators.derived_locator(
            derived=DerivedRef(
                formula_id=formula_id,
                display=formula.display_formula,
                method_flags=list(result.method_flags),
                inputs=[
                    DerivedInputRef(
                        ref="financial_fact" if doc_id is not None else "vendor_field",
                        item=concept.value,
                        period_end=pe.date().isoformat(),
                        doc_id=doc_id,
                    )
                    for concept, pe, doc_id in lineage
                ],
            )
        )
        new_id, _superseded_id = insert_kpi_with_restatement_detection(
            conn,
            ticker=ticker,
            period_end=period_end,
            fiscal_period_type=fiscal_period_type,
            kpi_definition_id=kpi_def_id,
            value=result.value,
            unit=formula.unit.value,
            source_doc_id=anchor_doc_id,
            confidence=confidence,
            extracted_by="metrics_engine",
            locator=derived_loc.to_json(),
            computed_from=_lineage_json(formula, lineage),
            formula_id=formula_id,
            formula_version=formula.version,
        )
        kpi_fact_id = new_id
    else:
        status = "not_computable"
        reason_code = result.reason_code.value
        reason_detail = result.reason_detail

    conn.execute(
        """
        INSERT INTO metric_computation_attempts
            (ticker, period_end, fiscal_period_type, formula_id, status,
             reason_code, reason_detail, kpi_fact_id, input_fingerprint,
             engine_version, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, period_end, fiscal_period_type, formula_id) DO UPDATE SET
            status = excluded.status,
            reason_code = excluded.reason_code,
            reason_detail = excluded.reason_detail,
            kpi_fact_id = excluded.kpi_fact_id,
            input_fingerprint = excluded.input_fingerprint,
            engine_version = excluded.engine_version,
            computed_at = excluded.computed_at
        """,
        (
            ticker.upper(),
            period_end,
            fiscal_period_type,
            formula_id,
            status,
            reason_code,
            reason_detail,
            kpi_fact_id,
            fingerprint,
            ENGINE_VERSION,
            datetime.now(),
        ),
    )


def compute_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    force: bool = False,
    repo_root: Path | None = None,
    price_reader: PriceReader = read_live_price,
) -> TickerComputeSummary:
    """End-to-end compute for one ticker: resolve inputs, compute every
    registry formula at every applicable period (calendar quarter for
    "quarterly"/"ttm" formulas, fiscal year for "fy" formulas -- Phase 2),
    persist. Idempotent (input_fingerprint skip) unless `force`.

    Phase 3: `repo_root` is required to fetch a live price for the valuation
    formulas (`_VALUATION_FORMULA_KEYS`) -- when omitted (an older caller, or
    a test with no filesystem to root against) those formulas degrade to
    MISSING_INPUT for MARKET_CAP/PRICE, exactly like any other absent input,
    rather than raising. `price_reader` defaults to
    `sources.price.read_live_price` (the same stack `src/dcf/reprice.py` and
    `research_cockpit.profile_quote` already use); tests inject a stub to
    avoid the network.
    """
    ticker = ticker.upper()
    business_model, standard = resolve_classification(conn, ticker)
    formula_ids = upsert_formula_definitions(conn)
    applicable = {f.formula_key for f in applicable_formulas(business_model, ticker=ticker)}
    quarterly_cells = _build_quarter_cells(conn, ticker, standard)
    annual_cells = _build_annual_cells(conn, ticker, standard)

    spot_values: dict[CanonicalConcept, Decimal] = {}
    price_asof: datetime | None = None
    price_source: str | None = None
    if quarterly_cells and repo_root is not None:
        spot_values, price_asof, price_source = _resolve_valuation_spot(
            price_reader, repo_root, ticker, quarterly_cells[-1]
        )

    attempts = 0
    computed_ok = 0
    not_computable = 0
    skipped = 0

    for formula in all_latest():
        formula_id = formula_ids[(formula.formula_key, formula.version)]
        is_applicable = formula.formula_key in applicable
        is_valuation = formula.formula_key in _VALUATION_FORMULA_KEYS
        # A concept `standard` has no verified field mapping for is the SAME
        # gap for every period of this ticker (resolve_concept doesn't vary
        # by period) -- checked once per formula rather than per cell.
        # MARKET_CAP/PRICE are excluded here (Phase 3): they resolve via the
        # live-price stack, never via resolve_concept, so their permanent
        # per-standard "no mapping" is not a MISSING_INPUT_MAPPING gap.
        unmapped_concepts = [
            c
            for c in formula.required_inputs
            if c not in _LIVE_PRICE_CONCEPTS and resolve_concept(standard, c) is None
        ]
        cells = annual_cells if formula.period_grid == "fy" else quarterly_cells
        for idx, cell in enumerate(cells):
            if is_valuation and idx != len(cells) - 1:
                # Spot-only: never attempted for a historical quarter -- not
                # even a not_computable row, since a spot valuation was never
                # tried for that past date (see _VALUATION_FORMULA_KEYS'
                # module-level docstring).
                continue
            fiscal_period_type = "TTM" if formula.period_grid == "ttm" else cell.fiscal_period_type

            if not is_applicable:
                result: ComputedValue | NotComputable = NotComputable(
                    reason_code=ReasonCode.NOT_APPLICABLE_BUSINESS_MODEL,
                    reason_detail=f"{business_model.value} excluded from {formula.formula_key}",
                )
                lineage: list[LineageEntry] = []
            elif unmapped_concepts:
                result = NotComputable(
                    reason_code=ReasonCode.MISSING_INPUT_MAPPING,
                    reason_detail=(
                        f"{standard.value} has no verified field mapping for "
                        f"{', '.join(c.value for c in unmapped_concepts)}"
                    ),
                )
                lineage = []
            else:
                resolved, prior, lineage = _resolve_inputs(cells, idx, formula)
                if is_valuation:
                    for concept, value in spot_values.items():
                        if concept in formula.required_inputs or concept in formula.optional_inputs:
                            resolved[concept] = value
                            if price_asof is not None:
                                lineage.append((concept, price_asof, None))
                result = compute(formula, resolved, prior_inputs=prior)
                if is_valuation and isinstance(result, ComputedValue) and price_source is not None:
                    result = ComputedValue(
                        value=result.value,
                        method_flags=(*result.method_flags, f"price_source_{price_source}"),
                    )

            fingerprint = _input_fingerprint(lineage)
            if not force:
                existing = _existing_attempt(
                    conn, ticker, cell.period_end, fiscal_period_type, formula_id
                )
                if existing is not None and existing == (fingerprint, ENGINE_VERSION):
                    skipped += 1
                    continue

            _persist_attempt(
                conn,
                ticker=ticker,
                period_end=cell.period_end,
                fiscal_period_type=fiscal_period_type,
                formula=formula,
                formula_id=formula_id,
                result=result,
                lineage=lineage,
            )
            attempts += 1
            if isinstance(result, ComputedValue):
                computed_ok += 1
            else:
                not_computable += 1

    conn.commit()
    return TickerComputeSummary(
        ticker=ticker,
        business_model=business_model,
        accounting_standard=standard,
        quarters_seen=len(quarterly_cells),
        attempts_written=attempts,
        computed_ok=computed_ok,
        not_computable=not_computable,
        skipped_unchanged=skipped,
    )


def latest_ttm_value(conn: sqlite3.Connection, ticker: str, formula_key: str) -> Decimal | None:
    """Most recent kpi_facts value for `formula_key` at fiscal_period_type='TTM'
    (or the latest quarterly value when the formula's grid is quarterly) --
    used by the parity harness to fetch the computed side of a comparison."""
    row = conn.execute(
        """
        SELECT kf.value FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ? AND kd.name = ?
        ORDER BY kf.period_end DESC
        LIMIT 1
        """,
        (ticker.upper(), formula_key),
    ).fetchone()
    if row is None:
        return None
    return Decimal(str(row["value"]))
