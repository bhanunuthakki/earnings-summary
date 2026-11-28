"""Unified nature-aware semantic qualification for atomic KPI ingestion.

Provides the single qualification builder shared by atomic manifest ingestion
(``persist_manifest(..., auto_admit=True)``) and batch semantic admission
(``pipeline.kpi_batch_admission``), plus workbook sheet validation for
``spreadsheet_cell`` locators.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from models.facts import FactLocator, LegacyEscapeHatch, LocatorKind, Unit
from models.kpis import KpiMetricNature
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiSourcePrecision,
    KpiUnitScale,
    current_kpi_semantic_context,
    normalize_source_numeric,
    parse_source_numeric,
    persist_kpi_semantic_context,
    validate_admitted_unit_scale,
)

DETERMINISTIC_ADMITTABLE_EXTRACTORS: Final[frozenset[str]] = frozenset(
    {
        "capture_xbrl_v1",
        "metrics_engine",
        "fmp",
        "fmp_derived",
        "kpi_transform_derived",
        "issuer_manifest_v1",
        "ir_spreadsheet",
        "ir_press_release_readout",
        "ir_presentation_readout",
        "sec_6k_extractor",
        "sec_fpi_ingest@1",
        "manual_filing_backfill",
    }
)

GAAP_FINANCIAL_EXTRACTORS: Final[frozenset[str]] = frozenset(
    {
        "capture_xbrl_v1",
        "metrics_engine",
        "fmp",
        "fmp_derived",
        "kpi_transform_derived",
    }
)

UNIT_TO_FIXED_SCALE: Final[dict[Unit, KpiUnitScale]] = {
    Unit.ACTUAL: KpiUnitScale.NONE,
    Unit.THOUSANDS: KpiUnitScale.THOUSANDS,
    Unit.MILLIONS: KpiUnitScale.MILLIONS,
    Unit.BILLIONS: KpiUnitScale.BILLIONS,
    Unit.PERCENT: KpiUnitScale.NONE,
    Unit.RATIO: KpiUnitScale.NONE,
    Unit.BPS: KpiUnitScale.NONE,
}

NON_GAAP_NAME_MARKERS: Final[tuple[str, ...]] = (
    "non-gaap",
    "non_gaap",
    "non gaap",
    "non-ifrs",
    "adjusted",
    "adj.",
    "adj ",
    "ebitda",
    "free cash flow",
    "fcf",
    "constant currency",
    "pro forma",
    "pro-forma",
)

_COUNT_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<token>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)"
    r"(?![0-9.,])(?!\s*%|-[0-9])"
    r"(?:\s*(?P<suffix>[KkMmBb](?:[MmNn])?|[Tt]housands?|[Mm]illions?|[Bb]illions?)\b)?"
)

_SUFFIX_SCALE_MAP: Final[dict[str, KpiUnitScale]] = {
    "k": KpiUnitScale.THOUSANDS,
    "thousand": KpiUnitScale.THOUSANDS,
    "thousands": KpiUnitScale.THOUSANDS,
    "m": KpiUnitScale.MILLIONS,
    "mm": KpiUnitScale.MILLIONS,
    "mn": KpiUnitScale.MILLIONS,
    "million": KpiUnitScale.MILLIONS,
    "millions": KpiUnitScale.MILLIONS,
    "b": KpiUnitScale.BILLIONS,
    "bn": KpiUnitScale.BILLIONS,
    "billion": KpiUnitScale.BILLIONS,
    "billions": KpiUnitScale.BILLIONS,
}


def normalize_reported_metric_label(raw: str) -> str:
    """Collapse newlines, tabs, and repeated whitespace to single spaces (<=256 chars)."""
    return " ".join(raw.split()).strip()[:256]


def infer_default_accounting_basis(metric_name: str, extractors: set[str]) -> KpiAccountingBasis:
    """Infer deterministic accounting basis from metric label and extractors."""
    lowered = metric_name.lower()
    if any(marker in lowered for marker in NON_GAAP_NAME_MARKERS):
        return KpiAccountingBasis.NON_GAAP
    if "gaap" in lowered:
        return KpiAccountingBasis.GAAP
    if extractors & GAAP_FINANCIAL_EXTRACTORS:
        return KpiAccountingBasis.GAAP
    return KpiAccountingBasis.MANAGEMENT


def match_count_source_value(
    persisted_value: Decimal, source_excerpt: str | None
) -> tuple[str, KpiUnitScale] | None:
    """Match a literal numeric token + explicit scale suffix in ``source_excerpt``.

    Note ``parse_source_numeric`` intentionally rejects ``K``/``M``/``B`` inside
    ``source_value_text``, so an excerpt like ``"8.6M"`` matches substring
    ``"8.6"`` paired with ``KpiUnitScale.MILLIONS``.
    """
    if source_excerpt is None or not source_excerpt.strip():
        return None

    matches: list[tuple[str, KpiUnitScale]] = []
    matched_scales: set[KpiUnitScale] = set()
    for found in _COUNT_TOKEN_PATTERN.finditer(source_excerpt):
        token = found.group("token")
        suffix = found.group("suffix")
        if suffix is None:
            scale = KpiUnitScale.NONE
        else:
            mapped = _SUFFIX_SCALE_MAP.get(suffix.lower())
            if mapped is None:
                continue
            scale = mapped

        candidates = [token]
        if persisted_value < 0:
            candidates = [f"-{token}", f"({token})", token]
        for candidate in candidates:
            if len(candidate) > 80 or candidate not in source_excerpt:
                continue
            try:
                parsed = parse_source_numeric(candidate)
                normalized = normalize_source_numeric(parsed, unit=Unit.COUNT, unit_scale=scale)
            except ValueError:
                continue
            if normalized == persisted_value:
                matches.append((candidate, scale))
                matched_scales.add(scale)
                break

    if not matches or len(matched_scales) > 1:
        return None
    return matches[0]


def _resolve_unit_scale_and_token(
    *,
    value: Decimal,
    unit: Unit,
    source_excerpt: str | None,
    source_value_text: str | None,
    unit_scale: KpiUnitScale | None,
    metric_nature: KpiMetricNature = KpiMetricNature.REPORTED_SCALAR,
) -> tuple[KpiUnitScale, str | None]:
    if unit is Unit.COUNT:
        if source_value_text is not None:
            if source_excerpt is None or source_value_text not in source_excerpt:
                raise ValueError("admitted COUNT source value text must occur in source_excerpt")
            resolved_scale = unit_scale
            if resolved_scale is None or resolved_scale is KpiUnitScale.UNKNOWN:
                matched = match_count_source_value(value, source_excerpt)
                if matched is not None and matched[0] == source_value_text:
                    resolved_scale = matched[1]
                else:
                    resolved_scale = KpiUnitScale.NONE
            validate_admitted_unit_scale(unit, resolved_scale)
            normalized = normalize_source_numeric(
                parse_source_numeric(source_value_text),
                unit=unit,
                unit_scale=resolved_scale,
            )
            if normalized != value:
                raise ValueError("admitted COUNT source_value_text does not match value")
            return resolved_scale, source_value_text
        matched = match_count_source_value(value, source_excerpt)
        if matched is not None:
            validate_admitted_unit_scale(unit, matched[1])
            return matched[1], matched[0]
        if metric_nature is not KpiMetricNature.REPORTED_SCALAR or (
            unit_scale is not None and unit_scale is not KpiUnitScale.UNKNOWN
        ):
            resolved_scale = (
                unit_scale
                if unit_scale is not None and unit_scale is not KpiUnitScale.UNKNOWN
                else KpiUnitScale.NONE
            )
            validate_admitted_unit_scale(unit, resolved_scale)
            return resolved_scale, None
        raise ValueError(
            "admitted COUNT extraction requires verifiable source_value_text in source_excerpt"
        )

    fixed_scale = UNIT_TO_FIXED_SCALE.get(unit)
    if fixed_scale is None:
        raise ValueError(f"unsupported KPI unit for atomic admission: {unit.value}")
    if unit_scale is not None and unit_scale is not KpiUnitScale.UNKNOWN:
        validate_admitted_unit_scale(unit, unit_scale)
        resolved_scale = unit_scale
    else:
        resolved_scale = fixed_scale
        validate_admitted_unit_scale(unit, resolved_scale)
    if source_value_text is not None and (
        source_excerpt is None or source_value_text not in source_excerpt
    ):
        raise ValueError("source_value_text must occur in source_excerpt")
    return resolved_scale, source_value_text


def build_atomic_semantic_context(
    *,
    metric_name: str,
    metric_name_as_reported: str | None = None,
    period_end: date,
    value: Decimal,
    unit: Unit,
    extracted_by: str,
    locator: FactLocator | LegacyEscapeHatch,
    source_excerpt: str | None = None,
    source_value_text: str | None = None,
    metric_nature: KpiMetricNature = KpiMetricNature.REPORTED_SCALAR,
    basis_note: str | None = None,
    accounting_basis: KpiAccountingBasis | None = None,
    consolidation_scope: KpiConsolidationScope | None = None,
    dimensions: dict[str, str] | None = None,
    unit_scale: KpiUnitScale | None = None,
    source_row_label: str | None = None,
    source_column_header: str | None = None,
    source_precision: KpiSourcePrecision | None = None,
) -> KpiSemanticContext:
    """Build an admitted ``KpiSemanticContext`` validated according to ``metric_nature``."""
    reported_label = normalize_reported_metric_label(metric_name_as_reported or metric_name)
    if not reported_label:
        raise ValueError("metric_name_as_reported cannot be empty")

    scope = consolidation_scope or KpiConsolidationScope.CONSOLIDATED
    if scope is KpiConsolidationScope.UNKNOWN:
        raise ValueError("consolidation_scope cannot be unknown for atomic admission")

    dims = dict(dimensions or {})
    if metric_nature is not KpiMetricNature.REPORTED_SCALAR:
        dims["metric_nature"] = metric_nature.value

    excerpt = source_excerpt.strip() if isinstance(source_excerpt, str) else None
    if excerpt == "":
        excerpt = None
    note = basis_note.strip() if isinstance(basis_note, str) else None
    if note == "":
        note = None

    resolved_precision = source_precision
    if metric_nature is KpiMetricNature.REPORTED_SCALAR:
        if not isinstance(locator, FactLocator):
            raise ValueError("reported_scalar atomic admission requires a concrete FactLocator")
        structured_deterministic = (
            extracted_by in DETERMINISTIC_ADMITTABLE_EXTRACTORS
            and locator.effective_kind()
            in {LocatorKind.DERIVED, LocatorKind.FMP_JSON_TABLE, LocatorKind.SPREADSHEET_CELL}
        )
        if (
            not structured_deterministic
            and locator.effective_kind() is not LocatorKind.DERIVED
            and excerpt is None
        ):
            raise ValueError("reported_scalar atomic admission requires non-empty source_excerpt")
    elif metric_nature is KpiMetricNature.INFERRED_EXTRAPOLATED:
        if note is None and excerpt is None:
            raise ValueError(
                "inferred_extrapolated atomic admission requires basis_note or source_excerpt"
            )
        qualifier = (note or excerpt or "inferred_extrapolated")[:512]
        resolved_precision = resolved_precision or KpiSourcePrecision(
            kind="approximate", qualifiers=(qualifier,)
        )
    elif metric_nature is KpiMetricNature.QUALITATIVE_OR_WEAKLY_QUANTIFIED:
        if excerpt is None:
            raise ValueError(
                "qualitative_or_weakly_quantified atomic admission requires non-empty source_excerpt"
            )
        qualifier = (note or excerpt)[:512]
        resolved_precision = resolved_precision or KpiSourcePrecision(
            kind="approximate", qualifiers=(qualifier,)
        )
    elif metric_nature is KpiMetricNature.BINARY_OR_ADHOC_MILESTONE and excerpt is None:
        raise ValueError(
            "binary_or_adhoc_milestone atomic admission requires non-empty source_excerpt"
        )

    if accounting_basis is not None and accounting_basis is not KpiAccountingBasis.UNKNOWN:
        resolved_basis = accounting_basis
    elif metric_nature is not KpiMetricNature.REPORTED_SCALAR:
        resolved_basis = KpiAccountingBasis.MANAGEMENT
    else:
        resolved_basis = infer_default_accounting_basis(reported_label, {extracted_by})

    resolved_scale, resolved_source_value = _resolve_unit_scale_and_token(
        value=value,
        unit=unit,
        source_excerpt=excerpt,
        source_value_text=source_value_text,
        unit_scale=unit_scale,
        metric_nature=metric_nature,
    )

    return KpiSemanticContext(
        metric_name_as_reported=reported_label,
        reported_period_end=period_end,
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=resolved_basis,
        consolidation_scope=scope,
        dimensions=dims,
        unit_scale=resolved_scale,
        source_row_label=source_row_label,
        source_column_header=source_column_header,
        source_value_text=resolved_source_value,
        source_precision=resolved_precision,
        status=KpiSemanticStatus.ADMITTED,
        reason_code=None,
    )


def ensure_deterministic_kpi_semantic_context(
    conn: sqlite3.Connection,
    *,
    kpi_fact_id: int,
    metric_name: str,
    period_end: date,
    value: Decimal,
    unit: Unit,
    extracted_by: str,
    locator: FactLocator | LegacyEscapeHatch | None = None,
    reviewed_by: str | None = None,
    source_excerpt: str | None = None,
    knowledge_at: datetime | None = None,
) -> int | None:
    """Stamp an ``ADMITTED`` semantic context if the fact has none or ``legacy_unknown``.

    Safe for both fresh inserts and idempotent replays. Never overrides an
    existing ``admitted`` or ``quarantined`` disposition.
    """
    existing = current_kpi_semantic_context(conn, kpi_fact_id=kpi_fact_id)
    if existing is not None and existing.context.status in {
        KpiSemanticStatus.ADMITTED,
        KpiSemanticStatus.QUARANTINED,
    }:
        return existing.id

    reported_label = normalize_reported_metric_label(metric_name)
    if not reported_label:
        return None
    if isinstance(locator, FactLocator):
        context = build_atomic_semantic_context(
            metric_name=reported_label,
            period_end=period_end,
            value=value,
            unit=unit,
            extracted_by=extracted_by,
            locator=locator,
            source_excerpt=source_excerpt,
        )
    else:
        resolved_scale, resolved_token = _resolve_unit_scale_and_token(
            value=value,
            unit=unit,
            source_excerpt=source_excerpt,
            source_value_text=None,
            unit_scale=KpiUnitScale.NONE if unit is Unit.COUNT else None,
            metric_nature=KpiMetricNature.REPORTED_SCALAR,
        )
        context = KpiSemanticContext(
            metric_name_as_reported=reported_label,
            reported_period_end=period_end,
            period_role=KpiPeriodRole.CURRENT,
            publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
            accounting_basis=infer_default_accounting_basis(reported_label, {extracted_by}),
            consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
            dimensions={},
            unit_scale=resolved_scale,
            source_value_text=resolved_token,
            status=KpiSemanticStatus.ADMITTED,
            reason_code=None,
        )
    return persist_kpi_semantic_context(
        conn,
        kpi_fact_id=kpi_fact_id,
        context=context,
        reviewed_by=reviewed_by or f"deterministic:{extracted_by}",
        knowledge_at=knowledge_at or datetime.now(UTC),
        kpi_definition_revision_id=None,
    )


def validate_spreadsheet_workbook_sheet(workbook_path: Path, sheet_name: str) -> None:
    """Verify read-only that ``sheet_name`` exists in an on-disk OOXML workbook."""
    if workbook_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return
    import openpyxl

    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(
                f"spreadsheet sheet {sheet_name!r} not found in workbook {workbook_path.name}"
            )
    finally:
        wb.close()
