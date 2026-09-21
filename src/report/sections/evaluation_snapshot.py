"""Canonical quick-categorization snapshot for evaluation reports."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from report.models import EvaluationSnapshotSection, QuickCategorizationRow, SectionStatus
from report.sections._common import missing, open_repo_db
from sources.discovery_market import read_market_context
from sources.report_financials import (
    FinancialTableCell,
    FinancialTableProjection,
    read_financial_table,
)

_FRACTION_TO_PCT = 0.01


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
    as_of: datetime | None = None,
) -> EvaluationSnapshotSection:
    """Build from one admitted financial projection at one aware cutoff."""
    ticker = ticker.upper()
    cutoff = datetime.now(UTC) if as_of is None else as_of
    if cutoff.tzinfo is None:
        raise ValueError("evaluation snapshot cutoff must be timezone-aware")
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return _missing(ticker, "PERSIST(canonical_financials)", "Configure the canonical database")
    try:
        projection = read_financial_table(db_conn, ticker, as_of=cutoff)
        market = read_market_context(
            db_conn, repo_root / "data" / "historical" / "fmp", ticker, as_of=cutoff
        )
    finally:
        if conn is None:
            db_conn.close()
    rows, years, manifest, unavailable = _build_rows(projection)
    if not rows:
        return EvaluationSnapshotSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(canonical_financial_facts)",
                fix_command=f"python execution/onboard_ticker.py --ticker {ticker}",
                detail="No admitted annual financial cells are available at the report cutoff.",
            ),
            ticker=ticker,
            company_name=market.name,
            sector=market.sector,
            canonical_financial_table=projection,
            market_context=market,
        )
    return EvaluationSnapshotSection(
        status=SectionStatus.OK,
        ticker=ticker,
        company_name=market.name,
        sector=market.sector,
        market_cap=float(market.market_cap)
        if market.status == "available" and market.market_cap is not None
        else None,
        current_price=float(market.price)
        if market.status == "available" and market.price is not None
        else None,
        rows=rows,
        fiscal_years=years,
        canonical_financial_table=projection,
        source_manifest=manifest,
        unavailable_reasons=unavailable,
        market_context=market,
    )


def _build_rows(
    projection: FinancialTableProjection,
) -> tuple[
    list[QuickCategorizationRow],
    list[int],
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
]:
    annual = _annual_cells(projection.cells)
    all_years = sorted({int(cell.display_coordinate) for cell in annual if cell.display_coordinate})
    if not all_years:
        return [], [], {}, {}
    latest_year = all_years[-1]
    # Calendar labels are not continuity evidence. Keep fixed display slots so
    # a sparse history cannot shift a lone FY into the LFY-2 column.
    years = [latest_year - 2, latest_year - 1, latest_year]
    annual_by_concept = _by_coordinate(annual)
    quarterly_by_concept = _by_coordinate(_quarterly_cells(projection.cells))
    currency = _currency(annual) or "Currency"
    manifest: dict[str, tuple[str, ...]] = {}
    unavailable: dict[str, tuple[str, ...]] = {}
    return (
        [
            _absolute_row(
                "Revenue",
                f"{currency} M",
                0,
                "revenue",
                years,
                annual_by_concept,
                quarterly_by_concept,
                manifest,
                unavailable,
            ),
            _absolute_row(
                "EPS diluted",
                currency,
                2,
                "eps_diluted",
                years,
                annual_by_concept,
                quarterly_by_concept,
                manifest,
                unavailable,
                allow_ttm=False,
            ),
            _margin_row(
                "Operating margin",
                "operating_income",
                years,
                annual_by_concept,
                quarterly_by_concept,
                manifest,
                unavailable,
            ),
            _margin_row(
                "FCF margin",
                "free_cash_flow",
                years,
                annual_by_concept,
                quarterly_by_concept,
                manifest,
                unavailable,
            ),
            QuickCategorizationRow(metric="ROE", unit="%", digits=1),
        ],
        years,
        manifest,
        {
            **unavailable,
            **{
                f"ROE {coordinate}": ("canonical_equity_fact_unavailable",)
                for coordinate in (*[f"FY{year}" for year in years], "TTM", "3y CAGR")
            },
        },
    )


def _annual_cells(cells: Iterable[FinancialTableCell]) -> list[FinancialTableCell]:
    return [cell for cell in cells if cell.available and cell.cadence == "annual"]


def _quarterly_cells(cells: Iterable[FinancialTableCell]) -> list[FinancialTableCell]:
    return [cell for cell in cells if cell.available and cell.cadence == "quarterly"]


def _by_coordinate(cells: Iterable[FinancialTableCell]) -> dict[str, dict[str, FinancialTableCell]]:
    result: dict[str, dict[str, FinancialTableCell]] = {}
    for cell in cells:
        if cell.display_coordinate is not None:
            result.setdefault(cell.concept, {})[cell.display_coordinate] = cell
    return result


def _absolute_row(
    metric: str,
    unit: str,
    digits: int,
    concept: str,
    years: list[int],
    annual: dict[str, dict[str, FinancialTableCell]],
    quarterly: dict[str, dict[str, FinancialTableCell]],
    manifest: dict[str, tuple[str, ...]],
    unavailable: dict[str, tuple[str, ...]],
    *,
    allow_ttm: bool = True,
) -> QuickCategorizationRow:
    cells = annual.get(concept, {})
    values = [_cell_value(cells.get(str(year))) for year in years]
    ids = [_cell_id(cells.get(str(year))) for year in years]
    for year, cell_id in zip(years, ids, strict=True):
        if cell_id:
            manifest[f"{metric} FY{year}"] = (cell_id,)
        else:
            unavailable[f"{metric} FY{year}"] = _coordinate_reasons(cells.get(str(year)))
    baseline_year = years[0] - 1 if len(years) == 3 else None
    baseline = _cell_value(cells.get(str(baseline_year))) if baseline_year is not None else None
    cagr = (
        _cagr_3y(baseline, values[-1])
        if _annual_span_supported(cells, baseline_year, years[-1])
        else None
    )
    baseline_id = _cell_id(cells.get(str(baseline_year)))
    if cagr is not None and baseline_id and ids[-1]:
        manifest[f"{metric} 3y CAGR"] = (baseline_id, ids[-1])
    else:
        unavailable[f"{metric} 3y CAGR"] = ("four_contiguous_compatible_fiscal_years_unavailable",)
    ttm, ttm_ids = _ttm_sum(quarterly.get(concept, {})) if allow_ttm else (None, ())
    if ttm_ids:
        manifest[f"{metric} TTM"] = ttm_ids
    else:
        unavailable[f"{metric} TTM"] = (
            "four_comparable_contiguous_quarters_unavailable"
            if allow_ttm
            else "per_share_ttm_requires_weighted_share_count",
        )
    return QuickCategorizationRow(
        metric=metric,
        unit=unit,
        digits=digits,
        lfy_minus_2=values[0] if values else None,
        lfy_minus_1=values[1] if len(values) > 1 else None,
        lfy=values[2] if len(values) > 2 else None,
        ttm=ttm,
        cagr_3y=cagr,
        source_cell_ids=tuple(value for value in (*ids, baseline_id, *ttm_ids) if value),
    )


def _margin_row(
    metric: str,
    numerator: str,
    years: list[int],
    annual: dict[str, dict[str, FinancialTableCell]],
    quarterly: dict[str, dict[str, FinancialTableCell]],
    manifest: dict[str, tuple[str, ...]],
    unavailable: dict[str, tuple[str, ...]],
) -> QuickCategorizationRow:
    values: list[float | None] = []
    all_ids: list[str] = []
    for year in years:
        value, ids = _ratio(
            annual.get(numerator, {}).get(str(year)), annual.get("revenue", {}).get(str(year))
        )
        values.append(value)
        if ids:
            manifest[f"{metric} FY{year}"] = ids
            all_ids.extend(ids)
        else:
            unavailable[f"{metric} FY{year}"] = _pair_reasons(
                annual.get(numerator, {}).get(str(year)),
                annual.get("revenue", {}).get(str(year)),
            )
    ttm, ttm_ids = _ttm_ratio(quarterly.get(numerator, {}), quarterly.get("revenue", {}))
    if ttm_ids:
        manifest[f"{metric} TTM"] = ttm_ids
    else:
        unavailable[f"{metric} TTM"] = (
            "matched_four_quarter_numerator_and_revenue_window_unavailable",
        )
    unavailable[f"{metric} 3y CAGR"] = ("ratio_cagr_not_meaningful",)
    return QuickCategorizationRow(
        metric=metric,
        unit="%",
        digits=1,
        lfy_minus_2=values[0] if values else None,
        lfy_minus_1=values[1] if len(values) > 1 else None,
        lfy=values[2] if len(values) > 2 else None,
        ttm=ttm,
        source_cell_ids=tuple((*all_ids, *ttm_ids)),
    )


def _ratio(
    numerator: FinancialTableCell | None, denominator: FinancialTableCell | None
) -> tuple[float | None, tuple[str, ...]]:
    if not _ratio_pair_supported(numerator, denominator):
        return None, ()
    value = _ratio_values(_cell_value(numerator), _cell_value(denominator))
    if value is None:
        return None, ()
    return value, tuple(value for value in (_cell_id(numerator), _cell_id(denominator)) if value)


def _ratio_values(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator / _FRACTION_TO_PCT


def _ttm_sum(cells: dict[str, FinancialTableCell]) -> tuple[float | None, tuple[str, ...]]:
    ordered = sorted(
        cells.values(),
        key=lambda cell: cell.provenance.observation.period_end if cell.provenance else date.min,
    )[-4:]
    if (
        len(ordered) != 4
        or not _quarter_span_supported(ordered)
        or not _series_context_supported(ordered)
    ):
        return None, ()
    values = [_cell_value(cell) for cell in ordered]
    if any(value is None for value in values):
        return None, ()
    return sum(value for value in values if value is not None), tuple(
        cell.canonical_metric_cell_id for cell in ordered
    )


def _ttm_ratio(
    numerators: dict[str, FinancialTableCell], denominators: dict[str, FinancialTableCell]
) -> tuple[float | None, tuple[str, ...]]:
    numerator_cells = sorted(numerators.values(), key=lambda cell: cell.display_coordinate or "")[
        -4:
    ]
    denominator_cells = sorted(
        denominators.values(), key=lambda cell: cell.display_coordinate or ""
    )[-4:]
    if len(numerator_cells) != 4 or len(denominator_cells) != 4:
        return None, ()
    if any(
        left.display_coordinate != right.display_coordinate
        or not _ratio_pair_supported(left, right)
        for left, right in zip(numerator_cells, denominator_cells, strict=True)
    ):
        return None, ()
    numerator, numerator_ids = _ttm_sum(
        {cell.display_coordinate or "": cell for cell in numerator_cells}
    )
    denominator, denominator_ids = _ttm_sum(
        {cell.display_coordinate or "": cell for cell in denominator_cells}
    )
    ratio = _ratio_values(numerator, denominator)
    return (ratio, (*numerator_ids, *denominator_ids)) if ratio is not None else (None, ())


def _quarter_span_supported(cells: list[FinancialTableCell]) -> bool:
    periods = [cell.provenance.observation for cell in cells if cell.provenance]
    bounds: list[tuple[datetime, datetime]] = []
    for item in periods:
        if item.period_start is None:
            return False
        bounds.append((item.period_start, item.period_end))
    if len(bounds) != 4:
        return False
    return (
        all(70 <= (end - start).days + 1 <= 105 for start, end in bounds)
        and all(
            later_start == earlier_end + timedelta(days=1)
            for (_, earlier_end), (later_start, _) in pairwise(bounds)
        )
        and 345 <= (bounds[-1][1] - bounds[0][0]).days + 1 <= 385
    )


def _annual_span_supported(
    cells: dict[str, FinancialTableCell], baseline_year: int | None, end_year: int
) -> bool:
    if baseline_year is None:
        return False
    chosen = [cells.get(str(year)) for year in range(baseline_year, end_year + 1)]
    if any(cell is None or cell.provenance is None for cell in chosen):
        return False
    ends = [cell.provenance.observation.period_end for cell in chosen if cell and cell.provenance]
    return _series_context_supported([cell for cell in chosen if cell is not None]) and all(
        345 <= (later - earlier).days <= 385 for earlier, later in pairwise(ends)
    )


def _series_context_supported(cells: list[FinancialTableCell]) -> bool:
    if not cells or any(cell.provenance is None for cell in cells):
        return False
    contexts = {
        (
            cell.concept,
            cell.metric_id,
            cell.metric_definition_revision_id,
            cell.provenance.cell.reporting_entity_id,
            cell.provenance.cell.accounting_basis,
            cell.provenance.cell.consolidation_scope,
            cell.provenance.observation.currency,
            cell.provenance.observation.unit_key,
        )
        for cell in cells
        if cell.provenance is not None
    }
    return len(contexts) == 1


def _ratio_pair_supported(
    numerator: FinancialTableCell | None, denominator: FinancialTableCell | None
) -> bool:
    if (
        numerator is None
        or denominator is None
        or numerator.provenance is None
        or denominator.provenance is None
    ):
        return False
    left, right = numerator.provenance, denominator.provenance
    return (
        left.observation.period_start == right.observation.period_start
        and left.observation.period_end == right.observation.period_end
        and left.cell.reporting_entity_id == right.cell.reporting_entity_id
        and left.cell.accounting_basis == right.cell.accounting_basis
        and left.cell.consolidation_scope == right.cell.consolidation_scope
        and left.observation.currency == right.observation.currency
    )


def _currency(cells: list[FinancialTableCell]) -> str | None:
    currencies = {
        cell.provenance.observation.currency
        for cell in cells
        if cell.provenance is not None and cell.provenance.observation.currency is not None
    }
    return next(iter(currencies)) if len(currencies) == 1 else None


def _cell_value(cell: FinancialTableCell | None) -> float | None:
    return None if cell is None or cell.display_value is None else float(cell.display_value)


def _cell_id(cell: FinancialTableCell | None) -> str | None:
    return None if cell is None else cell.canonical_metric_cell_id


def _coordinate_reasons(cell: FinancialTableCell | None) -> tuple[str, ...]:
    if cell is None:
        return ("canonical_cell_unavailable",)
    return cell.reason_codes or ("canonical_cell_value_unavailable",)


def _pair_reasons(
    numerator: FinancialTableCell | None, denominator: FinancialTableCell | None
) -> tuple[str, ...]:
    reasons = (*_coordinate_reasons(numerator), *_coordinate_reasons(denominator))
    if (
        numerator is not None
        and denominator is not None
        and not _ratio_pair_supported(numerator, denominator)
    ):
        reasons = (*reasons, "period_scope_basis_or_currency_mismatch")
    return tuple(dict.fromkeys(reasons))


def _missing(ticker: str, stage: str, fix_command: str) -> EvaluationSnapshotSection:
    return EvaluationSnapshotSection(
        status=SectionStatus.MISSING_DATA,
        missing=missing(
            stage=stage, fix_command=fix_command, detail="Canonical financial data unavailable."
        ),
        ticker=ticker,
    )


def _cagr_3y(baseline: float | None, end: float | None) -> float | None:
    return (
        None
        if baseline is None or end is None or baseline <= 0 or end <= 0
        else (end / baseline) ** (1.0 / 3.0) - 1.0
    )
