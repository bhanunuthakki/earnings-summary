"""Derive standard KPIs from FMP `financial_facts` (and transforms over kpi_facts).

The IR-doc KPI extraction pipeline ingests issuer-published metrics one PDF at
a time. This module fills the long tail by computing two categories of derived
KPI and persisting both into `kpi_facts`:

1. Margins & growth from FMP fundamentals (`financial_facts`):
  - Operating Margin (GAAP)         = operating_income / revenue * 100
  - Net Income Margin (GAAP)        = net_income / revenue * 100
  - Gross Margin (GAAP)             = gross_profit / revenue * 100
  - Revenue YoY Growth (USD)        = (rev_t - rev_{t-4Q}) / rev_{t-4Q} * 100
  All in PERCENT, provenance-tagged to the source `fmp_income_statement`
  document whose file_path ends `_quarterly.json` (standalone quarter, never
  TTM / YTD).

2. Same-fiscal-quarter YoY *transforms* over an existing kpi_facts LEVEL series
  (`derive_kpi_transforms`). A thesis break rule may reference the YoY *change*
  of a level series the pipeline only stores as a level — e.g. NU's rules want
  "Risk-adjusted NIM YoY change (bps)" but only "Risk-adjusted NIM (...)" is
  extracted. These derivers read the resolved base series from kpi_facts,
  compute the transform against the same fiscal quarter one year prior, and
  register the canonical derived name so the break-rule resolver hits a real
  series instead of evaluating against nothing. The base must be a PERCENT-unit
  series (the transforms assume a percentage scale); non-percent rows are
  skipped. A spec whose base does not resolve for a ticker simply emits nothing.

Every derived name is registered as a kpi_definition on first emission so
downstream break_rules can reference it by string match. `derive_for_ticker`
runs category 1 first (persisting Revenue YoY Growth), then category 2 (which
can read that freshly-persisted series as a base).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

from compute.kpi_resolver import resolve_kpi_definition_name
from credibility.observations import KPI_FACTS, record_restatement_observation
from models.documents import SourceQualityTier, SourceType
from models.facts import FiscalPeriodType, Unit
from pipeline.confidence import score_confidence
from pipeline.kpi_persistence import find_or_create_kpi_definition
from pipeline.restatement_detector import insert_kpi_with_restatement_detection
from provenance.overrides import KPI as OVERRIDE_KPI
from provenance.overrides import OverrideAction, active_scalar_override_map
from timeseries.loaders import reader_tier_rank_sql

log = logging.getLogger(__name__)

# Canonical KPI names registered with kpi_definitions on first emission.
KPI_OPERATING_MARGIN_GAAP = "Operating Margin (GAAP)"
KPI_NET_MARGIN_GAAP = "Net Income Margin (GAAP)"
KPI_GROSS_MARGIN_GAAP = "Gross Margin (GAAP)"
KPI_REVENUE_YOY_USD = "Revenue YoY Growth (USD)"
KPI_CAPEX_REVENUE_RATIO = "Capex / Revenue (GAAP)"
KPI_FCF_MARGIN_GAAP = "FCF Margin (GAAP)"
KPI_OCF_YOY_USD = "Operating Cash Flow YoY Growth (USD)"
KPI_ROE = "ROE"

_DERIVED_KPI_NAMES: tuple[str, ...] = (
    KPI_OPERATING_MARGIN_GAAP,
    KPI_NET_MARGIN_GAAP,
    KPI_GROSS_MARGIN_GAAP,
    KPI_REVENUE_YOY_USD,
    KPI_CAPEX_REVENUE_RATIO,
    KPI_FCF_MARGIN_GAAP,
    KPI_OCF_YOY_USD,
    KPI_ROE,
)

# Transform-derived KPIs (category 2): same-fiscal-quarter YoY transforms over an
# existing kpi_facts LEVEL series (see `derive_kpi_transforms` + `_TRANSFORM_DERIVER_SPECS`).
# Names match the `kpi_name` a holdings break_rule references verbatim, so the rule
# resolver hits the materialized series. Registered as kpi_definitions on first emission.
KPI_RISK_ADJ_NIM_YOY_BPS = "Risk-adjusted NIM YoY change (bps)"
KPI_REVENUE_YOY_DECELERATION = "Revenue YoY Growth Deceleration (YoY of YoY growth rate)"
KPI_NPL_15D_TOTAL_YOY_PP = "NPL 15d+ total YoY change (pp)"
KPI_CUSTOMERS_100K_YOY = "Customers >$100K ARR YoY Growth"

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

# Balance-sheet line item (doc_type fmp_balance_sheet) for ROE = trailing-4Q net
# income / period-end shareholders' equity. Optional — a quarter without equity
# (or without four trailing quarters of net income) simply gets no ROE row.
_BALANCE_LINE_ITEMS: tuple[str, ...] = ("total_stockholders_equity",)


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
    total_stockholders_equity: Decimal | None = None


def _fetch_quarterly_facts(conn: sqlite3.Connection, ticker: str) -> list[QuarterlyFacts]:
    """Pull standalone-quarter fundamentals for a ticker.

    Accepts rows from any `fmp_income_statement` / `fmp_cashflow` document
    EXCEPT the TTM / FY rollups (`%_ttm.json`, `%_annual.json`). The
    `fiscal_period_type IN ('Q1'..'Q4')` filter is the real safety net — it
    rejects any non-quarterly leakage regardless of file naming.

    On file naming: the live stable cacher (execution/save_fmp_data.py) writes
    SUFFIXED files — `{TICKER}_income_statement_quarterly.json`,
    `_annual.json`, `_ttm.json` — there is NO unsuffixed
    `{TICKER}_income_statement.json` (no code writes one). So we exclude the
    `_ttm`/`_annual` rollups by file path and lean on the
    `fiscal_period_type IN ('Q1'..'Q4')` filter as the real guard against any
    non-quarterly leakage.

    Per-cell tier-aware winner: the query orders ASCENDING by
    (source_quality_tier rank, id) and the ``bucket[line_item] = value``
    overwrite keeps whichever row is processed LAST, so the highest-(tier, id)
    row wins each (period_end, fiscal_period_type, line_item) cell — the SAME
    contract the Series loaders (``timeseries.loaders``) enforce, so a
    deterministic SEC XBRL row beats an FMP row regardless of ingest order. The
    prior grouping had NO dedup (first cursor row set the source_doc_id and the
    last write won each cell in arbitrary period_end-only order), so a single key
    could mix an FMP revenue with a SEC operating_income. ``_source_doc_id``
    tracks the winning revenue cell's document (the anchor the derived rows'
    provenance ties to). Doc-type is still restricted to the FMP statement docs
    the derivers were built around; a SEC row surfaces here only when it was
    persisted under one of those doc_types.
    """
    all_line_items = _REQUIRED_LINE_ITEMS + _OPTIONAL_LINE_ITEMS + _BALANCE_LINE_ITEMS
    placeholders = ",".join("?" * len(all_line_items))
    tier_rank = reader_tier_rank_sql(conn)
    cur = conn.execute(
        f"""
        SELECT ff.period_end, ff.fiscal_period_type, ff.line_item, ff.value, ff.source_doc_id
        FROM financial_facts ff
        JOIN documents d ON d.id = ff.source_doc_id
        WHERE ff.ticker = ?
          AND d.doc_type IN ('fmp_income_statement', 'fmp_cashflow', 'fmp_balance_sheet')
          AND d.file_path NOT LIKE '%_ttm.json'
          AND d.file_path NOT LIKE '%_annual.json'
          AND ff.line_item IN ({placeholders})
          AND ff.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
        ORDER BY ff.period_end ASC, {tier_rank} ASC, ff.id ASC
        """,
        (ticker.upper(), *all_line_items),
    )
    grouped: dict[tuple[datetime, str], dict[str, object]] = {}
    for row in cur.fetchall():
        pe = row["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        key = (pe, row["fiscal_period_type"])
        bucket = grouped.setdefault(key, {})
        bucket[row["line_item"]] = Decimal(str(row["value"]))
        # Anchor provenance to the winning revenue cell's document (revenue is
        # the required base of every derivation). Rows arrive tier-ascending, so
        # the last revenue write is the tier-winner.
        if row["line_item"] == "revenue":
            bucket["_source_doc_id"] = int(row["source_doc_id"])

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
        equity = bucket.get("total_stockholders_equity")
        equity_dec = equity if isinstance(equity, Decimal) else None
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
                total_stockholders_equity=equity_dec,
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
    # Pre-serialized lineage JSON for kpi_facts.computed_from (alembic 0087,
    # data_provenance.md §9) — the derivation recipe the chip popover renders
    # ("derived from: …" plus one mini-chip per input). None only when a
    # deriver predates lineage capture.
    computed_from: str | None = None


def _lineage(display: str, inputs: list[dict[str, object]]) -> str:
    """Serialize one ``computed_from`` payload. ``display`` is the human
    formula ("operating_income ÷ revenue (%)"); each input names its source
    row by logical key + the source document at derivation time. The
    per-input ``tier`` is injected later by ``persist_derived_kpis`` (it
    needs the documents table; the derivers are pure)."""
    return json.dumps({"display": display, "inputs": inputs}, separators=(",", ":"))


def _input_ref(ref: str, item: str, period_end: datetime, doc_id: int) -> dict[str, object]:
    """One computed_from input: ``ref`` ∈ financial_fact | kpi_fact | segment_fact."""
    return {
        "ref": ref,
        "item": item,
        "period_end": period_end.date().isoformat(),
        "doc_id": doc_id,
    }


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
                computed_from=_lineage(
                    "operating_income ÷ revenue (%)",
                    [
                        _input_ref(
                            "financial_fact", "operating_income", f.period_end, f.source_doc_id
                        ),
                        _input_ref("financial_fact", "revenue", f.period_end, f.source_doc_id),
                    ],
                ),
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
                computed_from=_lineage(
                    "net_income ÷ revenue (%)",
                    [
                        _input_ref("financial_fact", "net_income", f.period_end, f.source_doc_id),
                        _input_ref("financial_fact", "revenue", f.period_end, f.source_doc_id),
                    ],
                ),
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
                computed_from=_lineage(
                    "gross_profit ÷ revenue (%)",
                    [
                        _input_ref("financial_fact", "gross_profit", f.period_end, f.source_doc_id),
                        _input_ref("financial_fact", "revenue", f.period_end, f.source_doc_id),
                    ],
                ),
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
                    computed_from=_lineage(
                        "|capital_expenditure| ÷ revenue (%)",
                        [
                            _input_ref(
                                "financial_fact",
                                "capital_expenditure",
                                f.period_end,
                                f.source_doc_id,
                            ),
                            _input_ref("financial_fact", "revenue", f.period_end, f.source_doc_id),
                        ],
                    ),
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
                    computed_from=_lineage(
                        "free_cash_flow ÷ revenue (%)",
                        [
                            _input_ref(
                                "financial_fact", "free_cash_flow", f.period_end, f.source_doc_id
                            ),
                            _input_ref("financial_fact", "revenue", f.period_end, f.source_doc_id),
                        ],
                    ),
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
                    computed_from=_lineage(
                        "revenue vs same quarter 1y prior (%)",
                        [
                            _input_ref("financial_fact", "revenue", f.period_end, f.source_doc_id),
                            _input_ref(
                                "financial_fact", "revenue", prior.period_end, prior.source_doc_id
                            ),
                        ],
                    ),
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
                        computed_from=_lineage(
                            "operating_cash_flow vs same quarter 1y prior (%)",
                            [
                                _input_ref(
                                    "financial_fact",
                                    "operating_cash_flow",
                                    f.period_end,
                                    f.source_doc_id,
                                ),
                                _input_ref(
                                    "financial_fact",
                                    "operating_cash_flow",
                                    prior.period_end,
                                    prior.source_doc_id,
                                ),
                            ],
                        ),
                    )
                )

    # ROE = trailing-4Q net income / period-end shareholders' equity (percent).
    # Needs four consecutive quarters (the TTM window, within ~13 months to reject
    # gaps) and a non-zero equity reading at the period.
    chrono = sorted(facts, key=lambda q: q.period_end)
    for i in range(3, len(chrono)):
        cur = chrono[i]
        equity = cur.total_stockholders_equity
        if equity is None or equity == 0:
            continue
        window = chrono[i - 3 : i + 1]
        if (cur.period_end - window[0].period_end).days > 400:
            continue
        ttm_net_income = sum((q.net_income for q in window), Decimal(0))
        out.append(
            DerivedKpiRow(
                period_end=cur.period_end,
                fiscal_period_type=cur.fiscal_period_type,
                name=KPI_ROE,
                value=ttm_net_income / equity * Decimal(100),
                unit=Unit.PERCENT,
                source_doc_id=cur.source_doc_id,
                computed_from=_lineage(
                    "TTM net_income ÷ total_stockholders_equity (%)",
                    [
                        _input_ref("financial_fact", "net_income", q.period_end, q.source_doc_id)
                        for q in window
                    ]
                    + [
                        _input_ref(
                            "financial_fact",
                            "total_stockholders_equity",
                            cur.period_end,
                            cur.source_doc_id,
                        )
                    ],
                ),
            )
        )
    return out


def _derive_segment_kpis(conn: sqlite3.Connection, ticker: str) -> list[DerivedKpiRow]:
    """Derive segment-level growth and margins dynamically based on holdings.json."""
    # Junction tables must exist for any segment-derived KPI to make sense —
    # test fixtures without segment data fall through to an empty result.
    table_check = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name IN ('segment_periods', 'segment_dimensions')"
    ).fetchone()
    if not table_check or table_check[0] < 2:
        return []

    cur = conn.execute(
        """
        SELECT DISTINCT sd.dim_name AS segment_name
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        WHERE sp.ticker = ?
        """,
        (ticker.upper(),),
    )
    segments = [str(r["segment_name"]) for r in cur.fetchall() if r["segment_name"]]
    if not segments:
        return []

    repo_root = Path(__file__).resolve().parents[2]
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"

    kpi_targets: list[tuple[str, str, str]] = []
    if holdings_path.exists():
        try:
            with open(holdings_path, encoding="utf-8") as f:
                data = json.load(f)
            kpi_names: set[str] = set()
            for key in ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis", "business_model_rules"):
                items = data.get(key) or []
                for item in items:
                    name = item.get("name") or item.get("kpi_name")
                    if name:
                        kpi_names.add(str(name).strip())

            for kpi in kpi_names:
                kpi_lower = kpi.lower()
                for seg in segments:
                    if re.search(r"\b" + re.escape(seg.lower()) + r"\b", kpi_lower):
                        if "margin" in kpi_lower or "operating margin" in kpi_lower:
                            kpi_targets.append((kpi, seg, "operating_margin"))
                        elif "growth" in kpi_lower or "yoy" in kpi_lower:
                            if "operating income" in kpi_lower or "operating profit" in kpi_lower:
                                kpi_targets.append((kpi, seg, "operating_income_growth"))
                            else:
                                kpi_targets.append((kpi, seg, "revenue_growth"))
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            # Malformed/unreadable holdings config — degrade to the defaults
            # below rather than crashing the derive; record why it was skipped.
            log.warning(
                {"event": "fmp_kpi_target_config_unreadable", "ticker": ticker},
                exc_info=True,
            )

    if ticker.upper() == "AMZN":
        default_amzn = [
            ("AWS operating margin", "AWS", "operating_margin"),
            ("AWS Operating Margin", "AWS", "operating_margin"),
            ("North America retail operating margin", "North America", "operating_margin"),
            ("North America Retail Operating Margin", "North America", "operating_margin"),
            ("AWS revenue growth (YoY)", "AWS", "revenue_growth"),
            ("AWS Revenue YoY Growth", "AWS", "revenue_growth"),
        ]
        existing_keys = {(t[0], t[1]) for t in kpi_targets}
        for k, s, m in default_amzn:
            if (k, s) not in existing_keys:
                kpi_targets.append((k, s, m))

    if not kpi_targets:
        return []

    # Pull the same shape the legacy segment_facts query produced: one row per
    # (period, segment, metric) cell, with `metric` mapped back to the legacy
    # vocabulary so the downstream grouping logic — which keys off
    # `revenue_by_product` / `operating_income` — keeps working unchanged.
    cur = conn.execute(
        """
        SELECT
            sp.period_end AS period_end,
            sp.fiscal_period_type AS fiscal_period_type,
            sd.dim_name AS segment_name,
            CASE
                WHEN sd.dim_type = 'product' AND sd.metric = 'revenue'
                    THEN 'revenue_by_product'
                WHEN sd.dim_type = 'business_unit' AND sd.metric = 'operating_income'
                    THEN 'operating_income'
            END AS metric,
            sd.value AS value,
            sp.source_doc_id AS source_doc_id
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        WHERE sp.ticker = ?
          AND (
            (sd.dim_type = 'product' AND sd.metric = 'revenue')
            OR (sd.dim_type = 'business_unit' AND sd.metric = 'operating_income')
          )
          AND sp.fiscal_period_type IN ('Q1', 'Q2', 'Q3', 'Q4')
        ORDER BY sp.period_end ASC
        """,
        (ticker.upper(),),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    grouped: dict[tuple[datetime, str], dict[str, dict[str, Decimal]]] = {}
    source_docs: dict[tuple[datetime, str], int] = {}
    for r in rows:
        pe = r["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        key = (pe, r["fiscal_period_type"])
        seg = r["segment_name"]
        metric = r["metric"]
        val = Decimal(str(r["value"]))
        source_docs[key] = int(r["source_doc_id"])

        grouped.setdefault(key, {}).setdefault(seg, {})[metric] = val

    out: list[DerivedKpiRow] = []
    history: dict[str, dict[str, dict[str, dict[int, tuple[Decimal, int]]]]] = {}

    sorted_keys = sorted(grouped.keys(), key=lambda k: k[0])
    for key in sorted_keys:
        pe, fpt = key
        segments_data = grouped[key]
        doc_id = source_docs[key]

        for kpi_name, seg_name, metric_type in kpi_targets:
            if seg_name not in segments_data:
                continue
            seg_data = segments_data[seg_name]

            if metric_type == "operating_margin":
                rev = seg_data.get("revenue_by_product")
                oi = seg_data.get("operating_income")
                if rev and oi is not None:
                    margin = (oi / rev) * Decimal(100)
                    out.append(
                        DerivedKpiRow(
                            pe,
                            FiscalPeriodType(fpt),
                            kpi_name,
                            margin,
                            Unit.PERCENT,
                            doc_id,
                            computed_from=_lineage(
                                f"{seg_name} operating_income ÷ {seg_name} revenue (%)",
                                [
                                    _input_ref(
                                        "segment_fact", f"{seg_name} operating_income", pe, doc_id
                                    ),
                                    _input_ref("segment_fact", f"{seg_name} revenue", pe, doc_id),
                                ],
                            ),
                        )
                    )

            elif metric_type == "revenue_growth":
                rev = seg_data.get("revenue_by_product")
                if rev:
                    history.setdefault(seg_name, {}).setdefault("revenue", {}).setdefault(fpt, {})[
                        pe.year
                    ] = (rev, doc_id)

            elif metric_type == "operating_income_growth":
                oi = seg_data.get("operating_income")
                if oi is not None:
                    history.setdefault(seg_name, {}).setdefault("operating_income", {}).setdefault(
                        fpt, {}
                    )[pe.year] = (oi, doc_id)

    for seg_name, metrics in history.items():
        for metric_name, fpt_map in metrics.items():
            targets = [
                kpi_name
                for kpi_name, s, m in kpi_targets
                if s == seg_name
                and (
                    (metric_name == "revenue" and m == "revenue_growth")
                    or (metric_name == "operating_income" and m == "operating_income_growth")
                )
            ]
            if not targets:
                continue

            for fpt, year_map in fpt_map.items():
                for year, (val, doc_id) in year_map.items():
                    prior_year = year - 1
                    if prior_year in year_map:
                        prior_val, _ = year_map[prior_year]
                        if prior_val > 0:
                            yoy = ((val - prior_val) / prior_val) * Decimal(100)
                            pe_current = next(
                                k[0] for k in sorted_keys if k[0].year == year and k[1] == fpt
                            )
                            prior_doc_id = year_map[prior_year][1]
                            pe_prior = next(
                                (
                                    k[0]
                                    for k in sorted_keys
                                    if k[0].year == prior_year and k[1] == fpt
                                ),
                                None,
                            )
                            seg_inputs = [
                                _input_ref(
                                    "segment_fact", f"{seg_name} {metric_name}", pe_current, doc_id
                                )
                            ]
                            if pe_prior is not None:
                                seg_inputs.append(
                                    _input_ref(
                                        "segment_fact",
                                        f"{seg_name} {metric_name}",
                                        pe_prior,
                                        prior_doc_id,
                                    )
                                )
                            for kpi_name in targets:
                                out.append(
                                    DerivedKpiRow(
                                        pe_current,
                                        FiscalPeriodType(fpt),
                                        kpi_name,
                                        yoy,
                                        Unit.PERCENT,
                                        doc_id,
                                        computed_from=_lineage(
                                            f"{seg_name} {metric_name} vs same quarter 1y prior (%)",
                                            seg_inputs,
                                        ),
                                    )
                                )

    return out


# ---------------------------------------------------------------------------
# Category 2: same-fiscal-quarter YoY transforms over an existing kpi_facts
# LEVEL series. Materializes the derived series a break_rule references by name
# (e.g. "Risk-adjusted NIM YoY change (bps)") from the level series the pipeline
# actually stores ("Risk-adjusted NIM (...)"), so the rule resolver hits real
# observations instead of evaluating against nothing.
# ---------------------------------------------------------------------------


class TransformKind(StrEnum):
    """Which same-fiscal-quarter YoY transform `compute_yoy_transform` applies."""

    YOY_CHANGE_BPS = "yoy_change_bps"
    YOY_CHANGE_PP = "yoy_change_pp"
    YOY_DECELERATION = "yoy_deceleration"
    YOY_PCT_GROWTH = "yoy_pct_growth"


@dataclass(frozen=True)
class KpiSeriesPoint:
    """One observation of a base kpi_facts series, carrying source_doc_id so a
    derived row inherits the provenance of the period that produced it."""

    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    value: Decimal
    source_doc_id: int


@dataclass(frozen=True)
class _TransformSpec:
    """A transform deriver: read `base_label` from kpi_facts, apply `kind`, then
    register the output under `derived_name` with `unit`.

    `base_unit` filters the base series to that unit (the YoY transforms assume a
    percentage scale by default); set it to None to accept any numeric unit — used
    by YOY_PCT_GROWTH, whose base is a level/count rather than a percentage."""

    derived_name: str
    base_label: str
    kind: TransformKind
    unit: Unit
    base_unit: Unit | None = Unit.PERCENT


_TRANSFORM_DERIVER_SPECS: tuple[_TransformSpec, ...] = (
    # Risk-adjusted NIM contracting YoY — NU's expanding-spread breaker. Base is
    # the extracted level %; (curr - prior) * 100 expresses the pp delta in bps.
    _TransformSpec(
        derived_name=KPI_RISK_ADJ_NIM_YOY_BPS,
        base_label="Risk-adjusted NIM (NIM minus cost of risk)",
        kind=TransformKind.YOY_CHANGE_BPS,
        unit=Unit.BPS,
    ),
    # Revenue growth-rate collapse — base is the YoY growth RATE itself, so this is
    # the YoY decel OF that rate: (prior - curr) / prior, as a % of the prior rate.
    _TransformSpec(
        derived_name=KPI_REVENUE_YOY_DECELERATION,
        base_label=KPI_REVENUE_YOY_USD,
        kind=TransformKind.YOY_DECELERATION,
        unit=Unit.PERCENT,
    ),
    # Consolidated NPL 15d+ deteriorating YoY. `base_label` is the short normalized
    # form so the resolver reaches the verbose extracted def "NPL 15d+ ratio
    # (consolidated total: ...)" — which the rule's own label ("NPL 15d+ total")
    # does NOT normalize-match. (curr - prior) is the pp delta directly.
    _TransformSpec(
        derived_name=KPI_NPL_15D_TOTAL_YOY_PP,
        base_label="NPL 15d+ ratio",
        kind=TransformKind.YOY_CHANGE_PP,
        unit=Unit.PERCENT,
    ),
    # RBRK large-customer (>$100K ARR) count growth — the moat-attach breaker. The
    # base is the extracted customer COUNT (not a percentage), so accept any base
    # unit and emit relative YoY growth as a percent.
    _TransformSpec(
        derived_name=KPI_CUSTOMERS_100K_YOY,
        base_label="Customers >$100K ARR",
        kind=TransformKind.YOY_PCT_GROWTH,
        unit=Unit.PERCENT,
        base_unit=None,
    ),
)


_TRANSFORM_DISPLAY: dict[TransformKind, str] = {
    TransformKind.YOY_CHANGE_BPS: "{base} - same quarter 1y prior (bps)",
    TransformKind.YOY_CHANGE_PP: "{base} - same quarter 1y prior (pp)",
    TransformKind.YOY_DECELERATION: "YoY deceleration of {base} (%)",
    TransformKind.YOY_PCT_GROWTH: "{base} YoY growth (%)",
}


def compute_yoy_transform(
    points: list[KpiSeriesPoint],
    *,
    kind: TransformKind,
    name: str,
    unit: Unit,
    base_label: str | None = None,
) -> list[DerivedKpiRow]:
    """Compute a same-fiscal-quarter YoY transform over a level/rate series.

    For each point P, look up the point one year earlier in the SAME fiscal
    quarter ``(fiscal_period_type, period_end.year - 1)``. When found:

      - ``YOY_CHANGE_BPS``:   ``(P - prior) * 100`` — level pp delta, in bps
      - ``YOY_CHANGE_PP``:    ``(P - prior)``       — level pp delta (pct points)
      - ``YOY_DECELERATION``: ``(prior - P) / prior * 100``, skipping ``prior <= 0``
            (P and prior are themselves YoY growth-rate %; this is the YoY
             deceleration of that rate — positive = decelerating, negative =
             accelerating. A non-positive prior rate makes the ratio meaningless,
             so it is skipped rather than emitting a spurious value.)
      - ``YOY_PCT_GROWTH``:   ``(P - prior) / prior * 100``, skipping ``prior <= 0``
            (relative YoY % growth of a level/count base — the one kind whose base
             is not itself a percentage, e.g. a customer count.)

    Points with no same-quarter prior-year match are skipped — the natural case
    for a sparse series (e.g. NPL prints scattered across distinct quarters) and
    the first year of any series. Output ``period_end`` / ``fiscal_period_type`` /
    ``source_doc_id`` are the CURRENT point's, so provenance ties to the filing
    that reported the latest value. The percentage-change kinds assume a
    percentage-scale series; YOY_PCT_GROWTH takes a level/count base.

    ``base_label`` (the spec's human label for the base series) feeds the
    ``computed_from`` lineage: each output row records the current + prior-year
    base observations as its inputs. None → lineage omitted (legacy callers).
    """
    by_fiscal_quarter: dict[tuple[FiscalPeriodType, int], KpiSeriesPoint] = {}
    for p in points:
        by_fiscal_quarter[(p.fiscal_period_type, p.period_end.year)] = p

    out: list[DerivedKpiRow] = []
    for p in points:
        prior = by_fiscal_quarter.get((p.fiscal_period_type, p.period_end.year - 1))
        if prior is None:
            continue
        if kind is TransformKind.YOY_CHANGE_BPS:
            value = (p.value - prior.value) * Decimal(100)
        elif kind is TransformKind.YOY_CHANGE_PP:
            value = p.value - prior.value
        elif kind is TransformKind.YOY_DECELERATION:
            if prior.value <= 0:
                continue
            value = (prior.value - p.value) / prior.value * Decimal(100)
        elif kind is TransformKind.YOY_PCT_GROWTH:
            if prior.value <= 0:
                continue
            value = (p.value - prior.value) / prior.value * Decimal(100)
        else:  # pragma: no cover - exhaustive over TransformKind
            raise ValueError(f"Unhandled transform kind: {kind}")
        computed_from = None
        if base_label is not None:
            computed_from = _lineage(
                _TRANSFORM_DISPLAY[kind].format(base=base_label),
                [
                    _input_ref("kpi_fact", base_label, p.period_end, p.source_doc_id),
                    _input_ref("kpi_fact", base_label, prior.period_end, prior.source_doc_id),
                ],
            )
        out.append(
            DerivedKpiRow(
                period_end=p.period_end,
                fiscal_period_type=p.fiscal_period_type,
                name=name,
                value=value,
                unit=unit,
                source_doc_id=p.source_doc_id,
                computed_from=computed_from,
            )
        )
    return out


def _fetch_full_kpi_series(
    conn: sqlite3.Connection,
    ticker: str,
    base_label: str,
    base_unit: Unit | None = Unit.PERCENT,
) -> list[KpiSeriesPoint]:
    """Resolve `base_label` to its canonical definition and pull the full,
    per-period-deduped series from kpi_facts (oldest-first).

    Resolution + per-period source dedup mirror
    ``thesis_evaluator._fetch_kpi_history`` (richest definition wins; highest
    ``source_doc_id`` per ``(period_end, fiscal_period_type)`` wins) so the
    transform reads exactly the series the evaluator and the §3 chart read.
    Rows whose unit != ``base_unit`` are skipped (the YoY transforms assume a
    percentage scale by default); pass ``base_unit=None`` to accept any unit (for
    a level/count base). Returns ``[]`` when the label resolves to no
    fact-carrying definition.
    """
    resolved = resolve_kpi_definition_name(conn, ticker, base_label)
    if resolved is None:
        return []
    cur = conn.execute(
        "SELECT kf.period_end, kf.fiscal_period_type, kf.value, kf.unit, "
        "       kf.source_doc_id "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kd.name = ? "
        "  AND kf.source_doc_id = ("
        "      SELECT MAX(k2.source_doc_id) FROM kpi_facts k2 "
        "      WHERE k2.ticker = kf.ticker "
        "        AND k2.kpi_definition_id = kf.kpi_definition_id "
        "        AND k2.period_end = kf.period_end "
        "        AND k2.fiscal_period_type = kf.fiscal_period_type) "
        "ORDER BY kf.period_end ASC",
        (ticker.upper(), resolved),
    )
    # Company-doc overrides win over the FMP/LLM row before the YoY transform reads
    # it: a 'replace' substitutes the authoritative value, a 'drop' omits the period.
    ov_map = active_scalar_override_map(
        conn, ticker=ticker, fact_kind=OVERRIDE_KPI, fact_key=resolved
    )
    points: list[KpiSeriesPoint] = []
    for row in cur.fetchall():
        if base_unit is not None and Unit(row["unit"]) is not base_unit:
            continue
        pe = row["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        ftype = str(row["fiscal_period_type"])
        ov = ov_map.get((str(row["period_end"])[:10], ftype))
        if ov is not None and ov.action == OverrideAction.DROP.value:
            continue
        value = (
            Decimal(str(ov.value))
            if (ov is not None and ov.value is not None)
            else Decimal(str(row["value"]))
        )
        points.append(
            KpiSeriesPoint(
                period_end=pe,
                fiscal_period_type=FiscalPeriodType(row["fiscal_period_type"]),
                value=value,
                source_doc_id=int(row["source_doc_id"]),
            )
        )
    return points


def derive_kpi_transforms(conn: sqlite3.Connection, ticker: str) -> list[DerivedKpiRow]:
    """Compute every configured YoY transform whose base series resolves.

    Reads from kpi_facts (NOT financial_facts), so it must run AFTER the
    category-1 FMP derivation has persisted — a transform whose base is itself a
    derived KPI (Revenue YoY Growth Deceleration over Revenue YoY Growth (USD))
    then sees a populated base. A spec whose base does not resolve for this
    ticker (wrong business model, or not yet extracted) contributes nothing;
    a base with fewer than two observations cannot yield a YoY pair and is
    likewise skipped.
    """
    out: list[DerivedKpiRow] = []
    for spec in _TRANSFORM_DERIVER_SPECS:
        points = _fetch_full_kpi_series(conn, ticker, spec.base_label, spec.base_unit)
        if len(points) < 2:
            continue
        out.extend(
            compute_yoy_transform(
                points,
                kind=spec.kind,
                name=spec.derived_name,
                unit=spec.unit,
                base_label=spec.base_label,
            )
        )
    return out


def _input_doc_tiers(conn: sqlite3.Connection, rows: list[DerivedKpiRow]) -> dict[int, str]:
    """source_quality_tier per document id referenced by any row's lineage
    inputs. One query for the whole batch; {} when the documents table or
    tier column is absent (legacy fixtures) — inputs then stay tier-less."""
    doc_ids: set[int] = set()
    for row in rows:
        if row.computed_from is None:
            continue
        try:
            payload: object = json.loads(row.computed_from)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        inputs = cast("dict[str, object]", payload).get("inputs")
        if not isinstance(inputs, list):
            continue
        for entry in cast("list[object]", inputs):
            if isinstance(entry, dict):
                raw = cast("dict[str, object]", entry).get("doc_id")
                if isinstance(raw, int):
                    doc_ids.add(raw)
    if not doc_ids:
        return {}
    marks = ",".join("?" * len(doc_ids))
    try:
        cur = conn.execute(
            f"SELECT id, source_quality_tier FROM documents WHERE id IN ({marks})",
            tuple(doc_ids),
        )
        return {int(r[0]): str(r[1]) for r in cur.fetchall() if r[1] is not None}
    except sqlite3.Error:
        return {}


def _enrich_lineage_tiers(computed_from: str | None, tier_by_doc: dict[int, str]) -> str | None:
    """Inject each input's document tier into the lineage JSON so the chip
    popover can color the per-input mini-chips without a documents lookup at
    render time. No-op (returns the original) on missing/unparseable JSON."""
    if computed_from is None or not tier_by_doc:
        return computed_from
    try:
        payload: object = json.loads(computed_from)
    except ValueError:
        return computed_from
    if not isinstance(payload, dict):
        return computed_from
    payload_map = cast("dict[str, object]", payload)
    inputs = payload_map.get("inputs")
    if not isinstance(inputs, list):
        return computed_from
    for entry in cast("list[object]", inputs):
        if isinstance(entry, dict):
            entry_map = cast("dict[str, object]", entry)
            raw = entry_map.get("doc_id")
            if isinstance(raw, int) and raw in tier_by_doc:
                entry_map["tier"] = tier_by_doc[raw]
    return json.dumps(payload_map, separators=(",", ":"))


def persist_derived_kpis(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    rows: list[DerivedKpiRow],
    extracted_by: str = "fmp_derived",
) -> int:
    """Insert kpi_facts rows for the derived metrics. Returns count actually inserted.

    Routes through `insert_kpi_with_restatement_detection` so that when a
    later filing restates an earlier quarter's value, the new derived KPI
    row links back to the incumbent via `supersedes_id`. `extracted_by`
    tags the audit trail: category-1 FMP derivations keep the default
    `'fmp_derived'`; category-2 transforms over kpi_facts (see
    `derive_kpi_transforms`) pass `'kpi_transform_derived'` so the two are
    distinguishable from direct FMP ingestion and from each other.
    """
    # Derived rows are mechanical arithmetic over FMP-tier inputs; the
    # deterministic prior (pipeline.confidence) replaces the unset 1.0.
    confidence = score_confidence(tier=SourceQualityTier.FMP_NORMALIZED, extracted_by=extracted_by)
    tier_by_doc = _input_doc_tiers(conn, rows)
    inserted = 0
    for row in rows:
        kpi_def_id = find_or_create_kpi_definition(
            conn,
            ticker=ticker,
            name=row.name,
            unit=row.unit,
            primary_source=SourceType.FMP,
        )
        new_id, superseded_id = insert_kpi_with_restatement_detection(
            conn,
            ticker=ticker,
            period_end=row.period_end,
            fiscal_period_type=row.fiscal_period_type.value,
            kpi_definition_id=kpi_def_id,
            value=row.value,
            unit=row.unit.value,
            source_doc_id=row.source_doc_id,
            confidence=confidence,
            extracted_by=extracted_by,
            computed_from=_enrich_lineage_tiers(row.computed_from, tier_by_doc),
        )
        if new_id is not None:
            inserted += 1
        # L10: capture the restatement instead of discarding superseded_id.
        if superseded_id is not None:
            _ = record_restatement_observation(
                conn, fact_table=KPI_FACTS, superseded_id=superseded_id, new_value=row.value
            )
    conn.commit()
    return inserted


def derive_for_ticker(conn: sqlite3.Connection, ticker: str) -> tuple[int, int]:
    """End-to-end: derive KPIs and persist. Returns (rows_emitted, rows_inserted).

    Two phases. Phase 1 derives category-1 metrics from `financial_facts`
    (margins + Revenue YoY Growth + segment KPIs) and persists them. Phase 2
    runs `derive_kpi_transforms`, the same-fiscal-quarter YoY transforms over
    kpi_facts level series — it MUST follow phase 1's persist so a transform
    whose base is itself a phase-1 output (Revenue YoY Growth Deceleration)
    reads a populated base. Each phase is idempotent (UNIQUE index dedupes), so
    re-running inserts nothing new.
    """
    facts = _fetch_quarterly_facts(conn, ticker)
    rows: list[DerivedKpiRow] = []
    if facts:
        rows.extend(derive_for_facts(facts))
    rows.extend(_derive_segment_kpis(conn, ticker))

    inserted = 0
    if rows:
        inserted += persist_derived_kpis(conn, ticker=ticker, rows=rows)

    # Phase 2: transforms over the now-persisted kpi_facts (incl. the Revenue
    # YoY Growth just written, plus IR-extracted bases like Risk-adjusted NIM).
    transform_rows = derive_kpi_transforms(conn, ticker)
    if transform_rows:
        inserted += persist_derived_kpis(
            conn,
            ticker=ticker,
            rows=transform_rows,
            extracted_by="kpi_transform_derived",
        )

    return (len(rows) + len(transform_rows), inserted)
