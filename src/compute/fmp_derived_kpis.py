"""Derive standard KPIs from FMP `financial_facts` for every tracked ticker.

The IR-doc KPI extraction pipeline ingests issuer-published metrics one PDF at
a time. This module fills the long tail by computing universally-meaningful KPIs
from FMP fundamentals we already have on disk:

  - Operating Margin (GAAP)         = operating_income / revenue * 100
  - Net Income Margin (GAAP)        = net_income / revenue * 100
  - Gross Margin (GAAP)             = gross_profit / revenue * 100
  - Revenue YoY Growth (USD)        = (rev_t - rev_{t-4Q}) / rev_{t-4Q} * 100

All values are in PERCENT. Provenance: each derived kpi_fact references the
source FMP `fmp_income_statement` document whose file_path ends with
`_quarterly.json` — i.e., standalone-quarter values, never TTM or YTD.

The four metric names are registered as kpi_definitions on first use so
downstream break_rules can reference them by string match.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from models.documents import SourceType
from models.facts import FiscalPeriodType, Unit
from pipeline.kpi_persistence import find_or_create_kpi_definition

# Canonical KPI names registered with kpi_definitions on first emission.
KPI_OPERATING_MARGIN_GAAP = "Operating Margin (GAAP)"
KPI_NET_MARGIN_GAAP = "Net Income Margin (GAAP)"
KPI_GROSS_MARGIN_GAAP = "Gross Margin (GAAP)"
KPI_REVENUE_YOY_USD = "Revenue YoY Growth (USD)"
KPI_CAPEX_REVENUE_RATIO = "Capex / Revenue (GAAP)"
KPI_FCF_MARGIN_GAAP = "FCF Margin (GAAP)"
KPI_OCF_YOY_USD = "Operating Cash Flow YoY Growth (USD)"

_DERIVED_KPI_NAMES: tuple[str, ...] = (
    KPI_OPERATING_MARGIN_GAAP,
    KPI_NET_MARGIN_GAAP,
    KPI_GROSS_MARGIN_GAAP,
    KPI_REVENUE_YOY_USD,
    KPI_CAPEX_REVENUE_RATIO,
    KPI_FCF_MARGIN_GAAP,
    KPI_OCF_YOY_USD,
)

# Income-statement line items needed for margin + revenue YoY derivations.
_REQUIRED_LINE_ITEMS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "net_income",
    "gross_profit",
)

# Cash-flow / capex line items needed for the new derivers. Optional — if any
# are missing for a quarter we just skip that derivation rather than the row.
_OPTIONAL_LINE_ITEMS: tuple[str, ...] = (
    "capital_expenditure",
    "free_cash_flow",
    "operating_cash_flow",
)


@dataclass(frozen=True)
class QuarterlyFacts:
    """All four required line_items for one (ticker, period_end, fiscal_period_type).

    Optional cash-flow fields (capital_expenditure / free_cash_flow / operating_cash_flow)
    are None when not disclosed for that quarter — derivers that need them skip
    silently rather than failing the whole row.
    """

    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    revenue: Decimal
    operating_income: Decimal
    net_income: Decimal
    gross_profit: Decimal
    source_doc_id: int
    capital_expenditure: Decimal | None = None
    free_cash_flow: Decimal | None = None
    operating_cash_flow: Decimal | None = None


def _fetch_quarterly_facts(
    conn: sqlite3.Connection, ticker: str
) -> list[QuarterlyFacts]:
    """Pull standalone-quarter fundamentals for a ticker.

    Accepts rows from any `fmp_income_statement` / `fmp_cashflow` document
    EXCEPT the TTM / FY rollups (`%_ttm.json`, `%_annual.json`). The
    `fiscal_period_type IN ('Q1'..'Q4')` filter is the real safety net — it
    rejects any non-quarterly leakage regardless of file naming.

    Why the file-path filter widened: the legacy v3 statements endpoint
    wrote `{TICKER}_income_statement_quarterly.json` and we filtered to
    that. After v3 started returning 403 in May 2026, refreshes moved to
    /stable, which writes the unsuffixed `{TICKER}_income_statement.json`.
    The `grouped` dict below dedupes by (period_end, fiscal_period_type)
    so feeding rows from both sources is safe.
    """
    all_line_items = _REQUIRED_LINE_ITEMS + _OPTIONAL_LINE_ITEMS
    placeholders = ",".join("?" * len(all_line_items))
    cur = conn.execute(
        f"""
        SELECT ff.period_end, ff.fiscal_period_type, ff.line_item, ff.value, ff.source_doc_id
        FROM financial_facts ff
        JOIN documents d ON d.id = ff.source_doc_id
        WHERE ff.ticker = ?
          AND d.doc_type IN ('fmp_income_statement', 'fmp_cashflow')
          AND d.file_path NOT LIKE '%_ttm.json'
          AND d.file_path NOT LIKE '%_annual.json'
          AND ff.line_item IN ({placeholders})
          AND ff.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
        ORDER BY ff.period_end ASC
        """,
        (ticker.upper(), *all_line_items),
    )
    grouped: dict[tuple[datetime, str], dict[str, object]] = {}
    for row in cur.fetchall():
        pe = row["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        key = (pe, row["fiscal_period_type"])
        bucket = grouped.setdefault(key, {"_source_doc_id": int(row["source_doc_id"])})
        bucket[row["line_item"]] = Decimal(str(row["value"]))

    results: list[QuarterlyFacts] = []
    for (pe, fpt), bucket in sorted(grouped.items(), key=lambda kv: kv[0][0]):
        if not all(li in bucket for li in _REQUIRED_LINE_ITEMS):
            continue
        rev = bucket["revenue"]
        if not isinstance(rev, Decimal) or rev == 0:
            continue
        ocf = bucket.get("operating_cash_flow")
        capex = bucket.get("capital_expenditure")
        fcf = bucket.get("free_cash_flow")
        # FMP coverage-gap signature: when OCF, Capex, AND FCF are all
        # simultaneously exactly zero in a quarter with non-zero revenue, the
        # upstream parser failed (NTDOY's JP filing format and HDB's IN filing
        # format are the known cases). Treat all three as missing so downstream
        # derivers skip them rather than emitting 0% FCF Margin and -100% OCF YoY.
        # A real operating business with revenue cannot simultaneously have all
        # three cash-flow line items at exactly zero — at minimum working capital
        # moves something.
        if ocf == Decimal(0) and capex == Decimal(0) and fcf == Decimal(0):
            ocf = capex = fcf = None
        results.append(
            QuarterlyFacts(
                ticker=ticker.upper(),
                period_end=pe,
                fiscal_period_type=FiscalPeriodType(fpt),
                revenue=rev,
                operating_income=bucket["operating_income"],
                net_income=bucket["net_income"],
                gross_profit=bucket["gross_profit"],
                source_doc_id=int(bucket["_source_doc_id"]),
                capital_expenditure=capex,  # type: ignore[arg-type]
                free_cash_flow=fcf,  # type: ignore[arg-type]
                operating_cash_flow=ocf,  # type: ignore[arg-type]
            )
        )
    return results


@dataclass(frozen=True)
class DerivedKpiRow:
    """One derived KPI fact ready for kpi_facts insertion."""

    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    name: str
    value: Decimal
    unit: Unit
    source_doc_id: int


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Return numerator / denominator * 100 as a Decimal (safe for zero-revenue guard already applied)."""
    return (numerator / denominator) * Decimal(100)


