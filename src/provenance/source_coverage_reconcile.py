"""Strict import and deterministic reconciliation for the source-coverage ledger.

The importer records a supplied reporting-universe snapshot.  It deliberately
does not turn an absent local capture into a claim that a document was absent:
when no exact evidence identity is present, the inventory must supply an
explicit status and reason.  Positive states are instead derived only from
immutable document, extraction, sealed-manifest, and successful-index lineage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.issuer_registry import (
    IdentifierType,
    IssuerRegistry,
    UnresolvedIssuerIdentityError,
)
from provenance.search_index_lineage import sealed_index_lineage
from provenance.source_coverage import (
    CoverageAssessment,
    CoverageStatus,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_coverage import (
    ExpectedDocument as LedgerExpectedDocument,
)
from provenance.source_expectation_lifecycle import (
    ExpectedDocumentLifecycle,
    persist_expected_document_lifecycle,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)

_Mode = Literal["dry_run", "apply"]
_ExplicitStatus = Literal[
    "available",
    "not_published",
    "not_discovered",
    "fetch_failed",
    "quarantined",
    "unsupported",
    "authority_unavailable",
]
_POLICY_NAME = "evidence_lineage_coverage"
_POLICY_VERSION = "1"


def _timeline(value: datetime) -> datetime:
    """Compare legacy naive SQLite clocks and offset-aware clocks on one UTC timeline."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExplicitAbsence(_ClosedModel):
    """Required, accountable import judgment when no exact evidence anchor exists."""

    coverage_status: _ExplicitStatus
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)
    material_dissent: bool = False

    @model_validator(mode="after")
    def _validate_reason_details(self) -> Self:
        keys = [key for key, _ in self.reason_details]
        if any(not key or not value for key, value in self.reason_details):
            raise ValueError("explicit absence reason details require non-empty keys and values")
        if len(keys) != len(set(keys)):
            raise ValueError("explicit absence reason details require unique keys")
        return self


class ExpectedDocumentImport(_ClosedModel):
    expected_document_key: str = Field(min_length=1, max_length=256)
    source_kind: Literal["sec_filing", "ir_document", "earnings_call"]
    document_type: str = Field(min_length=1, max_length=64)
    form_type: str | None = Field(default=None, max_length=64)
    accession_number: str | None = Field(default=None, max_length=64)
    source_url: str | None = None
    primary_document: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    filing_at: datetime | None = None
    expected_at: datetime | None = None
    expectation_basis: Literal["authoritative", "publisher_candidate", "policy_inferred"]
    absence: ExplicitAbsence | None = None
    dissent: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_period_and_dissent(self) -> Self:
        if (
            self.period_start is not None
            and self.period_end is not None
            and _timeline(self.period_end) < _timeline(self.period_start)
        ):
            raise ValueError("period_end must not precede period_start")
        if any(not item for item in self.dissent):
            raise ValueError("dissent entries must be non-empty")
        return self


class InventoryComponentImport(_ClosedModel):
    """One authoritative response required to establish the source universe."""

    component_key: str = Field(min_length=1, max_length=256)
    component_kind: Literal["primary", "historical_page", "crawl_page", "event_feed", "other"]
    source_url: str = Field(min_length=1)
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: Literal["succeeded", "failed"]
    required: bool = True
    failure_reason: str | None = Field(default=None, min_length=1, max_length=128)
    ordinal: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_component(self) -> Self:
        if self.outcome == "succeeded":
            if self.source_observation_id is None or self.failure_reason is not None:
                raise ValueError("successful inventory component requires source observation only")
        elif self.source_observation_id is not None or self.failure_reason is None:
            raise ValueError(
                "failed inventory component requires failure reason and no observation"
            )
        return self


class ExpectedDocumentWithdrawalImport(_ClosedModel):
    expected_document_key: str = Field(min_length=1, max_length=256)
    status: Literal["withdrawn_by_authority", "superseded_by_authority"]
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)
    authority_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    replacement_document_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def _validate_details(self) -> Self:
        keys = [key for key, _ in self.reason_details]
        if any(not key or not value for key, value in self.reason_details):
            raise ValueError("withdrawal reason details require non-empty values")
        if len(keys) != len(set(keys)):
            raise ValueError("withdrawal reason detail keys must be unique")
        if self.status == "superseded_by_authority":
            if (
                self.authority_observation_id is None
                or self.replacement_document_version_id is None
            ):
                raise ValueError(
                    "authority supersession requires its replacement document and observation"
                )
        elif self.replacement_document_version_id is not None:
            raise ValueError("only authority supersession may identify a replacement document")
        return self


