"""Dedicated append-only persistence for a source-reviewed KPI correction."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from credibility.observations import KPI_FACTS, record_restatement_observation
from models.facts import Currency, FactLocator, Unit
from pipeline.kpi_semantics import KpiSemanticContext, persist_kpi_semantic_context


def insert_source_reviewed_kpi_supersession(
    conn: sqlite3.Connection,
    *,
    predecessor_id: int,
    expected_head_id: int,
    value: Decimal,
    unit: Unit,
    currency: Currency | None,
    source_doc_id: int,
    locator: FactLocator,
    source_excerpt: str,
    reviewer: str,
    knowledge_at: datetime,
    context: KpiSemanticContext,
) -> int:
    """Append exactly one governed successor, independent of filing chronology.

    This is intentionally separate from generic extraction/restatement logic.
    Owner-reviewed corrections may use the incumbent document, or a source that
    predates the corrupt extraction, while retaining the exact predecessor.
    """
    if knowledge_at.tzinfo is None:
        raise ValueError("source-reviewed KPI knowledge_at must be timezone-aware")
    predecessor = conn.execute(
        "SELECT ticker,period_end,fiscal_period_type,kpi_definition_id FROM kpi_facts WHERE id=?",
        (predecessor_id,),
    ).fetchone()
    if predecessor is None:
        raise ValueError("source-reviewed KPI predecessor is missing")
    successor = conn.execute(
        "SELECT id FROM kpi_facts WHERE supersedes_id=? ORDER BY id DESC LIMIT 1",
        (predecessor_id,),
    ).fetchone()
    actual_head = predecessor_id if successor is None else int(successor[0])
    if actual_head != expected_head_id or expected_head_id != predecessor_id:
        raise ValueError("source-reviewed KPI predecessor is not the exact current head")
    locator_json = locator.to_json()
    if locator_json is None:
        raise ValueError("source-reviewed KPI correction requires a concrete locator")
    cursor = conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,currency,"
        "source_doc_id,confidence,extracted_by,supersedes_id,locator,source_excerpt) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(predecessor["ticker"]).upper(),
            predecessor["period_end"],
            predecessor["fiscal_period_type"],
            predecessor["kpi_definition_id"],
            str(value),
            unit.value,
            None if currency is None else currency.value,
            source_doc_id,
            1.0,
            f"source_review:{reviewer}",
            predecessor_id,
            locator_json,
            source_excerpt,
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("source-reviewed KPI supersession did not return an identity")
    new_id = int(cursor.lastrowid)
    context_id = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=new_id,
        context=context,
        reviewed_by=reviewer,
        knowledge_at=knowledge_at,
    )
    if context_id is None:
        raise RuntimeError("source-reviewed KPI semantic context table is unavailable")
    _ = record_restatement_observation(
        conn,
        fact_table=KPI_FACTS,
        superseded_id=predecessor_id,
        new_value=value,
    )
    return new_id
