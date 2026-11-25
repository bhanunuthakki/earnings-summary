"""Auditable, append-only batch semantic admission for verified retained KPI facts.

Historical KPI extractors wrote hundreds of thousands of retained rows into
``kpi_facts`` with either no row in ``kpi_fact_semantic_contexts`` or an
unclassified ``legacy_unknown`` head context.  Strict canonical consumers
(``semantic_admission_sql(..., fail_closed=True)``) reject those rows.

This module admits eligible deterministic rows cleanly through
``persist_kpi_semantic_context`` without mutating ``kpi_facts`` or overriding
``quarantined`` / ``legacy_unknown``-conflicting human dispositions.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from compute.kpi_resolver import normalize_reported_series_key
from models.documents import reader_tier_join_sql, reader_tier_rank_sql
from models.facts import Currency, FiscalPeriodType, Unit
from pipeline.kpi_atomic_admission import (
    DETERMINISTIC_ADMITTABLE_EXTRACTORS as DETERMINISTIC_ADMITTABLE_EXTRACTORS,
)
from pipeline.kpi_atomic_admission import (
    UNIT_TO_FIXED_SCALE as _UNIT_TO_FIXED_SCALE,
)
from pipeline.kpi_atomic_admission import (
    infer_default_accounting_basis as _default_basis,
)
from pipeline.kpi_atomic_admission import (
    match_count_source_value as _match_count_source_value,
)
from pipeline.kpi_atomic_admission import (
    normalize_reported_metric_label,
)
from pipeline.kpi_definition_revisions import bind_forward_kpi_definition_revision
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    persist_kpi_semantic_context,
    validate_admitted_unit_scale,
)
from runtime.deterministic import table_or_view_exists as _table_exists


class KpiBatchAdmissionTickerSummary(BaseModel):
    """Per-ticker summary of scanned, eligible, admitted, and skipped KPI facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    scanned: int = 0
    already_admitted: int = 0
    eligible_for_admission: int = 0
    admitted_applied: int = 0
    skipped_counts: dict[str, int] = Field(default_factory=dict)


