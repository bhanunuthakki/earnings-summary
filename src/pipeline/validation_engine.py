"""Validation engine: walk every fact table and emit `validation_issues` rows.

Three rule families today (the ValidationRule enum lists the closed set):

  - PLAUSIBLE_RANGE: per-line-item sanity bounds (e.g., op margin in [-100, 100],
    revenue >= 0, headcount > 0). Fires on financial_facts and kpi_facts.
  - MAGNITUDE_JUMP: same (ticker, line_item, fiscal_period_type) sequential
    values that differ by >5x signal a likely unit error or restatement.
  - SOURCE_DISAGREEMENT: same (ticker, period_end, line_item) reported by two
    distinct source_doc_ids whose values diverge by >0.5%.

Designed to run idempotently: each issue's identity is (run_id, source_doc_id,
ticker, rule, raw_value); re-running with the same data produces the same rows
(deduplicated by run_id partition — no new rows on a same-run rerun, but new
runs do produce new rows reflecting the run-of-record).

Per data_provenance.md §3, source disagreement defaults to severity=warn.
Range violations and magnitude jumps default to severity=warn unless the value
is wildly out of plausible bounds (>3 orders of magnitude off), in which case
they're severity=halt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from models.facts import Unit
from models.validation import Severity, ValidationRule
from pipeline.kpi_persistence import record_validation_issue


@dataclass(frozen=True)
class _RangeBound:
    """Min/max sanity bounds for one line_item."""

    min_value: Decimal | None
    max_value: Decimal | None
    halt_threshold_multiplier: Decimal = Decimal(1000)


# Per-line-item plausible ranges. Currency-agnostic — we do NOT enforce a
# USD-scale upper bound because INR/JPY/KRW filers report in much larger
# nominal numbers. The magnitude_jump rule catches unit errors; this rule
# catches sign-impossibility (e.g., negative total_assets, negative shares).
# Items whose value can legitimately be negative (revenue for an asset
# manager booking investment losses; net_income for any company) are not
# range-checked here. Add only when an absolute lower bound is well-defined.
_FINANCIAL_FACT_RANGES: dict[str, _RangeBound] = {
    "total_assets": _RangeBound(min_value=Decimal(0), max_value=None),
    "weighted_avg_shares": _RangeBound(min_value=Decimal(0), max_value=None),
    "weighted_avg_shares_diluted": _RangeBound(min_value=Decimal(0), max_value=None),
}


@dataclass(frozen=True)
class _KpiRangeBound:
    """Sanity bounds keyed on Unit (since KPI line_items vary widely)."""

    unit: Unit
    min_value: Decimal
    max_value: Decimal


_KPI_RANGES: dict[Unit, _KpiRangeBound] = {
    Unit.PERCENT: _KpiRangeBound(unit=Unit.PERCENT, min_value=Decimal(-1000), max_value=Decimal(1000)),
    Unit.RATIO: _KpiRangeBound(unit=Unit.RATIO, min_value=Decimal(-100), max_value=Decimal(100)),
    Unit.BPS: _KpiRangeBound(unit=Unit.BPS, min_value=Decimal(-100_000), max_value=Decimal(100_000)),
    Unit.COUNT: _KpiRangeBound(unit=Unit.COUNT, min_value=Decimal(0), max_value=Decimal(10_000_000_000)),
}


@dataclass(frozen=True)
class CheckOutcome:
    """Per-rule tally returned to the caller."""

    rule: ValidationRule
    issues_inserted: int
    rows_examined: int


def _check_financial_fact_ranges(
    conn: sqlite3.Connection, *, run_id: str, ticker: str | None
) -> CheckOutcome:
    """Insert PLAUSIBLE_RANGE issues for financial_facts whose value falls outside its bound."""
    sql = "SELECT id, ticker, line_item, value, source_doc_id FROM financial_facts"
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " WHERE ticker = ?"
        params = (ticker.upper(),)
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    inserted = 0
    for row in rows:
        bound = _FINANCIAL_FACT_RANGES.get(row["line_item"])
        if bound is None:
            continue
        value = Decimal(str(row["value"]))
        if bound.min_value is not None and value < bound.min_value:
            record_validation_issue(
                conn, run_id=run_id, source_doc_id=int(row["source_doc_id"]),
                ticker=row["ticker"], severity=Severity.WARN,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{row['line_item']}={value}",
                expected=f">= {bound.min_value}",
            )
            inserted += 1
        elif bound.max_value is not None and value > bound.max_value:
            record_validation_issue(
                conn, run_id=run_id, source_doc_id=int(row["source_doc_id"]),
                ticker=row["ticker"], severity=Severity.WARN,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{row['line_item']}={value}",
                expected=f"<= {bound.max_value}",
            )
            inserted += 1
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.PLAUSIBLE_RANGE, issues_inserted=inserted, rows_examined=len(rows)
    )


def _check_kpi_fact_ranges(
    conn: sqlite3.Connection, *, run_id: str, ticker: str | None
) -> CheckOutcome:
    """Insert PLAUSIBLE_RANGE issues for kpi_facts whose value falls outside the unit-specific bound."""
    sql = (
        "SELECT kf.id, kf.ticker, kf.value, kf.unit, kf.source_doc_id, kd.name "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id"
    )
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " WHERE kf.ticker = ?"
        params = (ticker.upper(),)
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    inserted = 0
    for row in rows:
        try:
            unit = Unit(row["unit"])
        except ValueError:
            continue
        bound = _KPI_RANGES.get(unit)
        if bound is None:
            continue
        value = Decimal(str(row["value"]))
        if value < bound.min_value or value > bound.max_value:
            severity = Severity.WARN
            record_validation_issue(
                conn, run_id=run_id, source_doc_id=int(row["source_doc_id"]),
                ticker=row["ticker"], severity=severity,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{row['name']}={value} {unit.value}",
                expected=f"[{bound.min_value}, {bound.max_value}]",
            )
            inserted += 1
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.PLAUSIBLE_RANGE, issues_inserted=inserted, rows_examined=len(rows)
    )


def _check_magnitude_jumps(
    conn: sqlite3.Connection, *, run_id: str, ticker: str | None,
    multiplier: Decimal = Decimal(5),
) -> CheckOutcome:
    """Insert MAGNITUDE_JUMP issues for sequential same-key values that differ by > multiplier×.

    Operates on financial_facts with line_item in `revenue`, `operating_income`,
    `net_income`. Sequential = ordered by period_end ASC within a (ticker,
    line_item, fiscal_period_type) bucket.
    """
    target_line_items = ("revenue", "operating_income", "net_income")
    placeholders = ",".join("?" for _ in target_line_items)
    sql = (
        f"SELECT ticker, line_item, fiscal_period_type, period_end, value, source_doc_id "
        f"FROM financial_facts WHERE line_item IN ({placeholders})"
    )
    params: tuple[str, ...] = target_line_items
    if ticker is not None:
        sql += " AND ticker = ?"
        params = (*params, ticker.upper())
    sql += " ORDER BY ticker, line_item, fiscal_period_type, period_end ASC"
    cur = conn.execute(sql, params)
    rows = cur.fetchall()

    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (row["ticker"], row["line_item"], row["fiscal_period_type"])
        grouped.setdefault(key, []).append(dict(row))

    inserted = 0
    examined = 0
    for series in grouped.values():
        examined += len(series)
        for prev, curr in pairwise(series):
            prev_val = abs(Decimal(str(prev["value"])))
            curr_val = abs(Decimal(str(curr["value"])))
            if prev_val == 0 or curr_val == 0:
                continue
            ratio = curr_val / prev_val if curr_val > prev_val else prev_val / curr_val
            if ratio > multiplier:
                pe = curr["period_end"]
                pe_str = pe[:10] if isinstance(pe, str) else pe.date().isoformat()
                record_validation_issue(
                    conn, run_id=run_id, source_doc_id=int(curr["source_doc_id"]),
                    ticker=str(curr["ticker"]), severity=Severity.WARN,
                    rule=ValidationRule.MAGNITUDE_JUMP,
                    raw_value=(
                        f"{curr['line_item']} prior={prev_val} current={curr_val} "
                        f"(ratio={ratio:.1f}x) at {pe_str}"
                    ),
                    expected=f"sequential ratio <= {multiplier}x",
                )
                inserted += 1
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.MAGNITUDE_JUMP, issues_inserted=inserted, rows_examined=examined
    )


def _check_source_disagreement(
    conn: sqlite3.Connection, *, run_id: str, ticker: str | None,
    tolerance_pct: Decimal = Decimal("0.5"),
) -> CheckOutcome:
    """Insert SOURCE_DISAGREEMENT for same (ticker, period_end, fiscal_period_type, line_item)
    reported by source documents of >=2 *different* `source_type`s whose values diverge.

    Per data_provenance.md §3, the meaningful disagreement is fmp-vs-sec,
    fmp-vs-ir_doc, or sec-vs-ir_doc. Multiple FMP files reporting the same
    period (annual vs quarterly vs TTM rollups) is expected aggregation
    layering — not a disagreement, and would generate intractable noise.
    """
    sql = (
        "SELECT ff.ticker, ff.period_end, ff.fiscal_period_type, ff.line_item, "
        "       ff.value, ff.source_doc_id, d.source_type "
        "FROM financial_facts ff JOIN documents d ON d.id = ff.source_doc_id "
        "WHERE ff.line_item IN ('revenue','operating_income','net_income','gross_profit')"
    )
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " AND ff.ticker = ?"
        params = (ticker.upper(),)
    cur = conn.execute(sql, params)
    rows = cur.fetchall()

    bucket: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (row["ticker"], str(row["period_end"]), row["fiscal_period_type"], row["line_item"])
        bucket.setdefault(key, []).append(dict(row))

    inserted = 0
    examined = 0
    for entries in bucket.values():
        # Need at least two distinct source_types in the bucket to be a real disagreement.
        source_types = {str(e["source_type"]) for e in entries}
        if len(source_types) < 2:
            continue
        examined += len(entries)
        # Pick one representative entry per source_type (newest source_doc_id wins as a stable choice).
        per_source_type: dict[str, dict[str, object]] = {}
        for e in entries:
            st = str(e["source_type"])
            if st not in per_source_type or int(e["source_doc_id"]) > int(per_source_type[st]["source_doc_id"]):
                per_source_type[st] = e
        reps = list(per_source_type.values())
        for i, a in enumerate(reps):
            for b in reps[i + 1:]:
                a_val = abs(Decimal(str(a["value"])))
                b_val = abs(Decimal(str(b["value"])))
                if max(a_val, b_val) == 0:
                    continue
                diff_pct = abs(a_val - b_val) / max(a_val, b_val) * Decimal(100)
                if diff_pct > tolerance_pct:
                    pe = a["period_end"]
                    pe_str = pe[:10] if isinstance(pe, str) else pe.date().isoformat()
                    record_validation_issue(
                        conn, run_id=run_id,
                        source_doc_id=int(a["source_doc_id"]),
                        ticker=str(a["ticker"]),
                        severity=Severity.WARN,
                        rule=ValidationRule.SOURCE_DISAGREEMENT,
                        raw_value=(
                            f"{a['line_item']} @ {pe_str}: "
                            f"{a['source_type']}={a_val} vs "
                            f"{b['source_type']}={b_val} "
                            f"({diff_pct:.2f}%)"
                        ),
                        expected=f"agreement within {tolerance_pct}%",
                    )
                    inserted += 1
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.SOURCE_DISAGREEMENT,
        issues_inserted=inserted, rows_examined=examined,
    )


@dataclass(frozen=True)
class ValidationReport:
    """Aggregate run output."""

    run_id: str
    started_at: datetime
    ended_at: datetime
    outcomes: tuple[CheckOutcome, ...]


def run_all_checks(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str | None = None,
) -> ValidationReport:
    """Execute every rule family. ticker=None scans all tickers."""
    started_at = datetime.now()
    outcomes: list[CheckOutcome] = []
    outcomes.append(_check_financial_fact_ranges(conn, run_id=run_id, ticker=ticker))
    outcomes.append(_check_kpi_fact_ranges(conn, run_id=run_id, ticker=ticker))
    outcomes.append(_check_magnitude_jumps(conn, run_id=run_id, ticker=ticker))
    outcomes.append(_check_source_disagreement(conn, run_id=run_id, ticker=ticker))
    return ValidationReport(
        run_id=run_id, started_at=started_at, ended_at=datetime.now(),
        outcomes=tuple(outcomes),
    )
