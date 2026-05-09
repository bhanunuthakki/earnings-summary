"""DCF valuation — discount projected free cash flows to present value.

Two-stage Gordon Growth model:
1. Explicit projection over `horizon_years`, applying per-year revenue growth
   and a constant FCF margin to derive projected FCFs.
2. Terminal value via Gordon Growth at year (horizon + 1), discounted back.

Inputs are user-supplied (revenue growths, FCF margin, WACC, terminal growth).
Base revenue and shares-outstanding proxy come from financial_facts. Each run
writes a dcf_runs row for audit; the row's `notes` field can record any
analyst comments.

The shares-outstanding figure here uses `weighted_avg_shares_diluted` from
the most recent income statement as a proxy. That's an average over the
period, not point-in-time — adequate for first-cut valuations but not exact.
Future refinement: pull point-in-time shares from FMP profile or shares_float.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DcfInputs:
    """All assumptions needed for one DCF run.

    `wacc` and `terminal_growth` are always annualized (so 0.09 = 9% / yr).
    `revenue_growths` are per-period growth rates whose granularity matches
    `periods_per_year`: with periods_per_year=1 the list is annual growths;
    with periods_per_year=4 it's quarterly growths. The math in compute_dcf
    converts annualized WACC and terminal growth to per-period internally.
    """

    base_revenue: float
    revenue_growths: list[float]
    fcf_margin: float
    wacc: float
    terminal_growth: float
    shares_outstanding: float | None = None
    periods_per_year: int = 1


@dataclass(frozen=True)
class DcfResult:
    """Outputs of one DCF run."""

    projected_fcfs: list[float]
    sum_of_discounted_fcfs: float
    terminal_value: float
    discounted_terminal: float
    npv: float
    npv_per_share: float | None


def compute_dcf(inputs: DcfInputs) -> DcfResult:
    """Two-stage DCF: explicit projection + Gordon Growth terminal.

    Generalized to any periodicity via `periods_per_year`. Annualized WACC
    and terminal_growth are converted to per-period rates internally
    (per_period = (1 + annual)^(1/p) - 1). With periods_per_year=4, a
    40-element revenue_growths list yields a 10-year quarterly DCF.
    """
    if not inputs.revenue_growths:
        raise ValueError("revenue_growths must be non-empty")
    if inputs.periods_per_year < 1:
        raise ValueError(f"periods_per_year must be >= 1, got {inputs.periods_per_year}")
    if inputs.wacc <= inputs.terminal_growth:
        raise ValueError(
            f"wacc ({inputs.wacc}) must exceed terminal_growth "
            f"({inputs.terminal_growth}) for Gordon Growth"
        )
    if inputs.fcf_margin < -1 or inputs.fcf_margin > 1:
        raise ValueError(f"fcf_margin {inputs.fcf_margin} outside [-1, 1]")

    p = inputs.periods_per_year
    per_period_wacc = (1.0 + inputs.wacc) ** (1.0 / p) - 1.0
    per_period_terminal = (1.0 + inputs.terminal_growth) ** (1.0 / p) - 1.0

    rev = inputs.base_revenue
    projected_fcfs: list[float] = []
    sum_dcf = 0.0
    for i, growth in enumerate(inputs.revenue_growths, start=1):
        rev = rev * (1.0 + growth)
        fcf = rev * inputs.fcf_margin
        projected_fcfs.append(fcf)
        sum_dcf += fcf / (1.0 + per_period_wacc) ** i

    terminal_fcf = projected_fcfs[-1] * (1.0 + per_period_terminal)
    terminal_value = terminal_fcf / (per_period_wacc - per_period_terminal)
    discounted_terminal = terminal_value / (1.0 + per_period_wacc) ** len(inputs.revenue_growths)

    npv = sum_dcf + discounted_terminal
    npv_per_share = (
        npv / inputs.shares_outstanding
        if inputs.shares_outstanding and inputs.shares_outstanding > 0
        else None
    )

    return DcfResult(
        projected_fcfs=projected_fcfs,
        sum_of_discounted_fcfs=sum_dcf,
        terminal_value=terminal_value,
        discounted_terminal=discounted_terminal,
        npv=npv,
        npv_per_share=npv_per_share,
    )


def _latest_fact(conn: sqlite3.Connection, ticker: str, line_item: str) -> float | None:
    """Return the latest annual financial_facts.value for (ticker, line_item)."""
    cur = conn.execute(
        "SELECT value FROM financial_facts "
        "WHERE ticker = ? AND line_item = ? AND fiscal_period_type = 'FY' "
        "ORDER BY period_end DESC LIMIT 1",
        (ticker.upper(), line_item),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def fetch_dcf_base_inputs(
    conn: sqlite3.Connection, ticker: str
) -> tuple[float | None, float | None]:
    """Read latest annual revenue and weighted_avg_shares_diluted for ticker."""
    revenue = _latest_fact(conn, ticker, "revenue")
    shares = _latest_fact(conn, ticker, "weighted_avg_shares_diluted")
    return revenue, shares


@dataclass(frozen=True)
class DcfComponent:
    """One sub-DCF inside an aggregate run: a segment, or the unallocated overhead.

    `component_type` distinguishes segment-level cash flows from the residual
    corporate/overhead portion that's not attributed to any operating segment.
    The aggregate `npv` of a `dcf_runs` row equals the sum of its component NPVs.
    """

    component_name: str
    component_type: str
    inputs: DcfInputs
    result: DcfResult


def _component_to_dict(c: DcfComponent) -> dict[str, object]:
    return {
        "component_name": c.component_name,
        "component_type": c.component_type,
        "base_revenue": c.inputs.base_revenue,
        "revenue_growths": c.inputs.revenue_growths,
        "fcf_margin": c.inputs.fcf_margin,
        "wacc": c.inputs.wacc,
        "terminal_growth": c.inputs.terminal_growth,
        "periods_per_year": c.inputs.periods_per_year,
        "npv": c.result.npv,
        "npv_per_share": c.result.npv_per_share,
    }


def persist_dcf_run(
    conn: sqlite3.Connection,
    ticker: str,
    inputs: DcfInputs,
    result: DcfResult,
    notes: str | None = None,
    run_id: str | None = None,
    breakdown: list[DcfComponent] | None = None,
) -> int:
    """Upsert the single dcf_runs row for `ticker`. Returns row id.

    Schema invariant (since migration 0018): one row per ticker. The aggregate
    `npv` and `npv_per_share` equal the sum of `breakdown` component NPVs when
    a breakdown is supplied; for a pure consolidated run, breakdown is None.
    """
    breakdown_json = (
        json.dumps([_component_to_dict(c) for c in breakdown]) if breakdown else None
    )
    cur = conn.execute(
        "INSERT INTO dcf_runs ("
        "ticker, valuation_date, horizon_years, base_revenue, "
        "revenue_growths_json, fcf_margin, wacc, terminal_growth, "
        "npv, npv_per_share, shares_outstanding, currency, notes, run_id, "
        "segment_name, breakdown_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "  valuation_date = excluded.valuation_date, "
        "  horizon_years = excluded.horizon_years, "
        "  base_revenue = excluded.base_revenue, "
        "  revenue_growths_json = excluded.revenue_growths_json, "
        "  fcf_margin = excluded.fcf_margin, "
        "  wacc = excluded.wacc, "
        "  terminal_growth = excluded.terminal_growth, "
        "  npv = excluded.npv, "
        "  npv_per_share = excluded.npv_per_share, "
        "  shares_outstanding = excluded.shares_outstanding, "
        "  notes = excluded.notes, "
        "  run_id = excluded.run_id, "
        "  segment_name = NULL, "
        "  breakdown_json = excluded.breakdown_json",
        (
            ticker.upper(),
            datetime.now().date().isoformat(),
            len(inputs.revenue_growths),
            str(inputs.base_revenue),
            json.dumps(inputs.revenue_growths),
            inputs.fcf_margin,
            inputs.wacc,
            inputs.terminal_growth,
            str(result.npv),
            str(result.npv_per_share) if result.npv_per_share is not None else None,
            str(inputs.shares_outstanding) if inputs.shares_outstanding is not None else None,
            None,
            notes,
            run_id,
            breakdown_json,
        ),
    )
    conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    cur = conn.execute("SELECT id FROM dcf_runs WHERE ticker = ?", (ticker.upper(),))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def run_dcf_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    revenue_growths: list[float],
    fcf_margin: float,
    wacc: float,
    terminal_growth: float,
    notes: str | None = None,
    run_id: str | None = None,
) -> tuple[DcfInputs, DcfResult, int]:
    """End-to-end: fetch base inputs, compute consolidated DCF, persist."""
    base_revenue, shares = fetch_dcf_base_inputs(conn, ticker)
    if base_revenue is None:
        raise ValueError(f"No FY revenue facts found for {ticker!r}")

    inputs = DcfInputs(
        base_revenue=base_revenue,
        revenue_growths=revenue_growths,
        fcf_margin=fcf_margin,
        wacc=wacc,
        terminal_growth=terminal_growth,
        shares_outstanding=shares,
    )
    result = compute_dcf(inputs)
    row_id = persist_dcf_run(conn, ticker, inputs, result, notes=notes, run_id=run_id)
    return (inputs, result, row_id)


def fetch_segment_base_revenues(
    conn: sqlite3.Connection,
    ticker: str,
    metric: str = "revenue_by_product",
) -> dict[str, float]:
    """Latest annual segment revenue per segment_name from segment_facts.

    Picks the most recent FY period_end and returns one entry per segment.
    Default metric is the FMP product-segmentation endpoint output.
    """
    cur = conn.execute(
        "SELECT MAX(period_end) FROM segment_facts "
        "WHERE ticker = ? AND metric = ? AND fiscal_period_type = 'FY'",
        (ticker.upper(), metric),
    )
    row = cur.fetchone()
    latest = row[0] if row else None
    if latest is None:
        return {}
    cur = conn.execute(
        "SELECT segment_name, value FROM segment_facts "
        "WHERE ticker = ? AND metric = ? AND fiscal_period_type = 'FY' AND period_end = ?",
        (ticker.upper(), metric, latest),
    )
    return {r[0]: float(r[1]) for r in cur.fetchall()}


@dataclass(frozen=True)
class SegmentDcfRow:
    """One segment's DCF inside a multi-segment aggregate run.

    `row_id` references the single aggregate `dcf_runs` row that holds this
    segment within its `breakdown_json`. Multiple SegmentDcfRow values for the
    same ticker share the same `row_id` — one row per ticker, post-0018.
    """

    segment_name: str
    inputs: DcfInputs
    result: DcfResult
    row_id: int


@dataclass(frozen=True)
class AggregateDcfRow:
    """The single dcf_runs row for a ticker, with its full per-component breakdown."""

    ticker: str
    row_id: int
    aggregate_npv: float
    aggregate_npv_per_share: float | None
    components: list[DcfComponent]


def _aggregate_inputs_for_persist(
    components: list[DcfComponent], shares_outstanding: float | None
) -> DcfInputs:
    """Synthesize the top-level DcfInputs columns for a multi-component run.

    The top-level columns on `dcf_runs` (base_revenue, revenue_growths_json,
    fcf_margin, wacc, terminal_growth) carry no meaning when `breakdown_json`
    is set — readers should consult the breakdown. We populate them with the
    first component's inputs as a placeholder so downstream legacy reads
    don't choke on NULLs.
    """
    if not components:
        raise ValueError("Cannot synthesize aggregate inputs from empty component list")
    first = components[0].inputs
    return DcfInputs(
        base_revenue=first.base_revenue,
        revenue_growths=first.revenue_growths,
        fcf_margin=first.fcf_margin,
        wacc=first.wacc,
        terminal_growth=first.terminal_growth,
        shares_outstanding=shares_outstanding,
        periods_per_year=first.periods_per_year,
    )


def _aggregate_result(
    components: list[DcfComponent], shares_outstanding: float | None
) -> DcfResult:
    """Aggregate NPV = sum of component NPVs. Per-share scaled at the aggregate level."""
    aggregate_npv = sum(c.result.npv for c in components)
    npv_per_share = (
        aggregate_npv / shares_outstanding
        if shares_outstanding and shares_outstanding > 0
        else None
    )
    return DcfResult(
        projected_fcfs=[],
        sum_of_discounted_fcfs=0.0,
        terminal_value=0.0,
        discounted_terminal=0.0,
        npv=aggregate_npv,
        npv_per_share=npv_per_share,
    )


def persist_aggregate_dcf(
    conn: sqlite3.Connection,
    ticker: str,
    components: list[DcfComponent],
    shares_outstanding: float | None,
    notes: str | None = None,
    run_id: str | None = None,
) -> AggregateDcfRow:
    """Persist one dcf_runs row whose breakdown_json holds `components`.

    Aggregate NPV is the sum of component NPVs, by construction. Use this for
    segment + overhead style runs; for a pure consolidated DCF, call
    `persist_dcf_run` directly with `breakdown=None`.
    """
    if not components:
        raise ValueError(f"Cannot persist aggregate dcf for {ticker!r}: no components")
    inputs = _aggregate_inputs_for_persist(components, shares_outstanding)
    result = _aggregate_result(components, shares_outstanding)
    row_id = persist_dcf_run(
        conn,
        ticker,
        inputs,
        result,
        notes=notes,
        run_id=run_id,
        breakdown=components,
    )
    return AggregateDcfRow(
        ticker=ticker.upper(),
        row_id=row_id,
        aggregate_npv=result.npv,
        aggregate_npv_per_share=result.npv_per_share,
        components=components,
    )


def _filter_outliers(values: list[float], threshold_x: float = 5.0) -> list[float]:
    """Drop values more than `threshold_x` × the median of the sample.

    Guards against FMP data quality issues (e.g. an erroneous Q1 entry that's
    40x other quarters' revenue). With sample size <4, returns input unchanged.
    """
    if len(values) < 4:
        return values
    sample = sorted(values[:8])
    median = sample[len(sample) // 2]
    if median <= 0:
        return values
    threshold = threshold_x * median
    return [v for v in values if v <= threshold]


def fetch_segment_quarterly_per_period_base(
    conn: sqlite3.Connection,
    ticker: str,
    metric: str = "revenue_by_product",
) -> dict[str, float]:
    """Return per-quarter base revenue per segment_name (= TTM / 4).

    Trailing-twelve-months smooths seasonality; dividing by 4 gives the
    per-period (per-quarter) base that compute_dcf expects when called with
    periods_per_year=4. Outlier values >5× sample median are dropped (FMP
    occasionally returns a single anomalous quarter, e.g. cumulative-rolled-
    up data tagged as a single quarter).

    Falls back to the latest FY fact / 4 for segments without quarterly data.
    """
    cur = conn.execute(
        "SELECT segment_name, period_end, value FROM segment_facts "
        "WHERE ticker = ? AND metric = ? AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
        "ORDER BY segment_name, period_end DESC",
        (ticker.upper(), metric),
    )
    by_segment: dict[str, list[float]] = {}
    for r in cur.fetchall():
        by_segment.setdefault(r[0], []).append(float(r[2]))

    out: dict[str, float] = {}
    for segment, vals in by_segment.items():
        cleaned = _filter_outliers(vals)
        if len(cleaned) >= 4:
            ttm = sum(cleaned[:4])
            out[segment] = ttm / 4.0
    if out:
        return out
    annual = fetch_segment_base_revenues(conn, ticker, metric=metric)
    return {k: v / 4.0 for k, v in annual.items()}


def _build_segment_components(
    segment_inputs: dict[str, DcfInputs],
) -> list[DcfComponent]:
    """Compute compute_dcf for each segment-keyed inputs dict; return components."""
    out: list[DcfComponent] = []
    for segment_name, inputs in segment_inputs.items():
        result = compute_dcf(inputs)
        out.append(DcfComponent(segment_name, "segment", inputs, result))
    return out


def _build_overhead_component(
    overhead_inputs: DcfInputs | None,
) -> DcfComponent | None:
    """If overhead inputs supplied, compute and wrap as a component."""
    if overhead_inputs is None:
        return None
    return DcfComponent("overhead", "overhead", overhead_inputs, compute_dcf(overhead_inputs))


def run_quarterly_segment_dcf_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    segment_quarterly_growths: dict[str, list[float]],
    segment_fcf_margins: dict[str, float],
    wacc: float,
    terminal_growth: float,
    notes: str | None = None,
    run_id: str | None = None,
    metric: str = "revenue_by_product",
    overhead_inputs: DcfInputs | None = None,
) -> AggregateDcfRow:
    """Per-segment quarterly DCFs persisted as one aggregate row per ticker.

    Base revenue per segment = TTM (sum of last 4 quarters) from segment_facts.
    Growth lists are per-quarter (typically 40 entries for a 10-year horizon).
    WACC and terminal_growth remain annualized; compute_dcf converts internally.
    `overhead_inputs`, when supplied, is computed and appended as the trailing
    'overhead' component so the aggregate row's NPV equals
    `sum(segment NPVs) + overhead NPV`.
    """
    base_revenues = fetch_segment_quarterly_per_period_base(conn, ticker, metric=metric)
    if not base_revenues:
        raise ValueError(f"No quarterly segment revenue facts (metric={metric!r}) for {ticker!r}")

    segment_inputs: dict[str, DcfInputs] = {}
    for segment_name, base_revenue in base_revenues.items():
        if segment_name not in segment_quarterly_growths or segment_name not in segment_fcf_margins:
            continue
        segment_inputs[segment_name] = DcfInputs(
            base_revenue=base_revenue,
            revenue_growths=segment_quarterly_growths[segment_name],
            fcf_margin=segment_fcf_margins[segment_name],
            wacc=wacc,
            terminal_growth=terminal_growth,
            shares_outstanding=None,
            periods_per_year=4,
        )
    if not segment_inputs:
        raise ValueError(f"No segments matched assumptions for {ticker!r}")

    components = _build_segment_components(segment_inputs)
    overhead = _build_overhead_component(overhead_inputs)
    if overhead is not None:
        components.append(overhead)

    _, shares = fetch_dcf_base_inputs(conn, ticker)
    return persist_aggregate_dcf(
        conn,
        ticker,
        components,
        shares_outstanding=shares,
        notes=notes,
        run_id=run_id,
    )


def run_segment_dcf_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    segment_growths: dict[str, list[float]],
    segment_fcf_margins: dict[str, float],
    wacc: float,
    terminal_growth: float,
    notes: str | None = None,
    run_id: str | None = None,
    metric: str = "revenue_by_product",
    overhead_inputs: DcfInputs | None = None,
) -> AggregateDcfRow:
    """Annual per-segment DCFs persisted as one aggregate row per ticker.

    Segments not present in `segment_growths` or `segment_fcf_margins` are
    skipped. The persisted row's `breakdown_json` lists every segment plus an
    optional 'overhead' component supplied by the caller; the aggregate `npv`
    equals their sum.
    """
    base_revenues = fetch_segment_base_revenues(conn, ticker, metric=metric)
    if not base_revenues:
        raise ValueError(
            f"No segment revenue facts (metric={metric!r}, FY) for {ticker!r}; "
            f"run extract_facts on fmp_segment_product first"
        )

    segment_inputs: dict[str, DcfInputs] = {}
    for segment_name, base_revenue in base_revenues.items():
        if segment_name not in segment_growths or segment_name not in segment_fcf_margins:
            continue
        segment_inputs[segment_name] = DcfInputs(
            base_revenue=base_revenue,
            revenue_growths=segment_growths[segment_name],
            fcf_margin=segment_fcf_margins[segment_name],
            wacc=wacc,
            terminal_growth=terminal_growth,
            shares_outstanding=None,
        )
    if not segment_inputs:
        raise ValueError(f"No segments matched assumptions for {ticker!r}")

    components = _build_segment_components(segment_inputs)
    overhead = _build_overhead_component(overhead_inputs)
    if overhead is not None:
        components.append(overhead)

    _, shares = fetch_dcf_base_inputs(conn, ticker)
    return persist_aggregate_dcf(
        conn,
        ticker,
        components,
        shares_outstanding=shares,
        notes=notes,
        run_id=run_id,
    )
