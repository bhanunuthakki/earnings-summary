"""Prepare and apply explicit non-admission dispositions for scoped legacy KPIs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReference,
    ReportKpiReferenceDisposition,
    ReportKpiReferenceStatus,
    current_report_kpi_reference_disposition,
    load_report_kpi_reference_inventory,
    persist_report_kpi_reference_disposition,
)
from pipeline.kpi_semantic_review import (
    MAX_KPI_SEMANTIC_REVIEW_ITEMS,
    build_kpi_semantic_review_batch,
)
from pipeline.kpi_semantic_scope import portfolio_tickers, scoped_kpi_definitions
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
)
from provenance.financial_fact_resolution import canonical_fact_relation


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class KpiFactQuarantineDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    expected_fact_head_id: int = Field(gt=0)
    expected_context_head_id: int | None = Field(default=None, gt=0)
    expected_context_revision: int = Field(ge=0)
    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    stored_definition_name: str = Field(min_length=1, max_length=256)
    stored_period_end: date
    reason_code: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _heads_match(self) -> KpiFactQuarantineDisposition:
        if self.fact_id != self.expected_fact_head_id:
            raise ValueError("quarantine disposition must target the current fact head")
        if (self.expected_context_head_id is None) != (self.expected_context_revision == 0):
            raise ValueError("context head and revision expectation conflict")
        return self


class ReportKpiReferenceDispositionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ReportKpiReference
    expected_resolution_head_id: int | None = Field(default=None, gt=0)
    expected_resolution_revision: int = Field(ge=0)
    disposition: ReportKpiReferenceDisposition

    @model_validator(mode="after")
    def _heads_match(self) -> ReportKpiReferenceDispositionEntry:
        if (self.expected_resolution_head_id is None) != (self.expected_resolution_revision == 0):
            raise ValueError("reference disposition head and revision expectation conflict")
        return self


class KpiSemanticDispositionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi_semantic_dispositions.v1"] = "kpi_semantic_dispositions.v1"
    user_id: str = Field(min_length=1, max_length=128)
    logical_idempotency_key: str = Field(min_length=1, max_length=256)
    reviewer: str = Field(min_length=1, max_length=128)
    knowledge_at: datetime
    expected_schema_revision: str = Field(min_length=1, max_length=160)
    expected_database_instance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_restore_evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_dispositions: tuple[KpiFactQuarantineDisposition, ...]
    report_reference_dispositions: tuple[ReportKpiReferenceDispositionEntry, ...]

    @field_validator("knowledge_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("disposition manifest knowledge_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _not_empty(self) -> KpiSemanticDispositionManifest:
        if not self.fact_dispositions and not self.report_reference_dispositions:
            raise ValueError("disposition manifest has no work")
        if len({entry.fact_id for entry in self.fact_dispositions}) != len(self.fact_dispositions):
            raise ValueError("disposition manifest repeats a fact")
        reference_keys = {
            (
                entry.reference.ticker,
                entry.reference.source_path,
                entry.reference.json_pointer,
            )
            for entry in self.report_reference_dispositions
        }
        if len(reference_keys) != len(self.report_reference_dispositions):
            raise ValueError("disposition manifest repeats a report reference")
        return self

    def content_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class KpiSemanticDispositionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inserted_context_rows: int = Field(ge=0)
    replayed_context_rows: int = Field(ge=0)
    inserted_reference_rows: int = Field(ge=0)
    replayed_reference_rows: int = Field(ge=0)


def prepare_kpi_semantic_disposition_manifest(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str,
    reviewer: str,
    logical_idempotency_key: str,
    expected_schema_revision: str,
    review_bundle_sha256: str,
    backup_restore_evidence_id: str,
    knowledge_at: datetime | None = None,
) -> KpiSemanticDispositionManifest:
    """Plan exact quarantine/unresolved revisions without inferring semantics."""
    observed = knowledge_at or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("disposition manifest knowledge_at must be timezone-aware")
    identity_rows = conn.execute(
        "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
    ).fetchall()
    if len(identity_rows) != 1:
        raise ValueError("database lineage identity is missing or ambiguous")
    database_instance_sha256 = hashlib.sha256(str(identity_rows[0][0]).encode("utf-8")).hexdigest()
    review = build_kpi_semantic_review_batch(
        conn,
        repo_root=repo_root,
        user_id=user_id,
        limit=MAX_KPI_SEMANTIC_REVIEW_ITEMS,
        observed_at=observed,
    )
    if review.truncated:
        raise ValueError("KPI semantic review exceeds the bounded disposition manifest")
    fact_entries: list[KpiFactQuarantineDisposition] = []
    for item in review.items:
        if item.context_status == KpiSemanticStatus.QUARANTINED.value:
            continue
        current = current_kpi_semantic_context(conn, kpi_fact_id=item.fact_id)
        fact_entries.append(
            KpiFactQuarantineDisposition(
                fact_id=item.fact_id,
                expected_fact_head_id=item.fact_id,
                expected_context_head_id=None if current is None else current.id,
                expected_context_revision=0 if current is None else current.revision,
                ticker=item.ticker,
                kpi_definition_id=item.kpi_definition_id,
                stored_definition_name=item.kpi_name,
                stored_period_end=date.fromisoformat(item.period_end[:10]),
                reason_code=item.quarantine_reason_code,
            )
        )
    tickers = portfolio_tickers(conn, user_id=user_id)
    inventory = load_report_kpi_reference_inventory(repo_root, tickers)
    source_failures = tuple(
        source for source in inventory.source_states if source.reason_code is not None
    )
    if source_failures:
        reason_codes = ",".join(
            f"{source.ticker}:{source.reason_code}" for source in source_failures
        )
        raise ValueError(f"report KPI configuration inventory is incomplete: {reason_codes}")
    references = {
        (reference.ticker, reference.source_path, reference.json_pointer): reference
        for reference in inventory.references
    }
    reference_entries: list[ReportKpiReferenceDispositionEntry] = []
    for row in scoped_kpi_definitions(conn, repo_root=repo_root, user_id=user_id):
        if row.kpi_definition_id is not None or row.report_reference_status is not None:
            continue
        if row.report_reference_pointer is None:
            raise ValueError("undisposed report KPI reference lacks an exact pointer")
        key = (
            row.ticker,
            f"micro_thesis/holdings/{row.ticker}.json",
            row.report_reference_pointer,
        )
        reference = references.get(key)
        if reference is None:
            raise ValueError("undisposed report KPI reference is no longer current")
        current = current_report_kpi_reference_disposition(
            conn, user_id=user_id, reference=reference
        )
        reference_entries.append(
            ReportKpiReferenceDispositionEntry(
                reference=reference,
                expected_resolution_head_id=None if current is None else current.id,
                expected_resolution_revision=0 if current is None else current.revision,
                disposition=ReportKpiReferenceDisposition(
                    status=ReportKpiReferenceStatus.UNRESOLVED,
                    reason_code=(
                        row.report_reference_reason_code or "no_matching_reported_definition"
                    ),
                ),
            )
        )
    return KpiSemanticDispositionManifest(
        user_id=user_id,
        logical_idempotency_key=logical_idempotency_key,
        reviewer=reviewer,
        knowledge_at=observed,
        expected_schema_revision=expected_schema_revision,
        expected_database_instance_sha256=database_instance_sha256,
        review_bundle_sha256=review_bundle_sha256,
        backup_restore_evidence_id=backup_restore_evidence_id,
        fact_dispositions=tuple(fact_entries),
        report_reference_dispositions=tuple(reference_entries),
    )


def apply_kpi_semantic_disposition_manifest(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    manifest: KpiSemanticDispositionManifest,
) -> KpiSemanticDispositionResult:
    """Validate current heads and append the manifest's explicit dispositions."""
    revision_row = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    if [str(row[0]) for row in revision_row] != [manifest.expected_schema_revision]:
        raise ValueError("database schema revision changed after disposition preparation")
    identity_rows = conn.execute(
        "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
    ).fetchall()
    if (
        len(identity_rows) != 1
        or hashlib.sha256(str(identity_rows[0][0]).encode("utf-8")).hexdigest()
        != manifest.expected_database_instance_sha256
    ):
        raise ValueError("database lineage identity changed after disposition preparation")
    owner_tickers = frozenset(portfolio_tickers(conn, user_id=manifest.user_id))
    relation = canonical_fact_relation(conn, "kpi_facts")
    if relation.selection_mode != "resolved_view":
        raise ValueError("KPI semantic dispositions require the resolved current-fact view")
    inserted_context = replayed_context = 0
    for entry in manifest.fact_dispositions:
        if entry.ticker not in owner_tickers:
            raise ValueError("fact disposition escaped the owner portfolio scope")
        row = conn.execute(
            f"SELECT fact.id,fact.period_end,fact.kpi_definition_id,definition.name "  # nosec B608 -- resolver-owned relation
            f"FROM {relation.sql} fact JOIN kpi_definitions definition "
            "ON definition.id=fact.kpi_definition_id WHERE fact.id=? AND UPPER(fact.ticker)=?",
            (entry.expected_fact_head_id, entry.ticker),
        ).fetchone()
        if row is None or (int(row[0]), str(row[1])[:10], int(row[2]), str(row[3])) != (
            entry.fact_id,
            entry.stored_period_end.isoformat(),
            entry.kpi_definition_id,
            entry.stored_definition_name,
        ):
            raise ValueError("fact disposition target changed after preparation")
        current = current_kpi_semantic_context(conn, kpi_fact_id=entry.fact_id)
        current_identity = None if current is None else current.id
        current_revision = 0 if current is None else current.revision
        context = KpiSemanticContext(
            metric_name_as_reported=entry.stored_definition_name,
            reported_period_end=entry.stored_period_end,
            period_role=KpiPeriodRole.UNKNOWN,
            publication_lane=KpiPublicationLane.UNCLASSIFIED,
            accounting_basis=KpiAccountingBasis.UNKNOWN,
            consolidation_scope=KpiConsolidationScope.UNKNOWN,
            dimensions={},
            unit_scale=KpiUnitScale.UNKNOWN,
            status=KpiSemanticStatus.QUARANTINED,
            reason_code=entry.reason_code,
        )
        if current is not None and current.context == context:
            replayed_context += 1
            continue
        if (current_identity, current_revision) != (
            entry.expected_context_head_id,
            entry.expected_context_revision,
        ):
            raise ValueError("fact semantic head changed after disposition preparation")
        inserted_id = persist_kpi_semantic_context(
            conn,
            kpi_fact_id=entry.fact_id,
            context=context,
            reviewed_by=manifest.reviewer,
            knowledge_at=manifest.knowledge_at,
        )
        if inserted_id == current_identity:
            replayed_context += 1
        else:
            inserted_context += 1
    current_inventory = load_report_kpi_reference_inventory(repo_root, tuple(sorted(owner_tickers)))
    if any(source.reason_code is not None for source in current_inventory.source_states):
        raise ValueError("report KPI configuration inventory changed or became incomplete")
    current_references = {
        (reference.ticker, reference.source_path, reference.json_pointer): reference
        for reference in current_inventory.references
    }
    inserted_reference = replayed_reference = 0
    for entry in manifest.report_reference_dispositions:
        reference = entry.reference
        if reference.ticker not in owner_tickers:
            raise ValueError("report reference disposition escaped the owner portfolio scope")
        key = (reference.ticker, reference.source_path, reference.json_pointer)
        if current_references.get(key) != reference:
            raise ValueError("report reference changed after disposition preparation")
        current = current_report_kpi_reference_disposition(
            conn, user_id=manifest.user_id, reference=reference
        )
        current_identity = None if current is None else current.id
        current_revision = 0 if current is None else current.revision
        if (
            current is not None
            and current.reference == reference
            and current.disposition == entry.disposition
        ):
            replayed_reference += 1
            continue
        if (current_identity, current_revision) != (
            entry.expected_resolution_head_id,
            entry.expected_resolution_revision,
        ):
            raise ValueError("report reference disposition head changed after preparation")
        inserted_id = persist_report_kpi_reference_disposition(
            conn,
            user_id=manifest.user_id,
            reference=reference,
            disposition=entry.disposition,
            reviewed_by=manifest.reviewer,
            knowledge_at=manifest.knowledge_at,
        )
        if inserted_id == current_identity:
            replayed_reference += 1
        else:
            inserted_reference += 1
    return KpiSemanticDispositionResult(
        inserted_context_rows=inserted_context,
        replayed_context_rows=replayed_context,
        inserted_reference_rows=inserted_reference,
        replayed_reference_rows=replayed_reference,
    )


__all__ = [
    "KpiFactQuarantineDisposition",
    "KpiSemanticDispositionManifest",
    "KpiSemanticDispositionResult",
    "ReportKpiReferenceDispositionEntry",
    "apply_kpi_semantic_disposition_manifest",
    "prepare_kpi_semantic_disposition_manifest",
]
