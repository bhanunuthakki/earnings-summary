"""Dedicated append-only persistence for a source-reviewed KPI correction."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from credibility.observations import KPI_FACTS, record_restatement_observation
from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiDefinitionComparabilityRevision,
    current_kpi_definition_revision,
    kpi_definition_revision_by_id,
    persist_kpi_definition_comparability_revision,
    persist_kpi_definition_revision,
)
from pipeline.kpi_persistence import (
    ExactDefinitionPersistResult,
    persist_kpi_value_at_exact_definition,
)
from pipeline.kpi_semantics import KpiSemanticContext, persist_kpi_semantic_context
from provenance.financial_fact_resolution import require_exact_canonical_fact_row


@contextmanager
def _source_review_transaction(conn: sqlite3.Connection):
    """Keep the caller's final commit/rollback boundary intact.

    The semantic-refresh executor intentionally commits only after every entry
    and postcondition passes, and rolls a dry run back. Releasing an outermost
    SQLite savepoint would commit an idle connection prematurely, so an idle
    writer starts one transaction here and leaves its successful boundary to
    the caller. Existing transactions receive a nested savepoint.
    """

    nested = conn.in_transaction
    if nested:
        conn.execute("SAVEPOINT source_reviewed_kpi_supersession")
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        if nested:
            conn.execute("ROLLBACK TO SAVEPOINT source_reviewed_kpi_supersession")
            conn.execute("RELEASE SAVEPOINT source_reviewed_kpi_supersession")
        else:
            conn.rollback()
        raise
    if nested:
        conn.execute("RELEASE SAVEPOINT source_reviewed_kpi_supersession")


def require_canonical_kpi_resolution(
    conn: sqlite3.Connection,
    *,
    fact_row_id: int,
    knowledge_cutoff: datetime,
) -> None:
    """Resolve one KPI row and prove that exact observation is canonical."""

    _ = require_exact_canonical_fact_row(
        conn,
        fact_table=KPI_FACTS,
        fact_row_id=fact_row_id,
        knowledge_cutoff=knowledge_cutoff,
    )


def _require_definition_head(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    expected_definition_head_id: str | None,
    expected_definition_revision: int,
) -> None:
    current = current_kpi_definition_revision(conn, kpi_definition_id=kpi_definition_id)
    actual = (
        None if current is None else current.kpi_definition_revision_id,
        0 if current is None else current.revision,
    )
    if actual != (expected_definition_head_id, expected_definition_revision):
        raise ValueError("KPI definition revision head changed after source review")


def _persist_definition_capture(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    definition_revision: IssuerKpiDefinitionRevision,
    expected_definition_head_id: str | None,
    expected_definition_revision: int,
    comparability_revisions: Sequence[KpiDefinitionComparabilityRevision],
) -> str:
    if kpi_definition_id != definition_revision.kpi_definition_id:
        raise ValueError("definition revision root does not match the KPI fact lifecycle")
    replay = kpi_definition_revision_by_id(
        conn,
        kpi_definition_revision_id=definition_revision.kpi_definition_revision_id,
    )
    if replay is not None:
        if replay.commitment_sha256 != definition_revision.commitment_sha256:
            raise ValueError("definition revision identity conflicts with reviewed content")
        expected_predecessor = definition_revision.supersedes_definition_revision_id
        expected_predecessor_revision = definition_revision.revision - 1
        if (expected_definition_head_id, expected_definition_revision) != (
            expected_predecessor,
            expected_predecessor_revision,
        ):
            raise ValueError("reviewed definition predecessor expectation is inconsistent")
        current = current_kpi_definition_revision(conn, kpi_definition_id=kpi_definition_id)
        if (
            current is None
            or current.kpi_definition_revision_id != replay.kpi_definition_revision_id
        ):
            raise ValueError("KPI definition revision head changed after source review")
        binding_id = replay.kpi_definition_revision_id
        for relation in comparability_revisions:
            if binding_id not in {
                relation.predecessor_definition_revision_id,
                relation.successor_definition_revision_id,
            }:
                raise ValueError("comparability capture must relate the captured definition")
            _ = persist_kpi_definition_comparability_revision(conn, relation)
        return binding_id
    _require_definition_head(
        conn,
        kpi_definition_id=kpi_definition_id,
        expected_definition_head_id=expected_definition_head_id,
        expected_definition_revision=expected_definition_revision,
    )
    persisted = persist_kpi_definition_revision(conn, definition_revision)
    binding_id = persisted.kpi_definition_revision_id
    for relation in comparability_revisions:
        if binding_id not in {
            relation.predecessor_definition_revision_id,
            relation.successor_definition_revision_id,
        }:
            raise ValueError("comparability capture must relate the captured definition")
        _ = persist_kpi_definition_comparability_revision(conn, relation)
    return binding_id


@dataclass(frozen=True, slots=True)
class SourceReviewedKpiCaptureResult:
    """Measured durable effects of one reviewed batch-capture attempt."""

    fact_id: int
    fact_inserted: bool
    semantic_context_inserted: bool
    definition_revision_inserted: bool
    comparability_revisions_inserted: int
    definition_revision_id: str


def insert_source_reviewed_kpi_capture(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    source_doc_id: int,
    kpi_definition_id: int,
    expected_definition_name: str,
    value: Decimal,
    unit: Unit,
    currency: Currency | None,
    locator: FactLocator,
    source_excerpt: str | None,
    reviewer: str,
    knowledge_at: datetime,
    context: KpiSemanticContext,
    definition_revision: IssuerKpiDefinitionRevision,
    comparability_revisions: Sequence[KpiDefinitionComparabilityRevision],
    expected_definition_head_id: str | None,
    expected_definition_revision: int,
) -> SourceReviewedKpiCaptureResult:
    """Atomically insert one reviewed batch fact at its exact registry root."""

    if knowledge_at.tzinfo is None:
        raise ValueError("source-reviewed KPI knowledge_at must be timezone-aware")
    if definition_revision.reviewed_by != reviewer:
        raise ValueError("source-reviewed KPI reviewer must match its definition")
    if (
        definition_revision.knowledge_at > knowledge_at
        or definition_revision.recorded_at > knowledge_at
    ):
        raise ValueError("fact review cannot predate its definition revision")
    with _source_review_transaction(conn):
        prior_definition = kpi_definition_revision_by_id(
            conn,
            kpi_definition_revision_id=definition_revision.kpi_definition_revision_id,
        )
        prior_relation_ids = {
            relation.comparability_revision_id
            for relation in comparability_revisions
            if conn.execute(
                "SELECT 1 FROM kpi_definition_comparability_revisions "
                "WHERE comparability_revision_id=?",
                (relation.comparability_revision_id,),
            ).fetchone()
            is not None
        }
        binding_id = _persist_definition_capture(
            conn,
            kpi_definition_id=kpi_definition_id,
            definition_revision=definition_revision,
            expected_definition_head_id=expected_definition_head_id,
            expected_definition_revision=expected_definition_revision,
            comparability_revisions=comparability_revisions,
        )
        fact_result: ExactDefinitionPersistResult = persist_kpi_value_at_exact_definition(
            conn,
            ticker=ticker,
            period_end=period_end,
            fiscal_period_type=fiscal_period_type,
            source_doc_id=source_doc_id,
            kpi_definition_id=kpi_definition_id,
            expected_definition_name=expected_definition_name,
            value=value,
            unit=unit,
            currency=currency,
            locator=locator,
            source_excerpt=source_excerpt,
            context=context,
            reviewed_by=reviewer,
            knowledge_at=knowledge_at,
            kpi_definition_revision_id=binding_id,
            extracted_by=f"source_review:{reviewer}:issuer_manifest_v2",
        )
        require_canonical_kpi_resolution(
            conn,
            fact_row_id=fact_result.fact_id,
            knowledge_cutoff=knowledge_at,
        )
        return SourceReviewedKpiCaptureResult(
            fact_id=fact_result.fact_id,
            fact_inserted=fact_result.inserted,
            semantic_context_inserted=fact_result.semantic_context_inserted,
            definition_revision_inserted=prior_definition is None,
            comparability_revisions_inserted=sum(
                relation.comparability_revision_id not in prior_relation_ids
                for relation in comparability_revisions
            ),
            definition_revision_id=binding_id,
        )


def bind_source_reviewed_kpi_definition(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    expected_fact_head_id: int,
    expected_definition_head_id: str | None,
    expected_definition_revision: int,
    reviewer: str,
    knowledge_at: datetime,
    context: KpiSemanticContext,
    definition_revision: IssuerKpiDefinitionRevision,
    comparability_revisions: Sequence[KpiDefinitionComparabilityRevision] = (),
) -> int:
    """Atomically bind one existing canonical fact to an exact reviewed definition."""
    if knowledge_at.tzinfo is None:
        raise ValueError("source-reviewed KPI knowledge_at must be timezone-aware")
    if (
        definition_revision.knowledge_at > knowledge_at
        or definition_revision.recorded_at > knowledge_at
    ):
        raise ValueError("fact review cannot predate its definition revision")
    with _source_review_transaction(conn):
        fact = conn.execute(
            "SELECT kpi_definition_id FROM kpi_facts WHERE id=?", (fact_id,)
        ).fetchone()
        if fact is None:
            raise ValueError("source-reviewed KPI fact is missing")
        successor = conn.execute(
            "SELECT id FROM kpi_facts WHERE supersedes_id=? ORDER BY id DESC LIMIT 1",
            (fact_id,),
        ).fetchone()
        actual_head = fact_id if successor is None else int(successor[0])
        if actual_head != expected_fact_head_id or expected_fact_head_id != fact_id:
            raise ValueError("source-reviewed KPI fact is not the exact current head")
        binding_id = _persist_definition_capture(
            conn,
            kpi_definition_id=int(fact["kpi_definition_id"]),
            definition_revision=definition_revision,
            expected_definition_head_id=expected_definition_head_id,
            expected_definition_revision=expected_definition_revision,
            comparability_revisions=comparability_revisions,
        )
        context_id = persist_kpi_semantic_context(
            conn,
            kpi_fact_id=fact_id,
            context=context,
            reviewed_by=reviewer,
            knowledge_at=knowledge_at,
            kpi_definition_revision_id=binding_id,
        )
        if context_id is None:
            raise RuntimeError("source-reviewed KPI semantic context table is unavailable")
        require_canonical_kpi_resolution(
            conn,
            fact_row_id=fact_id,
            knowledge_cutoff=knowledge_at,
        )
        return fact_id


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
    definition_revision: IssuerKpiDefinitionRevision | None = None,
    comparability_revisions: Sequence[KpiDefinitionComparabilityRevision] = (),
    expected_definition_head_id: str | None = None,
    expected_definition_revision: int | None = None,
) -> int:
    """Append exactly one governed successor, independent of filing chronology.

    This is intentionally separate from generic extraction/restatement logic.
    Owner-reviewed corrections may use the incumbent document, or a source that
    predates the corrupt extraction, while retaining the exact predecessor.
    """
    if knowledge_at.tzinfo is None:
        raise ValueError("source-reviewed KPI knowledge_at must be timezone-aware")
    if definition_revision is None and comparability_revisions:
        raise ValueError("comparability capture requires an exact definition revision")
    if definition_revision is None and expected_definition_revision is not None:
        raise ValueError("definition-head expectation requires a definition revision")
    if definition_revision is not None and expected_definition_revision is None:
        raise ValueError("definition capture requires an explicit expected head")
    if definition_revision is not None and (
        definition_revision.knowledge_at > knowledge_at
        or definition_revision.recorded_at > knowledge_at
    ):
        raise ValueError("fact review cannot predate its definition revision")
    with _source_review_transaction(conn):
        predecessor = conn.execute(
            "SELECT ticker,period_end,fiscal_period_type,kpi_definition_id "
            "FROM kpi_facts WHERE id=?",
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
        binding_id: str | None = None
        if definition_revision is not None:
            if expected_definition_revision is None:
                raise RuntimeError("validated definition head expectation disappeared")
            binding_id = _persist_definition_capture(
                conn,
                kpi_definition_id=int(predecessor["kpi_definition_id"]),
                definition_revision=definition_revision,
                expected_definition_head_id=expected_definition_head_id,
                expected_definition_revision=expected_definition_revision,
                comparability_revisions=comparability_revisions,
            )
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
            kpi_definition_revision_id=binding_id,
        )
        if context_id is None:
            raise RuntimeError("source-reviewed KPI semantic context table is unavailable")
        _ = record_restatement_observation(
            conn,
            fact_table=KPI_FACTS,
            superseded_id=predecessor_id,
            new_value=value,
        )
        require_canonical_kpi_resolution(
            conn,
            fact_row_id=new_id,
            knowledge_cutoff=knowledge_at,
        )
        return new_id