class SourceCoverageImport(_ClosedModel):
    inventory_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str | None = Field(default=None, max_length=32)
    source_kind: Literal["sec_submissions", "ir_crawl", "earnings_events"]
    source_url: str = Field(min_length=1)
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: Literal["succeeded", "partial", "failed"]
    authoritative: bool
    retrieval_config_sha256: str = Field(min_length=64, max_length=64)
    collector_code_version: str = Field(min_length=1, max_length=255)
    started_at: datetime
    completed_at: datetime
    recorded_at: datetime
    reconciled_at: datetime
    components: tuple[InventoryComponentImport, ...] = ()
    expected_documents: tuple[ExpectedDocumentImport, ...] = ()
    withdrawals: tuple[ExpectedDocumentWithdrawalImport, ...] = ()
    apply: bool = False

    @model_validator(mode="after")
    def _validate_snapshot(self) -> Self:
        if self.outcome in {"succeeded", "partial"}:
            if self.source_observation_id is None:
                raise ValueError(
                    "successful or partial source inventory requires source_observation_id"
                )
            if not self.expected_documents and not self.withdrawals and not self.components:
                raise ValueError(
                    "successful or partial source inventory requires components, "
                    "expectations, or withdrawals"
                )
        elif self.source_observation_id is not None:
            raise ValueError("failed source inventory cannot claim source observation bytes")
        elif self.expected_documents:
            raise ValueError("failed source inventory must have zero expected documents")
        elif self.withdrawals:
            raise ValueError("failed source inventory cannot claim authority withdrawals")
        if _timeline(self.completed_at) < _timeline(self.started_at) or _timeline(
            self.recorded_at
        ) < _timeline(self.completed_at):
            raise ValueError("source inventory clocks are out of order")
        if _timeline(self.reconciled_at) < _timeline(self.recorded_at):
            raise ValueError("reconciled_at must not precede source inventory recorded_at")
        keys = [document.expected_document_key for document in self.expected_documents]
        if len(keys) != len(set(keys)):
            raise ValueError("expected_document_key values must be unique")
        withdrawal_keys = [item.expected_document_key for item in self.withdrawals]
        if len(withdrawal_keys) != len(set(withdrawal_keys)):
            raise ValueError("withdrawal expected_document_key values must be unique")
        if set(keys) & set(withdrawal_keys):
            raise ValueError("a document cannot be both expected and withdrawn")
        if self.revision == 1 and self.withdrawals:
            raise ValueError("first source inventory cannot withdraw prior expectations")
        component_keys = [component.component_key for component in self.components]
        component_ordinals = [component.ordinal for component in self.components]
        if len(component_keys) != len(set(component_keys)):
            raise ValueError("source inventory component keys must be unique")
        if len(component_ordinals) != len(set(component_ordinals)):
            raise ValueError("source inventory component ordinals must be unique")
        if self.components:
            complete = all(
                not component.required or component.outcome == "succeeded"
                for component in self.components
            )
            if self.outcome == "succeeded" and not complete:
                raise ValueError("succeeded source inventory requires all required components")
            if self.outcome == "partial" and complete:
                raise ValueError("partial source inventory requires a failed required component")
        return self


class SourceCoverageReconcileResult(_ClosedModel):
    mode: _Mode
    snapshot_id: str
    expected_document_count: int = Field(ge=0)
    assessment_statuses: tuple[str, ...]
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    policy_config_sha256: str


class _Plan(_ClosedModel):
    snapshot: SourceInventorySnapshot
    components: tuple[InventoryComponent, ...]
    inventory_seal: InventorySeal | None
    expected_documents: tuple[LedgerExpectedDocument, ...]
    lifecycle_revisions: tuple[ExpectedDocumentLifecycle, ...]
    assessments: tuple[CoverageAssessment, ...]
    policy_config_sha256: str


def load_source_coverage_import(path: Path) -> SourceCoverageImport:
    """Load exactly one strict JSON import file; unknown fields fail closed."""

    return SourceCoverageImport.model_validate_json(path.read_text(encoding="utf-8"))


def reconcile_source_coverage(
    conn: sqlite3.Connection, request: SourceCoverageImport
) -> SourceCoverageReconcileResult:
    """Plan or atomically append one inventory plus its defensible coverage state."""

    if not request.apply:
        return _result(_plan(conn, request), mode="dry_run", records_created=0, records_replayed=0)
    ledger = SourceCoverageLedger(conn)
    seal_store = SourceInventorySealStore(conn)
    created = 0
    replayed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = _plan(conn, request)
        persisted = ledger.persist(plan.snapshot)
        created += int(persisted.created)
        replayed += int(not persisted.created)
        for component in plan.components:
            persisted = seal_store.persist(component)
            created += int(persisted.created)
            replayed += int(not persisted.created)
        if plan.inventory_seal is not None:
            persisted = seal_store.persist(plan.inventory_seal)
            created += int(persisted.created)
            replayed += int(not persisted.created)
        for record in plan.expected_documents:
            persisted = ledger.persist(record)
            created += int(persisted.created)
            replayed += int(not persisted.created)
        for lifecycle in plan.lifecycle_revisions:
            persisted = persist_expected_document_lifecycle(conn, lifecycle)
            created += int(persisted.created)
            replayed += int(not persisted.created)
        for record in plan.assessments:
            persisted = ledger.persist(record)
            created += int(persisted.created)
            replayed += int(not persisted.created)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _result(plan, mode="apply", records_created=created, records_replayed=replayed)


