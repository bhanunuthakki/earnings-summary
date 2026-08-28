"""Validation engine: walk every fact table and emit `validation_issues` rows.

Three rule families today (the ValidationRule enum lists the closed set):

  - PLAUSIBLE_RANGE: per-line-item sanity bounds (e.g., op margin in [-100, 100],
    revenue >= 0, headcount > 0). Fires on financial_facts and kpi_facts.
  - MAGNITUDE_JUMP: same (ticker, line_item, fiscal_period_type) sequential
    values that jump implausibly signal a likely unit error or restatement.
    Two passes: income-statement flows (revenue/operating_income/net_income)
    at >5x, and balance-sheet stocks (cash, total assets/liabilities, current
    assets/liabilities, total debt) at a tighter >3x — levels are stickier
    than flows. Equity and net_debt are excluded (both cross zero).
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
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from models.facts import Unit
from models.validation import Severity, ValidationRule
from pipeline.kpi_persistence import record_validation_issue
from pipeline.kpi_semantic_scope import scoped_kpi_definitions


@dataclass(frozen=True)
class _RangeBound:
    """Min/max sanity bounds for one line_item."""

    min_value: Decimal | None
    max_value: Decimal | None
    halt_threshold_multiplier: Decimal = Decimal(1000)


def _sqlite_period_text(value: object) -> str:
    """Normalize a validated SQLite date value without trusting row typing."""
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Unsupported SQLite period value: {type(value).__name__}")


def _sqlite_int(value: object) -> int:
    """Return an integer only after validating the SQLite boundary value."""
    if not isinstance(value, int):
        raise TypeError(f"Expected SQLite integer, got {type(value).__name__}")
    return value


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
    Unit.PERCENT: _KpiRangeBound(
        unit=Unit.PERCENT, min_value=Decimal(-1000), max_value=Decimal(1000)
    ),
    Unit.RATIO: _KpiRangeBound(unit=Unit.RATIO, min_value=Decimal(-100), max_value=Decimal(100)),
    Unit.BPS: _KpiRangeBound(
        unit=Unit.BPS, min_value=Decimal(-100_000), max_value=Decimal(100_000)
    ),
    Unit.COUNT: _KpiRangeBound(
        unit=Unit.COUNT, min_value=Decimal(0), max_value=Decimal(10_000_000_000)
    ),
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
                conn,
                run_id=run_id,
                source_doc_id=int(row["source_doc_id"]),
                ticker=row["ticker"],
                severity=Severity.WARN,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{row['line_item']}={value}",
                expected=f">= {bound.min_value}",
            )
            inserted += 1
        elif bound.max_value is not None and value > bound.max_value:
            record_validation_issue(
                conn,
                run_id=run_id,
                source_doc_id=int(row["source_doc_id"]),
                ticker=row["ticker"],
                severity=Severity.WARN,
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
                conn,
                run_id=run_id,
                source_doc_id=int(row["source_doc_id"]),
                ticker=row["ticker"],
                severity=severity,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{row['name']}={value} {unit.value}",
                expected=f"[{bound.min_value}, {bound.max_value}]",
            )
            inserted += 1
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.PLAUSIBLE_RANGE, issues_inserted=inserted, rows_examined=len(rows)
    )


# Income-statement flows: a >5x quarter-over-quarter (or year-over-year within
# a cadence bucket) swing is the classic unit error (thousands vs millions) or
# a restatement worth surfacing.
_MAGNITUDE_JUMP_INCOME_ITEMS: tuple[str, ...] = ("revenue", "operating_income", "net_income")
_MAGNITUDE_JUMP_INCOME_MULTIPLIER = Decimal(5)

# Balance-sheet stocks: levels are far stickier than P&L flows, so a tighter
# >3x jump is already suspicious (e.g. an FMP cash row spiking one quarter well
# above its run-rate — MELI's cash_and_equivalents 4.1x case). Equity and
# net_debt are deliberately EXCLUDED: both legitimately cross zero (WIX carries
# negative book equity), which would detonate the magnitude ratio into false
# positives.
_MAGNITUDE_JUMP_BALANCE_ITEMS: tuple[str, ...] = (
    "cash_and_equivalents",
    "cash_and_short_term_investments",
    "total_assets",
    "total_liabilities",
    "total_current_assets",
    "total_current_liabilities",
    "total_debt",
)
_MAGNITUDE_JUMP_BALANCE_MULTIPLIER = Decimal(3)


def _scan_series_for_jumps(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str | None,
    line_items: tuple[str, ...],
    multiplier: Decimal,
) -> tuple[int, int]:
    """Scan financial_facts for `line_items`, grouping into
    (ticker, line_item, fiscal_period_type) series ordered by period_end ASC,
    and emit a MAGNITUDE_JUMP issue whenever two sequential absolute values
    differ by more than `multiplier`×. Returns (issues_inserted, rows_examined).

    Zero-crossing safety: values are compared on absolute magnitude and any
    step touching 0 is skipped, so this must only be handed line_items whose
    sign is stable (never equity or net_debt, which cross zero).
    """
    if not line_items:
        return (0, 0)
    placeholders = ",".join("?" for _ in line_items)
    sql = (
        f"SELECT ticker, line_item, fiscal_period_type, period_end, value, source_doc_id "
        f"FROM financial_facts WHERE line_item IN ({placeholders})"
    )
    params: tuple[str, ...] = line_items
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
                pe_str = _sqlite_period_text(curr["period_end"])
                record_validation_issue(
                    conn,
                    run_id=run_id,
                    source_doc_id=_sqlite_int(curr["source_doc_id"]),
                    ticker=str(curr["ticker"]),
                    severity=Severity.WARN,
                    rule=ValidationRule.MAGNITUDE_JUMP,
                    raw_value=(
                        f"{curr['line_item']} prior={prev_val} current={curr_val} "
                        f"(ratio={ratio:.1f}x) at {pe_str}"
                    ),
                    expected=f"sequential ratio <= {multiplier}x",
                )
                inserted += 1
    return inserted, examined


def _check_magnitude_jumps(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str | None,
) -> CheckOutcome:
    """Insert MAGNITUDE_JUMP issues for sequential same-key values that jump
    implausibly, in two passes over financial_facts:

      - income-statement flows (revenue/operating_income/net_income) at >5x,
      - balance-sheet stocks (cash, total assets/liabilities, current
        assets/liabilities, total debt) at a tighter >3x — levels are stickier
        than flows, so a smaller jump is already suspicious. This is what
        catches an FMP cash row spiking one quarter (MELI's cash 4.1x above its
        run-rate) that the income-only 5x pass never looked at.

    Both passes key on (ticker, line_item, fiscal_period_type) series ordered
    by period_end ASC. Equity and net_debt are intentionally out of scope: both
    legitimately cross zero, which would blow the magnitude ratio into false
    positives. Everything is WARN — this flags, it never drops data.
    """
    income_inserted, income_examined = _scan_series_for_jumps(
        conn,
        run_id=run_id,
        ticker=ticker,
        line_items=_MAGNITUDE_JUMP_INCOME_ITEMS,
        multiplier=_MAGNITUDE_JUMP_INCOME_MULTIPLIER,
    )
    balance_inserted, balance_examined = _scan_series_for_jumps(
        conn,
        run_id=run_id,
        ticker=ticker,
        line_items=_MAGNITUDE_JUMP_BALANCE_ITEMS,
        multiplier=_MAGNITUDE_JUMP_BALANCE_MULTIPLIER,
    )
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.MAGNITUDE_JUMP,
        issues_inserted=income_inserted + balance_inserted,
        rows_examined=income_examined + balance_examined,
    )


def _check_source_disagreement(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str | None,
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
            if st not in per_source_type or _sqlite_int(e["source_doc_id"]) > _sqlite_int(
                per_source_type[st]["source_doc_id"]
            ):
                per_source_type[st] = e
        reps = list(per_source_type.values())
        for i, a in enumerate(reps):
            for b in reps[i + 1 :]:
                a_val = abs(Decimal(str(a["value"])))
                b_val = abs(Decimal(str(b["value"])))
                if max(a_val, b_val) == 0:
                    continue
                diff_pct = abs(a_val - b_val) / max(a_val, b_val) * Decimal(100)
                if diff_pct > tolerance_pct:
                    pe_str = _sqlite_period_text(a["period_end"])
                    record_validation_issue(
                        conn,
                        run_id=run_id,
                        source_doc_id=_sqlite_int(a["source_doc_id"]),
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
        issues_inserted=inserted,
        rows_examined=examined,
    )


def _check_kpi_semantic_coverage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str | None,
) -> CheckOutcome:
    """HALT when an owner-visible KPI lacks admitted source semantics."""
    repo_root = Path(__file__).resolve().parents[2]
    rows = scoped_kpi_definitions(conn, repo_root=repo_root)
    if ticker is not None:
        rows = tuple(row for row in rows if row.ticker == ticker.upper())
    inserted = 0
    examined = 0
    for row in rows:
        examined += row.fact_count
        unresolved = row.kpi_definition_id is None
        if not (
            unresolved
            or row.missing_context_count
            or row.quarantined_context_count
            or row.legacy_unknown_context_count
        ):
            continue
        source_doc = None
        if row.kpi_definition_id is not None:
            source = conn.execute(
                "SELECT source_doc_id FROM kpi_facts WHERE kpi_definition_id=? "
                "ORDER BY id DESC LIMIT 1",
                (row.kpi_definition_id,),
            ).fetchone()
            source_doc = int(source[0]) if source is not None else None
        record_validation_issue(
            conn,
            run_id=run_id,
            source_doc_id=source_doc,
            ticker=row.ticker,
            severity=Severity.HALT,
            rule=ValidationRule.KPI_SEMANTIC_CONTEXT,
            raw_value=(
                f"{row.name}: missing={row.missing_context_count}, "
                f"quarantined={row.quarantined_context_count}, "
                f"legacy_unknown={row.legacy_unknown_context_count}, "
                f"unresolved={unresolved}"
            ),
            expected="every report/Facts & Metrics KPI fact has admitted source-bound semantics",
        )
        inserted += 1
    conn.commit()
    return CheckOutcome(
        rule=ValidationRule.KPI_SEMANTIC_CONTEXT,
        issues_inserted=inserted,
        rows_examined=examined,
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
    outcomes.append(_check_kpi_semantic_coverage(conn, run_id=run_id, ticker=ticker))
    return ValidationReport(
        run_id=run_id,
        started_at=started_at,
        ended_at=datetime.now(),
        outcomes=tuple(outcomes),
    )