def derive_for_facts(facts: list[QuarterlyFacts]) -> list[DerivedKpiRow]:
    """Compute derived KPIs across the time series.

    Margins are point-in-time per quarter. Revenue YoY needs the same fiscal_period_type
    from one year earlier; we look it up via a (ticker, fiscal_period_type) -> [list]
    map then index by period_end.
    """
    by_quarter_label: dict[FiscalPeriodType, list[QuarterlyFacts]] = {}
    for f in facts:
        by_quarter_label.setdefault(f.fiscal_period_type, []).append(f)
    for series in by_quarter_label.values():
        series.sort(key=lambda f: f.period_end)

    out: list[DerivedKpiRow] = []
    for f in facts:
        out.append(
            DerivedKpiRow(
                period_end=f.period_end,
                fiscal_period_type=f.fiscal_period_type,
                name=KPI_OPERATING_MARGIN_GAAP,
                value=_pct(f.operating_income, f.revenue),
                unit=Unit.PERCENT,
                source_doc_id=f.source_doc_id,
            )
        )
        out.append(
            DerivedKpiRow(
                period_end=f.period_end,
                fiscal_period_type=f.fiscal_period_type,
                name=KPI_NET_MARGIN_GAAP,
                value=_pct(f.net_income, f.revenue),
                unit=Unit.PERCENT,
                source_doc_id=f.source_doc_id,
            )
        )
        out.append(
            DerivedKpiRow(
                period_end=f.period_end,
                fiscal_period_type=f.fiscal_period_type,
                name=KPI_GROSS_MARGIN_GAAP,
                value=_pct(f.gross_profit, f.revenue),
                unit=Unit.PERCENT,
                source_doc_id=f.source_doc_id,
            )
        )
        # Capex / Revenue — capex stored as a negative cash-flow figure;
        # take abs() so the ratio reads as a positive intensity %.
        if f.capital_expenditure is not None:
            out.append(
                DerivedKpiRow(
                    period_end=f.period_end,
                    fiscal_period_type=f.fiscal_period_type,
                    name=KPI_CAPEX_REVENUE_RATIO,
                    value=_pct(abs(f.capital_expenditure), f.revenue),
                    unit=Unit.PERCENT,
                    source_doc_id=f.source_doc_id,
                )
            )
        if f.free_cash_flow is not None:
            out.append(
                DerivedKpiRow(
                    period_end=f.period_end,
                    fiscal_period_type=f.fiscal_period_type,
                    name=KPI_FCF_MARGIN_GAAP,
                    value=_pct(f.free_cash_flow, f.revenue),
                    unit=Unit.PERCENT,
                    source_doc_id=f.source_doc_id,
                )
            )

    for series in by_quarter_label.values():
        for i, f in enumerate(series):
            if i == 0:
                continue
            prior = series[i - 1]
            year_diff = f.period_end.year - prior.period_end.year
            if year_diff != 1:
                continue
            if prior.revenue == 0:
                continue
            yoy = (f.revenue - prior.revenue) / prior.revenue * Decimal(100)
            out.append(
                DerivedKpiRow(
                    period_end=f.period_end,
                    fiscal_period_type=f.fiscal_period_type,
                    name=KPI_REVENUE_YOY_USD,
                    value=yoy,
                    unit=Unit.PERCENT,
                    source_doc_id=f.source_doc_id,
                )
            )
            if (
                f.operating_cash_flow is not None
                and prior.operating_cash_flow is not None
                and prior.operating_cash_flow != 0
            ):
                ocf_yoy = (
                    (f.operating_cash_flow - prior.operating_cash_flow)
                    / prior.operating_cash_flow
                    * Decimal(100)
                )
                out.append(
                    DerivedKpiRow(
                        period_end=f.period_end,
                        fiscal_period_type=f.fiscal_period_type,
                        name=KPI_OCF_YOY_USD,
                        value=ocf_yoy,
                        unit=Unit.PERCENT,
                        source_doc_id=f.source_doc_id,
                    )
                )
    return out


def persist_derived_kpis(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    rows: list[DerivedKpiRow],
) -> int:
    """Insert kpi_facts rows for the derived metrics. Returns count actually inserted."""
    inserted = 0
    for row in rows:
        kpi_def_id = find_or_create_kpi_definition(
            conn,
            ticker=ticker,
            name=row.name,
            unit=row.unit,
            primary_source=SourceType.FMP,
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, "
            " value, unit, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                row.period_end,
                row.fiscal_period_type.value,
                kpi_def_id,
                str(row.value),
                row.unit.value,
                row.source_doc_id,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted


def derive_for_ticker(conn: sqlite3.Connection, ticker: str) -> tuple[int, int]:
    """End-to-end: fetch quarterly facts, derive KPIs, persist. Returns (rows_emitted, rows_inserted)."""
    facts = _fetch_quarterly_facts(conn, ticker)
    if not facts:
        return (0, 0)
    rows = derive_for_facts(facts)
    inserted = persist_derived_kpis(conn, ticker=ticker, rows=rows)
    return (len(rows), inserted)