def _plan(conn: sqlite3.Connection, request: SourceCoverageImport) -> _Plan:
    inventory = tuple(
        sorted(request.expected_documents, key=lambda item: item.expected_document_key)
    )
    snapshot_seed = _sha_json(
        {
            "inventory": request.model_dump(
                mode="json",
                exclude={
                    "apply",
                    "components",
                    "expected_documents",
                    "withdrawals",
                    "reconciled_at",
                },
            ),
            "components": [
                component.model_dump(mode="json")
                for component in sorted(request.components, key=lambda item: item.ordinal)
            ],
            "expected_documents": [document.model_dump(mode="json") for document in inventory],
            "withdrawals": [
                withdrawal.model_dump(mode="json")
                for withdrawal in sorted(
                    request.withdrawals, key=lambda item: item.expected_document_key
                )
            ],
        }
    )
    snapshot_id = f"source-snapshot:{snapshot_seed}"
    snapshot = SourceInventorySnapshot(
        snapshot_id=snapshot_id,
        idempotency_key=f"source-snapshot:{snapshot_seed}",
        inventory_key=request.inventory_key,
        revision=request.revision,
        issuer_id=request.issuer_id,
        ticker=request.ticker,
        source_kind=request.source_kind,
        source_url=request.source_url,
        source_observation_id=request.source_observation_id,
        outcome=request.outcome,
        authoritative=request.authoritative,
        retrieval_config_sha256=request.retrieval_config_sha256,
        collector_code_version=request.collector_code_version,
        started_at=request.started_at,
        completed_at=request.completed_at,
        recorded_at=request.recorded_at,
        supersedes_snapshot_id=_prior_snapshot_id(conn, request),
    )
    expected_documents = tuple(
        _expected_document(snapshot_id, request, imported) for imported in inventory
    )
    policy_sha = _sha_json(
        {
            "policy_name": _POLICY_NAME,
            "policy_version": _POLICY_VERSION,
            "lineage": "exact_accession_or_source_url_then_extraction_sealed_index",
        }
    )
    assessments = tuple(
        _assessment(conn, expected, imported, request, policy_sha)
        for expected, imported in zip(expected_documents, inventory, strict=True)
    )
    lifecycle_revisions = _expectation_lifecycle(conn, request, snapshot_id, expected_documents)
    components = tuple(
        _inventory_component(snapshot_id, request, imported)
        for imported in sorted(request.components, key=lambda item: item.ordinal)
    )
    inventory_seal = (
        None
        if not components
        else InventorySeal(
            snapshot_id=snapshot_id,
            expected_component_count=len(components),
            component_digest_sha256=component_digest(components),
            completion_status=(
                "complete"
                if all(
                    not component.required or component.outcome == "succeeded"
                    for component in components
                )
                else "incomplete"
            ),
            sealed_at=request.recorded_at,
        )
    )
    return _Plan(
        snapshot=snapshot,
        components=components,
        inventory_seal=inventory_seal,
        expected_documents=expected_documents,
        lifecycle_revisions=lifecycle_revisions,
        assessments=assessments,
        policy_config_sha256=policy_sha,
    )


