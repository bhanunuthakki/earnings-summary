"""Small read-only rollout projection over existing provenance authorities.

This is an evidence census, not a completeness receipt or cutover verifier.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from provenance.population_completeness import PopulationTemporalScope
from provenance.reporting_document_scope import reporting_document_scope


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadinessEvidence(_Record):
    state: Literal["unknown", "missing", "observed_unverified"]
    counts: dict[str, int | None]
    blockers: tuple[str, ...]
    verification_required: str
    evidence_scope: Literal["issuer", "global"] = "issuer"


class IssuerReadiness(_Record):
    issuer_id: str
    acquisition: ReadinessEvidence
    extraction: ReadinessEvidence
    semantic_admission: ReadinessEvidence
    consumer_readiness: ReadinessEvidence


class CohortReadinessReport(_Record):
    schema_version: Literal["cohort-readiness-census/v1"] = "cohort-readiness-census/v1"
    mode: Literal["read_only"] = "read_only"
    issuer_scope: tuple[str, ...]
    temporal_scope: PopulationTemporalScope
    issuers: tuple[IssuerReadiness, ...]
    consumer_audit_executed: Literal[False] = False
    has_blockers: bool
    readiness_evaluated: Literal[False] = False
    evidence_clock_precision: Literal["whole_seconds"] = "whole_seconds"
    receipt_clock_precision: Literal["microseconds"] = "microseconds"
    verification_command: str = "execution/audit_data_cutover_readiness.py"
    cutover_evaluator: str = "execution/evaluate_population_cutover.py"


def _count(conn: sqlite3.Connection, query: str, params: tuple[str, ...]) -> int | None:
    try:
        return int(conn.execute(query, params).fetchone()[0])
    except sqlite3.OperationalError as exc:
        if "no such table:" in str(exc) or "no such column:" in str(exc):
            return None
        raise


def _stage(
    counts: dict[str, int | None],
    primary: str,
    missing: str,
    verification: str,
    *,
    blockers: tuple[str, ...] = (),
    evidence_scope: Literal["issuer", "global"] = "issuer",
) -> ReadinessEvidence:
    if any(value is None for value in counts.values()):
        state: Literal["unknown", "missing", "observed_unverified"] = "unknown"
        reasons = (*blockers, "schema_evidence_unavailable")
    elif counts[primary] == 0:
        state = "missing"
        reasons = (*blockers, missing)
    else:
        state = "observed_unverified"
        reasons = blockers
    return ReadinessEvidence(
        state=state,
        counts=counts,
        blockers=reasons,
        evidence_scope=evidence_scope,
        verification_required=verification,
    )


def _acquisition(
    conn: sqlite3.Connection, issuer: str, scope: PopulationTemporalScope
) -> ReadinessEvidence:
    observed = scope.observed_through.isoformat()
    try:
        inventory_ids = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT inventory.snapshot_id FROM source_inventory_snapshots inventory "
                "WHERE inventory.issuer_id=? AND datetime(inventory.recorded_at)<=datetime(?) "
                "AND NOT EXISTS (SELECT 1 FROM source_inventory_snapshots newer "
                "WHERE newer.inventory_key=inventory.inventory_key "
                "AND newer.revision>inventory.revision "
                "AND datetime(newer.recorded_at)<=datetime(?)) ORDER BY inventory.snapshot_id",
                (issuer, observed, observed),
            )
        )
    except sqlite3.OperationalError as exc:
        if "no such table:" not in str(exc) and "no such column:" not in str(exc):
            raise
        return _stage(
            {"inventories": None},
            "inventories",
            "source_inventory_missing",
            "acquisition_completeness_not_verified",
        )
    counts: dict[str, int | None] = {"inventories": len(inventory_ids)}
    blockers: list[str] = []
    if inventory_ids:
        try:
            decisions, _, incomplete = reporting_document_scope(
                conn,
                scope.knowledge_cutoff,
                scope.observed_through,
                inventory_snapshot_ids=inventory_ids,
            )
        except (sqlite3.OperationalError, ValueError):
            counts["expected_reporting_documents"] = None
            blockers.append("reporting_document_scope_unavailable")
        else:
            counts["incomplete_inventories"] = incomplete
            counts["expected_reporting_documents"] = sum(
                item.outcome == "governed_reporting" for item in decisions
            )
            counts["unresolved_documents"] = sum(item.outcome == "unresolved" for item in decisions)
            counts["excluded_supporting_documents"] = sum(
                item.outcome == "excluded_supporting" for item in decisions
            )
            for item in decisions:
                if item.outcome != "excluded_supporting":
                    key = f"coverage_{item.coverage_status}"
                    counts[key] = (counts.get(key) or 0) + 1
            if incomplete:
                blockers.append("source_inventory_incomplete")
            if counts["unresolved_documents"]:
                blockers.append("reporting_document_scope_unresolved")
            if any(
                item.outcome == "governed_reporting"
                and item.coverage_status not in {"captured", "extracted", "indexed"}
                for item in decisions
            ):
                blockers.append("reporting_documents_not_captured")
    return _stage(
        counts,
        "inventories",
        "source_inventory_missing",
        "acquisition_completeness_not_verified",
        blockers=tuple(blockers),
    )


def _extraction(
    conn: sqlite3.Connection, issuer: str, scope: PopulationTemporalScope
) -> ReadinessEvidence:
    cutoff, observed = scope.knowledge_cutoff.isoformat(), scope.observed_through.isoformat()
    params = (issuer, cutoff, observed, cutoff, observed)
    counts = {
        "processing_obligations": _count(
            conn,
            (
                "SELECT COUNT(*) FROM document_processing_obligation_revisions obligation JOIN "
                "source_obligation_revisions source ON "
                "source.obligation_revision_id=obligation.source_obligation_revision_id WHERE "
                "source.issuer_id=? AND datetime(obligation.knowledge_at)<=datetime(?) AND "
                "datetime(obligation.recorded_at)<=datetime(?) AND NOT EXISTS (SELECT 1 FROM "
                "document_processing_obligation_revisions newer WHERE "
                "newer.processing_obligation_key=obligation.processing_obligation_key AND "
                "newer.revision>obligation.revision AND datetime(newer.knowledge_at)<=datetime(?) AND "
                "datetime(newer.recorded_at)<=datetime(?))"
            ),
            params,
        )
    }
    for name, status in (
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("quarantined", "quarantined"),
        ("not_applicable", "not_applicable"),
        ("source_unavailable", "source_unavailable"),
    ):
        counts[name] = _count(
            conn,
            (
                "SELECT COUNT(*) FROM document_processing_obligation_revisions obligation JOIN "
                "source_obligation_revisions source ON "
                "source.obligation_revision_id=obligation.source_obligation_revision_id WHERE "
                "source.issuer_id=? AND datetime(obligation.knowledge_at)<=datetime(?) AND "
                "datetime(obligation.recorded_at)<=datetime(?) AND NOT EXISTS (SELECT 1 FROM "
                "document_processing_obligation_revisions newer WHERE "
                "newer.processing_obligation_key=obligation.processing_obligation_key AND "
                "newer.revision>obligation.revision AND datetime(newer.knowledge_at)<=datetime(?) AND "
                "datetime(newer.recorded_at)<=datetime(?)) AND EXISTS (SELECT 1 FROM "
                "document_processing_disposition_headers disposition JOIN "
                "document_processing_disposition_seals seal USING (processing_disposition_id) WHERE "
                "disposition.processing_obligation_revision_id=obligation.processing_obligation_revision_id "
                "AND disposition.terminal_status=? AND datetime(disposition.knowledge_at)<=datetime(?) "
                "AND datetime(disposition.recorded_at)<=datetime(?) AND "
                "datetime(seal.sealed_at)<=datetime(?))"
            ),
            (*params, status, cutoff, observed, observed),
        )
    blockers = tuple(
        f"processing_{name}"
        for name in ("failed", "quarantined", "source_unavailable")
        if counts[name]
    )
    if all(value is not None for value in counts.values()):
        terminal = sum(
            value or 0 for key, value in counts.items() if key != "processing_obligations"
        )
        counts["without_sealed_disposition"] = (counts["processing_obligations"] or 0) - terminal
        if counts["without_sealed_disposition"]:
            blockers += ("processing_dispositions_missing",)
    return _stage(
        counts,
        "processing_obligations",
        "processing_obligations_missing",
        "extraction_completeness_not_verified",
        blockers=blockers,
    )


def _semantic(
    conn: sqlite3.Connection, issuer: str, scope: PopulationTemporalScope
) -> ReadinessEvidence:
    cutoff, observed = scope.knowledge_cutoff.isoformat(), scope.observed_through.isoformat()
    params = (issuer, cutoff, observed)
    counts = {
        "fact_cells": _count(
            conn,
            (
                "SELECT COUNT(*) FROM fact_cells_v2 cell JOIN reporting_entities entity USING "
                "(reporting_entity_id) WHERE entity.issuer_id=? AND "
                "datetime(cell.knowledge_at)<=datetime(?) AND datetime(cell.recorded_at)<=datetime(?)"
            ),
            params,
        )
    }

    counts["observations"] = _count(
        conn,
        (
            "SELECT COUNT(*) FROM fact_observations_v2 observation JOIN fact_cells_v2 cell USING "
            "(fact_cell_id) JOIN reporting_entities entity USING (reporting_entity_id) WHERE "
            "entity.issuer_id=? AND datetime(cell.knowledge_at)<=datetime(?) AND "
            "datetime(cell.recorded_at)<=datetime(?) AND "
            "datetime(observation.knowledge_at)<=datetime(?) AND "
            "datetime(observation.recorded_at)<=datetime(?)"
        ),
        (*params, cutoff, observed),
    )
    counts["observations_with_publication_seal"] = _count(
        conn,
        (
            "SELECT COUNT(*) FROM fact_observations_v2 observation JOIN fact_cells_v2 cell USING "
            "(fact_cell_id) JOIN reporting_entities entity USING (reporting_entity_id) WHERE "
            "entity.issuer_id=? AND datetime(cell.knowledge_at)<=datetime(?) AND "
            "datetime(cell.recorded_at)<=datetime(?) AND "
            "datetime(observation.knowledge_at)<=datetime(?) AND "
            "datetime(observation.recorded_at)<=datetime(?) AND EXISTS (SELECT 1 FROM "
            "source_fact_publication_members member JOIN source_fact_publication_seals seal USING "
            "(publication_id) WHERE member.record_kind='fact_observation' AND "
            "member.record_id=observation.observation_id AND "
            "datetime(member.recorded_at)<=datetime(?) AND datetime(seal.sealed_at)<=datetime(?))"
        ),
        (*params, cutoff, observed, observed, observed),
    )
    counts["canonical_metric_cells"] = _count(
        conn,
        "SELECT COUNT(*) FROM canonical_metric_cells cell "
        "JOIN reporting_entities entity USING (reporting_entity_id) WHERE entity.issuer_id=? "
        "AND datetime(cell.knowledge_at)<=datetime(?) AND datetime(cell.recorded_at)<=datetime(?)",
        params,
    )
    blockers = ()
    if counts["canonical_metric_cells"] == 0:
        blockers += ("canonical_metric_cells_missing",)
    observations_count = counts["observations"]
    published_count = counts["observations_with_publication_seal"]
    if (
        observations_count is not None
        and published_count is not None
        and published_count < observations_count
    ):
        blockers += ("source_fact_publication_seals_missing",)
    return _stage(
        counts,
        "observations",
        "source_fact_observations_missing",
        "semantic_admission_and_publication_not_verified",
        blockers=blockers,
    )


def _exact_cutover_receipts(conn: sqlite3.Connection, scope: PopulationTemporalScope) -> int | None:
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    try:
        # SQLite datetime() removes fractions. Compare parsed clocks without that loss.
        rows = conn.execute(
            "SELECT knowledge_cutoff,observed_through FROM population_cutover_receipts"
        )
        return sum(
            utc(datetime.fromisoformat(str(row[0]))) == utc(scope.knowledge_cutoff)
            and utc(datetime.fromisoformat(str(row[1]))) == utc(scope.observed_through)
            for row in rows
        )
    except sqlite3.OperationalError as exc:
        if "no such table:" in str(exc) or "no such column:" in str(exc):
            return None
        raise


def audit_cohort_readiness(
    conn: sqlite3.Connection, issuer_ids: tuple[str, ...], scope: PopulationTemporalScope
) -> CohortReadinessReport:
    """Inspect only explicit canonical issuer IDs; never infer a ticker binding."""
    if not issuer_ids or any(not item.strip() for item in issuer_ids):
        raise ValueError("at least one nonempty issuer ID is required")
    issuer_ids = tuple(sorted(set(issuer_ids)))
    try:
        known = {
            str(row[0])
            for row in conn.execute(
                "SELECT issuer_id FROM issuer_entities WHERE datetime(created_at)<=datetime(?)",
                (scope.observed_through.isoformat(),),
            )
        }
    except sqlite3.OperationalError as exc:
        raise ValueError("canonical issuer identity evidence is unavailable") from exc
    if any(issuer not in known for issuer in issuer_ids):
        raise ValueError("cohort includes an unknown canonical issuer ID at the observation clock")
    receipts = _exact_cutover_receipts(conn, scope)
    consumer = _stage(
        {"population_cutover_receipts_at_exact_clocks": receipts},
        "population_cutover_receipts_at_exact_clocks",
        "exact_population_cutover_receipt_missing",
        "global_consumer_audit_and_population_verification_required",
        evidence_scope="global",
    )
    issuers = tuple(
        IssuerReadiness(
            issuer_id=issuer,
            acquisition=_acquisition(conn, issuer, scope),
            extraction=_extraction(conn, issuer, scope),
            semantic_admission=_semantic(conn, issuer, scope),
            consumer_readiness=consumer,
        )
        for issuer in issuer_ids
    )
    return CohortReadinessReport(
        issuer_scope=issuer_ids,
        temporal_scope=scope,
        issuers=issuers,
        has_blockers=any(
            stage.blockers
            for item in issuers
            for stage in (
                item.acquisition,
                item.extraction,
                item.semantic_admission,
                item.consumer_readiness,
            )
        ),
    )
