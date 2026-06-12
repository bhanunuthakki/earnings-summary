"""§3 Financials — last 12 quarters wide-form + last 10 FY (workbook only).

Quarterly: pull one deduped row per calendar quarter straight from
``financial_facts`` (NOT the ``metrics`` view — see ``_load_quarterly`` for
why the view cross-labels quarters for off-calendar reporters), display the
most recent 12, and compute QoQ / YoY / 1Y-TTM CAGR / 3Y-TTM CAGR.

Annual: pull 10 fiscal years from ``financial_facts`` (fiscal_period_type IN
('FY','annual')) for the workbook's Annual_Financials tab. Display only —
no growth columns at the annual cadence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from compute.kpi_resolver import (
    ANNUAL_FACT_PERIOD_TYPES,
    QUARTERLY_FACT_PERIOD_TYPES,
    reporting_cadence_for,
    resolve_kpi_definition_name,
)
from report.models import (
    AnnualKpiSeries,
    AnnualLineItem,
    CellSource,
    FinancialsSection,
    KpiSeries,
    QuarterlyLineItem,
    SectionStatus,
)
from report.sections._common import (
    ANNUAL_PERIOD_TYPES,
    DISPLAY_QUARTERS,
    QUARTERLY_PERIOD_TYPES,
    UNDERLYING_QUARTERS,
    calendar_quarter_key,
    compute_growth,
    has_table,
    missing,
    open_repo_db,
    quarter_label,
)
from timeseries.loaders import load_financial_cell_provenance, load_financial_fact_provenance

# (metrics-view column, display name, unit, display digits)
_LINE_ITEM_SPECS: list[tuple[str, str, str, int]] = [
    ("revenue", "Revenue", "USD millions", 0),
    ("gross_profit", "Gross profit", "USD millions", 0),
    ("operating_income", "Operating income", "USD millions", 0),
    ("net_income", "Net income", "USD millions", 0),
    ("eps_diluted", "EPS (diluted)", "USD", 2),
    ("operating_cash_flow", "Operating cash flow", "USD millions", 0),
    ("free_cash_flow", "Free cash flow", "USD millions", 0),
    ("capex", "Capex", "USD millions", 0),
]

# metrics-view column -> financial_facts.line_item, for the per-cell source
# chips (P3.3). The view renames only capital_expenditure; everything else
# is identity. Keep in lockstep with migration 0012's view DDL.
_COL_TO_FACT_LINE_ITEM: dict[str, str] = {
    "revenue": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "eps_diluted": "eps_diluted",
    "operating_cash_flow": "operating_cash_flow",
    "free_cash_flow": "free_cash_flow",
    "capex": "capital_expenditure",
}

ANNUAL_HISTORY_YEARS = 10

# Fallback when a holdings JSON omits chart_priorities.
_DEFAULT_CHART_PRIORITIES: tuple[str, ...] = (
    "Revenue",
    "Operating income",
    "Operating cash flow",
    "Free cash flow",
)


def build(ticker: str, repo_root: Path) -> FinancialsSection:
    conn = open_repo_db(repo_root)
    if conn is None:
        return _missing(
            "no DB at data/portfolio.db",
            "alembic upgrade head && python execution/extract_facts.py --ticker " + ticker.upper(),
        )
    if not has_table(conn, "financial_facts"):
        conn.close()
        return _missing(
            "financial_facts table absent",
            "alembic upgrade head && python execution/extract_facts.py --ticker " + ticker.upper(),
            "§3 reads facts directly — the metrics view's MAX(period_end) cross-labels "
            "fiscal quarters for off-calendar reporters.",
        )

    quarterly_rows = _load_quarterly(conn, ticker)
    annual_rows = _load_annual(conn, ticker)
    conn.close()

    if not quarterly_rows and not annual_rows:
        return _missing(
            "no facts in metrics view",
            f"python execution/extract_facts.py --ticker {ticker.upper()}",
        )

    deduped_quarterly = _dedupe_by_calendar_quarter(quarterly_rows)[-UNDERLYING_QUARTERS:]
    quarter_labels_full = [quarter_label(r["period_end"]) for r in deduped_quarterly]
    display_labels = quarter_labels_full[-DISPLAY_QUARTERS:]

    # Per-cell source provenance for the P3.3 chips — one query for every
    # (line_item, period) pair; best-effort ({} on legacy DBs).
    cell_prov = load_financial_cell_provenance(
        ticker, sorted(set(_COL_TO_FACT_LINE_ITEM.values())), repo_root
    )

    line_items: list[QuarterlyLineItem] = []
    for col, name, unit, digits in _LINE_ITEM_SPECS:
        full_series = [_to_display(r.get(col), col) for r in deduped_quarterly]
        if all(v is None for v in full_series):
            continue
        prov_by_period = cell_prov.get(_COL_TO_FACT_LINE_ITEM.get(col, col), {})
        sources_full = [
            _to_cell_source(prov_by_period.get(_period_date_key(r.get("period_end"))))
            for r in deduped_quarterly
        ]
        line_items.append(
            QuarterlyLineItem(
                line_item=name,
                unit=unit,
                digits=digits,
                quarters=display_labels,
                values=full_series[-DISPLAY_QUARTERS:],
                growth=compute_growth(full_series),
                levels_full=full_series,
                sources_full=sources_full,
            )
        )

    annual_years, annual_items = _build_annual(annual_rows)

    quarterly_ok = bool(line_items)
    annual_ok = bool(annual_items)
    status = SectionStatus.OK if (quarterly_ok and annual_ok) else (
        SectionStatus.PARTIAL if (quarterly_ok or annual_ok) else SectionStatus.MISSING_DATA
    )

    requested_priorities = _read_chart_priorities_request(ticker, repo_root)
    resolved_priorities, kpi_series, annual_kpi_series, annual_kpi_years = _resolve_priorities(
        requested_priorities, line_items, ticker, repo_root, display_labels, quarter_labels_full
    )

    currency = "USD"
    if deduped_quarterly:
        raw_ccy = deduped_quarterly[-1].get("currency")
        if isinstance(raw_ccy, str) and raw_ccy.strip():
            currency = raw_ccy.strip().upper()

    return FinancialsSection(
        status=status,
        quarter_labels=display_labels,
        line_items=line_items,
        annual_years=annual_years,
        annual_line_items=annual_items,
        chart_priorities=resolved_priorities,
        kpi_chart_series=kpi_series,
        annual_kpi_chart_series=annual_kpi_series,
        annual_kpi_years=annual_kpi_years,
        quarter_labels_full=quarter_labels_full,
        currency=currency,
    )


def _period_date_key(raw: object) -> str:
    """Coerce a metrics-view period_end to the YYYY-MM-DD key the cell
    provenance map is keyed by. Tolerant: non-string/short values key to ''
    (which simply never matches a provenance entry)."""
    s = str(raw) if raw is not None else ""
    return s[:10]


def _to_cell_source(prov: dict[str, object] | None) -> CellSource | None:
    """Map one load_financial_cell_provenance payload to the report model."""
    if prov is None:
        return None

    def _opt(key: str) -> str | None:
        v = prov.get(key)
        return str(v) if v is not None else None

    source = prov.get("source")
    raw_doc_id = prov.get("source_doc_id")
    return CellSource(
        source=str(source) if source is not None else "unknown",
        fetched_at=_opt("fetched_at"),
        source_url=_opt("source_url"),
        doc_type=_opt("doc_type"),
        accession_number=_opt("accession_number"),
        filing_date=_opt("filing_date"),
        locator=_opt("locator"),
        doc_id=int(raw_doc_id) if isinstance(raw_doc_id, int) else None,
    )


def _read_chart_priorities_request(ticker: str, repo_root: Path) -> list[str]:
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not holdings_path.exists():
        return list(_DEFAULT_CHART_PRIORITIES)
    with open(holdings_path, encoding="utf-8") as f:
        holdings = json.load(f)
    raw = holdings.get("chart_priorities") or []
    if isinstance(raw, list):
        cleaned = [str(x) for x in raw if isinstance(x, str)]
        if cleaned:
            return cleaned
    return list(_DEFAULT_CHART_PRIORITIES)


def _resolve_priorities(
    requested: list[str],
    line_items: list[QuarterlyLineItem],
    ticker: str,
    repo_root: Path,
    quarter_labels: list[str],
    quarter_labels_full: list[str],
) -> tuple[list[str], list[KpiSeries], list[AnnualKpiSeries], list[int]]:
    """Resolve each requested name against line_items → kpi_facts → drop.

    Each name is tried first against the financials line_items (case-insensitive).
    If not found, queried against kpi_facts. If neither has data, dropped silently.
    Preserves the holdings-JSON ordering.

    Cadence-aware: a name whose definition is annual-cadence
    (``kpi_definitions.reporting_cadence='annual'``) resolves to an
    ``AnnualKpiSeries`` on the fiscal-year axis instead of the gappy quarterly
    series — kept out of ``kpi_chart_series`` so the quarterly heatmap never shows
    a mostly-empty annual metric. Returns
    ``(resolved_names, quarterly_series, annual_series, annual_years)``.
    """
    li_map = {li.line_item.lower(): li.line_item for li in line_items}
    resolved: list[str] = []
    kpi_series: list[KpiSeries] = []
    annual_raw: list[tuple[str, str, dict[int, float]]] = []

    db_path = repo_root / "data" / "portfolio.db"
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

    try:
        for name in requested:
            lower = name.lower()
            if lower in li_map:
                resolved.append(li_map[lower])
                continue
            if conn is None:
                continue
            if reporting_cadence_for(conn, ticker, name) == "annual":
                annual = _annual_kpi_raw_for(conn, ticker, name)
                if annual is not None:
                    annual_raw.append(annual)
                    resolved.append(annual[0])
                continue
            series = _kpi_series_for(conn, ticker, name, quarter_labels, quarter_labels_full)
            if series is not None:
                kpi_series.append(series)
                resolved.append(series.name)
    finally:
        if conn is not None:
            conn.close()

    annual_series, annual_years = _align_annual_kpis(annual_raw)
    return resolved, kpi_series, annual_series, annual_years


def _annual_kpi_raw_for(
    conn: sqlite3.Connection, ticker: str, kpi_name: str
) -> tuple[str, str, dict[int, float]] | None:
    """Pull an annual-cadence KPI's fiscal-year-end series as ``(canonical_name,
    pretty_unit, {fiscal_year: value})``, or None when nothing resolves.

    Reads only ``FY``/``annual`` rows (``ANNUAL_FACT_PERIOD_TYPES``) so interim
    prints on an otherwise-annual metric never pollute the annual axis; dedupes to
    the latest-ingested source per period (highest ``source_doc_id``), matching
    every other kpi_facts reader. ``period_end`` year is the axis key.
    """
    resolved_name = resolve_kpi_definition_name(
        conn, ticker, kpi_name, period_types=ANNUAL_FACT_PERIOD_TYPES
    )
    if resolved_name is None:
        return None
    placeholders = ",".join("?" * len(ANNUAL_FACT_PERIOD_TYPES))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT kf.period_end, kf.value, kf.unit, kd.name
        FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ?
          AND kd.name = ?
          AND kf.fiscal_period_type IN ({placeholders})
          AND kf.source_doc_id = (
              SELECT MAX(k2.source_doc_id) FROM kpi_facts k2
              WHERE k2.ticker = kf.ticker
                AND k2.kpi_definition_id = kf.kpi_definition_id
                AND k2.period_end = kf.period_end
                AND k2.fiscal_period_type = kf.fiscal_period_type
          )
        ORDER BY kf.period_end ASC
        """,
        (ticker.upper(), resolved_name, *ANNUAL_FACT_PERIOD_TYPES),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    by_year: dict[int, float] = {}
    canonical_name = kpi_name
    canonical_unit = ""
    for r in rows:
        canonical_name = str(r["name"])
        canonical_unit = str(r["unit"] or "")
        try:
            year = int(str(r["period_end"])[:4])
            by_year[year] = float(str(r["value"]))
        except ValueError:
            continue
    if not by_year:
        return None
    return canonical_name, _pretty_unit(canonical_unit), by_year