def _expectation_lifecycle(
    conn: sqlite3.Connection,
    request: SourceCoverageImport,
    snapshot_id: str,
    expected_documents: tuple[LedgerExpectedDocument, ...],
) -> tuple[ExpectedDocumentLifecycle, ...]:
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'expected_document_lifecycle_revisions'"
        ).fetchone()
        is None
    ):
        return ()
    if request.source_observation_id is None:
        if expected_documents or request.withdrawals:
            raise ValueError("expectation lifecycle requires an authority observation")
        return ()
    current_by_key = {document.expected_document_key: document for document in expected_documents}
    prior_keys: set[str] = set()
    if request.revision > 1:
        prior_snapshot = _prior_snapshot_id(conn, request)
        if prior_snapshot is None:
            raise ValueError("source inventory prior snapshot unexpectedly missing")
        prior_keys = {
            str(row[0])
            for row in conn.execute(
                "SELECT expected_document_key FROM expected_documents WHERE snapshot_id = ?",
                (prior_snapshot,),
            ).fetchall()
        }
    disappeared = prior_keys - set(current_by_key)
    withdrawals = {item.expected_document_key: item for item in request.withdrawals}
    if disappeared != set(withdrawals):
        missing = sorted(disappeared - set(withdrawals))
        unexpected = sorted(set(withdrawals) - disappeared)
        details: list[str] = []
        if missing:
            details.append("missing withdrawals: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected withdrawals: " + ", ".join(unexpected))
        raise ValueError(
            "source inventory cannot silently change prior expectations; " + "; ".join(details)
        )
    records: list[ExpectedDocumentLifecycle] = []
    for key in sorted((*current_by_key, *withdrawals)):
        replay = conn.execute(
            "SELECT lifecycle_id, revision, supersedes_lifecycle_id "
            "FROM expected_document_lifecycle_revisions "
            "WHERE source_inventory_snapshot_id = ? AND expected_document_key = ?",
            (snapshot_id, key),
        ).fetchone()
        prior = conn.execute(
            "SELECT lifecycle_id, revision FROM expected_document_lifecycle_revisions "
            "WHERE inventory_key = ? AND expected_document_key = ? "
            "ORDER BY revision DESC LIMIT 1",
            (request.inventory_key, key),
        ).fetchone()
        if replay is not None:
            lifecycle_revision = int(replay[1])
            supersedes = None if replay[2] is None else str(replay[2])
        else:
            lifecycle_revision = 1 if prior is None else int(prior[1]) + 1
            supersedes = None if prior is None else str(prior[0])
        if key in current_by_key:
            status: Literal["expected", "withdrawn_by_authority", "superseded_by_authority"] = (
                "expected"
            )
            document_id = current_by_key[key].expected_document_id
            authority_observation_id = request.source_observation_id
            reason_code = "authority_inventory_current"
            reason_details = (("snapshot_revision", str(request.revision)),)
        else:
            withdrawal = withdrawals[key]
            status = withdrawal.status
            document_id = None
            reason_code = withdrawal.reason_code
            authority_observation_id = (
                withdrawal.authority_observation_id or request.source_observation_id
            )
            reason_details = _validated_withdrawal_details(
                conn,
                request=request,
                withdrawal=withdrawal,
                authority_observation_id=authority_observation_id,
            )
        semantic = {
            "inventory_key": request.inventory_key,
            "expected_document_key": key,
            "source_inventory_snapshot_id": snapshot_id,
            "revision": lifecycle_revision,
            "status": status,
            "expected_document_id": document_id,
            "authority_observation_id": authority_observation_id,
            "reason_code": reason_code,
            "reason_details": reason_details,
            "effective_at": request.completed_at,
            "knowledge_at": request.reconciled_at,
            "recorded_at": request.reconciled_at,
            "supersedes_lifecycle_id": supersedes,
        }
        seed = _sha_json(semantic)
        records.append(
            ExpectedDocumentLifecycle(
                lifecycle_id=f"expected-lifecycle:{seed}",
                idempotency_key=f"expected-lifecycle:{seed}",
                inventory_key=request.inventory_key,
                expected_document_key=key,
                source_inventory_snapshot_id=snapshot_id,
                revision=lifecycle_revision,
                status=status,
                expected_document_id=document_id,
                authority_observation_id=authority_observation_id,
                reason_code=reason_code,
                reason_details=reason_details,
                effective_at=request.completed_at,
                knowledge_at=request.reconciled_at,
                recorded_at=request.reconciled_at,
                supersedes_lifecycle_id=supersedes,
            )
        )
    return tuple(records)


def _validated_withdrawal_details(
    conn: sqlite3.Connection,
    *,
    request: SourceCoverageImport,
    withdrawal: ExpectedDocumentWithdrawalImport,
    authority_observation_id: str,
) -> tuple[tuple[str, str], ...]:
    observation = conn.execute(
        "SELECT retrieved_at FROM evidence_source_observations WHERE observation_id = ?",
        (authority_observation_id,),
    ).fetchone()
    if observation is None:
        raise ValueError("withdrawal authority observation does not exist")
    if _timeline(datetime.fromisoformat(str(observation[0]))) > _timeline(request.reconciled_at):
        raise ValueError("withdrawal authority observation is unavailable at reconciliation cutoff")
    replacement_id = withdrawal.replacement_document_version_id
    if replacement_id is None:
        return withdrawal.reason_details
    replacement = conn.execute(
        "SELECT observation_id, issuer_id, recorded_at FROM evidence_document_versions "
        "WHERE document_version_id = ?",
        (replacement_id,),
    ).fetchone()
    if replacement is None:
        raise ValueError("authority supersession replacement document does not exist")
    if _timeline(datetime.fromisoformat(str(replacement[2]))) > _timeline(request.reconciled_at):
        raise ValueError(
            "authority supersession replacement document is unavailable at reconciliation cutoff"
        )
    if str(replacement[0]) != authority_observation_id:
        raise ValueError("authority supersession observation does not own the replacement")
    expected_issuer = _supersession_canonical_issuer_id(
        conn,
        request.issuer_id,
        request.reconciled_at,
    )
    replacement_issuer = _supersession_canonical_issuer_id(
        conn,
        str(replacement[1]),
        request.reconciled_at,
    )
    if replacement_issuer != expected_issuer:
        raise ValueError("authority supersession replacement crosses issuer identity")
    return (
        *withdrawal.reason_details,
        ("replacement_document_version_id", replacement_id),
        ("replacement_authority_observation_id", authority_observation_id),
    )


