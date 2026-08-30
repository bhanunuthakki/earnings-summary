"""Deterministic KPI refresh scope for the owner-facing metric surfaces."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReference,
    ReportKpiReferenceSourceStatus,
    ReportKpiReferenceStatus,
    current_report_kpi_reference_disposition,
    load_report_kpi_reference_inventory,
)
from provenance.financial_fact_resolution import canonical_fact_relation


class ScopedKpiDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    kpi_definition_id: int | None
    name: str
    reasons: tuple[str, ...]
    fact_count: int
    admitted_context_count: int
    quarantined_context_count: int
    legacy_unknown_context_count: int
    missing_context_count: int
    current_actual_count: int
    comparator_count: int
    guidance_target_count: int
    management_explanation_count: int
    analyst_question_count: int
    report_reference_status: ReportKpiReferenceStatus | None = None
    report_reference_reason_code: str | None = None
    report_reference_pointer: str | None = None
    report_reference_source_status: ReportKpiReferenceSourceStatus | None = None
    report_reference_source_reason_code: str | None = None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
        is not None
    )


def portfolio_tickers(conn: sqlite3.Connection, *, user_id: str = "default") -> tuple[str, ...]:
    """Current portfolio companies only; watchlist/evaluation names stay out."""
    if not _table_exists(conn, "tracked_companies"):
        return ()
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")}
    predicates = ["list_type='portfolio'"]
    params: list[object] = []
    if "user_id" in columns:
        predicates.append("user_id=?")
        params.append(user_id)
    if "archived_at" in columns:
        predicates.append("archived_at IS NULL")
    rows = conn.execute(
        "SELECT DISTINCT UPPER(ticker) FROM tracked_companies WHERE "  # nosec B608
        + " AND ".join(predicates)
        + " ORDER BY UPPER(ticker)",
        tuple(params),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def scoped_kpi_definitions(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str = "default",
) -> tuple[ScopedKpiDefinition, ...]:
    """Union report-used KPIs with every KPI exposed in Facts & Metrics."""
    tickers = portfolio_tickers(conn, user_id=user_id)
    if not tickers or not _table_exists(conn, "kpi_definitions"):
        return ()
    inventory = load_report_kpi_reference_inventory(repo_root, tickers)
    references = inventory.references
    marks = ",".join("?" for _ in tickers)
    fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
    has_context = _table_exists(conn, "kpi_fact_semantic_contexts")
    context_columns: set[str] = set()
    if has_context:
        context_columns = {
            str(cast("tuple[object, ...]", row)[1])
            for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
        }
    has_revisions = {"revision", "supersedes_context_id"}.issubset(context_columns)
    has_lanes = "publication_lane" in context_columns
    context_join = ""
    if has_context:
        context_join = "LEFT JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id=fact.id"
        if has_revisions:
            context_join += (
                " AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts context_successor "
                "WHERE context_successor.supersedes_context_id=context.id)"
            )
    admitted = "SUM(CASE WHEN context.status='admitted' THEN 1 ELSE 0 END)" if has_context else "0"
    quarantined = (
        "SUM(CASE WHEN context.status='quarantined' THEN 1 ELSE 0 END)" if has_context else "0"
    )
    legacy_unknown = (
        "SUM(CASE WHEN context.status='legacy_unknown' THEN 1 ELSE 0 END)" if has_context else "0"
    )
    missing = (
        "SUM(CASE WHEN fact.id IS NOT NULL AND context.id IS NULL THEN 1 ELSE 0 END)"
        if has_context
        else "COUNT(fact.id)"
    )
    lane_names = (
        "current_actual",
        "comparator",
        "guidance_target",
        "management_explanation",
        "analyst_question",
    )
    lane_counts = [
        (
            f"SUM(CASE WHEN context.status='admitted' AND context.publication_lane='{lane}' "
            "THEN 1 ELSE 0 END)"
            if has_lanes
            else (admitted if lane == "current_actual" else "0")
        )
        for lane in lane_names
    ]
    rows = conn.execute(
        f"SELECT definition.id,UPPER(definition.ticker),definition.name,COUNT(fact.id),"  # nosec B608
        f"{admitted},{quarantined},{legacy_unknown},{missing},{','.join(lane_counts)} "
        "FROM kpi_definitions definition "
        f"LEFT JOIN {fact_relation} fact ON fact.kpi_definition_id=definition.id "
        f"{context_join} WHERE UPPER(definition.ticker) IN ({marks}) "
        "GROUP BY definition.id,UPPER(definition.ticker),definition.name ORDER BY 2,3,1",
        tickers,
    ).fetchall()
    out: list[ScopedKpiDefinition] = []
    resolved_definition_references: dict[int, list[ReportKpiReference]] = {}
    unresolved_references: list[
        tuple[ReportKpiReference, ReportKpiReferenceStatus | None, str | None]
    ] = []
    definition_keys: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        definition_keys.setdefault((str(row[1]), str(row[2]).strip().casefold()), []).append(
            int(row[0])
        )
    for reference in references:
        direct_ids = definition_keys.get(
            (reference.ticker, reference.requested_label.strip().casefold())
        )
        if direct_ids is not None and len(direct_ids) == 1:
            resolved_definition_references.setdefault(direct_ids[0], []).append(reference)
            continue
        ambiguous_exact_match = direct_ids is not None and len(direct_ids) > 1
        revision = current_report_kpi_reference_disposition(
            conn, user_id=user_id, reference=reference
        )
        if revision is not None and revision.reference != reference:
            revision = None
        unresolved_references.append(
            (
                reference,
                None if revision is None else revision.disposition.status,
                (
                    "ambiguous_exact_reported_definition"
                    if revision is None and ambiguous_exact_match
                    else (None if revision is None else revision.disposition.reason_code)
                ),
            )
        )
    for row in rows:
        definition_id, ticker, name = int(row[0]), str(row[1]), str(row[2])
        fact_count = int(row[3])
        reasons: list[str] = []
        if definition_id in resolved_definition_references:
            reasons.append("report")
        if fact_count > 0:
            reasons.append("facts_metrics")
        if reasons:
            out.append(
                ScopedKpiDefinition(
                    ticker=ticker,
                    kpi_definition_id=definition_id,
                    name=name,
                    reasons=tuple(reasons),
                    fact_count=fact_count,
                    admitted_context_count=int(row[4]),
                    quarantined_context_count=int(row[5]),
                    legacy_unknown_context_count=int(row[6]),
                    missing_context_count=int(row[7]),
                    current_actual_count=int(row[8]),
                    comparator_count=int(row[9]),
                    guidance_target_count=int(row[10]),
                    management_explanation_count=int(row[11]),
                    analyst_question_count=int(row[12]),
                )
            )
    for reference, status, reason_code in unresolved_references:
        out.append(
            ScopedKpiDefinition(
                ticker=reference.ticker,
                kpi_definition_id=None,
                name=reference.requested_label,
                reasons=("report",),
                fact_count=0,
                admitted_context_count=0,
                quarantined_context_count=0,
                legacy_unknown_context_count=0,
                missing_context_count=0,
                current_actual_count=0,
                comparator_count=0,
                guidance_target_count=0,
                management_explanation_count=0,
                analyst_question_count=0,
                report_reference_status=status,
                report_reference_reason_code=reason_code,
                report_reference_pointer=reference.json_pointer,
                report_reference_source_status=ReportKpiReferenceSourceStatus.VALID,
            )
        )
    for source in inventory.source_states:
        if source.status is ReportKpiReferenceSourceStatus.VALID:
            continue
        out.append(
            ScopedKpiDefinition(
                ticker=source.ticker,
                kpi_definition_id=None,
                name="<report_configuration>",
                reasons=("report_configuration",),
                fact_count=0,
                admitted_context_count=0,
                quarantined_context_count=0,
                legacy_unknown_context_count=0,
                missing_context_count=0,
                current_actual_count=0,
                comparator_count=0,
                guidance_target_count=0,
                management_explanation_count=0,
                analyst_question_count=0,
                report_reference_source_status=source.status,
                report_reference_source_reason_code=source.reason_code,
            )
        )
    return tuple(
        sorted(
            out, key=lambda item: (item.ticker, item.name.casefold(), item.kpi_definition_id or 0)
        )
    )
