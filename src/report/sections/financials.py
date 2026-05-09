"""§3 Financials — last 12 quarters wide-form + last 10 FY (workbook only).

Quarterly: pull 16 quarters from the metrics view, dedupe to one row per
calendar quarter (the view holds both 'Q1' and 'quarterly' fiscal_period_type
buckets — picking just one drops columns, so we COALESCE non-null fields
across all rows for the same calendar quarter), display the most recent 12,
and compute QoQ / YoY / 1Y-TTM CAGR / 3Y-TTM CAGR.

Annual: pull 10 fiscal years from the metrics view (fiscal_period_type IN
('FY','annual')) for the workbook's Annual_Financials tab. Display only —
no growth columns at the annual cadence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from report.models import (
    AnnualLineItem,
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
    if not has_table(conn, "metrics"):
        conn.close()
        return _missing("metrics view absent", "alembic upgrade head", "Migration 0012 creates the metrics view.")

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

    line_items: list[QuarterlyLineItem] = []
    for col, name, unit, digits in _LINE_ITEM_SPECS:
        full_series = [_to_display(r.get(col), col) for r in deduped_quarterly]
        if all(v is None for v in full_series):
            continue
        line_items.append(
            QuarterlyLineItem(
                line_item=name,
                unit=unit,
                digits=digits,
                quarters=display_labels,
                values=full_series[-DISPLAY_QUARTERS:],
                growth=compute_growth(full_series),
            )
        )

    annual_years, annual_items = _build_annual(annual_rows)

    quarterly_ok = bool(line_items)
    annual_ok = bool(annual_items)
    status = SectionStatus.OK if (quarterly_ok and annual_ok) else (
        SectionStatus.PARTIAL if (quarterly_ok or annual_ok) else SectionStatus.MISSING_DATA
    )

    requested_priorities = _read_chart_priorities_request(ticker, repo_root)
    resolved_priorities, kpi_series = _resolve_priorities(
        requested_priorities, line_items, ticker, repo_root, display_labels
    )

    return FinancialsSection(
        status=status,
        quarter_labels=display_labels,
        line_items=line_items,
        annual_years=annual_years,
        annual_line_items=annual_items,
        chart_priorities=resolved_priorities,
        kpi_chart_series=kpi_series,
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
) -> tuple[list[str], list[KpiSeries]]:
    """Resolve each requested name against line_items → kpi_facts → drop.

    Each name is tried first against the financials line_items (case-insensitive).
    If not found, queried against kpi_facts. If neither has data, dropped silently.
    Preserves the holdings-JSON ordering.
    """
    li_map = {li.line_item.lower(): li.line_item for li in line_items}
    resolved: list[str] = []
    kpi_series: list[KpiSeries] = []

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
            series = _kpi_series_for(conn, ticker, name, quarter_labels)
            if series is not None:
                kpi_series.append(series)
                resolved.append(series.name)
    finally:
        if conn is not None:
            conn.close()

    return resolved, kpi_series


def _kpi_series_for(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    quarter_labels: list[str],
) -> KpiSeries | None:
    """Pull a 12-quarter series for a kpi_facts metric, aligned to quarter_labels."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT kf.period_end, kf.value, kf.unit, kd.name
        FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ?
          AND kd.name = ?
          AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
        ORDER BY kf.period_end ASC
        """,
        (ticker.upper(), kpi_name),
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
    return KpiSeries(
        name=canonical_name,
        unit=_pretty_unit(canonical_unit),
        quarters=quarter_labels,
        values=values,
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


def _load_quarterly(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """Pull a generous window of quarterly rows (we'll dedupe in Python)."""
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT * FROM metrics
        WHERE ticker = ? AND fiscal_period_type IN ({placeholders})
        ORDER BY period_end DESC LIMIT ?
        """,
        (ticker.upper(), *QUARTERLY_PERIOD_TYPES, UNDERLYING_QUARTERS * 4),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    rows.reverse()  # → oldest first
    return rows


def _dedupe_by_calendar_quarter(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge rows that share a calendar quarter — first non-null wins per column.

    Prefer rows with a 'Qx' fiscal_period_type over 'quarterly' (the SEC XBRL
    bucket tends to carry fewer line items than the FMP 'Qx' rows).
    """
    groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    for r in rows:
        key = calendar_quarter_key(r["period_end"])
        groups.setdefault(key, []).append(r)

    merged: list[dict[str, object]] = []
    for key, group in sorted(groups.items()):
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


def _load_annual(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    placeholders = ",".join("?" * len(ANNUAL_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT * FROM metrics
        WHERE ticker = ? AND fiscal_period_type IN ({placeholders})
        ORDER BY period_end DESC LIMIT ?
        """,
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