def _inventory_component(
    snapshot_id: str,
    request: SourceCoverageImport,
    imported: InventoryComponentImport,
) -> InventoryComponent:
    seed = _sha_json(
        {
            "snapshot_id": snapshot_id,
            "component": imported.model_dump(mode="json"),
        }
    )
    return InventoryComponent(
        component_id=f"source-component:{seed}",
        idempotency_key=f"source-component:{seed}",
        snapshot_id=snapshot_id,
        component_key=imported.component_key,
        component_kind=imported.component_kind,
        source_url=imported.source_url,
        source_observation_id=imported.source_observation_id,
        outcome=imported.outcome,
        required=imported.required,
        failure_reason=imported.failure_reason,
        ordinal=imported.ordinal,
        recorded_at=request.recorded_at,
    )


def _prior_snapshot_id(conn: sqlite3.Connection, request: SourceCoverageImport) -> str | None:
    if request.revision == 1:
        return None
    row = conn.execute(
        "SELECT snapshot_id FROM source_inventory_snapshots "
        "WHERE inventory_key = ? AND revision = ?",
        (request.inventory_key, request.revision - 1),
    ).fetchone()
    if row is None:
        raise ValueError("source inventory revision requires its exact prior snapshot")
    return str(row[0])


def _expected_document(
    snapshot_id: str, request: SourceCoverageImport, imported: ExpectedDocumentImport
) -> LedgerExpectedDocument:
    seed = _sha_text(snapshot_id + "\0" + imported.expected_document_key)
    return LedgerExpectedDocument(
        expected_document_id=f"expected-document:{seed}",
        idempotency_key=f"expected-document:{seed}",
        snapshot_id=snapshot_id,
        expected_document_key=imported.expected_document_key,
        issuer_id=request.issuer_id,
        ticker=request.ticker,
        source_kind=imported.source_kind,
        document_type=imported.document_type,
        form_type=imported.form_type,
        accession_number=imported.accession_number,
        source_url=imported.source_url,
        primary_document=imported.primary_document,
        period_start=imported.period_start,
        period_end=imported.period_end,
        filing_at=imported.filing_at,
        expected_at=imported.expected_at,
        expectation_basis=imported.expectation_basis,
        recorded_at=request.recorded_at,
    )