def _align_annual_kpis(
    annual_raw: list[tuple[str, str, dict[int, float]]],
) -> tuple[list[AnnualKpiSeries], list[int]]:
    """Build a shared fiscal-year axis (union of all series' years, newest
    ``ANNUAL_HISTORY_YEARS``) and align each annual KPI to it. Returns
    ``([], [])`` when there are no annual KPIs."""
    if not annual_raw:
        return [], []
    all_years: set[int] = set()
    for _name, _unit, by_year in annual_raw:
        all_years.update(by_year)
    years = sorted(all_years)[-ANNUAL_HISTORY_YEARS:]
    series = [
        AnnualKpiSeries(
            name=name,
            unit=unit,
            years=years,
            values=[by_year.get(y) for y in years],
        )
        for name, unit, by_year in annual_raw
    ]
    return series, years


def _kpi_series_for(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    quarter_labels: list[str],
    quarter_labels_full: list[str],
) -> KpiSeries | None:
    """Pull a kpi_facts series, aligned to both display + full-history labels."""
    resolved_name = resolve_kpi_definition_name(
        conn, ticker, kpi_name, period_types=QUARTERLY_FACT_PERIOD_TYPES
    )
    if resolved_name is None:
        return None
    cur = conn.cursor()
    # When several sources report the same KPI for one period (e.g. an LLM brief
    # value later restated by the issuer's IR spreadsheet, which coexist as
    # separate rows), keep only the latest-ingested row per logical key —
    # highest source_doc_id wins, matching purge_duplicate_kpi_facts' dedup. This
    # makes the higher-tier IR-spreadsheet figure authoritative and the series
    # deterministic (no arbitrary tie-break between duplicate period rows).
    cur.execute(
        """
        SELECT kf.period_end, kf.value, kf.unit, kd.name
        FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ?
          AND kd.name = ?
          AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
          AND kf.source_doc_id = (
              SELECT MAX(k2.source_doc_id) FROM kpi_facts k2
              WHERE k2.ticker = kf.ticker
                AND k2.kpi_definition_id = kf.kpi_definition_id
                AND k2.period_end = kf.period_end
                AND k2.fiscal_period_type = kf.fiscal_period_type
          )
        ORDER BY kf.period_end ASC
        """,
        (ticker.upper(), resolved_name),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    by_label: dict[str, float] = {}
    canonical_name = kpi_name
    canonical_unit = ""
    for r in rows:
        canonical_name = str(r["name"])
        canonical_unit = str(r["unit"] or "")
        period_end = str(r["period_end"])[:10]
        # Period_end YYYY-MM-DD → "YYYY Qn" matching report quarter labels.
        try:
            year = int(period_end[:4])
            month = int(period_end[5:7])
        except ValueError:
            continue
        quarter = (month - 1) // 3 + 1
        label = f"{year} Q{quarter}"
        try:
            by_label[label] = float(str(r["value"]))
        except ValueError:
            continue
    values = [by_label.get(lbl) for lbl in quarter_labels]
    if all(v is None for v in values):
        return None
    levels_full = [by_label.get(lbl) for lbl in quarter_labels_full]
    return KpiSeries(
        name=canonical_name,
        unit=_pretty_unit(canonical_unit),
        quarters=quarter_labels,
        values=values,
        levels_full=levels_full,
    )


def _pretty_unit(raw_unit: str) -> str:
    if raw_unit == "percent":
        return "%"
    if raw_unit == "actual":
        return ""
    return raw_unit


# ---------------------------------------------------------------------------
# Quarterly path
# ---------------------------------------------------------------------------

# The line-item pivot §3 renders, named exactly as the old ``metrics`` view
# named its columns (so every downstream consumer — _LINE_ITEM_SPECS, the
# currency pluck, the per-cell provenance keys — is unchanged). ``capex``
# aliases the ``capital_expenditure`` line_item, matching the view. Each
# ``MAX(CASE …)`` selects the single non-null value of the one rn=1 winner per
# line_item in the group (it is a pick-the-non-null, NOT a numeric/string max).
_FACT_PIVOT = """
    MAX(period_end) AS period_end,
    MAX(currency) AS currency,
    MAX(fiscal_period_type) AS fiscal_period_type,
    MAX(CASE WHEN line_item = 'revenue' THEN value END) AS revenue,
    MAX(CASE WHEN line_item = 'gross_profit' THEN value END) AS gross_profit,
    MAX(CASE WHEN line_item = 'operating_income' THEN value END) AS operating_income,
    MAX(CASE WHEN line_item = 'net_income' THEN value END) AS net_income,
    MAX(CASE WHEN line_item = 'eps_diluted' THEN value END) AS eps_diluted,
    MAX(CASE WHEN line_item = 'operating_cash_flow' THEN value END) AS operating_cash_flow,
    MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS free_cash_flow,
    MAX(CASE WHEN line_item = 'capital_expenditure' THEN value END) AS capex
""".strip()

# Quarterly facts keyed by CALENDAR QUARTER derived from the actual period_end
# date — deliberately NOT the ``metrics`` view. The view groups by
# (fiscal_year=substr(period_end,1,4), fiscal_period_type) and stamps each
# group with MAX(period_end) over all ~35 line items. For off-calendar
# fiscal-year reporters FMP and SEC disagree on the fiscal-quarter LABEL, so a
# single mislabeled fact (e.g. AMAT's Q4-ending net_income filed under 'Q3' in
# SEC companyfacts) drags the whole group's period_end — and, because each
# line_item's winner is picked independently, sometimes its VALUES — into the
# wrong quarter. In prod this shifted/collided/dropped quarters for AMAT, MU,
# TOL, VEEV (COST, off by only a few days, was unaffected). Keying identity off
# the real period_end DATE instead makes the wrong fiscal_period_type
# label inert and scopes MAX(period_end) within the quarter. PR #452 took the
# research cockpit off the same view for the same reason. Bonus: filtering
# ticker inside the scan is ~70x faster than the view's ROW_NUMBER() over all
# ~726k facts. Day-level period-end wobble within a quarter (FMP vs SEC ±1 day)
# is folded into the calendar-quarter GROUP BY here, and _dedupe_by_calendar_quarter
# stays downstream as a defensive pass-through.
_QUARTERLY_FACTS_SQL = """
WITH dedup AS (
    SELECT
        period_end, currency, fiscal_period_type, line_item, value,
        CAST(substr(period_end, 1, 4) AS INTEGER) AS cal_year,
        (CAST(substr(period_end, 6, 2) AS INTEGER) - 1) / 3 + 1 AS cal_quarter,
        ROW_NUMBER() OVER (
            PARTITION BY
                CAST(substr(period_end, 1, 4) AS INTEGER),
                (CAST(substr(period_end, 6, 2) AS INTEGER) - 1) / 3 + 1,
                line_item
            ORDER BY source_doc_id DESC, period_end DESC
        ) AS rn
    FROM financial_facts
    WHERE ticker = ? AND fiscal_period_type IN ({marks})
)
SELECT
{pivot}
FROM dedup
WHERE rn = 1
GROUP BY cal_year, cal_quarter
ORDER BY period_end DESC
LIMIT ?
"""


def _load_quarterly(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """One deduped row per calendar quarter, read straight from financial_facts.

    See ``_QUARTERLY_FACTS_SQL`` for why this bypasses the ``metrics`` view.
    Over-pulls a generous window; ``_dedupe_by_calendar_quarter`` runs
    downstream as a defensive pass-through (the SQL already returns one row per
    calendar quarter)."""
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        _QUARTERLY_FACTS_SQL.format(marks=placeholders, pivot=_FACT_PIVOT),
        (ticker.upper(), *QUARTERLY_PERIOD_TYPES, UNDERLYING_QUARTERS * 4),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    rows.reverse()  # → oldest first
    return rows


def _dedupe_by_calendar_quarter(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge rows that share a calendar quarter — first non-null wins per column.

    ``_load_quarterly`` already returns one row per calendar quarter, so this is
    now a defensive pass-through; it remains as a safety net that would still
    coalesce should two rows ever land in the same calendar quarter, preferring
    a 'Qx' fiscal_period_type over 'quarterly'.
    """
    groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    for r in rows:
        key = calendar_quarter_key(r["period_end"])
        groups.setdefault(key, []).append(r)

    merged: list[dict[str, object]] = []
    for _key, group in sorted(groups.items()):
        ordered = sorted(group, key=_priority)
        merged.append(_coalesce_rows(ordered))
    return merged


def _priority(row: dict[str, object]) -> tuple[int, str]:
    """Sort key: prefer Q-bucket, then earlier period_end (deterministic)."""
    fpt = str(row.get("fiscal_period_type") or "")
    is_q = 0 if fpt.startswith("Q") else 1
    return (is_q, str(row.get("period_end") or ""))


def _coalesce_rows(ordered: Iterable[dict[str, object]]) -> dict[str, object]:
    """First non-null value per key wins. Caller must order by preference."""
    out: dict[str, object] = {}
    for r in ordered:
        for k, v in r.items():
            if k not in out or out[k] is None:
                out[k] = v
    return out


def _to_display(value: object, column: str) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if column.startswith("eps"):
        return v
    return v / 1_000_000.0


# ---------------------------------------------------------------------------
# Annual path
# ---------------------------------------------------------------------------


# One deduped row per fiscal year, read straight from financial_facts (same
# direct-read rationale as the quarterly path). The annual axis only ever uses
# the period_end YEAR (substr 1..4), so it was never quarter-cross-labeled —
# but reading facts directly keeps §3 entirely off the slow/date-buggy view on
# one code path. fiscal_year = the period_end's calendar year, exactly the
# view's grouping, so values are byte-for-byte unchanged from the view here.
_ANNUAL_FACTS_SQL = """
WITH dedup AS (
    SELECT
        period_end, currency, fiscal_period_type, line_item, value,
        CAST(substr(period_end, 1, 4) AS INTEGER) AS fiscal_year,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(substr(period_end, 1, 4) AS INTEGER), line_item
            ORDER BY source_doc_id DESC, period_end DESC
        ) AS rn
    FROM financial_facts
    WHERE ticker = ? AND fiscal_period_type IN ({marks})
)
SELECT
{pivot}
FROM dedup
WHERE rn = 1
GROUP BY fiscal_year
ORDER BY period_end DESC
LIMIT ?
"""


def _load_annual(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    placeholders = ",".join("?" * len(ANNUAL_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        _ANNUAL_FACTS_SQL.format(marks=placeholders, pivot=_FACT_PIVOT),
        (ticker.upper(), *ANNUAL_PERIOD_TYPES, ANNUAL_HISTORY_YEARS * 3),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    rows.reverse()
    return rows


def _build_annual(rows: list[dict[str, object]]) -> tuple[list[int], list[AnnualLineItem]]:
    if not rows:
        return ([], [])
    by_year: dict[int, list[dict[str, object]]] = {}
    for r in rows:
        y = int(str(r["period_end"])[:4])
        by_year.setdefault(y, []).append(r)

    merged_by_year: dict[int, dict[str, object]] = {
        y: _coalesce_rows(sorted(group, key=_priority)) for y, group in by_year.items()
    }
    years = sorted(merged_by_year.keys())[-ANNUAL_HISTORY_YEARS:]

    items: list[AnnualLineItem] = []
    for col, name, unit, digits in _LINE_ITEM_SPECS:
        series = [_to_display(merged_by_year[y].get(col), col) for y in years]
        if all(v is None for v in series):
            continue
        items.append(AnnualLineItem(line_item=name, unit=unit, digits=digits, years=years, values=series))
    return (years, items)


def _missing(detail: str, fix: str, extra: str | None = None) -> FinancialsSection:
    full_detail = detail if extra is None else f"{detail}. {extra}"
    return FinancialsSection(
        status=SectionStatus.MISSING_DATA,
        missing=missing(stage="COMPUTE(extract_facts)", fix_command=fix, detail=full_detail),
    )


# Map metrics-view column names to the financial_facts.line_item string they
# came from. The `capex` column aliases `capital_expenditure` in the view —
# every other column shares its line_item name. Used by build_per_metric to
# look up provenance via the tier-aware loader.
_LINE_ITEM_TO_FACT_KEY: dict[str, str] = {
    "revenue": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "eps_diluted": "eps_diluted",
    "operating_cash_flow": "operating_cash_flow",
    "free_cash_flow": "free_cash_flow",
    "capex": "capital_expenditure",
}


def build_per_metric(ticker: str, repo_root: Path) -> dict[str, dict[str, object]]:
    """Per-line-item provenance for the §3 quarterly facts.

    For each line item the financials table renders, look up the tier-
    winning row for the latest quarter via `load_financial_fact_provenance`
    and surface its (source, fact_id, fetched_at) for the brief audit
    log. Keys are suffixed with `_q_latest` so a future extension to
    annual / per-period provenance can sit alongside without colliding.

    Returns {} when no facts are present (synthetic env, fresh ticker,
    missing migrations) — caller writes a sparse per_metric dict.
    """
    out: dict[str, dict[str, object]] = {}
    for view_col, fact_key in _LINE_ITEM_TO_FACT_KEY.items():
        prov = load_financial_fact_provenance(
            ticker.upper(), fact_key, repo_root=repo_root
        )
        if prov is None:
            continue
        out[f"{view_col}_q_latest"] = prov
    return out
