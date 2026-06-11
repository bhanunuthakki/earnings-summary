"""Metric definitions for the pivot UI (Ask v4).

Every metric token the builder/results surface can carry a one-line
definition tooltip:

  kpi:…  — from the ``kpi_definitions`` table (unit + source + notes)
  fin:…  — a small curated glossary over the FMP-normalized line items,
           with a generic fallback for the long tail
  seg:…  — composed from the slice's dimension parts

``metric_definitions`` feeds ``ViewResult.definitions`` (row-label
tooltips); ``attach_catalog_titles`` decorates the picker catalog. Both
are best-effort: a missing DB/table simply yields fewer tooltips.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from viewspec.spec import MetricRef

# Curated one-liners for the line items people actually pivot on. The long
# tail falls back to a generic description naming the substrate.
FIN_GLOSSARY: dict[str, str] = {
    "revenue": "Total revenue as reported — FMP-normalized income-statement top line.",
    "cost_of_revenue": "Cost of revenue / cost of goods sold, as reported.",
    "gross_profit": "Revenue minus cost of revenue.",
    "operating_income": "Operating income (EBIT) — profit before interest and taxes.",
    "operating_expenses": "Total operating expenses below gross profit.",
    "net_income": "Net income attributable to the company (GAAP bottom line).",
    "ebitda": "Earnings before interest, taxes, depreciation and amortization.",
    "eps": "Basic earnings per share, as reported.",
    "eps_diluted": "Diluted earnings per share, as reported.",
    "shares_outstanding": "Weighted-average shares outstanding (basic).",
    "shares_diluted": "Weighted-average shares outstanding (diluted).",
    "operating_cash_flow": "Net cash provided by operating activities.",
    "capital_expenditure": "Purchases of property, plant and equipment (capex).",
    "free_cash_flow": "Operating cash flow minus capital expenditure.",
    "research_and_development": "R&D expense, as reported.",
    "selling_general_admin": "Selling, general & administrative expense.",
    "total_assets": "Total assets at period end.",
    "total_liabilities": "Total liabilities at period end.",
    "total_equity": "Total shareholders' equity at period end.",
    "total_debt": "Short- plus long-term interest-bearing debt at period end.",
    "cash_and_equivalents": "Cash and cash equivalents at period end.",
    "interest_expense": "Interest expense for the period.",
    "income_tax_expense": "Income tax expense for the period.",
}


def fin_definition(line_item: str) -> str:
    curated = FIN_GLOSSARY.get(line_item)
    if curated:
        return curated
    pretty = line_item.replace("_", " ")
    return f"Financial line item '{pretty}' — FMP-normalized statement series (financial_facts)."


def seg_definition(dim_type: str, dim_name: str, key: str) -> str:
    return (
        f"Segment slice: {dim_name} {key.replace('_', ' ')} on the {dim_type} axis — "
        "from the company's segment disclosures (segment_facts)."
    )


def kpi_definition_titles(
    db_path: Path | None,
    names: Iterable[str],
    tickers: Iterable[str] = (),
) -> dict[str, str]:
    """{kpi name: tooltip} from kpi_definitions — unit, source(s), notes.

    When several tickers define the same name, a row for one of ``tickers``
    wins, then any row with notes. Best-effort: missing DB/table → {}.
    """
    wanted = [n for n in dict.fromkeys(names) if n]
    if not wanted or db_path is None or not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return {}
    marks = ",".join("?" * len(wanted))
    try:
        rows = conn.execute(
            f"SELECT name, ticker, unit, primary_source, fallback_source, notes "
            f"FROM kpi_definitions WHERE name IN ({marks})",
            tuple(wanted),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    preferred = {str(t).upper() for t in tickers}

    def _rank(row: tuple[object, ...]) -> tuple[int, int]:
        ticker_hit = 0 if str(row[1]).upper() in preferred else 1
        has_notes = 0 if (row[5] is not None and str(row[5]).strip()) else 1
        return (ticker_hit, has_notes)

    out: dict[str, str] = {}
    for row in sorted(rows, key=_rank):
        name = str(row[0])
        if name in out:
            continue
        unit, primary, fallback, notes = (row[2], row[3], row[4], row[5])
        bits: list[str] = []
        if unit and str(unit) != "actual":
            bits.append(f"unit: {unit}")
        if primary:
            src = str(primary)
            if fallback:
                src += f" (fallback: {fallback})"
            bits.append(f"source: {src}")
        text = " · ".join(bits)
        if notes is not None and str(notes).strip():
            text = f"{text} — {str(notes).strip()}" if text else str(notes).strip()
        out[name] = text or f"Company KPI '{name}' (kpi_definitions / kpi_facts)."
    return out


def metric_definitions(
    metrics: Iterable[MetricRef],
    *,
    db_path: Path | None,
    tickers: Iterable[str] = (),
) -> dict[str, str]:
    """{metric token: definition} for a spec's metrics — the row-tooltip map
    execute_view stores on ViewResult."""
    refs = list(metrics)
    kpi_names = [m.key for m in refs if m.domain == "kpi"]
    kpi_titles = kpi_definition_titles(db_path, kpi_names, tickers)
    out: dict[str, str] = {}
    for m in refs:
        if m.domain == "fin":
            out[m.token()] = fin_definition(m.key)
        elif m.domain == "kpi":
            out[m.token()] = kpi_titles.get(
                m.key, f"Company KPI '{m.key}' (kpi_definitions / kpi_facts)."
            )
        elif m.domain == "seg" and m.dim_type and m.dim_name:
            out[m.token()] = seg_definition(m.dim_type, m.dim_name, m.key)
    return out


def attach_catalog_titles(
    catalog: dict[str, list[dict[str, object]]],
    *,
    db_path: Path | None,
    tickers: Iterable[str] = (),
) -> None:
    """Decorate metric_catalog entries in place with a ``title`` field — the
    picker <option> tooltips."""
    kpi_names = [str(e.get("label") or "") for e in catalog.get("kpi", [])]
    kpi_titles = kpi_definition_titles(db_path, kpi_names, tickers)
    for entry in catalog.get("fin", []):
        entry["title"] = fin_definition(str(entry.get("label") or ""))
    for entry in catalog.get("kpi", []):
        name = str(entry.get("label") or "")
        entry["title"] = kpi_titles.get(name, f"Company KPI '{name}'.")
    for entry in catalog.get("seg", []):
        token = str(entry.get("token") or "")
        parts = token.split(":")
        if len(parts) == 4:
            entry["title"] = seg_definition(parts[1], parts[2], parts[3])


__all__ = [
    "FIN_GLOSSARY",
    "attach_catalog_titles",
    "fin_definition",
    "kpi_definition_titles",
    "metric_definitions",
    "seg_definition",
]