def _assessment(
    conn: sqlite3.Connection,
    expected: LedgerExpectedDocument,
    imported: ExpectedDocumentImport,
    request: SourceCoverageImport,
    policy_sha: str,
) -> CoverageAssessment:
    status: CoverageStatus
    document = _exact_document(
        conn,
        imported,
        issuer_id=request.issuer_id,
        reconciled_at=request.reconciled_at,
    )
    if document is None:
        if imported.absence is None:
            raise ValueError(
                "expected document without exact evidence requires explicit absence status and reason: "
                + imported.expected_document_key
            )
        status = imported.absence.coverage_status
        reason_code = imported.absence.reason_code
        details = _details(
            imported.absence.reason_details,
            imported.dissent,
            (("expected_document_key", imported.expected_document_key),),
        )
        lineage: tuple[str | None, str | None, str | None, str | None] = (None, None, None, None)
        material_dissent = imported.absence.material_dissent or bool(imported.dissent)
        decision_kind: Literal["deterministic", "imported"] = "imported"
    else:
        document_version_id = document[0]
        extraction_run_id = _successful_extraction(conn, document_version_id)
        if extraction_run_id is None:
            status = _captured_or_quarantined(conn, document_version_id)
            reason_code = "exact_document_evidence"
            manifest_id = None
            index_run_id = None
        else:
            indexed = _sealed_index_lineage(conn, document_version_id, extraction_run_id)
            if indexed is None:
                status = "extracted"
                reason_code = "succeeded_extraction"
                manifest_id = None
                index_run_id = None
            else:
                status = "indexed"
                reason_code = "sealed_index_lineage"
                manifest_id, index_run_id = indexed
        details = _details(
            (
                ("document_version_id", document_version_id),
                *_imported_absence_details(imported.absence),
            ),
            imported.dissent,
            (("expected_document_key", imported.expected_document_key),),
        )
        lineage = (document_version_id, extraction_run_id, manifest_id, index_run_id)
        material_dissent = bool(imported.dissent) or document[1] > 1 or imported.absence is not None
        decision_kind = "deterministic"
    document_version_id, extraction_run_id, manifest_id, index_run_id = lineage
    semantic = {
        "expected_document_id": expected.expected_document_id,
        "coverage_status": status,
        "document_version_id": document_version_id,
        "extraction_run_id": extraction_run_id,
        "manifest_id": manifest_id,
        "index_run_id": index_run_id,
        "reason_code": reason_code,
        "reason_details": details,
        "decision_kind": decision_kind,
        "policy_config_sha256": policy_sha,
        "effective_at": request.completed_at,
        "knowledge_at": request.reconciled_at,
        "recorded_at": request.reconciled_at,
        "material_dissent": material_dissent,
    }
    fingerprint = _sha_json(semantic)
    existing = conn.execute(
        "SELECT assessment_id, revision FROM source_coverage_assessments WHERE idempotency_key = ?",
        (f"coverage-assessment:{fingerprint}",),
    ).fetchone()
    if existing is None:
        latest = conn.execute(
            "SELECT assessment_id, revision FROM source_coverage_assessments "
            "WHERE expected_document_id = ? ORDER BY revision DESC LIMIT 1",
            (expected.expected_document_id,),
        ).fetchone()
        revision = 1 if latest is None else int(latest[1]) + 1
        supersedes = None if latest is None else str(latest[0])
        assessment_id = f"coverage-assessment:{_sha_text(fingerprint + chr(0) + str(revision))}"
    else:
        assessment_id = str(existing[0])
        revision = int(existing[1])
        supersedes = _existing_supersedes(conn, assessment_id)
    return CoverageAssessment(
        assessment_id=assessment_id,
        idempotency_key=f"coverage-assessment:{fingerprint}",
        expected_document_id=expected.expected_document_id,
        revision=revision,
        coverage_status=status,
        document_version_id=document_version_id,
        extraction_run_id=extraction_run_id,
        manifest_id=manifest_id,
        index_run_id=index_run_id,
        reason_code=reason_code,
        reason_details=details,
        decision_kind=decision_kind,
        policy_name=_POLICY_NAME,
        policy_version=_POLICY_VERSION,
        policy_config_sha256=policy_sha,
        effective_at=request.completed_at,
        knowledge_at=request.reconciled_at,
        recorded_at=request.reconciled_at,
        supersedes_assessment_id=supersedes,
        material_dissent=material_dissent,
    )


