"""Validate or apply a source-reviewed, append-only KPI repair manifest.

Dry runs and applies use the same locked validation. An apply additionally
requires an exact owner-approved manifest hash and a PASS receipt from Sol
bound to the latest dry-run receipt. No series shape or metric name is used to
infer a correction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_WINDOWS_STATE_ROOT = Path(
    os.environ.get("EARNINGS_SUMMARY_STATE_ROOT")
    or Path.home() / ".gemini" / "antigravity" / "scratch" / "earnings-summary"
)
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from backup_restore_readiness_receipt import (  # noqa: E402
    BackupRestoreReadinessReceipt,
    validate_receipt_for_source,
)
from fetch_windows_review_bundle import (  # noqa: E402
    WindowsReviewPins,
    identity_sha256,
    validate_pinned_identity,
)

from models.documents import SourceType  # noqa: E402
from models.facts import Currency, FactLocator, Unit  # noqa: E402
from operations.kpi_repair_receipts import (  # noqa: E402
    KpiRepairAttemptReceipt,
    KpiRepairJudgeReceipt,
    canonical_sha256,
    repair_executor_code_sha256,
    seal_attempt,
)
from operations.review_bundle import (  # noqa: E402
    OperationsReviewBundle,
    database_lineage_identity,
    review_code_identity,
)
from pipeline.kpi_definition_revisions import (  # noqa: E402
    IssuerKpiDefinitionRevision,
    KpiDefinitionComparabilityRevision,
    current_kpi_definition_comparability_revision,
    current_kpi_definition_revision,
    kpi_definition_revision_by_id,
    validate_kpi_definition_comparability_candidate,
    validate_kpi_definition_revision_candidate,
)
from pipeline.kpi_semantic_review import (  # noqa: E402
    KpiEvidenceLocatorCoordinates,
    fact_locator_from_evidence_coordinates,
)
from pipeline.kpi_semantic_scope import portfolio_tickers, scoped_kpi_definitions  # noqa: E402
from pipeline.kpi_semantics import (  # noqa: E402
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiSemanticContext,
    KpiUnitScale,
    current_kpi_semantic_context,
    normalize_source_numeric,
    parse_source_numeric,
    persist_kpi_semantic_context,
    validate_admitted_unit_scale,
)
from pipeline.kpi_source_review import (  # noqa: E402
    bind_source_reviewed_kpi_definition,
    insert_source_reviewed_kpi_supersession,
    require_canonical_kpi_resolution,
)
from pipeline.queries import open_db  # noqa: E402
from provenance.evidence_ledger import EvidenceLocator  # noqa: E402
from provenance.financial_fact_resolution import canonical_fact_relation  # noqa: E402
from provenance.fulltext_extractor_identity import (  # noqa: E402
    resolve_fulltext_extractor_identity,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402

_SHA256 = r"^[0-9a-f]{64}$"
# Five minutes matches the repository's trusted-evidence clock-skew allowance while
# remaining far too small to move a decision across a reporting or thesis horizon.
MAX_KNOWLEDGE_AT_FUTURE_SKEW = timedelta(minutes=5)
_REVIEWABLE_SOURCE_TYPES = frozenset(
    {
        SourceType.SEC_XBRL,
        SourceType.SEC_S1,
        SourceType.IR_DOC,
        SourceType.MANUAL_CSV,
        SourceType.MANUAL_ENTRY,
    }
)


class RepairBlockedError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class KpiAuthorityManifest(Protocol):
    review_bundle_sha256: str
    expected_schema_revision: str
    backup_restore_evidence_id: str


@contextmanager
def _repair_database(
    *,
    live_db: Path,
    backup: BackupRestoreReadinessReceipt,
    apply: bool,
) -> Generator[Path]:
    """Yield the live DB for apply or an isolated snapshot clone for dry-run.

    Opening the live SQLite database with a writer-capable connection can
    change its WAL freshness token even when the transaction is rolled back.
    A dry-run therefore exercises the exact write shape against a disposable
    clone of the already verified backup snapshot while all authority and
    freshness checks remain bound to the live canonical database.
    """

    if apply:
        yield live_db
        return

    snapshot = Path(backup.snapshot_resolved_path)
    with tempfile.TemporaryDirectory(prefix="kpi-repair-dry-run-") as temp_root:
        clone = Path(temp_root) / "portfolio-dry-run.db"
        try:
            shutil.copy2(snapshot, clone)
        except OSError as exc:
            raise RepairBlockedError("backup_restore_snapshot_clone_failed") from exc
        try:
            clone_size = clone.stat().st_size
            with clone.open("rb") as clone_file:
                clone_sha256 = hashlib.file_digest(clone_file, "sha256").hexdigest()
        except OSError as exc:
            raise RepairBlockedError("backup_restore_snapshot_clone_failed") from exc
        if backup.snapshot_byte_size != clone_size or backup.snapshot_sha256 != clone_sha256:
            raise RepairBlockedError("backup_restore_snapshot_clone_identity_mismatch")
        yield clone


class SemanticEvidenceQuotes(BaseModel):
    """Exact source wording supporting each semantic classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_name_value: str = Field(min_length=1, max_length=256)
    metric_name_quote: str = Field(min_length=1, max_length=512)
    reported_period_end_value: date
    reported_period_quote: str = Field(min_length=1, max_length=512)
    accounting_basis_value: KpiAccountingBasis
    accounting_basis_quote: str = Field(min_length=1, max_length=512)
    consolidation_scope_value: KpiConsolidationScope
    consolidation_scope_quote: str = Field(min_length=1, max_length=512)
    unit_scale_value: KpiUnitScale
    unit_scale_quote: str = Field(min_length=1, max_length=512)
    dimension_values: dict[str, str] = Field(default_factory=dict)
    dimension_quotes: dict[str, str] = Field(default_factory=dict)


class RefreshEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["bind_existing", "supersede"]
    predecessor_resolution_state: Literal["canonical_current", "quarantined_legacy"] = (
        "canonical_current"
    )
    old_fact_id: int = Field(gt=0)
    expected_fact_head_id: int = Field(gt=0)
    expected_context_head_id: int | None = Field(default=None, gt=0)
    expected_context_revision: int = Field(ge=0)
    expected_old_source_doc_id: int = Field(gt=0)
    expected_old_source_sha256: str = Field(pattern=_SHA256)
    source_doc_id: int = Field(gt=0)
    source_content_sha256: str = Field(pattern=_SHA256)
    source_observation_version: str = Field(min_length=1, max_length=80)
    source_period_end: str | None = Field(default=None, min_length=10, max_length=40)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    evidence_locator_sha256: str = Field(pattern=_SHA256)
    fact_locator_sha256: str = Field(pattern=_SHA256)
    source_excerpt: str = Field(min_length=1, max_length=1024)
    source_value_text: str = Field(min_length=1, max_length=80)
    value: Decimal
    unit: Unit
    currency: Currency | None = None
    locator: FactLocator
    context: KpiSemanticContext
    semantic_evidence: SemanticEvidenceQuotes
    expected_definition_head_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_definition_revision: int = Field(default=0, ge=0)
    definition_revision: IssuerKpiDefinitionRevision | None = None
    comparability_revisions: tuple[KpiDefinitionComparabilityRevision, ...] = ()
    expected_inserted_fact_rows: Literal[0, 1]
    expected_inserted_context_rows: Literal[1] = 1

    @model_validator(mode="after")
    def _shape(self) -> RefreshEntry:
        if self.expected_fact_head_id != self.old_fact_id:
            raise ValueError("expected_fact_head_id must identify old_fact_id")
        if (self.expected_context_head_id is None) != (self.expected_context_revision == 0):
            raise ValueError("context head and revision-zero expectations conflict")
        if self.action == "bind_existing" and self.expected_inserted_fact_rows != 0:
            raise ValueError("bind_existing cannot insert a fact row")
        if self.action == "supersede" and self.expected_inserted_fact_rows != 1:
            raise ValueError("supersede must expect one fact row")
        if self.predecessor_resolution_state == "quarantined_legacy" and self.action != "supersede":
            raise ValueError("quarantined legacy predecessors may only be superseded")
        if self.context.status.value != "admitted":
            raise ValueError("repair context must be source-qualified")
        if self.locator.verbatim_snippet != self.source_excerpt:
            raise ValueError("fact locator must carry the exact source excerpt")
        locator_json = self.locator.to_json()
        locator_sha = (
            None
            if locator_json is None
            else hashlib.sha256(locator_json.encode("utf-8")).hexdigest()
        )
        if locator_sha != self.fact_locator_sha256:
            raise ValueError("fact locator hash mismatch")
        evidence = self.semantic_evidence
        if evidence.metric_name_value != self.context.metric_name_as_reported:
            raise ValueError("metric-name evidence value must match semantic context")
        if evidence.reported_period_end_value != self.context.reported_period_end:
            raise ValueError("reported-period evidence value must match semantic context")
        if evidence.accounting_basis_value is not self.context.accounting_basis:
            raise ValueError("accounting-basis evidence value must match semantic context")
        if evidence.consolidation_scope_value is not self.context.consolidation_scope:
            raise ValueError("consolidation-scope evidence value must match semantic context")
        if evidence.unit_scale_value is not self.context.unit_scale:
            raise ValueError("unit-scale evidence value must match semantic context")
        validate_admitted_unit_scale(self.unit, self.context.unit_scale)
        if evidence.dimension_values != self.context.dimensions:
            raise ValueError("dimension evidence values must exactly match semantic dimensions")
        if set(evidence.dimension_quotes) != set(self.context.dimensions):
            raise ValueError("dimension evidence quote keys must match semantic dimensions")
        if (self.expected_definition_head_id is None) != (self.expected_definition_revision == 0):
            raise ValueError("definition head and revision-zero expectations conflict")
        if self.definition_revision is not None:
            definition = self.definition_revision
            if definition.kpi_definition_id <= 0:
                raise ValueError("reviewed definition requires its canonical KPI root")
            if definition.reported_label != self.context.metric_name_as_reported:
                raise ValueError("definition label must match the reviewed semantic context")
            if definition.unit_key != self.unit:
                raise ValueError("definition unit must match the reviewed fact unit")
            if definition.unit_scale is not self.context.unit_scale:
                raise ValueError("definition scale must match the reviewed semantic context")
            if definition.accounting_basis is not self.context.accounting_basis:
                raise ValueError("definition basis must match the reviewed semantic context")
            if definition.consolidation_scope is not self.context.consolidation_scope:
                raise ValueError("definition scope must match the reviewed semantic context")
            if definition.dimensions != self.context.dimensions:
                raise ValueError("definition dimensions must match the reviewed semantic context")
            if definition.currency != self.currency:
                raise ValueError("definition currency must match the reviewed fact currency")
        return self


class RefreshManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "kpi_semantic_refresh.v5",
        "kpi_semantic_refresh.v6",
        "kpi_semantic_refresh.v7",
    ]
    user_id: str = Field(min_length=1, max_length=128)
    logical_idempotency_key: str = Field(min_length=1, max_length=256)
    reviewer: str = Field(min_length=1, max_length=128)
    knowledge_at: datetime
    review_bundle_sha256: str = Field(pattern=_SHA256)
    expected_schema_revision: str = Field(min_length=1, max_length=160)
    backup_restore_evidence_id: str = Field(pattern=_SHA256)
    entries: tuple[RefreshEntry, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _version_requires_explicit_entry_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = cast(dict[str, object], value)
        schema_version = payload.get("schema_version")
        if schema_version not in {"kpi_semantic_refresh.v6", "kpi_semantic_refresh.v7"}:
            return payload
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, (list, tuple)):
            return payload
        entries = cast(list[object] | tuple[object, ...], raw_entries)
        if any(
            isinstance(entry, dict)
            and "predecessor_resolution_state" not in cast(dict[str, object], entry)
            for entry in entries
        ):
            raise ValueError(
                "kpi_semantic_refresh.v6 requires predecessor_resolution_state on every entry"
            )
        if schema_version == "kpi_semantic_refresh.v7" and any(
            not isinstance(entry, RefreshEntry)
            and (
                not isinstance(entry, dict)
                or not {
                    "expected_definition_head_id",
                    "expected_definition_revision",
                    "definition_revision",
                    "comparability_revisions",
                }.issubset(cast(dict[str, object], entry))
            )
            for entry in entries
        ):
            raise ValueError("kpi_semantic_refresh.v7 requires complete definition capture fields")
        return payload

    @model_validator(mode="after")
    def _schema_matches_predecessor_contract(self) -> RefreshManifest:
        if self.schema_version == "kpi_semantic_refresh.v5" and any(
            entry.predecessor_resolution_state != "canonical_current" for entry in self.entries
        ):
            raise ValueError("kpi_semantic_refresh.v5 supports canonical-current predecessors only")
        if self.schema_version == "kpi_semantic_refresh.v7":
            if any(entry.definition_revision is None for entry in self.entries):
                raise ValueError("kpi_semantic_refresh.v7 requires a reviewed definition revision")
            for entry in self.entries:
                definition = entry.definition_revision
                if definition is None:
                    continue
                if (
                    definition.reviewed_by != self.reviewer
                    or definition.knowledge_at > self.knowledge_at
                    or definition.recorded_at > self.knowledge_at
                ):
                    raise ValueError("definition revision is not bound to the manifest review")
                for relation in entry.comparability_revisions:
                    if (
                        relation.reviewed_by != self.reviewer
                        or relation.knowledge_at > self.knowledge_at
                        or relation.recorded_at > self.knowledge_at
                    ):
                        raise ValueError(
                            "comparability revision is not bound to the manifest review"
                        )
            definitions = [
                entry.definition_revision
                for entry in self.entries
                if entry.definition_revision is not None
            ]
            relations = [
                relation for entry in self.entries for relation in entry.comparability_revisions
            ]
            duplicate_checks: tuple[tuple[list[object], str], ...] = (
                (
                    [definition.kpi_definition_revision_id for definition in definitions],
                    "definition revision identities",
                ),
                (
                    [definition.idempotency_key for definition in definitions],
                    "definition idempotency keys",
                ),
                (
                    [definition.commitment_sha256 for definition in definitions],
                    "definition commitments",
                ),
                (
                    [relation.comparability_revision_id for relation in relations],
                    "comparability revision identities",
                ),
                (
                    [relation.idempotency_key for relation in relations],
                    "comparability idempotency keys",
                ),
                (
                    [relation.commitment_sha256 for relation in relations],
                    "comparability commitments",
                ),
                (
                    [
                        frozenset(
                            (
                                relation.predecessor_definition_revision_id,
                                relation.successor_definition_revision_id,
                            )
                        )
                        for relation in relations
                    ],
                    "comparability pairs",
                ),
            )
            for values, label in duplicate_checks:
                if len(values) != len(set(values)):
                    raise ValueError(f"v7 manifest repeats {label}")
        elif any(
            entry.definition_revision is not None
            or entry.comparability_revisions
            or entry.expected_definition_head_id is not None
            or entry.expected_definition_revision != 0
            for entry in self.entries
        ):
            raise ValueError("legacy refresh manifests cannot carry definition capture")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned_contract(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized = handler(self)
        if not isinstance(serialized, dict):
            raise TypeError("refresh manifest serializer must return an object")
        payload = cast(dict[str, object], serialized)
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, (list, tuple)):
            raise TypeError("refresh manifest entries must serialize as an array")
        serialized_entries = cast(list[object] | tuple[object, ...], raw_entries)
        entries: list[dict[str, object]] = []
        for raw_entry in serialized_entries:
            if not isinstance(raw_entry, dict):
                raise TypeError("refresh manifest entries must serialize as objects")
            entry = cast(dict[str, object], raw_entry).copy()
            if self.schema_version == "kpi_semantic_refresh.v5":
                entry.pop("predecessor_resolution_state", None)
            if self.schema_version != "kpi_semantic_refresh.v7":
                for field in (
                    "expected_definition_head_id",
                    "expected_definition_revision",
                    "definition_revision",
                    "comparability_revisions",
                ):
                    entry.pop(field, None)
            entries.append(entry)
        return {**payload, "entries": entries}

    @field_validator("knowledge_at")
    @classmethod
    def _knowledge_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("knowledge_at must be timezone-aware")
        return value

    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def validate_manifest_knowledge_time(
    manifest: RefreshManifest,
    *,
    now: datetime,
) -> None:
    """Reject future decision authority before its timestamp becomes a cutoff."""
    if now.tzinfo is None:
        raise RepairBlockedError("validation_clock_not_timezone_aware")
    if manifest.knowledge_at > now.astimezone(UTC) + MAX_KNOWLEDGE_AT_FUTURE_SKEW:
        raise RepairBlockedError("manifest_knowledge_at_from_future")


class KpiRepairSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=32, max_length=32)
    state: Literal["passed", "applied", "replayed", "blocked", "failed"]
    receipt_path: str
    receipt_sha256: str = Field(pattern=_SHA256)
    blocker_codes: tuple[str, ...]


def _context_for_entry(entry: RefreshEntry) -> KpiSemanticContext:
    return entry.context.model_copy(update={"source_value_text": entry.source_value_text})


def _write_content_addressed(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = payload.rstrip() + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise RepairBlockedError("content_addressed_receipt_conflict")
        return
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _write_latest(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(payload.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def _schema_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1 or not rows[0][0]:
        raise RepairBlockedError("database_schema_not_singular")
    return str(rows[0][0])


def _validate_external_evidence(
    *,
    manifest: KpiAuthorityManifest,
    db_path: Path,
    review_bundle: OperationsReviewBundle,
    trusted_pins: WindowsReviewPins,
    backup: BackupRestoreReadinessReceipt,
    now: datetime,
    max_review_age: timedelta,
) -> None:
    try:
        validate_pinned_identity(bundle=review_bundle, pins=trusted_pins, now=now)
    except ValueError:
        raise RepairBlockedError("trusted_review_pin_mismatch") from None
    if review_bundle.content_sha256 != manifest.review_bundle_sha256:
        raise RepairBlockedError("review_bundle_hash_mismatch")
    if review_bundle.observed_at > now + timedelta(minutes=5):
        raise RepairBlockedError("review_bundle_from_future")
    if now - review_bundle.observed_at > max_review_age:
        raise RepairBlockedError("review_bundle_stale")
    scheduler_recorded_at = review_bundle.scheduler.observation.evidence_recorded_at
    if review_bundle.scheduler.observation.state != "current":
        raise RepairBlockedError("scheduler_runtime_evidence_unhealthy")
    if scheduler_recorded_at is not None and scheduler_recorded_at > now + timedelta(minutes=5):
        raise RepairBlockedError("scheduler_runtime_evidence_from_future")
    if scheduler_recorded_at is None or now - scheduler_recorded_at > max_review_age:
        raise RepairBlockedError("scheduler_runtime_evidence_stale")
    if review_bundle.schema_revision.matches is not True:
        raise RepairBlockedError("review_bundle_schema_unhealthy")
    if review_bundle.schema_revision.actual_heads != (manifest.expected_schema_revision,):
        raise RepairBlockedError("review_bundle_schema_revision_mismatch")
    if backup.evidence_id != manifest.backup_restore_evidence_id:
        raise RepairBlockedError("backup_restore_evidence_id_mismatch")
    reasons = validate_receipt_for_source(
        backup,
        source_db=db_path,
        source_revision=manifest.expected_schema_revision,
        require_current_identity=True,
    )
    if reasons:
        raise RepairBlockedError(reasons[0])


def _validate_apply_authority(
    *,
    db_path: Path,
    receipt_root: Path,
    review_bundle: OperationsReviewBundle,
) -> None:
    if sys.platform != "win32":
        raise RepairBlockedError("apply_requires_windows_authority")
    canonical_db = CANONICAL_WINDOWS_STATE_ROOT / "data" / "portfolio.db"
    canonical_receipt_root = CANONICAL_WINDOWS_STATE_ROOT / "data" / "operations" / "kpi_repairs"
    if db_path.resolve() != canonical_db.resolve():
        raise RepairBlockedError("apply_database_is_not_canonical_windows_authority")
    if receipt_root.resolve() != canonical_receipt_root.resolve():
        raise RepairBlockedError("apply_receipt_root_is_not_canonical_operations_surface")
    if (
        identity_sha256(review_code_identity(PROJECT_ROOT))
        != review_bundle.identity.code_instance_sha256
    ):
        raise RepairBlockedError("apply_code_identity_mismatch")


def _repair_lock_root(db_path: Path) -> Path:
    canonical_db = CANONICAL_WINDOWS_STATE_ROOT / "data" / "portfolio.db"
    if sys.platform == "win32" and db_path.resolve() == canonical_db.resolve():
        return CANONICAL_WINDOWS_STATE_ROOT
    return PROJECT_ROOT


# Shared public authority seam for append-only KPI mutation executors.
repair_database_authority = _repair_database
repair_lock_root = _repair_lock_root
schema_revision = _schema_revision
validate_external_repair_evidence = _validate_external_evidence


def _validate_source_binding(
    conn: sqlite3.Connection, entry: RefreshEntry
) -> tuple[SourceType, str]:
    document = conn.execute(
        "SELECT ticker,source_type,doc_type,period_end,sha256,fetched_at,file_path "
        "FROM documents WHERE id=?",
        (entry.source_doc_id,),
    ).fetchone()
    if document is None:
        raise RepairBlockedError("source_document_missing")
    if str(document["sha256"]) != entry.source_content_sha256:
        raise RepairBlockedError("source_content_identity_mismatch")
    if str(document["fetched_at"]) != entry.source_observation_version:
        raise RepairBlockedError("source_observation_version_mismatch")
    document_period_end = None if document["period_end"] is None else str(document["period_end"])
    if entry.source_period_end is None:
        if (
            str(document["doc_type"]) != "ir_historical_spreadsheet"
            or document_period_end is not None
        ):
            raise RepairBlockedError("source_period_mismatch")
    elif document_period_end != entry.source_period_end:
        raise RepairBlockedError("source_period_mismatch")
    source_type = SourceType(str(document["source_type"]))
    if source_type not in _REVIEWABLE_SOURCE_TYPES:
        raise RepairBlockedError("source_type_not_reviewable")
    evidence = conn.execute(
        "SELECT node.text,node.locator_json,node.locator_sha256,node.node_kind,"
        "run.document_version_id,"
        "run.extractor_name,run.extractor_config_sha256,run.extractor_code_version,"
        "run.outcome,version.blob_sha256,version.ticker FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
        "JOIN evidence_document_versions version ON version.document_version_id=run.document_version_id "
        "WHERE node.node_id=? AND version.legacy_document_id=?",
        (entry.evidence_node_id, entry.source_doc_id),
    ).fetchone()
    if evidence is None:
        raise RepairBlockedError("evidence_node_not_bound_to_source")
    if str(evidence["outcome"]) != "succeeded":
        raise RepairBlockedError("evidence_extraction_not_succeeded")
    if str(evidence["node_kind"]) not in {
        "section",
        "passage",
        "table",
        "table_row",
        "table_cell",
        "pdf_page",
    }:
        raise RepairBlockedError("evidence_node_not_substantive")
    extractor = resolve_fulltext_extractor_identity(str(document["file_path"]), None)
    if (
        str(evidence["extractor_name"]) != extractor.name
        or str(evidence["extractor_config_sha256"]) != extractor.config_sha256
        or str(evidence["extractor_code_version"]) != extractor.code_version
    ):
        raise RepairBlockedError("evidence_extractor_not_promoted")
    if str(evidence["ticker"]).upper() != str(document["ticker"]).upper():
        raise RepairBlockedError("evidence_document_issuer_mismatch")
    if str(evidence["blob_sha256"]) != entry.source_content_sha256:
        raise RepairBlockedError("evidence_document_content_mismatch")
    binding = conn.execute(
        "SELECT binding.document_version_id,binding.scope_content_sha256,"
        "bound_node.node_kind,bound_run.document_version_id AS bound_node_document_version_id "
        "FROM v_legacy_document_evidence_bindings_current AS binding "
        "JOIN evidence_nodes AS bound_node ON bound_node.node_id=binding.evidence_node_id "
        "JOIN evidence_extraction_runs AS bound_run "
        "ON bound_run.extraction_run_id=bound_node.extraction_run_id "
        "WHERE binding.legacy_document_id=?",
        (entry.source_doc_id,),
    ).fetchone()
    if binding is None:
        raise RepairBlockedError("source_evidence_binding_missing")
    if str(binding["node_kind"]) != "document":
        raise RepairBlockedError("source_evidence_binding_not_document")
    if str(binding["document_version_id"]) != str(evidence["document_version_id"]):
        raise RepairBlockedError("source_evidence_binding_version_mismatch")
    if str(binding["bound_node_document_version_id"]) != str(binding["document_version_id"]):
        raise RepairBlockedError("source_evidence_binding_node_version_mismatch")
    if str(binding["scope_content_sha256"]) != entry.source_content_sha256:
        raise RepairBlockedError("source_evidence_binding_content_mismatch")
    if str(evidence["locator_sha256"] or "") != entry.evidence_locator_sha256:
        raise RepairBlockedError("evidence_locator_mismatch")
    try:
        evidence_locator = EvidenceLocator.model_validate_json(str(evidence["locator_json"] or ""))
        if (
            str(evidence["locator_json"]) != evidence_locator.canonical_json
            or evidence_locator.canonical_sha256 != entry.evidence_locator_sha256
        ):
            raise ValueError("evidence locator is not canonical")
        expected_locator = fact_locator_from_evidence_coordinates(
            KpiEvidenceLocatorCoordinates.from_evidence_locator(evidence_locator),
            verbatim_snippet=entry.source_excerpt,
        )
    except ValueError as exc:
        raise RepairBlockedError("evidence_locator_payload_invalid") from exc
    if entry.locator != expected_locator:
        raise RepairBlockedError("fact_locator_evidence_mismatch")
    evidence_text = str(evidence["text"])
    if entry.source_excerpt not in evidence_text:
        raise RepairBlockedError("source_excerpt_mismatch")
    if entry.source_value_text not in entry.source_excerpt:
        raise RepairBlockedError("source_value_not_in_excerpt")
    try:
        source_value = parse_source_numeric(entry.source_value_text)
    except ValueError as exc:
        raise RepairBlockedError("source_value_text_not_numeric") from exc
    source_numeric = normalize_source_numeric(
        source_value, unit=entry.unit, unit_scale=entry.context.unit_scale
    )
    if source_numeric != entry.value:
        raise RepairBlockedError("source_value_mismatch")
    semantic_quotes = (
        entry.semantic_evidence.metric_name_quote,
        entry.semantic_evidence.reported_period_quote,
        entry.semantic_evidence.accounting_basis_quote,
        entry.semantic_evidence.consolidation_scope_quote,
        entry.semantic_evidence.unit_scale_quote,
        *entry.semantic_evidence.dimension_quotes.values(),
    )
    if any(quote not in evidence_text for quote in semantic_quotes):
        raise RepairBlockedError("semantic_evidence_quote_mismatch")
    label = entry.context.source_row_label or entry.context.metric_name_as_reported
    if label.casefold() not in entry.semantic_evidence.metric_name_quote.casefold():
        raise RepairBlockedError("semantic_metric_label_not_bound")
    return source_type, str(document["ticker"]).upper()


def _validate_entry(
    conn: sqlite3.Connection,
    entry: RefreshEntry,
    allowed: set[int],
    *,
    owner_tickers: frozenset[str] = frozenset(),
) -> tuple[sqlite3.Row, SourceType]:
    row = conn.execute(
        "SELECT fact.*,definition.name,definition.ticker AS definition_ticker,"
        "definition.unit AS definition_unit,document.sha256 AS old_source_sha256 "
        "FROM kpi_facts fact JOIN kpi_definitions definition ON definition.id=fact.kpi_definition_id "
        "JOIN documents document ON document.id=fact.source_doc_id WHERE fact.id=?",
        (entry.old_fact_id,),
    ).fetchone()
    if row is None:
        raise RepairBlockedError("old_fact_missing")
    definition_id = int(row["kpi_definition_id"])
    canonical = canonical_fact_relation(conn, "kpi_facts")
    predecessor_is_canonical = (
        conn.execute(
            f"SELECT 1 FROM {canonical.sql} WHERE id=?",  # nosec B608 -- resolver-owned relation
            (entry.old_fact_id,),
        ).fetchone()
        is not None
    )
    if entry.predecessor_resolution_state == "canonical_current":
        if definition_id not in allowed:
            raise RepairBlockedError("fact_outside_owner_visible_scope")
        if not predecessor_is_canonical:
            raise RepairBlockedError("expected_canonical_predecessor_missing")
    else:
        if str(row["ticker"]).upper() not in owner_tickers:
            raise RepairBlockedError("quarantined_predecessor_outside_owner_portfolio")
        if predecessor_is_canonical:
            raise RepairBlockedError("quarantined_predecessor_is_canonical")
    if (
        int(row["source_doc_id"]) != entry.expected_old_source_doc_id
        or str(row["old_source_sha256"]) != entry.expected_old_source_sha256
    ):
        raise RepairBlockedError("old_fact_source_identity_changed")
    successor = conn.execute(
        "SELECT id FROM kpi_facts WHERE supersedes_id=? ORDER BY id DESC LIMIT 1",
        (entry.old_fact_id,),
    ).fetchone()
    actual_fact_head = entry.old_fact_id if successor is None else int(successor["id"])
    if actual_fact_head != entry.expected_fact_head_id:
        raise RepairBlockedError("fact_chain_head_changed")
    current = current_kpi_semantic_context(conn, kpi_fact_id=entry.old_fact_id)
    actual_context_id = None if current is None else current.id
    actual_context_revision = 0 if current is None else current.revision
    if (
        actual_context_id != entry.expected_context_head_id
        or actual_context_revision != entry.expected_context_revision
    ):
        raise RepairBlockedError("semantic_context_head_changed")
    if entry.predecessor_resolution_state == "quarantined_legacy" and current is not None:
        raise RepairBlockedError("quarantined_predecessor_has_semantic_context")
    if current is not None and current.context == entry.context:
        raise RepairBlockedError("semantic_context_already_current")
    if entry.context.reported_period_end != datetime.fromisoformat(str(row["period_end"])).date():
        raise RepairBlockedError("semantic_period_mismatch")
    source_type, source_ticker = _validate_source_binding(conn, entry)
    fact_ticker = str(row["ticker"]).upper()
    definition_ticker = str(row["definition_ticker"]).upper()
    if fact_ticker != definition_ticker or fact_ticker != source_ticker:
        raise RepairBlockedError("source_issuer_mismatch")
    if str(row["definition_unit"]) != entry.unit.value:
        raise RepairBlockedError("definition_unit_mismatch")
    definition = entry.definition_revision
    if definition is not None:
        if definition.kpi_definition_id != definition_id:
            raise RepairBlockedError("definition_revision_root_mismatch")
        current_definition = current_kpi_definition_revision(conn, kpi_definition_id=definition_id)
        actual_definition_head = (
            None if current_definition is None else current_definition.kpi_definition_revision_id
        )
        actual_definition_revision = (
            0 if current_definition is None else current_definition.revision
        )
        if (actual_definition_head, actual_definition_revision) != (
            entry.expected_definition_head_id,
            entry.expected_definition_revision,
        ):
            raise RepairBlockedError("definition_revision_head_changed")
        evidence = conn.execute(
            "SELECT run.document_version_id,node.locator_json "
            "FROM evidence_nodes node JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=node.extraction_run_id WHERE node.node_id=?",
            (entry.evidence_node_id,),
        ).fetchone()
        if evidence is None:
            raise RepairBlockedError("definition_evidence_missing")
        try:
            source_locator = json.loads(str(evidence["locator_json"]))
        except json.JSONDecodeError as exc:
            raise RepairBlockedError("definition_evidence_locator_invalid") from exc
        if (
            definition.source_document_version_id != str(evidence["document_version_id"])
            or definition.source_evidence_node_id != entry.evidence_node_id
            or definition.source_locator != source_locator
        ):
            raise RepairBlockedError("definition_evidence_binding_mismatch")
        for relation in entry.comparability_revisions:
            if (
                relation.source_document_version_id != definition.source_document_version_id
                or relation.source_evidence_node_id != definition.source_evidence_node_id
                or relation.source_locator != definition.source_locator
            ):
                raise RepairBlockedError("comparability_evidence_binding_mismatch")
        try:
            validate_kpi_definition_revision_candidate(
                conn,
                definition,
                expected_definition_head_id=entry.expected_definition_head_id,
                expected_definition_revision=entry.expected_definition_revision,
            )
            for relation in entry.comparability_revisions:
                validate_kpi_definition_comparability_candidate(
                    conn,
                    relation,
                    proposed_definition=definition,
                )
        except (sqlite3.Error, ValueError, RuntimeError) as exc:
            raise RepairBlockedError("definition_lineage_validation_failed") from exc
    if entry.action == "bind_existing":
        if entry.source_doc_id != int(row["source_doc_id"]):
            raise RepairBlockedError("bind_source_does_not_match_fact")
        if Decimal(str(row["value"])) != entry.value or Unit(str(row["unit"])) != entry.unit:
            raise RepairBlockedError("bind_value_or_unit_mismatch")
    return row, source_type


# Public read-only validation seam for deterministic manifest builders.
validate_refresh_entry = _validate_entry


@dataclass(frozen=True, slots=True)
class _EntryEffect:
    inserted_fact_rows: int
    inserted_context_rows: int
    inserted_definition_rows: int
    inserted_comparability_rows: int
    fact_head_id: int
    definition_revision_id: str | None
    definition_commitment_sha256: str | None


def _apply_entry(
    conn: sqlite3.Connection,
    *,
    manifest: RefreshManifest,
    entry: RefreshEntry,
    row: sqlite3.Row,
    source_type: SourceType,
) -> _EntryEffect:
    persisted_context = _context_for_entry(entry)
    definition = entry.definition_revision
    definition_ids_before = {
        definition.kpi_definition_revision_id
        for definition in (() if definition is None else (definition,))
        if kpi_definition_revision_by_id(
            conn,
            kpi_definition_revision_id=definition.kpi_definition_revision_id,
        )
        is not None
    }
    relation_ids_before = {
        relation.comparability_revision_id
        for relation in entry.comparability_revisions
        if conn.execute(
            "SELECT 1 FROM kpi_definition_comparability_revisions "
            "WHERE comparability_revision_id=?",
            (relation.comparability_revision_id,),
        ).fetchone()
        is not None
    }

    def definition_effect_counts() -> tuple[int, int]:
        definition_ids_after = {
            candidate.kpi_definition_revision_id
            for candidate in (() if definition is None else (definition,))
            if kpi_definition_revision_by_id(
                conn,
                kpi_definition_revision_id=candidate.kpi_definition_revision_id,
            )
            is not None
        }
        relation_ids_after = {
            relation.comparability_revision_id
            for relation in entry.comparability_revisions
            if conn.execute(
                "SELECT 1 FROM kpi_definition_comparability_revisions "
                "WHERE comparability_revision_id=?",
                (relation.comparability_revision_id,),
            ).fetchone()
            is not None
        }
        return (
            len(definition_ids_after - definition_ids_before),
            len(relation_ids_after - relation_ids_before),
        )

    if entry.action == "bind_existing":
        if definition is None:
            context_id = persist_kpi_semantic_context(
                conn,
                kpi_fact_id=entry.old_fact_id,
                context=persisted_context,
                reviewed_by=manifest.reviewer,
                knowledge_at=manifest.knowledge_at,
                kpi_definition_revision_id=None,
            )
            if context_id is None:
                raise RepairBlockedError("semantic_context_insert_unavailable")
            try:
                require_canonical_kpi_resolution(
                    conn,
                    fact_row_id=entry.old_fact_id,
                    knowledge_cutoff=manifest.knowledge_at,
                )
            except (sqlite3.Error, ValueError, RuntimeError) as exc:
                raise RepairBlockedError("canonical_fact_resolution_failed") from exc
        else:
            try:
                _ = bind_source_reviewed_kpi_definition(
                    conn,
                    fact_id=entry.old_fact_id,
                    expected_fact_head_id=entry.expected_fact_head_id,
                    expected_definition_head_id=entry.expected_definition_head_id,
                    expected_definition_revision=entry.expected_definition_revision,
                    reviewer=manifest.reviewer,
                    knowledge_at=manifest.knowledge_at,
                    context=persisted_context,
                    definition_revision=definition,
                    comparability_revisions=entry.comparability_revisions,
                )
            except RepairBlockedError:
                raise
            except (sqlite3.Error, ValueError, RuntimeError) as exc:
                raise RepairBlockedError("source_reviewed_definition_binding_failed") from exc
        inserted_definitions, inserted_relations = definition_effect_counts()
        return _EntryEffect(
            inserted_fact_rows=0,
            inserted_context_rows=1,
            inserted_definition_rows=inserted_definitions,
            inserted_comparability_rows=inserted_relations,
            fact_head_id=entry.old_fact_id,
            definition_revision_id=(
                None if definition is None else definition.kpi_definition_revision_id
            ),
            definition_commitment_sha256=(
                None if definition is None else definition.commitment_sha256
            ),
        )
    del source_type
    try:
        new_fact_id = insert_source_reviewed_kpi_supersession(
            conn,
            predecessor_id=entry.old_fact_id,
            expected_head_id=entry.expected_fact_head_id,
            value=entry.value,
            unit=entry.unit,
            currency=entry.currency,
            source_doc_id=entry.source_doc_id,
            locator=entry.locator,
            source_excerpt=entry.source_excerpt,
            reviewer=manifest.reviewer,
            knowledge_at=manifest.knowledge_at,
            context=persisted_context,
            definition_revision=definition,
            comparability_revisions=entry.comparability_revisions,
            expected_definition_head_id=entry.expected_definition_head_id,
            expected_definition_revision=(
                None if definition is None else entry.expected_definition_revision
            ),
        )
    except (sqlite3.Error, ValueError, RuntimeError) as exc:
        raise RepairBlockedError("source_reviewed_supersession_failed") from exc
    context = current_kpi_semantic_context(conn, kpi_fact_id=new_fact_id)
    if context is None or context.context != persisted_context:
        raise RepairBlockedError("semantic_context_postcondition_failed")
    inserted_definitions, inserted_relations = definition_effect_counts()
    return _EntryEffect(
        inserted_fact_rows=1,
        inserted_context_rows=1,
        inserted_definition_rows=inserted_definitions,
        inserted_comparability_rows=inserted_relations,
        fact_head_id=new_fact_id,
        definition_revision_id=(
            None if definition is None else definition.kpi_definition_revision_id
        ),
        definition_commitment_sha256=(None if definition is None else definition.commitment_sha256),
    )


def _require_canonical_result_heads(
    conn: sqlite3.Connection, *, result_heads: tuple[int, ...]
) -> None:
    canonical = canonical_fact_relation(conn, "kpi_facts")
    for head_id in result_heads:
        if (
            conn.execute(
                f"SELECT 1 FROM {canonical.sql} WHERE id=?",  # nosec B608 -- resolver-owned relation
                (head_id,),
            ).fetchone()
            is None
        ):
            raise RepairBlockedError("result_fact_not_canonically_resolved")


def _validate_applied_entry_postcondition(
    conn: sqlite3.Connection,
    *,
    manifest: RefreshManifest,
    entry: RefreshEntry,
    head_id: int,
) -> None:
    """Prove one exact committed result for marker replay or crash recovery."""
    row = conn.execute(
        "SELECT fact.*,document.sha256 AS source_sha256 "
        "FROM kpi_facts fact JOIN documents document ON document.id=fact.source_doc_id "
        "WHERE fact.id=?",
        (head_id,),
    ).fetchone()
    if row is None:
        raise RepairBlockedError("replay_fact_postcondition_mismatch")
    successor_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM kpi_facts WHERE supersedes_id=?",
            (head_id,),
        ).fetchone()[0]
    )
    if successor_count != 0:
        raise RepairBlockedError("replay_fact_head_changed")
    if entry.action == "bind_existing":
        exact_fact = (
            int(row["id"]) == entry.old_fact_id
            and int(row["source_doc_id"]) == entry.source_doc_id
            and Decimal(str(row["value"])) == entry.value
            and Unit(str(row["unit"])) is entry.unit
        )
    else:
        expected_currency = None if entry.currency is None else entry.currency.value
        predecessor_successors = tuple(
            int(successor[0])
            for successor in conn.execute(
                "SELECT id FROM kpi_facts WHERE supersedes_id=? ORDER BY id",
                (entry.old_fact_id,),
            )
        )
        exact_fact = (
            int(row["id"]) != entry.old_fact_id
            and predecessor_successors == (head_id,)
            and row["supersedes_id"] is not None
            and int(row["supersedes_id"]) == entry.old_fact_id
            and int(row["source_doc_id"]) == entry.source_doc_id
            and str(row["source_sha256"]) == entry.source_content_sha256
            and Decimal(str(row["value"])) == entry.value
            and Unit(str(row["unit"])) is entry.unit
            and row["currency"] == expected_currency
            and row["source_excerpt"] == entry.source_excerpt
            and row["locator"] == entry.locator.to_json()
            and row["extracted_by"] == f"source_review:{manifest.reviewer}"
        )
    if not exact_fact:
        raise RepairBlockedError("replay_fact_postcondition_mismatch")
    try:
        _validate_source_binding(conn, entry)
    except RepairBlockedError as exc:
        raise RepairBlockedError("replay_source_postcondition_mismatch") from exc
    canonical = canonical_fact_relation(conn, "kpi_facts")
    if (
        conn.execute(
            f"SELECT 1 FROM {canonical.sql} WHERE id=?",  # nosec B608 -- resolver-owned relation
            (head_id,),
        ).fetchone()
        is None
    ):
        raise RepairBlockedError("replay_fact_not_canonically_resolved")
    context = current_kpi_semantic_context(conn, kpi_fact_id=head_id)
    if (
        context is None
        or context.context != _context_for_entry(entry)
        or context.reviewed_by != manifest.reviewer
        or context.knowledge_at != manifest.knowledge_at
        or context.kpi_definition_revision_id
        != (
            None
            if entry.definition_revision is None
            else entry.definition_revision.kpi_definition_revision_id
        )
    ):
        raise RepairBlockedError("replay_semantic_context_changed")
    definition = entry.definition_revision
    if definition is not None:
        persisted_definition = kpi_definition_revision_by_id(
            conn,
            kpi_definition_revision_id=definition.kpi_definition_revision_id,
        )
        current_definition = current_kpi_definition_revision(
            conn, kpi_definition_id=definition.kpi_definition_id
        )
        if (
            persisted_definition is None
            or persisted_definition.commitment_sha256 != definition.commitment_sha256
            or current_definition is None
            or current_definition.kpi_definition_revision_id
            != definition.kpi_definition_revision_id
        ):
            raise RepairBlockedError("replay_definition_revision_changed")
        for relation in entry.comparability_revisions:
            persisted_relation = current_kpi_definition_comparability_revision(
                conn,
                predecessor_definition_revision_id=relation.predecessor_definition_revision_id,
                successor_definition_revision_id=relation.successor_definition_revision_id,
            )
            if (
                persisted_relation is None
                or persisted_relation.comparability_revision_id
                != relation.comparability_revision_id
                or persisted_relation.commitment_sha256 != relation.commitment_sha256
            ):
                raise RepairBlockedError("replay_comparability_revision_changed")
    if entry.predecessor_resolution_state != "quarantined_legacy":
        return
    predecessor = conn.execute(
        "SELECT fact.source_doc_id,document.sha256 AS source_sha256 "
        "FROM kpi_facts fact JOIN documents document ON document.id=fact.source_doc_id "
        "WHERE fact.id=?",
        (entry.old_fact_id,),
    ).fetchone()
    if (
        predecessor is None
        or int(predecessor["source_doc_id"]) != entry.expected_old_source_doc_id
        or str(predecessor["source_sha256"]) != entry.expected_old_source_sha256
        or conn.execute(
            f"SELECT 1 FROM {canonical.sql} WHERE id=?",  # nosec B608 -- resolver-owned relation
            (entry.old_fact_id,),
        ).fetchone()
        is not None
        or current_kpi_semantic_context(conn, kpi_fact_id=entry.old_fact_id) is not None
    ):
        raise RepairBlockedError("replay_quarantined_predecessor_changed")


def _verify_replay(
    conn: sqlite3.Connection,
    *,
    manifest: RefreshManifest,
    result_heads: tuple[int, ...],
    result_definition_revision_ids: tuple[str | None, ...],
    result_definition_commitment_sha256s: tuple[str | None, ...],
) -> None:
    if len(result_heads) != len(manifest.entries):
        raise RepairBlockedError("idempotency_marker_result_shape_mismatch")
    expected_definition_ids = tuple(
        None
        if entry.definition_revision is None
        else entry.definition_revision.kpi_definition_revision_id
        for entry in manifest.entries
    )
    expected_definition_commitments = tuple(
        None if entry.definition_revision is None else entry.definition_revision.commitment_sha256
        for entry in manifest.entries
    )
    if (
        result_definition_revision_ids != expected_definition_ids
        or result_definition_commitment_sha256s != expected_definition_commitments
    ):
        raise RepairBlockedError("idempotency_marker_definition_binding_mismatch")
    for entry, head_id in zip(manifest.entries, result_heads, strict=True):
        if entry.action == "bind_existing" and head_id != entry.old_fact_id:
            raise RepairBlockedError("idempotency_marker_result_head_mismatch")
        _validate_applied_entry_postcondition(
            conn,
            manifest=manifest,
            entry=entry,
            head_id=head_id,
        )


def _detect_applied_postcondition(
    conn: sqlite3.Connection, *, manifest: RefreshManifest
) -> tuple[int, ...] | None:
    """Recognize an exact commit after a crash before marker publication."""
    heads: list[int] = []
    for entry in manifest.entries:
        if entry.action == "bind_existing":
            head_id = entry.old_fact_id
        else:
            row = conn.execute(
                "SELECT fact.id FROM kpi_facts fact WHERE fact.supersedes_id=? "
                "ORDER BY fact.id DESC LIMIT 1",
                (entry.old_fact_id,),
            ).fetchone()
            if row is None:
                return None
            head_id = int(row["id"])
        try:
            _validate_applied_entry_postcondition(
                conn,
                manifest=manifest,
                entry=entry,
                head_id=head_id,
            )
        except RepairBlockedError:
            return None
        heads.append(head_id)
    return tuple(heads)


def _validate_v2_idempotency_marker(
    prior: dict[str, object],
    *,
    receipt_root: Path,
    logical_key_sha256: str,
    manifest_sha256: str,
) -> tuple[tuple[int, ...], tuple[str | None, ...], tuple[str | None, ...]]:
    """Bind marker claims to one exact sealed apply/recovery attempt receipt."""

    attempt_id = prior.get("apply_attempt_id")
    if (
        not isinstance(attempt_id, str)
        or len(attempt_id) != 32
        or any(character not in "0123456789abcdef" for character in attempt_id)
    ):
        raise RepairBlockedError("idempotency_marker_apply_attempt_invalid")
    receipt_path = receipt_root / "attempts" / f"{attempt_id}.json"
    try:
        apply_receipt = KpiRepairAttemptReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
        result_heads = tuple(
            TypeAdapter(list[int]).validate_python(prior.get("result_fact_head_ids"))
        )
        result_definition_ids = tuple(
            TypeAdapter(list[str | None]).validate_python(
                prior.get("result_definition_revision_ids")
            )
        )
        result_definition_commitments = tuple(
            TypeAdapter(list[str | None]).validate_python(
                prior.get("result_definition_commitment_sha256s")
            )
        )
    except (OSError, ValueError):
        raise RepairBlockedError("idempotency_marker_apply_receipt_invalid") from None
    marker_definition_rows = prior.get("inserted_definition_rows")
    marker_comparability_rows = prior.get("inserted_comparability_rows")
    if (
        type(marker_definition_rows) is not int
        or type(marker_comparability_rows) is not int
        or marker_definition_rows < 0
        or marker_comparability_rows < 0
    ):
        raise RepairBlockedError("idempotency_marker_result_shape_mismatch")
    if (
        prior.get("logical_idempotency_key_sha256") != logical_key_sha256
        or prior.get("manifest_sha256") != manifest_sha256
        or prior.get("apply_receipt_sha256") != apply_receipt.content_sha256
        or apply_receipt.attempt_id != attempt_id
        or apply_receipt.schema_version != "kpi_repair_attempt.v3"
        or apply_receipt.mode != "apply"
        or apply_receipt.state not in {"applied", "replayed"}
        or apply_receipt.logical_idempotency_key_sha256 != logical_key_sha256
        or apply_receipt.manifest_sha256 != manifest_sha256
        or apply_receipt.result_fact_head_ids != result_heads
        or apply_receipt.result_definition_revision_ids != result_definition_ids
        or apply_receipt.result_definition_commitment_sha256s != result_definition_commitments
        or apply_receipt.inserted_definition_rows != marker_definition_rows
        or apply_receipt.inserted_comparability_rows != marker_comparability_rows
    ):
        raise RepairBlockedError("idempotency_marker_apply_receipt_mismatch")
    return result_heads, result_definition_ids, result_definition_commitments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--user-id",
        required=True,
        help="Explicit owner identity; must exactly match the signed repair manifest",
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--review-bundle", type=Path, required=True)
    parser.add_argument("--trusted-review-pins", type=Path, required=True)
    parser.add_argument("--backup-restore-receipt", type=Path, required=True)
    parser.add_argument("--judge-receipt", type=Path)
    parser.add_argument("--dry-run-receipt", type=Path)
    parser.add_argument("--approved-manifest-sha256")
    parser.add_argument("--max-review-age-seconds", type=int, default=1200)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = datetime.now(UTC)
    attempt_id = uuid4().hex
    mode: Literal["dry_run", "apply"] = "apply" if args.apply else "dry_run"
    receipt_root = args.receipt_root.resolve()
    executor_code_sha = repair_executor_code_sha256(PROJECT_ROOT)
    try:
        manifest = RefreshManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
        review_bundle = OperationsReviewBundle.model_validate_json(
            args.review_bundle.read_text(encoding="utf-8")
        )
        trusted_pins = WindowsReviewPins.model_validate_json(
            args.trusted_review_pins.read_text(encoding="utf-8")
        )
        backup = BackupRestoreReadinessReceipt.model_validate_json(
            args.backup_restore_receipt.read_text(encoding="utf-8")
        )
    except Exception as exc:

        def artifact_sha(path: Path) -> str:
            try:
                return hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                return hashlib.sha256(b"unreadable-artifact").hexdigest()

        completed = datetime.now(UTC)
        receipt = seal_attempt(
            attempt_id=attempt_id,
            logical_idempotency_key_sha256=artifact_sha(args.manifest),
            manifest_sha256=artifact_sha(args.manifest),
            review_bundle_sha256=artifact_sha(args.review_bundle),
            backup_restore_evidence_id=artifact_sha(args.backup_restore_receipt),
            executor_code_sha256=executor_code_sha,
            mode=mode,
            state="failed",
            started_at=started,
            completed_at=completed,
            validated_entries=0,
            inserted_fact_rows=0,
            inserted_context_rows=0,
            blocker_codes=(f"invalid_input_{type(exc).__name__}",),
            result_fact_head_ids=(),
            inserted_definition_rows=0,
            inserted_comparability_rows=0,
            result_definition_revision_ids=(),
            result_definition_commitment_sha256s=(),
        )
        receipt_path = receipt_root / "attempts" / f"{attempt_id}.json"
        _write_content_addressed(receipt_path, receipt.model_dump_json(indent=2))
        _write_latest(receipt_root / "latest.json", receipt.model_dump_json(indent=2))
        print(
            KpiRepairSummary(
                attempt_id=attempt_id,
                state="failed",
                receipt_path=str(receipt_path),
                receipt_sha256=receipt.content_sha256,
                blocker_codes=receipt.blocker_codes,
            ).model_dump_json()
        )
        return 2
    manifest_sha = manifest.content_sha256()
    logical_key_sha = hashlib.sha256(manifest.logical_idempotency_key.encode("utf-8")).hexdigest()
    blocker_codes: tuple[str, ...] = ()
    fact_rows = 0
    context_rows = 0
    definition_rows = 0
    comparability_rows = 0
    result_heads: tuple[int, ...] = ()
    result_definition_ids: tuple[str | None, ...] = ()
    result_definition_commitments: tuple[str | None, ...] = ()
    state: Literal["passed", "applied", "replayed", "blocked", "failed"] = "failed"
    publish_marker = False
    marker = receipt_root / "by_logical_key" / f"{logical_key_sha}.json"
    try:
        if args.user_id != manifest.user_id:
            raise RepairBlockedError("manifest_user_identity_mismatch")
        if args.max_review_age_seconds <= 0:
            raise RepairBlockedError("invalid_review_age")
        validate_manifest_knowledge_time(manifest, now=started)
        _validate_external_evidence(
            manifest=manifest,
            db_path=args.db,
            review_bundle=review_bundle,
            trusted_pins=trusted_pins,
            backup=backup,
            now=datetime.now(UTC),
            max_review_age=timedelta(seconds=args.max_review_age_seconds),
        )
        if args.apply:
            _validate_apply_authority(
                db_path=args.db,
                receipt_root=receipt_root,
                review_bundle=review_bundle,
            )
            if args.approved_manifest_sha256 != manifest_sha:
                raise RepairBlockedError("owner_manifest_approval_mismatch")
            if args.judge_receipt is None:
                raise RepairBlockedError("judge_receipt_missing")
            if args.dry_run_receipt is None:
                raise RepairBlockedError("dry_run_receipt_missing")
            dry_run = KpiRepairAttemptReceipt.model_validate_json(
                args.dry_run_receipt.read_text(encoding="utf-8")
            )
            judge = KpiRepairJudgeReceipt.model_validate_json(
                args.judge_receipt.read_text(encoding="utf-8")
            )
            if (
                dry_run.mode != "dry_run"
                or dry_run.state != "passed"
                or dry_run.manifest_sha256 != manifest_sha
                or dry_run.review_bundle_sha256 != manifest.review_bundle_sha256
                or dry_run.executor_code_sha256 != executor_code_sha
                or judge.dry_run_receipt_sha256 != dry_run.content_sha256
                or judge.verdict != "PASS"
                or judge.manifest_sha256 != manifest_sha
                or judge.review_bundle_sha256 != manifest.review_bundle_sha256
                or judge.executor_code_sha256 != executor_code_sha
                or judge.purpose != "kpi_source_repair"
                or (
                    manifest.schema_version == "kpi_semantic_refresh.v7"
                    and judge.rubric_version != "kpi-semantic-refresh-v7"
                )
            ):
                raise RepairBlockedError("judge_receipt_not_authorizing")
        with JobLock(
            _repair_lock_root(args.db),
            "kpi-semantic-refresh",
            ["portfolio-db"],
            wait_s=0,
        ):
            resources = ExitStack()
            try:
                repair_db = resources.enter_context(
                    _repair_database(live_db=args.db, backup=backup, apply=args.apply)
                )
                conn = open_db(repair_db)
            except Exception:
                resources.close()
                raise
            try:
                actual_revision = _schema_revision(conn)
                if actual_revision != manifest.expected_schema_revision:
                    raise RepairBlockedError("database_schema_revision_changed")
                database_identity_sha256 = hashlib.sha256(
                    database_lineage_identity(conn).encode("utf-8")
                ).hexdigest()
                if review_bundle.identity.database_instance_sha256 != database_identity_sha256:
                    raise RepairBlockedError("review_bundle_database_identity_mismatch")
                _validate_external_evidence(
                    manifest=manifest,
                    db_path=args.db,
                    review_bundle=review_bundle,
                    trusted_pins=trusted_pins,
                    backup=backup,
                    now=datetime.now(UTC),
                    max_review_age=timedelta(seconds=args.max_review_age_seconds),
                )
                conn.execute("BEGIN IMMEDIATE")
                if args.apply and marker.exists():
                    prior = json.loads(marker.read_text(encoding="utf-8"))
                    if (
                        prior.get("manifest_sha256") != manifest_sha
                        or prior.get("logical_idempotency_key_sha256") != logical_key_sha
                    ):
                        raise RepairBlockedError("logical_idempotency_key_conflict")
                    marker_schema = prior.get("schema_version")
                    if marker_schema == "kpi_repair_idempotency.v1":
                        if manifest.schema_version == "kpi_semantic_refresh.v7":
                            raise RepairBlockedError(
                                "idempotency_marker_definition_binding_missing"
                            )
                        try:
                            result_heads = tuple(
                                TypeAdapter(list[int]).validate_python(
                                    prior.get("result_fact_head_ids")
                                )
                            )
                        except ValueError:
                            raise RepairBlockedError(
                                "idempotency_marker_result_shape_mismatch"
                            ) from None
                        result_definition_ids = tuple(None for _ in manifest.entries)
                        result_definition_commitments = tuple(None for _ in manifest.entries)
                    elif marker_schema == "kpi_repair_idempotency.v2":
                        (
                            result_heads,
                            result_definition_ids,
                            result_definition_commitments,
                        ) = _validate_v2_idempotency_marker(
                            prior,
                            receipt_root=receipt_root,
                            logical_key_sha256=logical_key_sha,
                            manifest_sha256=manifest_sha,
                        )
                    else:
                        raise RepairBlockedError("idempotency_marker_schema_unknown")
                    _verify_replay(
                        conn,
                        manifest=manifest,
                        result_heads=result_heads,
                        result_definition_revision_ids=result_definition_ids,
                        result_definition_commitment_sha256s=result_definition_commitments,
                    )
                    state = "replayed"
                    conn.rollback()
                else:
                    recovered_heads = (
                        _detect_applied_postcondition(conn, manifest=manifest)
                        if args.apply
                        else None
                    )
                    if recovered_heads is not None:
                        result_heads = recovered_heads
                        result_definition_ids = tuple(
                            None
                            if entry.definition_revision is None
                            else entry.definition_revision.kpi_definition_revision_id
                            for entry in manifest.entries
                        )
                        result_definition_commitments = tuple(
                            None
                            if entry.definition_revision is None
                            else entry.definition_revision.commitment_sha256
                            for entry in manifest.entries
                        )
                        state = "replayed"
                        publish_marker = True
                        conn.rollback()
                    else:
                        allowed = {
                            row.kpi_definition_id
                            for row in scoped_kpi_definitions(
                                conn,
                                repo_root=PROJECT_ROOT,
                                user_id=manifest.user_id,
                            )
                            if row.kpi_definition_id is not None
                        }
                        owner_ticker_set = frozenset(
                            portfolio_tickers(conn, user_id=manifest.user_id)
                        )
                        validated = [
                            _validate_entry(
                                conn,
                                entry,
                                allowed,
                                owner_tickers=owner_ticker_set,
                            )
                            for entry in manifest.entries
                        ]
                        heads: list[int] = []
                        definition_ids: list[str | None] = []
                        definition_commitments: list[str | None] = []
                        for entry, (row, source_type) in zip(
                            manifest.entries, validated, strict=True
                        ):
                            effect = _apply_entry(
                                conn,
                                manifest=manifest,
                                entry=entry,
                                row=row,
                                source_type=source_type,
                            )
                            fact_rows += effect.inserted_fact_rows
                            context_rows += effect.inserted_context_rows
                            definition_rows += effect.inserted_definition_rows
                            comparability_rows += effect.inserted_comparability_rows
                            heads.append(effect.fact_head_id)
                            definition_ids.append(effect.definition_revision_id)
                            definition_commitments.append(effect.definition_commitment_sha256)
                        if fact_rows != sum(
                            e.expected_inserted_fact_rows for e in manifest.entries
                        ):
                            raise RepairBlockedError("fact_row_effect_total_mismatch")
                        if context_rows != sum(
                            e.expected_inserted_context_rows for e in manifest.entries
                        ):
                            raise RepairBlockedError("context_row_effect_total_mismatch")
                        result_heads = tuple(heads)
                        result_definition_ids = tuple(definition_ids)
                        result_definition_commitments = tuple(definition_commitments)
                        _require_canonical_result_heads(conn, result_heads=result_heads)
                        if args.apply:
                            conn.commit()
                            state = "applied"
                            publish_marker = True
                        else:
                            conn.rollback()
                            state = "passed"
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
                resources.close()
    except JobAlreadyRunningError:
        blocker_codes = ("portfolio_db_lock_contended",)
        state = "blocked"
    except RepairBlockedError as exc:
        blocker_codes = (exc.code,)
        state = "blocked"
    except Exception as exc:
        blocker_codes = (f"unexpected_{type(exc).__name__}",)
        state = "failed"
    completed = datetime.now(UTC)
    receipt = seal_attempt(
        attempt_id=attempt_id,
        logical_idempotency_key_sha256=logical_key_sha,
        manifest_sha256=manifest_sha,
        review_bundle_sha256=manifest.review_bundle_sha256,
        backup_restore_evidence_id=manifest.backup_restore_evidence_id,
        executor_code_sha256=executor_code_sha,
        mode=mode,
        state=state,
        started_at=started,
        completed_at=completed,
        validated_entries=len(manifest.entries)
        if state in {"passed", "applied", "replayed"}
        else 0,
        inserted_fact_rows=fact_rows,
        inserted_context_rows=context_rows,
        inserted_definition_rows=definition_rows,
        inserted_comparability_rows=comparability_rows,
        blocker_codes=blocker_codes,
        result_fact_head_ids=result_heads,
        result_definition_revision_ids=result_definition_ids,
        result_definition_commitment_sha256s=result_definition_commitments,
    )
    receipt_path = receipt_root / "attempts" / f"{attempt_id}.json"
    _write_content_addressed(receipt_path, receipt.model_dump_json(indent=2))
    _write_latest(receipt_root / "latest.json", receipt.model_dump_json(indent=2))
    if publish_marker:
        _write_content_addressed(
            marker,
            json.dumps(
                {
                    "schema_version": "kpi_repair_idempotency.v2",
                    "apply_attempt_id": receipt.attempt_id,
                    "logical_idempotency_key_sha256": logical_key_sha,
                    "manifest_sha256": manifest_sha,
                    "apply_receipt_sha256": receipt.content_sha256,
                    "inserted_definition_rows": definition_rows,
                    "inserted_comparability_rows": comparability_rows,
                    "result_fact_head_ids": list(result_heads),
                    "result_definition_revision_ids": list(result_definition_ids),
                    "result_definition_commitment_sha256s": list(result_definition_commitments),
                },
                sort_keys=True,
                indent=2,
            ),
        )
    summary = KpiRepairSummary(
        attempt_id=attempt_id,
        state=state,
        receipt_path=str(receipt_path),
        receipt_sha256=receipt.content_sha256,
        blocker_codes=blocker_codes,
    )
    sys.stderr.write(
        json.dumps(
            {
                "event": "kpi_semantic_refresh_completed",
                "attempt_id": attempt_id,
                "state": state,
                "receipt_sha256": receipt.content_sha256,
                "blocker_codes": blocker_codes,
            },
            sort_keys=True,
        )
        + "\n"
    )
    print(summary.model_dump_json())
    return 0 if state in {"passed", "applied", "replayed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