class KpiBatchAdmissionReport(BaseModel):
    """Aggregate report emitted by dry-run or ``--apply`` batch admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    apply: bool
    as_of: date
    reviewed_by: str
    include_llm_with_excerpt: bool
    tickers: tuple[str, ...]
    total_scanned: int
    total_already_admitted: int
    total_eligible: int
    total_admitted_applied: int
    skipped_counts: dict[str, int]
    per_ticker: tuple[KpiBatchAdmissionTickerSummary, ...]


@dataclass(frozen=True, slots=True)
class _SeriesSignature:
    metric_name_as_reported: str
    accounting_basis: KpiAccountingBasis
    consolidation_scope: KpiConsolidationScope
    dimensions_json: str


@dataclass(frozen=True, slots=True)
class _ActiveFactRow:
    fact_id: int
    supersedes_id: int | None
    ticker: str
    kpi_definition_id: int
    definition_name: str
    period_end_raw: object
    fiscal_period_type_raw: object
    value_raw: object
    unit_raw: object
    currency_raw: object
    locator_raw: object
    source_doc_id: int | None
    doc_exists: bool
    source_excerpt: str | None
    extracted_by: str | None
    ctx_status: str | None
    ctx_publication_lane: str | None
    ctx_metric_name: str | None
    ctx_accounting_basis: str | None
    ctx_consolidation_scope: str | None
    ctx_dimensions_json: str | None
    ctx_unit_scale: str | None
    in_resolved_view: bool
    is_unresolved_material: bool


def _is_llm_extractor(extracted_by: str) -> bool:
    normalized = extracted_by.strip().lower()
    return normalized == "llm" or normalized.startswith("llm:") or normalized.startswith("llm_")


def _parse_period_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_finite_decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        dec = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not dec.is_finite():
        return None
    return dec


def _load_active_fact_rows(
    conn: sqlite3.Connection, *, tickers: Sequence[str] | None
) -> tuple[list[_ActiveFactRow], bool]:
    for required in ("kpi_facts", "kpi_definitions", "kpi_fact_semantic_contexts"):
        if not _table_exists(conn, required):
            raise RuntimeError(f"required table {required!r} is unavailable")

    has_documents = _table_exists(conn, "documents")
    has_resolved_view = _table_exists(conn, "v_kpi_facts_resolved_current")
    has_resolution_tables = (
        _table_exists(conn, "fact_observation_revisions")
        and _table_exists(conn, "v_observation_resolution_current")
        and _table_exists(conn, "fact_resolution_outcomes")
    )
    kf_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()}
    ctx_cols = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)").fetchall()
    }
    has_revisions = {"revision", "supersedes_context_id"}.issubset(ctx_cols)
    has_lane = "publication_lane" in ctx_cols

    normalized_tickers = (
        sorted({t.strip().upper() for t in tickers if t.strip()}) if tickers else []
    )
    where_clause = ""
    params: tuple[object, ...] = ()
    if tickers is not None:
        if not normalized_tickers:
            return [], has_resolved_view
        marks = ",".join("?" for _ in normalized_tickers)
        where_clause = f"WHERE UPPER(f.ticker) IN ({marks})"
        params = tuple(normalized_tickers)

    head_filter = (
        "AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts succ "
        "WHERE succ.supersedes_context_id = ksc.id)"
        if has_revisions
        else ""
    )
    doc_expr = (
        "CASE WHEN f.source_doc_id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM documents doc WHERE doc.id = f.source_doc_id) THEN 1 ELSE 0 END"
        if has_documents
        else "CASE WHEN f.source_doc_id IS NOT NULL THEN 1 ELSE 0 END"
    )
    resolved_expr = (
        "CASE WHEN EXISTS (SELECT 1 FROM v_kpi_facts_resolved_current rv "
        "WHERE rv.id = f.id) THEN 1 ELSE 0 END"
        if has_resolved_view
        else "1"
    )
    unresolved_expr = (
        "CASE WHEN EXISTS ("
        "SELECT 1 FROM fact_observation_revisions link "
        "JOIN v_observation_resolution_current res "
        "  ON res.selected_observation_id = link.observation_id "
        "JOIN fact_resolution_outcomes fro ON fro.resolution_id = res.resolution_id "
        "WHERE link.fact_table = 'kpi_facts' AND link.fact_row_id = f.id "
        "  AND link.fact_revision = ("
        "    SELECT MAX(latest.fact_revision) FROM fact_observation_revisions latest "
        "    WHERE latest.fact_table = 'kpi_facts' AND latest.fact_row_id = f.id"
        "  ) AND fro.resolution_status <> 'resolved'"
        ") THEN 1 ELSE 0 END"
        if has_resolution_tables
        else "0"
    )
    supersedes_expr = "f.supersedes_id" if "supersedes_id" in kf_cols else "NULL"
    fpt_expr = "f.fiscal_period_type" if "fiscal_period_type" in kf_cols else "NULL"
    currency_expr = "f.currency" if "currency" in kf_cols else "NULL"
    locator_expr = "f.locator" if "locator" in kf_cols else "NULL"
    lane_expr = "ksc.publication_lane" if has_lane else "NULL"

    sql_template = (
        "SELECT f.id, {supersedes_expr}, UPPER(f.ticker), f.kpi_definition_id, "  # nosec B608
        "       kd.name, f.period_end, f.value, f.unit, f.source_doc_id, {doc_expr}, "
        "       f.source_excerpt, f.extracted_by, "
        "       ksc.status, {lane_expr}, ksc.metric_name_as_reported, ksc.accounting_basis, "
        "       ksc.consolidation_scope, ksc.dimensions_json, ksc.unit_scale, "
        "       {resolved_expr}, {unresolved_expr}, "
        "       {fpt_expr}, {currency_expr}, {locator_expr} "
        "FROM kpi_facts f "
        "JOIN kpi_definitions kd ON kd.id = f.kpi_definition_id "
        "LEFT JOIN kpi_fact_semantic_contexts ksc "
        "  ON ksc.kpi_fact_id = f.id {head_filter} "
        "{where_clause} "
        "ORDER BY UPPER(f.ticker), f.kpi_definition_id, f.period_end, f.id"
    )
    sql = sql_template.format(
        supersedes_expr=supersedes_expr,
        doc_expr=doc_expr,
        lane_expr=lane_expr,
        resolved_expr=resolved_expr,
        unresolved_expr=unresolved_expr,
        fpt_expr=fpt_expr,
        currency_expr=currency_expr,
        locator_expr=locator_expr,
        head_filter=head_filter,
        where_clause=where_clause,
    )
    raw_rows = conn.execute(sql, params).fetchall()

    superseded_ids: set[int] = set()
    parsed_rows: list[_ActiveFactRow] = []
    for raw in raw_rows:
        sup_id = int(raw[1]) if raw[1] is not None else None
        if sup_id is not None:
            superseded_ids.add(sup_id)
        parsed_rows.append(
            _ActiveFactRow(
                fact_id=int(raw[0]),
                supersedes_id=sup_id,
                ticker=str(raw[2]),
                kpi_definition_id=int(raw[3]),
                definition_name=str(raw[4] or "").strip(),
                period_end_raw=raw[5],
                fiscal_period_type_raw=raw[21],
                value_raw=raw[6],
                unit_raw=raw[7],
                currency_raw=raw[22],
                locator_raw=raw[23],
                source_doc_id=int(raw[8]) if raw[8] is not None else None,
                doc_exists=bool(raw[9]),
                source_excerpt=str(raw[10]) if raw[10] is not None else None,
                extracted_by=str(raw[11]).strip() if raw[11] is not None else None,
                ctx_status=str(raw[12]) if raw[12] is not None else None,
                ctx_publication_lane=str(raw[13]) if raw[13] is not None else None,
                ctx_metric_name=str(raw[14]) if raw[14] is not None else None,
                ctx_accounting_basis=str(raw[15]) if raw[15] is not None else None,
                ctx_consolidation_scope=str(raw[16]) if raw[16] is not None else None,
                ctx_dimensions_json=str(raw[17]) if raw[17] is not None else None,
                ctx_unit_scale=str(raw[18]) if raw[18] is not None else None,
                in_resolved_view=bool(raw[19]),
                is_unresolved_material=bool(raw[20]),
            )
        )
    return [row for row in parsed_rows if row.fact_id not in superseded_ids], has_resolved_view


def _collect_existing_admitted_signatures(
    active_rows: Sequence[_ActiveFactRow],
) -> dict[int, tuple[_SeriesSignature, KpiUnitScale] | Literal["AMBIGUOUS"]]:
    by_def: dict[int, dict[tuple[str, str, str, str, str], str]] = {}
    for row in active_rows:
        if row.ctx_status != KpiSemanticStatus.ADMITTED.value:
            continue
        if (
            row.ctx_publication_lane is not None
            and row.ctx_publication_lane != KpiPublicationLane.CURRENT_ACTUAL.value
        ):
            continue
        if not (
            row.ctx_metric_name
            and row.ctx_accounting_basis
            and row.ctx_consolidation_scope
            and row.ctx_dimensions_json
            and row.ctx_unit_scale
        ):
            by_def[row.kpi_definition_id] = {}
            continue
        sig_key = (
            normalize_reported_series_key(row.ctx_metric_name),
            row.ctx_accounting_basis,
            row.ctx_consolidation_scope,
            row.ctx_dimensions_json,
            row.ctx_unit_scale,
        )
        by_def.setdefault(row.kpi_definition_id, {}).setdefault(
            sig_key, normalize_reported_metric_label(row.ctx_metric_name) or row.ctx_metric_name
        )

    out: dict[int, tuple[_SeriesSignature, KpiUnitScale] | Literal["AMBIGUOUS"]] = {}
    for def_id, sigs in by_def.items():
        if len(sigs) != 1:
            out[def_id] = "AMBIGUOUS"
            continue
        (sig_key, display_name) = next(iter(sigs.items()))
        _, basis_raw, scope_raw, dims_raw, scale_raw = sig_key
        try:
            sig = _SeriesSignature(
                metric_name_as_reported=display_name,
                accounting_basis=KpiAccountingBasis(basis_raw),
                consolidation_scope=KpiConsolidationScope(scope_raw),
                dimensions_json=dims_raw,
            )
            scale = KpiUnitScale(scale_raw)
        except ValueError:
            out[def_id] = "AMBIGUOUS"
            continue
        out[def_id] = (sig, scale)
    return out


def _collect_default_signatures(
    active_rows: Sequence[_ActiveFactRow],
    *,
    existing_admitted: dict[int, tuple[_SeriesSignature, KpiUnitScale] | Literal["AMBIGUOUS"]],
    include_llm_with_excerpt: bool,
) -> dict[int, _SeriesSignature]:
    extractors_by_def: dict[int, set[str]] = {}
    def_names: dict[int, str] = {}
    legacy_bases: dict[int, set[KpiAccountingBasis]] = {}

    for row in active_rows:
        def_id = row.kpi_definition_id
        def_names.setdefault(def_id, row.definition_name)
        ext = row.extracted_by or ""
        if ext in DETERMINISTIC_ADMITTABLE_EXTRACTORS or (
            include_llm_with_excerpt
            and _is_llm_extractor(ext)
            and row.source_excerpt is not None
            and row.source_excerpt.strip()
        ):
            extractors_by_def.setdefault(def_id, set()).add(ext)
        if row.ctx_status == KpiSemanticStatus.LEGACY_UNKNOWN.value and row.ctx_accounting_basis:
            try:
                basis = KpiAccountingBasis(row.ctx_accounting_basis)
            except ValueError:
                basis = KpiAccountingBasis.UNKNOWN
            if basis is not KpiAccountingBasis.UNKNOWN:
                legacy_bases.setdefault(def_id, set()).add(basis)

    defaults: dict[int, _SeriesSignature] = {}
    for def_id, name in def_names.items():
        if def_id in existing_admitted:
            continue
        cleaned_name = (name.strip() or f"kpi_{def_id}")[:256]
        known_bases = legacy_bases.get(def_id, set())
        basis = (
            next(iter(known_bases))
            if len(known_bases) == 1
            else _default_basis(cleaned_name, extractors_by_def.get(def_id, set()))
        )
        defaults[def_id] = _SeriesSignature(
            metric_name_as_reported=cleaned_name,
            accounting_basis=basis,
            consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
            dimensions_json="{}",
        )
    return defaults


def _qualify_row(
    row: _ActiveFactRow,
    *,
    as_of: date,
    include_llm_with_excerpt: bool,
    has_resolved_view: bool,
    require_resolved_view: bool,
    existing_admitted: dict[int, tuple[_SeriesSignature, KpiUnitScale] | Literal["AMBIGUOUS"]],
    default_signatures: dict[int, _SeriesSignature],
) -> tuple[str | None, KpiSemanticContext | None]:
    if row.ctx_status == KpiSemanticStatus.ADMITTED.value:
        return "already_admitted", None
    if row.ctx_status == KpiSemanticStatus.QUARANTINED.value:
        return "quarantined", None
    if row.ctx_status not in {None, KpiSemanticStatus.LEGACY_UNKNOWN.value}:
        return "unsupported_semantic_status", None
    if row.is_unresolved_material:
        return "unresolved_material_dissent", None

    ext = row.extracted_by or ""
    if ext not in DETERMINISTIC_ADMITTABLE_EXTRACTORS:
        if _is_llm_extractor(ext):
            if not include_llm_with_excerpt or not (
                row.source_excerpt and row.source_excerpt.strip()
            ):
                return "unverified_llm_extractor", None
        else:
            return "unsupported_extractor", None

    if row.source_doc_id is None or not row.doc_exists:
        return "missing_source_document", None

    period_date = _parse_period_date(row.period_end_raw)
    if period_date is None:
        return "invalid_period_end", None
    if period_date > as_of:
        return "future_period_end", None

    persisted_value = _parse_finite_decimal(row.value_raw)
    if persisted_value is None:
        return "invalid_value", None

    try:
        persisted_unit = Unit(str(row.unit_raw))
    except ValueError:
        return "invalid_unit", None

    source_value_text: str | None = None
    if persisted_unit is Unit.COUNT:
        matched = _match_count_source_value(persisted_value, row.source_excerpt)
        if matched is not None:
            source_value_text, unit_scale = matched
        elif ext in DETERMINISTIC_ADMITTABLE_EXTRACTORS or (
            row.source_excerpt is not None and row.source_excerpt.strip()
        ):
            source_value_text = None
            unit_scale = KpiUnitScale.NONE
        else:
            return "count_source_value_unverified", None
    else:
        fixed = _UNIT_TO_FIXED_SCALE.get(persisted_unit)
        if fixed is None:
            return "invalid_unit", None
        unit_scale = fixed
        validate_admitted_unit_scale(persisted_unit, unit_scale)

    if has_resolved_view and require_resolved_view and not row.in_resolved_view:
        return "unlinked_observation_resolution", None

    admitted_info = existing_admitted.get(row.kpi_definition_id)
    if admitted_info == "AMBIGUOUS":
        return "ambiguous_existing_admitted_series", None
    if admitted_info is not None:
        sig, expected_scale = admitted_info
        if unit_scale is not expected_scale:
            return "existing_admitted_series_mismatch", None
    else:
        sig = default_signatures[row.kpi_definition_id]

    raw_dims: object = json.loads(sig.dimensions_json)
    if not isinstance(raw_dims, dict):
        return "invalid_dimensions", None
    dims = {str(k): str(v) for k, v in cast("dict[object, object]", raw_dims).items()}

    context = KpiSemanticContext(
        metric_name_as_reported=sig.metric_name_as_reported,
        reported_period_end=period_date,
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=sig.accounting_basis,
        consolidation_scope=sig.consolidation_scope,
        dimensions=dims,
        unit_scale=unit_scale,
        source_value_text=source_value_text,
        status=KpiSemanticStatus.ADMITTED,
        reason_code=None,
    )
    return None, context


def _default_focus_tickers(conn: sqlite3.Connection) -> tuple[str, ...] | None:
    if not _table_exists(conn, "tracked_companies"):
        return None
    tc_cols = _table_columns(conn, "tracked_companies")
    if not {"ticker", "list_type"}.issubset(tc_cols):
        return None
    clauses = ["list_type IN ('portfolio', 'evaluation')"]
    if "archived_at" in tc_cols:
        clauses.append("archived_at IS NULL")
    if "instrument_type" in tc_cols:
        clauses.append("COALESCE(LOWER(instrument_type), '') <> 'etf'")
    rows = conn.execute(
        "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
        f"WHERE {' AND '.join(clauses)} ORDER BY 1"  # nosec B608 -- fixed column checks above
    ).fetchall()
    if not rows:
        return None
    return tuple(str(r[0]).strip() for r in rows if r[0])


def run_batch_kpi_admission(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str] | None = None,
    as_of: date | None = None,
    apply: bool = False,
    include_llm_with_excerpt: bool = True,
    require_resolved_view: bool = False,
    reviewed_by: str = "batch_admission:deterministic_v1",
    knowledge_at: datetime | None = None,
) -> KpiBatchAdmissionReport:
    """Evaluate active ``kpi_facts`` and optionally append ``admitted`` semantic contexts.

    When ``apply=False`` (the default), zero database writes occur, so ``conn``
    may be opened with ``mode=ro`` and ``PRAGMA query_only=1``.
    """
    effective_as_of = as_of or datetime.now(UTC).date()
    effective_knowledge_at = knowledge_at or datetime.now(UTC)
    if effective_knowledge_at.tzinfo is None:
        effective_knowledge_at = effective_knowledge_at.replace(tzinfo=UTC)

    effective_tickers: Sequence[str] | None = (
        tickers if tickers is not None else _default_focus_tickers(conn)
    )
    active_rows, has_resolved_view = _load_active_fact_rows(conn, tickers=effective_tickers)
    existing_admitted = _collect_existing_admitted_signatures(active_rows)
    default_signatures = _collect_default_signatures(
        active_rows,
        existing_admitted=existing_admitted,
        include_llm_with_excerpt=include_llm_with_excerpt,
    )

    per_ticker_counts: dict[str, dict[str, object]] = {}
    aggregate_skipped: dict[str, int] = {}

    requested = (
        tuple(dict.fromkeys(t.strip().upper() for t in effective_tickers if t.strip()))
        if effective_tickers is not None
        else tuple(sorted({row.ticker for row in active_rows}))
    )
    for ticker in requested:
        per_ticker_counts[ticker] = {
            "scanned": 0,
            "already_admitted": 0,
            "eligible": 0,
            "applied": 0,
            "skipped": {},
        }

    for row in active_rows:
        bucket = per_ticker_counts.setdefault(
            row.ticker,
            {
                "scanned": 0,
                "already_admitted": 0,
                "eligible": 0,
                "applied": 0,
                "skipped": {},
            },
        )
        bucket["scanned"] = cast(int, bucket["scanned"]) + 1
        skip_reason, context = _qualify_row(
            row,
            as_of=effective_as_of,
            include_llm_with_excerpt=include_llm_with_excerpt,
            has_resolved_view=has_resolved_view,
            require_resolved_view=require_resolved_view,
            existing_admitted=existing_admitted,
            default_signatures=default_signatures,
        )
        if skip_reason is not None or context is None:
            reason = skip_reason or "qualification_failed"
            if reason == "already_admitted":
                bucket["already_admitted"] = cast(int, bucket["already_admitted"]) + 1
            else:
                skipped_map = cast("dict[str, int]", bucket["skipped"])
                skipped_map[reason] = skipped_map.get(reason, 0) + 1
                aggregate_skipped[reason] = aggregate_skipped.get(reason, 0) + 1
            continue

        bucket["eligible"] = cast(int, bucket["eligible"]) + 1
        if apply:
            definition_binding = None
            if context.reported_period_end is not None and row.period_end_raw is not None:
                try:
                    parsed_period_end = datetime.fromisoformat(
                        str(row.period_end_raw).replace("Z", "+00:00")
                    )
                    parsed_unit = Unit(str(row.unit_raw))
                    parsed_fpt = (
                        FiscalPeriodType(str(row.fiscal_period_type_raw))
                        if row.fiscal_period_type_raw is not None
                        else FiscalPeriodType.FY
                    )
                    parsed_currency = (
                        Currency(str(row.currency_raw)) if row.currency_raw is not None else None
                    )
                except ValueError:
                    parsed_period_end = None
                    parsed_unit = None
                    parsed_fpt = None
                    parsed_currency = None
                if (
                    parsed_period_end is not None
                    and parsed_unit is not None
                    and parsed_fpt is not None
                ):
                    definition_binding = bind_forward_kpi_definition_revision(
                        conn,
                        kpi_definition_id=row.kpi_definition_id,
                        period_end=parsed_period_end,
                        fiscal_period_type=parsed_fpt,
                        unit=parsed_unit,
                        currency=parsed_currency,
                        source_doc_id=row.source_doc_id,
                        context=context,
                        reviewed_by=reviewed_by,
                        knowledge_at=effective_knowledge_at,
                        source_locator_json=(
                            None if row.locator_raw is None else str(row.locator_raw)
                        ),
                    )
            if definition_binding is not None:
                persist_kpi_semantic_context(
                    conn,
                    kpi_fact_id=row.fact_id,
                    context=context,
                    reviewed_by=reviewed_by,
                    knowledge_at=definition_binding.knowledge_at,
                    kpi_definition_revision_id=definition_binding.kpi_definition_revision_id,
                )
            else:
                persist_kpi_semantic_context(
                    conn,
                    kpi_fact_id=row.fact_id,
                    context=context,
                    reviewed_by=reviewed_by,
                    knowledge_at=effective_knowledge_at,
                )
            bucket["applied"] = cast(int, bucket["applied"]) + 1

    if apply:
        conn.commit()

    summaries = tuple(
        KpiBatchAdmissionTickerSummary(
            ticker=ticker,
            scanned=cast(int, data["scanned"]),
            already_admitted=cast(int, data["already_admitted"]),
            eligible_for_admission=cast(int, data["eligible"]),
            admitted_applied=cast(int, data["applied"]),
            skipped_counts=dict(sorted(cast("dict[str, int]", data["skipped"]).items())),
        )
        for ticker, data in sorted(per_ticker_counts.items())
    )
    return KpiBatchAdmissionReport(
        apply=apply,
        as_of=effective_as_of,
        reviewed_by=reviewed_by,
        include_llm_with_excerpt=include_llm_with_excerpt,
        tickers=tuple(s.ticker for s in summaries),
        total_scanned=sum(s.scanned for s in summaries),
        total_already_admitted=sum(s.already_admitted for s in summaries),
        total_eligible=sum(s.eligible_for_admission for s in summaries),
        total_admitted_applied=sum(s.admitted_applied for s in summaries),
        skipped_counts=dict(sorted(aggregate_skipped.items())),
        per_ticker=summaries,
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return frozenset()
    return frozenset(str(row[1]) for row in rows)


def unreviewed_fallback_clauses(
    conn: sqlite3.Connection,
    *,
    fact_alias: str = "kf",
    context_alias: str = "ksc",
) -> tuple[str, str]:
    """Return ``(join_sql, where_sql)`` for non-strict unreviewed KPI fallback reads.

    Reads unreviewed (missing-context or ``legacy_unknown``) rows while still
    blocking ``quarantined``, already ``admitted`` non-current rows, rows without
    ``source_doc_id``, and rows whose latest resolution outcome is dissenting
    (``resolution_status <> 'resolved'``, such as ``unresolved_material``).
    """
    has_sem_ctx = _table_exists(conn, "kpi_fact_semantic_contexts")
    join = ""
    predicates = [f"{fact_alias}.source_doc_id IS NOT NULL"]
    if has_sem_ctx:
        columns = _table_columns(conn, "kpi_fact_semantic_contexts")
        if {"revision", "supersedes_context_id"}.issubset(columns):
            join = (
                f"LEFT JOIN kpi_fact_semantic_contexts {context_alias} "  # nosec B608
                f"ON {context_alias}.kpi_fact_id={fact_alias}.id "
                f"AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts "
                f"{context_alias}_successor "
                f"WHERE {context_alias}_successor.supersedes_context_id={context_alias}.id)"
            )
        else:
            join = (
                f"LEFT JOIN kpi_fact_semantic_contexts {context_alias} "  # nosec B608
                f"ON {context_alias}.kpi_fact_id={fact_alias}.id"
            )
        predicates.append(
            f"({context_alias}.id IS NULL OR {context_alias}.status='legacy_unknown')"
        )
    if (
        _table_exists(conn, "fact_observation_revisions")
        and _table_exists(conn, "v_observation_resolution_current")
        and _table_exists(conn, "fact_resolution_outcomes")
    ):
        predicates.append(
            "NOT EXISTS ("  # nosec B608
            "SELECT 1 FROM fact_observation_revisions forv "
            "JOIN v_observation_resolution_current res "
            "ON res.selected_observation_id=forv.observation_id "
            "JOIN fact_resolution_outcomes fro ON fro.resolution_id=res.resolution_id "
            f"WHERE forv.fact_table='kpi_facts' AND forv.fact_row_id={fact_alias}.id "
            "AND forv.fact_revision=("
            "SELECT MAX(latest.fact_revision) FROM fact_observation_revisions latest "
            f"WHERE latest.fact_table='kpi_facts' AND latest.fact_row_id={fact_alias}.id"
            ") AND fro.resolution_status<>'resolved')"
        )
    return join, " AND ".join(predicates)


def read_unreviewed_kpi_fallback_rows(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    definition_ids: Sequence[int],
    tier1_only: bool = False,
    max_period_end: str | None = None,
    order_by_tier: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Read retained, non-quarantined, unreviewed legacy KPI rows for non-strict surfaces.

    Computes supersession across all fetched candidates first so a superseding row
    that is itself quarantined, dissenting, or future-dated still retires its
    predecessor cleanly.
    """
    if (
        not tickers
        or not definition_ids
        or not _table_exists(conn, "kpi_facts")
        or not _table_exists(conn, "kpi_definitions")
    ):
        return []
    fact_columns = _table_columns(conn, "kpi_facts")
    if "source_doc_id" not in fact_columns:
        return []
    fb_join, fb_where = unreviewed_fallback_clauses(conn, fact_alias="f", context_alias="ksc")
    context_columns = (
        _table_columns(conn, "kpi_fact_semantic_contexts") if fb_join else frozenset[str]()
    )
    supersedes_expr = "f.supersedes_id" if "supersedes_id" in fact_columns else "NULL"
    fpt_expr = "f.fiscal_period_type" if "fiscal_period_type" in fact_columns else "NULL"
    conf_expr = "f.confidence" if "confidence" in fact_columns else "NULL"

    projection_fields = [
        "f.id AS input_fact_id",
        "f.ticker AS input_ticker",
        "d.id AS input_definition_id",
        "d.name AS input_definition_name",
    ]
    for col in ("source_doc_id", "locator", "fiscal_period_type"):
        expr = f"f.{col}" if col in fact_columns else "NULL"
        projection_fields.append(f"{expr} AS input_{col}")
    for col, alias in (
        ("id", "semantic_context_id"),
        ("kpi_definition_revision_id", "definition_revision_id"),
        ("source_row_label", "source_row_label"),
        ("source_column_header", "source_column_header"),
        ("source_value_text", "source_value_text"),
    ):
        expr = f"ksc.{col}" if col in context_columns else "NULL"
        projection_fields.append(f"{expr} AS input_{alias}")
    input_projection = ", ".join(projection_fields)

    if order_by_tier:
        doc_join = reader_tier_join_sql(conn, fact_alias="f", doc_alias="doc")
        tier_rank = reader_tier_rank_sql(conn, tier_column="doc.source_quality_tier")
        order_sql = f"f.period_end DESC, {tier_rank} DESC, f.id DESC"
    else:
        doc_join = ""
        order_sql = "f.ticker, d.id, f.period_end DESC"

    tier1_filter = "AND d.threshold_tier = 'tier_1_break'" if tier1_only else ""
    ticker_marks = ",".join("?" for _ in tickers)
    def_marks = ",".join("?" for _ in definition_ids)
    sql_template = (
        "SELECT f.id AS fact_id, {supersedes_expr} AS supersedes_id, "  # nosec B608
        "f.ticker AS ticker, d.id AS def_id, d.name AS name, f.unit AS unit, "
        "f.period_end AS period_end, date(f.period_end) AS period_date, "
        "f.value AS value, {fpt_expr} AS fiscal_period_type, "
        "f.source_doc_id AS source_doc_id, {conf_expr} AS confidence, "
        "{input_projection}, "
        "CASE WHEN ({fb_where}) THEN 1 ELSE 0 END AS _fb_eligible "
        "FROM kpi_facts f "
        "JOIN kpi_definitions d ON d.id = f.kpi_definition_id AND d.ticker = f.ticker "
        "{doc_join} {fb_join} "
        "WHERE f.ticker IN ({ticker_marks}) AND f.kpi_definition_id IN ({def_marks}) "
        "{tier1_filter} "
        "ORDER BY {order_sql}"
    )
    sql = sql_template.format(
        supersedes_expr=supersedes_expr,
        fpt_expr=fpt_expr,
        conf_expr=conf_expr,
        input_projection=input_projection,
        fb_where=fb_where,
        doc_join=doc_join,
        fb_join=fb_join,
        ticker_marks=ticker_marks,
        def_marks=def_marks,
        tier1_filter=tier1_filter,
        order_sql=order_sql,
    )
    try:
        cur = conn.execute(sql, (*tickers, *definition_ids))
    except sqlite3.OperationalError:
        return []
    cur.row_factory = sqlite3.Row
    all_rows = cur.fetchall()
    superseded_ids = {int(r["supersedes_id"]) for r in all_rows if r["supersedes_id"] is not None}
    eligible: list[sqlite3.Row] = []
    for row in all_rows:
        if int(row["_fb_eligible"]) != 1:
            continue
        if int(row["fact_id"]) in superseded_ids:
            continue
        if max_period_end is not None:
            period_date = row["period_date"]
            if period_date is None or str(period_date) > max_period_end:
                continue
        eligible.append(row)
        if limit is not None and len(eligible) >= limit:
            break
    return eligible