def _exact_document(
    conn: sqlite3.Connection,
    imported: ExpectedDocumentImport,
    *,
    issuer_id: str,
    reconciled_at: datetime,
) -> tuple[str, int] | None:
    """Resolve one issuer-scoped logical document without collapsing ambiguity.

    A publisher URL is allowed to remain stable while its bytes and document
    version advance.  Multiple versions of one ``document_key`` are therefore
    a history, while multiple document keys are an identity ambiguity.  The
    latter must not be resolved by a recency tie-break.
    """

    predicates = [
        "document.document_type = ?",
        "document.recorded_at <= ?",
    ]
    identity_params: list[object] = [imported.document_type, reconciled_at]
    for column, value in (
        ("form_type", imported.form_type),
        ("period_start", imported.period_start),
        ("period_end", imported.period_end),
    ):
        if value is not None:
            predicates.append(f"document.{column} = ?")
            identity_params.append(value)
    if imported.accession_number is not None:
        predicates.append("document.accession_number = ?")
        identity_params.append(imported.accession_number)
    document_predicates = " AND ".join(predicates)
    rows: list[sqlite3.Row | tuple[object, ...]]
    if imported.source_url is not None:
        filing_clause = ""
        filing_params: tuple[object, ...] = ()
        if imported.filing_at is not None:
            filing_clause = " AND observation.filing_at = ?"
            filing_params = (imported.filing_at,)
        query = (
            "SELECT document_version_id, document_key, version_sequence, recorded_issuer_id "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "FROM ("
            "SELECT DISTINCT document.document_version_id, document.document_key, "
            "document.version_sequence, document.issuer_id AS recorded_issuer_id "
            "FROM evidence_document_versions AS document "
            "JOIN evidence_source_observations AS observation "
            "ON observation.observation_id = document.observation_id "
            "WHERE observation.source_url = ? "
            "AND observation.retrieved_at <= ? "
            f"AND {document_predicates}{filing_clause} UNION "
            "SELECT DISTINCT document.document_version_id, document.document_key, "
            "document.version_sequence, document.issuer_id AS recorded_issuer_id "
            "FROM evidence_document_versions AS document "
            "JOIN evidence_document_observation_links AS link "
            "ON link.document_version_id = document.document_version_id "
            "JOIN evidence_source_observations AS observation "
            "ON observation.observation_id = link.observation_id "
            "WHERE observation.source_url = ? "
            "AND observation.retrieved_at <= ? AND link.linked_at <= ? "
            f"AND {document_predicates}{filing_clause}"
            ") ORDER BY document_key, version_sequence DESC, document_version_id "
            "LIMIT 101"
        )
        params = (
            imported.source_url,
            reconciled_at,
            *identity_params,
            *filing_params,
            imported.source_url,
            reconciled_at,
            reconciled_at,
            *identity_params,
            *filing_params,
        )
        rows = conn.execute(query, params).fetchall()
    elif imported.accession_number is not None:
        filing_clause = ""
        filing_params: tuple[object, ...] = ()
        if imported.filing_at is not None:
            filing_clause = " AND observation.filing_at = ?"
            filing_params = (imported.filing_at,)
        primary_clause = ""
        primary_params: tuple[object, ...] = ()
        if imported.primary_document is not None:
            primary_clause = " AND (observation.source_url LIKE ? OR observation.source_url LIKE ?)"
            primary_params = (
                f"%/{imported.primary_document}",
                f"%/{imported.primary_document}?%",
            )
        query = (
            "SELECT document.document_version_id, document.document_key, "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "document.version_sequence, document.issuer_id "
            "FROM evidence_document_versions AS document "
            "JOIN evidence_source_observations AS observation "
            "ON observation.observation_id = document.observation_id "
            f"WHERE {document_predicates} AND observation.retrieved_at <= ? "
            f"{filing_clause}{primary_clause} "
            "ORDER BY document.document_key, document.version_sequence DESC, "
            "document.document_version_id LIMIT 101"
        )
        params = (
            *identity_params,
            reconciled_at,
            *filing_params,
            *primary_params,
        )
        rows = conn.execute(query, params).fetchall()
    else:
        return None
    canonical_expected = _canonical_issuer_id(conn, issuer_id, reconciled_at)
    scoped = [
        row
        for row in rows
        if _canonical_issuer_id(conn, str(row[3]), reconciled_at) == canonical_expected
    ]
    if not scoped:
        return None
    document_keys = {str(row[1]) for row in scoped}
    if len(rows) == 101 or len(document_keys) != 1:
        return None
    latest_sequence = max(int(str(row[2])) for row in scoped)
    latest = [row for row in scoped if int(str(row[2])) == latest_sequence]
    if len(latest) != 1:
        return None
    return str(latest[0][0]), len(scoped)


def _canonical_issuer_id(conn: sqlite3.Connection, issuer_id: str, reconciled_at: datetime) -> str:
    """Resolve a recorded issuer through clock-safe registry decisions when present."""

    if not _table_exists(conn, "issuer_entities"):
        return issuer_id
    canonical = conn.execute(
        "SELECT issuer_id FROM issuer_entities WHERE issuer_id = ? AND created_at <= ?",
        (issuer_id, reconciled_at),
    ).fetchone()
    if canonical is not None:
        return str(canonical[0])
    binding = conn.execute(
        "SELECT issuer_id, outcome FROM legacy_issuer_binding_revisions "
        "WHERE recorded_issuer_id = ? AND recorded_at <= ? "
        "ORDER BY revision DESC LIMIT 1",
        (issuer_id, reconciled_at),
    ).fetchone()
    if binding is not None and str(binding[1]) == "selected" and binding[0] is not None:
        return str(binding[0])
    authority_identity = _authority_identifier(issuer_id)
    if authority_identity is None:
        return issuer_id
    identifier_type, normalized_value = authority_identity
    resolution_key = f"{identifier_type}:{normalized_value}"
    resolved = conn.execute(
        "SELECT resolution.outcome, assertion.issuer_id, assertion.recorded_at "
        "FROM issuer_identifier_resolution_outcomes AS resolution "
        "LEFT JOIN issuer_identifier_assertions AS assertion "
        "ON assertion.assertion_id = resolution.selected_assertion_id "
        "WHERE resolution.resolution_key = ? AND resolution.recorded_at <= ? "
        "ORDER BY resolution.revision DESC LIMIT 1",
        (resolution_key, reconciled_at),
    ).fetchone()
    if (
        resolved is None
        or str(resolved[0]) != "selected"
        or resolved[1] is None
        or resolved[2] is None
        or _timeline(datetime.fromisoformat(str(resolved[2]))) > _timeline(reconciled_at)
    ):
        return issuer_id
    return str(resolved[1])


def _supersession_canonical_issuer_id(
    conn: sqlite3.Connection,
    issuer_id: str,
    reconciled_at: datetime,
) -> str:
    """Resolve supersession ownership through the canonical cutoff-aware registry."""

    if not _table_exists(conn, "issuer_entities"):
        # Historical schemas predate canonical issuer identity. Their immutable
        # evidence IDs remain the only available ownership boundary.
        return issuer_id
    direct = conn.execute(
        "SELECT issuer_id FROM issuer_entities WHERE issuer_id = ? AND created_at <= ?",
        (issuer_id, reconciled_at),
    ).fetchone()
    if direct is not None:
        return str(direct[0])
    registry = IssuerRegistry(conn)
    try:
        authority_identity = _authority_identifier(issuer_id)
        if authority_identity is None:
            canonical = registry.canonicalize_recorded_issuer(
                issuer_id,
                knowledge_at=reconciled_at,
            )
        else:
            canonical = registry.resolve_identifier(
                authority_identity[0],
                authority_identity[1],
                knowledge_at=reconciled_at,
            )
    except UnresolvedIssuerIdentityError as exc:
        raise ValueError(
            "authority supersession issuer identity is unresolved at reconciliation cutoff"
        ) from exc
    if canonical.material_dissent:
        raise ValueError(
            "authority supersession issuer identity has material dissent at reconciliation cutoff"
        )
    return canonical.issuer_id


def _authority_identifier(issuer_id: str) -> tuple[IdentifierType, str] | None:
    prefix, separator, value = issuer_id.partition(":")
    if not separator or not value:
        return None
    if prefix in {"sec-cik", "sec_cik"}:
        return "sec_cik", value
    if prefix == "lei":
        return "lei", value
    if prefix in {"sedar-profile", "sedar_profile"}:
        return "sedar_profile", value
    return None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _successful_extraction(conn: sqlite3.Connection, document_version_id: str) -> str | None:
    row = conn.execute(
        "SELECT extraction_run_id FROM evidence_extraction_runs "
        "WHERE document_version_id = ? AND outcome = 'succeeded' "
        "AND EXISTS (SELECT 1 FROM v_evidence_current AS node "
        "WHERE node.extraction_run_id = evidence_extraction_runs.extraction_run_id "
        "AND node.node_kind <> 'document') "
        "ORDER BY completed_at DESC, extraction_run_id DESC LIMIT 1",
        (document_version_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _captured_or_quarantined(
    conn: sqlite3.Connection, document_version_id: str
) -> Literal["captured", "quarantined"]:
    row = conn.execute(
        "SELECT COUNT(*) AS count, SUM(availability_state = 'present') AS present, "
        "SUM(availability_state = 'quarantined') AS quarantined "
        "FROM v_evidence_blob_locations_current AS location "
        "JOIN evidence_document_versions AS document ON document.blob_sha256 = location.blob_sha256 "
        "WHERE document.document_version_id = ?",
        (document_version_id,),
    ).fetchone()
    if row is not None and int(row[0]) > 0 and int(row[1] or 0) == 0 and int(row[2] or 0) > 0:
        return "quarantined"
    return "captured"


def _sealed_index_lineage(
    conn: sqlite3.Connection, document_version_id: str, extraction_run_id: str
) -> tuple[str, str] | None:
    lineage = sealed_index_lineage(
        conn,
        document_version_id=document_version_id,
        extraction_run_id=extraction_run_id,
    )
    if lineage is None:
        return None
    return lineage.manifest_id, lineage.index_run_id


def _details(
    primary: tuple[tuple[str, str], ...],
    dissent: tuple[str, ...],
    extra: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    details = list(primary) + list(extra)
    details.extend((f"dissent_{index}", value) for index, value in enumerate(dissent, start=1))
    keys = [key for key, _ in details]
    if len(keys) != len(set(keys)):
        raise ValueError("coverage reason detail keys conflict")
    return tuple(sorted(details))


def _imported_absence_details(absence: ExplicitAbsence | None) -> tuple[tuple[str, str], ...]:
    """Keep a supplied contrary absence judgment visible when lineage wins."""

    if absence is None:
        return ()
    details = [
        ("imported_coverage_status", absence.coverage_status),
        ("imported_reason_code", absence.reason_code),
    ]
    details.extend((f"imported_detail_{key}", value) for key, value in absence.reason_details)
    return tuple(details)


def _existing_supersedes(conn: sqlite3.Connection, assessment_id: str) -> str | None:
    row = conn.execute(
        "SELECT supersedes_assessment_id FROM source_coverage_assessments WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("existing coverage assessment disappeared during reconciliation")
    return None if row[0] is None else str(row[0])


def _result(
    plan: _Plan, *, mode: _Mode, records_created: int, records_replayed: int
) -> SourceCoverageReconcileResult:
    return SourceCoverageReconcileResult(
        mode=mode,
        snapshot_id=plan.snapshot.snapshot_id,
        expected_document_count=len(plan.expected_documents),
        assessment_statuses=tuple(item.coverage_status for item in plan.assessments),
        records_created=records_created,
        records_replayed=records_replayed,
        policy_config_sha256=plan.policy_config_sha256,
    )


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, default=_json_default, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")
