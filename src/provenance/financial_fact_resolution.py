"""Canonical observation/resolution cutover for financial and KPI facts.

The database migration captures future fact writes.  This module owns the
bounded legacy backfill, complete-candidate resolution policy, and the one
read-side compatibility boundary.  It never averages or deletes candidates:
material ambiguity is recorded and canonical reads fail closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, TypeAlias, cast
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from models.documents import SourceType
from provenance.observation_resolution import (
    ObservationDimension,
    ObservationResolutionLedger,
    ReportedObservation,
    ResolutionRevision,
)
from provenance.source_regime import (
    EvidenceAuthority,
    SourceDomain,
    SourceRegime,
    classification_for_source_type,
    contract_for,
)

log = logging.getLogger(__name__)

FactTable: TypeAlias = Literal["financial_facts", "kpi_facts"]
ResolutionStatus: TypeAlias = Literal["resolved", "unresolved_material"]
SelectionMode: TypeAlias = Literal["resolved_view", "legacy_pre_cutover"]
DocumentFactAdmissionStatus: TypeAlias = Literal["inserted", "idempotent_replay", "empty"]

_TABLES: tuple[FactTable, ...] = ("financial_facts", "kpi_facts")
_POLICY_VERSION = "investor-grade-fact-resolution@2"
_MATERIAL_RELATIVE_DELTA = Decimal("0.01")
_COMPANYFACTS_PATH = re.compile(r"^facts\.([^.]+)\.([^.]+)\.units\.([^\[]+)\[([0-9]+)\]$")
_TIER_RANK = {
    "sec_official": 50,
    "fmp_normalized": 40,
    "llm_extracted": 20,
    "yfinance_fallback": 10,
    "s1_provisional": 0,
}
_AUTHORITY_PRECEDENCE = contract_for(SourceRegime.COMBINED).precedence(SourceDomain.REPORTED_FACT)
_AUTHORITY_RANK = {
    authority: len(_AUTHORITY_PRECEDENCE) - index
    for index, authority in enumerate(_AUTHORITY_PRECEDENCE)
}
DOCUMENT_FACT_REHYDRATION_SQL: Final = (
    "SELECT fact.id, fact.ticker, link.fact_row_id "
    "FROM financial_facts AS fact "
    "LEFT JOIN fact_observation_revisions AS link "
    "ON link.source_document_id=? "
    "AND link.fact_table='financial_facts' AND link.fact_row_id=fact.id "
    "WHERE fact.source_doc_id=? ORDER BY fact.id"
)


class _CutoverModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactCutoverRequest(_CutoverModel):
    """One bounded, resumable cutover batch."""

    apply: bool = False
    batch_size: int = Field(default=500, ge=1, le=10_000)
    checkpoint_path: Path
    knowledge_cutoff: datetime


class FactCutoverCheckpoint(_CutoverModel):
    """The exact last processed row across the two-table traversal."""

    table_index: int = Field(default=0, ge=0, le=len(_TABLES))
    last_row_id: int = Field(default=0, ge=0)
    updated_at: datetime


class FactCutoverSummary(BaseModel):
    """Machine-readable result of one dry-run or apply batch."""

    model_config = ConfigDict(extra="forbid")

    apply: bool
    rows_considered: int = 0
    rows_planned: int = 0
    rows_captured: int = 0
    rows_replayed: int = 0
    rows_quarantined: int = 0
    resolutions_created: int = 0
    resolutions_replayed: int = 0
    unresolved_material: int = 0
    checkpoint_complete: bool = False
    finding_counts: dict[str, int] = Field(default_factory=dict[str, int])


class FactResolutionResult(_CutoverModel):
    """One deterministic decision over the complete current candidate set."""

    resolution_id: str
    logical_key: str
    candidate_count: int = Field(gt=0)
    selected_observation_id: str
    resolution_status: ResolutionStatus
    material_dissent: bool
    created: bool


class GovernedDocumentFactAdmission(_CutoverModel):
    """Canonical proof that one exact evidence-backed document owns admitted facts."""

    document_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=16)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inserted_count: int = Field(ge=0)
    total_admitted_count: int = Field(ge=0)
    status: DocumentFactAdmissionStatus


class DocumentFactObservationRehydration(_CutoverModel):
    """Atomic repair proof for exact legacy facts missing immutable observations."""

    document_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=16)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_fact_count: int = Field(ge=0)
    captured_count: int = Field(ge=0)
    admission: GovernedDocumentFactAdmission


def governed_document_fact_admission(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    ticker: str,
    content_sha256: str,
    inserted_count: int,
) -> GovernedDocumentFactAdmission:
    """Prove total facts for an exact document/hash through canonical evidence links."""
    governed = conn.execute(
        "SELECT 1 FROM documents AS document "
        "JOIN evidence_document_versions AS version "
        "ON version.legacy_document_id = document.id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.document_version_id = version.document_version_id "
        "JOIN evidence_nodes AS node ON node.extraction_run_id = run.extraction_run_id "
        "WHERE document.id = ? AND UPPER(document.ticker) = UPPER(?) "
        "AND document.sha256 = ? AND version.blob_sha256 = document.sha256 "
        "AND run.outcome = 'succeeded' AND node.node_kind = 'document' LIMIT 1",
        (document_id, ticker, content_sha256),
    ).fetchone()
    if governed is None:
        raise ValueError("document/hash lacks canonical evidence admission")

    row = conn.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT link.fact_table, link.fact_row_id "
        "FROM fact_observation_revisions AS link "
        "JOIN reported_observations AS observation "
        "ON observation.observation_id = link.observation_id "
        "JOIN evidence_nodes AS node ON node.node_id = observation.evidence_node_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "JOIN evidence_document_versions AS version "
        "ON version.document_version_id = run.document_version_id "
        "WHERE link.source_document_id = ? AND link.fact_table = 'financial_facts' "
        "AND version.legacy_document_id = link.source_document_id "
        "AND version.blob_sha256 = ? AND run.outcome = 'succeeded' "
        "GROUP BY link.fact_table, link.fact_row_id)",
        (document_id, content_sha256),
    ).fetchone()
    total_admitted_count = int(row[0]) if row is not None else 0
    if inserted_count > total_admitted_count:
        raise RuntimeError("extractor insert count exceeds canonical admitted fact count")
    if total_admitted_count == 0:
        status: DocumentFactAdmissionStatus = "empty"
    elif inserted_count == 0:
        status = "idempotent_replay"
    else:
        status = "inserted"
    return GovernedDocumentFactAdmission(
        document_id=document_id,
        ticker=ticker.upper(),
        content_sha256=content_sha256,
        inserted_count=inserted_count,
        total_admitted_count=total_admitted_count,
        status=status,
    )


def rehydrate_document_fact_observations(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    ticker: str,
    content_sha256: str,
    inserted_count: int,
    recorded_at: datetime,
) -> DocumentFactObservationRehydration:
    """Capture missing observations for every fact owned by one exact document.

    The caller owns an active transaction and must commit only after its corpus
    handle is re-read unchanged. Any mismatch or partial capture therefore
    rolls back as one unit instead of blessing legacy rows blindly.

    This transitional legacy read retires once the governed FMP corpus
    backfill proves no financial-fact row remains without an observation link.
    """
    if not conn.in_transaction:
        raise RuntimeError("document fact observation rehydration requires a transaction")
    normalized_ticker = ticker.upper()
    # This proves the document/hash has a succeeded canonical evidence chain
    # before any missing fact observation is created. An empty admission is an
    # expected legacy starting state, not authorization by itself.
    governed_document_fact_admission(
        conn,
        document_id=document_id,
        ticker=normalized_ticker,
        content_sha256=content_sha256,
        inserted_count=0,
    )
    rows = conn.execute(
        DOCUMENT_FACT_REHYDRATION_SQL,
        (document_id, document_id),
    ).fetchall()
    fact_ids: set[int] = set()
    linked_ids: set[int] = set()
    for row in rows:
        if str(row["ticker"]).upper() != normalized_ticker:
            raise ValueError("document owns a financial fact for a different ticker")
        fact_id = int(row["id"])
        fact_ids.add(fact_id)
        if row["fact_row_id"] is not None:
            linked_ids.add(fact_id)

    captured_count = 0
    for fact_id in sorted(fact_ids - linked_ids):
        if capture_fact_row_observation(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_id,
            recorded_at=recorded_at,
        ):
            captured_count += 1
        resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_id,
            knowledge_cutoff=recorded_at,
        )
    admission = governed_document_fact_admission(
        conn,
        document_id=document_id,
        ticker=normalized_ticker,
        content_sha256=content_sha256,
        inserted_count=inserted_count,
    )
    if admission.total_admitted_count != len(fact_ids):
        raise RuntimeError("not every exact document fact has canonical evidence admission")
    return DocumentFactObservationRehydration(
        document_id=document_id,
        ticker=normalized_ticker,
        content_sha256=content_sha256,
        exact_fact_count=len(fact_ids),
        captured_count=captured_count,
        admission=admission,
    )


@dataclass(frozen=True, slots=True)
class CanonicalFactRelation:
    """A constant-safe SQL relation plus its explicit compatibility mode."""

    sql: str
    selection_mode: SelectionMode

    def __str__(self) -> str:
        return self.sql


@dataclass(frozen=True, slots=True)
class _EvidenceContext:
    issuer_id: str
    evidence_node_id: str
    period_start: datetime | None
    available_at: datetime
    source_tier: str
    source_effective_at: datetime
    source_kind: str | None = None
    blob_sha256: str | None = None
    storage_uri: str | None = None
    scope_locator_json: str | None = None
    legacy_binding_revision_id: str | None = None
    legacy_binding_revision: int | None = None
    binding_scope_content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _FactCapturePlan:
    observation: ReportedObservation | None
    logical_key: str
    source_document_id: int
    source_tier: str
    locator_json: str | None
    captured_at: datetime
    match_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    observation_id: str
    fact_table: FactTable
    fact_row_id: int
    fact_revision: int
    numeric_value: Decimal
    currency: str | None
    unit: str
    period_start: datetime
    period_end: datetime
    fiscal_period_type: str
    source_tier: str
    source_type: str
    source_effective_at: datetime
    available_at: datetime


def fact_resolution_cutover_available(conn: sqlite3.Connection) -> bool:
    """True only when the complete 0225 write and read boundary is installed."""

    return (
        _object_exists(conn, "fact_observation_revisions", object_type="table")
        and _object_exists(conn, "fact_resolution_outcomes", object_type="table")
        and _object_exists(conn, "v_financial_facts_resolved_current", object_type="view")
        and _object_exists(conn, "v_kpi_facts_resolved_current", object_type="view")
    )


def canonical_fact_relation(
    conn: sqlite3.Connection, fact_table: FactTable
) -> CanonicalFactRelation:
    """Return the resolved relation after 0225 and legacy semantics only before it."""

    if fact_table not in _TABLES:
        raise ValueError(f"unsupported fact table: {fact_table}")
    view = (
        "v_financial_facts_resolved_current"
        if fact_table == "financial_facts"
        else "v_kpi_facts_resolved_current"
    )
    if _object_exists(conn, view, object_type="view"):
        relation = CanonicalFactRelation(view, "resolved_view")
    elif not _object_exists(conn, "fact_observation_revisions", object_type="table"):
        relation = CanonicalFactRelation(fact_table, "legacy_pre_cutover")
    else:
        raise RuntimeError(
            f"fact cutover schema is partial: required canonical view {view!r} is missing"
        )
    log.info(
        "canonical_fact_relation",
        extra={"fact_table": fact_table, "selection_mode": relation.selection_mode},
    )
    return relation


def execute_fact_cutover(
    conn: sqlite3.Connection, request: FactCutoverRequest
) -> FactCutoverSummary:
    """Capture and resolve one bounded batch, advancing state only after commit."""

    _require_cutover_schema(conn)
    checkpoint = _read_checkpoint(request.checkpoint_path)
    summary = FactCutoverSummary(apply=request.apply)
    rows, next_checkpoint = _next_rows(conn, checkpoint, request.batch_size)
    summary.rows_considered = len(rows)
    summary.rows_planned = len(rows)
    if not request.apply:
        for fact_table, row_id in rows:
            try:
                _plan_fact_row(
                    conn,
                    fact_table=fact_table,
                    row_id=row_id,
                    recorded_at=request.knowledge_cutoff,
                )
            except (ValueError, sqlite3.Error) as exc:
                reason = _quarantine_reason(exc)
                summary.rows_quarantined += 1
                _finding(summary, reason)
                _emit_event(
                    "financial_fact_cutover_dry_run_quarantined",
                    fact_table=fact_table,
                    fact_row_id=row_id,
                    reason=reason,
                )
        summary.checkpoint_complete = _is_complete(conn, next_checkpoint)
        return summary

    if conn.in_transaction:
        raise RuntimeError(
            "fact cutover apply requires an idle writer connection before BEGIN IMMEDIATE"
        )
    conn.execute("BEGIN IMMEDIATE")
    logical_keys: set[str] = set()
    try:
        for fact_table, row_id in rows:
            try:
                created, logical_key = _capture_fact_row(
                    conn,
                    fact_table=fact_table,
                    row_id=row_id,
                    recorded_at=request.knowledge_cutoff,
                )
            except (ValueError, sqlite3.Error) as exc:
                summary.rows_quarantined += 1
                _finding(summary, _quarantine_reason(exc))
                _emit_event(
                    "financial_fact_cutover_quarantined",
                    fact_table=fact_table,
                    fact_row_id=row_id,
                    reason=_quarantine_reason(exc),
                )
                continue
            logical_keys.add(logical_key)
            if created:
                summary.rows_captured += 1
            else:
                summary.rows_replayed += 1
        for logical_key in sorted(logical_keys):
            result = resolve_fact_logical_key(
                conn,
                logical_key=logical_key,
                knowledge_cutoff=request.knowledge_cutoff,
                recorded_at=request.knowledge_cutoff,
            )
            if result.created:
                summary.resolutions_created += 1
            else:
                summary.resolutions_replayed += 1
            if result.resolution_status == "unresolved_material":
                summary.unresolved_material += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    _write_checkpoint(
        request.checkpoint_path,
        FactCutoverCheckpoint(
            table_index=next_checkpoint.table_index,
            last_row_id=next_checkpoint.last_row_id,
            updated_at=datetime.now(UTC),
        ),
    )
    summary.checkpoint_complete = _is_complete(conn, next_checkpoint)
    return summary


def resolve_fact_logical_key(
    conn: sqlite3.Connection,
    *,
    logical_key: str,
    knowledge_cutoff: datetime,
    recorded_at: datetime,
) -> FactResolutionResult:
    """Resolve all current candidates available at the cutoff under policy v2."""

    candidates = _load_complete_candidates(conn, logical_key, knowledge_cutoff)
    if not candidates:
        raise ValueError(f"logical key {logical_key!r} has no available current candidates")
    checks = _candidate_checks(candidates)
    selected = max(
        candidates,
        key=lambda candidate: (
            _TIER_RANK.get(candidate.source_tier, -1),
            _candidate_authority_rank(candidate),
            _utc_instant(candidate.source_effective_at),
            candidate.fact_row_id,
            candidate.fact_revision,
            candidate.observation_id,
        ),
    )
    material_dissent = _has_material_dissent(candidates) or not all(checks.values())
    top_rank = max(_TIER_RANK.get(candidate.source_tier, -1) for candidate in candidates)
    top_tier_candidates = tuple(
        candidate
        for candidate in candidates
        if _TIER_RANK.get(candidate.source_tier, -1) == top_rank
    )
    top_authority_rank = max(
        _candidate_authority_rank(candidate) for candidate in top_tier_candidates
    )
    top_candidates = tuple(
        candidate
        for candidate in top_tier_candidates
        if _candidate_authority_rank(candidate) == top_authority_rank
    )
    top_authority_dissent = _has_any_value_dissent(top_candidates)
    material_dissent = material_dissent or top_authority_dissent
    unresolved = not all(checks.values()) or top_authority_dissent
    status: ResolutionStatus = "unresolved_material" if unresolved else "resolved"
    candidate_ids = tuple(sorted(candidate.observation_id for candidate in candidates))
    candidate_digest = hashlib.sha256("\0".join(candidate_ids).encode()).hexdigest()
    current = _current_resolution(conn, logical_key)
    if current is not None and _same_current_decision(
        conn,
        current_resolution_id=str(current["resolution_id"]),
        candidate_ids=candidate_ids,
        selected_observation_id=selected.observation_id,
        status=status,
        candidate_digest=candidate_digest,
    ):
        return FactResolutionResult(
            resolution_id=str(current["resolution_id"]),
            logical_key=logical_key,
            candidate_count=len(candidates),
            selected_observation_id=selected.observation_id,
            resolution_status=status,
            material_dissent=material_dissent,
            created=False,
        )
    revision = 1 if current is None else int(current["revision"]) + 1
    parent = None if current is None else str(current["resolution_id"])
    identity = hashlib.sha256(
        (
            f"{logical_key}\0{candidate_digest}\0{selected.observation_id}\0"
            f"{status}\0{_POLICY_VERSION}"
        ).encode()
    ).hexdigest()
    resolution_id = f"fact-resolution:{identity}"
    reason = _resolution_reason(
        selected=selected,
        status=status,
        material_dissent=material_dissent,
        checks=checks,
    )
    ledger = ObservationResolutionLedger(conn)
    persisted = ledger.persist_resolution(
        ResolutionRevision(
            resolution_id=resolution_id,
            idempotency_key=f"fact-resolution:{identity}",
            logical_key=logical_key,
            revision=revision,
            candidate_observation_ids=candidate_ids,
            selected_observation_id=selected.observation_id,
            resolver_kind="deterministic_policy",
            policy_version=_POLICY_VERSION,
            reason=reason,
            knowledge_cutoff=knowledge_cutoff,
            effective_at=max(
                candidates,
                key=lambda candidate: _utc_instant(candidate.available_at),
            ).available_at,
            material_dissent=material_dissent,
            supersedes_resolution_id=parent,
            recorded_at=recorded_at,
        )
    )
    checks_json = json.dumps(checks, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO fact_resolution_outcomes "
        "(resolution_id, resolution_status, candidate_set_sha256, checks_json, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (resolution_id, status, candidate_digest, checks_json, recorded_at),
    )
    _emit_event(
        "financial_fact_resolution_recorded",
        logical_key=logical_key,
        resolution_id=resolution_id,
        status=status,
        candidate_count=len(candidates),
        material_dissent=material_dissent,
    )
    return FactResolutionResult(
        resolution_id=resolution_id,
        logical_key=logical_key,
        candidate_count=len(candidates),
        selected_observation_id=selected.observation_id,
        resolution_status=status,
        material_dissent=material_dissent,
        created=persisted.created,
    )


def resolve_fact_row(
    conn: sqlite3.Connection,
    *,
    fact_table: FactTable,
    fact_row_id: int,
    knowledge_cutoff: datetime | None = None,
) -> FactResolutionResult | None:
    """Resolve the row's complete logical key when the 0225 cutover is installed.

    Pre-cutover schemas return ``None`` and announce the compatibility branch.
    """

    if not _object_exists(conn, "fact_observation_revisions", object_type="table"):
        log.info(
            "financial_fact_resolution_pre_cutover",
            extra={"fact_table": fact_table, "fact_row_id": fact_row_id},
        )
        return None
    row = conn.execute(
        "SELECT logical_key FROM fact_observation_revisions "
        "WHERE fact_table = ? AND fact_row_id = ? ORDER BY fact_revision DESC LIMIT 1",
        (fact_table, fact_row_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"{fact_table}.id {fact_row_id} has no immutable observation after cutover"
        )
    available = conn.execute(
        "SELECT observation.available_at FROM fact_observation_revisions AS link "
        "JOIN reported_observations AS observation USING (observation_id) "
        "WHERE link.fact_table = ? AND link.fact_row_id = ? "
        "ORDER BY link.fact_revision DESC LIMIT 1",
        (fact_table, fact_row_id),
    ).fetchone()
    if available is None:
        raise RuntimeError(f"{fact_table}.id {fact_row_id} has no available observation clock")
    available_at = _datetime(available[0], field="available_at")
    now = datetime.now(UTC)
    if available_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    cutoff = knowledge_cutoff or max(now, available_at)
    return resolve_fact_logical_key(
        conn,
        logical_key=str(row[0]),
        knowledge_cutoff=cutoff,
        recorded_at=cutoff,
    )


def capture_fact_row_observation(
    conn: sqlite3.Connection,
    *,
    fact_table: FactTable,
    fact_row_id: int,
    recorded_at: datetime,
) -> bool:
    """Capture one already-persisted row without resolving its logical key.

    Live CompanyFacts admission uses this after its exact evidence match is
    accepted and before ``resolve_fact_row``.  The caller owns the transaction,
    so a later failure rolls back the fact, match, observation, and proof as one
    atomic unit.
    """

    created, _ = _capture_fact_row(
        conn,
        fact_table=fact_table,
        row_id=fact_row_id,
        recorded_at=recorded_at,
    )
    return created


def _capture_fact_row(
    conn: sqlite3.Connection,
    *,
    fact_table: FactTable,
    row_id: int,
    recorded_at: datetime,
) -> tuple[bool, str]:
    plan = _plan_fact_row(
        conn,
        fact_table=fact_table,
        row_id=row_id,
        recorded_at=recorded_at,
    )
    if plan.observation is None:
        return False, plan.logical_key
    persisted = ObservationResolutionLedger(conn).persist_observation(plan.observation)
    conn.execute(
        "INSERT INTO fact_observation_revisions "
        "(fact_table, fact_row_id, fact_revision, observation_id, logical_key, "
        "source_document_id, source_tier, locator_json, captured_at) "
        "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            fact_table,
            row_id,
            plan.observation.observation_id,
            plan.logical_key,
            plan.source_document_id,
            plan.source_tier,
            plan.locator_json,
            plan.captured_at,
        ),
    )
    if plan.match_revision_id is not None:
        proof_id = (
            "fact-proof:"
            + hashlib.sha256(
                (f"{plan.observation.observation_id}\0{plan.match_revision_id}").encode()
            ).hexdigest()
        )
        conn.execute(
            "INSERT INTO fact_observation_match_proofs "
            "(proof_id, idempotency_key, observation_id, match_revision_id, "
            "fact_table, fact_row_id, fact_revision, effective_at, "
            "knowledge_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                proof_id,
                proof_id,
                plan.observation.observation_id,
                plan.match_revision_id,
                fact_table,
                row_id,
                plan.observation.available_at,
                plan.captured_at,
                plan.captured_at,
            ),
        )
    return persisted.created, plan.logical_key


def _plan_fact_row(
    conn: sqlite3.Connection,
    *,
    fact_table: FactTable,
    row_id: int,
    recorded_at: datetime,
) -> _FactCapturePlan:
    row = _fact_row(conn, fact_table, row_id)
    existing = conn.execute(
        "SELECT observation_id, logical_key FROM fact_observation_revisions "
        "WHERE fact_table = ? AND fact_row_id = ? ORDER BY fact_revision DESC LIMIT 1",
        (fact_table, row_id),
    ).fetchone()
    if existing is not None:
        evidence = _evidence_context(conn, int(row["source_doc_id"]))
        if (
            evidence.source_kind == "sec_companyfacts"
            and _object_exists(
                conn,
                "fact_observation_match_proofs",
                object_type="table",
            )
            and conn.execute(
                "SELECT 1 "
                "FROM v_fact_observation_match_proofs_current_valid "
                "WHERE observation_id = ?",
                (str(existing["observation_id"]),),
            ).fetchone()
            is None
        ):
            raise ValueError("companyfacts_observation_match_proof_missing")
        return _FactCapturePlan(
            observation=None,
            logical_key=str(existing["logical_key"]),
            source_document_id=int(row["source_doc_id"]),
            source_tier="",
            locator_json=None,
            captured_at=recorded_at,
        )
    evidence = _evidence_context(conn, int(row["source_doc_id"]))
    logical_key = _logical_key(fact_table, row)
    observation_id = f"{fact_table}:{row_id}:r1"
    period_end = _datetime(row["period_end"], field="period_end")
    period_start = (
        period_end
        if evidence.period_start is None
        else _not_after(evidence.period_start, period_end)
    )
    method = str(row["extracted_by"] or "legacy-fact-backfill")
    numeric_value = _canonical_decimal(row["value"])
    match_revision_id = _validate_bound_companyfacts_fact(
        conn,
        row,
        fact_table=fact_table,
        evidence=evidence,
        numeric_value=numeric_value,
        knowledge_cutoff=recorded_at,
    )
    captured_at = _not_before(recorded_at, evidence.available_at)
    observation = ReportedObservation(
        observation_id=observation_id,
        idempotency_key=f"fact-capture:{observation_id}",
        issuer_id=evidence.issuer_id,
        ticker=str(row["ticker"]).upper(),
        concept_key=_concept_key(fact_table, row),
        period_start=period_start,
        period_end=period_end,
        fiscal_period_type=_observation_period_type(str(row["fiscal_period_type"])),
        dimensions=_dimensions(conn, fact_table, row),
        numeric_value=numeric_value,
        text_value=None,
        currency=_optional_text(row["currency"]) if fact_table == "financial_facts" else None,
        unit=_required_text(row["unit"], "unit"),
        scale=0,
        observation_status=_observation_status(fact_table, row),
        evidence_node_id=evidence.evidence_node_id,
        available_at=evidence.available_at,
        recorded_at=captured_at,
        method=method,
        method_version=(
            "0236-companyfacts-match-v1" if match_revision_id is not None else "0225-backfill-v1"
        ),
        confidence=float(row["confidence"]),
        legacy_table=None,
        legacy_row_id=None,
    )
    return _FactCapturePlan(
        observation=observation,
        logical_key=logical_key,
        source_document_id=int(row["source_doc_id"]),
        source_tier=evidence.source_tier,
        locator_json=_optional_text(row["locator"]),
        captured_at=captured_at,
        match_revision_id=match_revision_id,
    )


def _fact_row(conn: sqlite3.Connection, fact_table: FactTable, row_id: int) -> sqlite3.Row:
    if fact_table == "financial_facts":
        select = (
            "SELECT id, ticker, period_end, fiscal_period_type, line_item, value, currency, "
            "unit, source_doc_id, confidence, extracted_by, locator, NULL AS computed_from "
            "FROM financial_facts WHERE id = ?"
        )
    else:
        select = (
            "SELECT id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, "
            "NULL AS currency, unit, source_doc_id, confidence, extracted_by, locator, "
            "computed_from FROM kpi_facts WHERE id = ?"
        )
    row = conn.execute(select, (row_id,)).fetchone()
    if row is None:
        raise ValueError(f"{fact_table}.id {row_id} does not exist")
    if not isinstance(row, sqlite3.Row):
        raise RuntimeError("fact cutover requires sqlite3.Row row_factory")
    return row


def _evidence_context(conn: sqlite3.Connection, document_id: int) -> _EvidenceContext:
    row = None
    if _object_exists(
        conn,
        "v_legacy_document_evidence_bindings_current",
        object_type="view",
    ):
        row = conn.execute(
            "SELECT version.issuer_id, binding.evidence_node_id, "
            "COALESCE(version.period_start, document.period_start, document.period_end) "
            "AS period_start, source.retrieved_at, document.source_quality_tier, "
            "COALESCE(document.filing_date, document.fetched_at) AS source_effective_at, "
            "source.source_kind, source.blob_sha256, blob.storage_uri, "
            "binding.scope_locator_json, binding.binding_revision_id, "
            "binding.revision, binding.scope_content_sha256 "
            "FROM v_legacy_document_evidence_bindings_current AS binding "
            "JOIN evidence_document_versions AS version "
            "ON version.document_version_id = binding.document_version_id "
            "JOIN evidence_source_observations AS source "
            "ON source.observation_id = version.observation_id "
            "JOIN evidence_content_blobs AS blob ON blob.sha256 = source.blob_sha256 "
            "JOIN evidence_nodes AS node ON node.node_id = binding.evidence_node_id "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = node.extraction_run_id "
            "JOIN documents AS document ON document.id = binding.legacy_document_id "
            "WHERE binding.legacy_document_id = ? "
            "AND run.document_version_id = binding.document_version_id "
            "AND run.outcome = 'succeeded' "
            "ORDER BY binding.revision DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT version.issuer_id, node.node_id, "
            "COALESCE(version.period_start, document.period_start, document.period_end) AS period_start, "
            "source.retrieved_at, document.source_quality_tier, "
            "COALESCE(document.filing_date, document.fetched_at) AS source_effective_at, "
            "source.source_kind, source.blob_sha256, blob.storage_uri, "
            "NULL, NULL, NULL, NULL "
            "FROM evidence_document_versions AS version "
            "JOIN evidence_source_observations AS source "
            "ON source.observation_id = version.observation_id "
            "JOIN evidence_content_blobs AS blob ON blob.sha256 = source.blob_sha256 "
            "JOIN evidence_extraction_runs AS run "
            "ON run.document_version_id = version.document_version_id "
            "JOIN evidence_nodes AS node ON node.extraction_run_id = run.extraction_run_id "
            "JOIN documents AS document ON document.id = version.legacy_document_id "
            "WHERE version.legacy_document_id = ? AND run.outcome = 'succeeded' "
            "AND node.node_kind = 'document' "
            "ORDER BY version.version_sequence DESC, node.revision DESC, "
            "run.completed_at DESC, node.node_id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    if row is None:
        raise ValueError("missing_evidence_document_anchor")
    source_tier = _required_text(row[4], "source_quality_tier")
    if source_tier not in _TIER_RANK:
        raise ValueError("unknown_source_tier")
    return _EvidenceContext(
        issuer_id=_required_text(row[0], "issuer_id"),
        evidence_node_id=_required_text(row[1], "evidence_node_id"),
        period_start=(None if row[2] is None else _datetime(row[2], field="period_start")),
        available_at=_datetime(row[3], field="retrieved_at"),
        source_tier=source_tier,
        source_effective_at=_datetime(row[5], field="source_effective_at"),
        source_kind=_optional_text(row[6]),
        blob_sha256=_optional_text(row[7]),
        storage_uri=_optional_text(row[8]),
        scope_locator_json=_optional_text(row[9]),
        legacy_binding_revision_id=_optional_text(row[10]),
        legacy_binding_revision=(None if row[11] is None else int(row[11])),
        binding_scope_content_sha256=_optional_text(row[12]),
    )


def _validate_bound_companyfacts_fact(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    fact_table: FactTable,
    evidence: _EvidenceContext,
    numeric_value: str,
    knowledge_cutoff: datetime,
) -> str | None:
    """Prove a legacy fact still exists in the exact immutable SEC snapshot.

    At schema 0235+, an accepted current matcher revision is mandatory and is
    returned for the observation-proof bridge.  Older schemas retain the
    direct immutable-byte check for compatibility.  An accession binding by
    itself is never sufficient because an aggregate SEC response can relocate
    or duplicate individual entries.
    """

    if evidence.source_kind != "sec_companyfacts" or evidence.scope_locator_json is None:
        return None
    if fact_table == "kpi_facts":
        raise ValueError("companyfacts_derived_fact_requires_input_lineage")
    if _object_exists(
        conn,
        "legacy_fact_evidence_match_revisions",
        object_type="table",
    ):
        if not _object_exists(
            conn,
            "fact_observation_match_proofs",
            object_type="table",
        ):
            raise ValueError("companyfacts_fact_match_proof_schema_required")
        if (
            evidence.legacy_binding_revision_id is None
            or evidence.legacy_binding_revision is None
            or evidence.binding_scope_content_sha256 is None
        ):
            raise ValueError("companyfacts_fact_evidence_match_required")
        matches = conn.execute(
            "SELECT match_revision_id, knowledge_at "
            "FROM v_legacy_fact_evidence_matches_accepted_current "
            "WHERE fact_table = ? AND fact_row_id = ? "
            "AND legacy_binding_revision_id = ? "
            "AND legacy_binding_revision = ? "
            "AND binding_scope_content_sha256 = ? "
            "AND evidence_node_id = ? "
            "ORDER BY revision DESC",
            (
                fact_table,
                int(row["id"]),
                evidence.legacy_binding_revision_id,
                evidence.legacy_binding_revision,
                evidence.binding_scope_content_sha256,
                evidence.evidence_node_id,
            ),
        ).fetchall()
        eligible = [
            match
            for match in matches
            if _utc_instant(_datetime(match[1], field="match knowledge_at"))
            <= _utc_instant(knowledge_cutoff)
        ]
        if len(eligible) != 1:
            raise ValueError("companyfacts_fact_evidence_match_required")
        return _required_text(eligible[0][0], "match_revision_id")
    if evidence.blob_sha256 is None or evidence.storage_uri is None:
        raise ValueError("companyfacts_locator_unverified")
    locator_raw = _optional_text(row["locator"])
    if locator_raw is None:
        raise ValueError("companyfacts_locator_unverified")
    try:
        locator_decoded: object = json.loads(locator_raw)
        scope_decoded: object = json.loads(evidence.scope_locator_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("companyfacts_locator_unverified") from exc
    if not isinstance(locator_decoded, dict) or not isinstance(scope_decoded, dict):
        raise ValueError("companyfacts_locator_unverified")
    locator = cast("dict[str, object]", locator_decoded)
    scope = cast("dict[str, object]", scope_decoded)
    table_cell = locator.get("table_cell")
    nested = cast("dict[str, object]", table_cell) if isinstance(table_cell, dict) else {}
    path_raw = locator.get("json_path") or nested.get("json_path")
    if not isinstance(path_raw, str):
        raise ValueError("companyfacts_locator_unverified")
    match = _COMPANYFACTS_PATH.fullmatch(path_raw)
    if match is None:
        raise ValueError("companyfacts_locator_unverified")
    namespace, concept, unit_code, index_raw = match.groups()
    payload = _verified_companyfacts_payload(
        evidence.storage_uri,
        evidence.blob_sha256,
    )
    try:
        facts = _companyfacts_dict(payload.get("facts"))
        namespace_facts = _companyfacts_dict(facts.get(namespace))
        concept_payload = _companyfacts_dict(namespace_facts.get(concept))
        units = _companyfacts_dict(concept_payload.get("units"))
        entries = _companyfacts_list(units.get(unit_code))
        entry_raw = entries[int(index_raw)]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("companyfacts_locator_unverified") from exc
    if not isinstance(entry_raw, dict):
        raise ValueError("companyfacts_locator_unverified")
    entry = cast("dict[str, object]", entry_raw)
    accession = scope.get("accession_number")
    if not isinstance(accession, str) or entry.get("accn") != accession:
        raise ValueError("companyfacts_locator_unverified")
    period_end = str(row["period_end"])[:10]
    if entry.get("end") != period_end:
        raise ValueError("companyfacts_locator_unverified")
    try:
        extracted_value = _decimal(entry.get("val"), field="companyfacts value")
    except ValueError as exc:
        raise ValueError("companyfacts_locator_unverified") from exc
    if extracted_value != _decimal(numeric_value, field="fact value"):
        raise ValueError("companyfacts_locator_unverified")
    cited_value = nested.get("cell_value_as_extracted")
    if cited_value is not None:
        try:
            if _decimal(cited_value, field="cited companyfacts value") != extracted_value:
                raise ValueError("companyfacts_locator_unverified")
        except ValueError as exc:
            raise ValueError("companyfacts_locator_unverified") from exc
    return None


@lru_cache(maxsize=256)
def _verified_companyfacts_payload(
    storage_uri: str,
    expected_sha256: str,
) -> dict[str, object]:
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file":
        raise ValueError("companyfacts_locator_unverified")
    path_text = unquote(parsed.path)
    if re.fullmatch(r"/[A-Za-z]:/.*", path_text):
        path_text = path_text[1:]
    path = Path(path_text)
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise ValueError("companyfacts_locator_unverified") from exc
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ValueError("companyfacts_locator_unverified")
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("companyfacts_locator_unverified") from exc
    if not isinstance(decoded, dict):
        raise ValueError("companyfacts_locator_unverified")
    return cast("dict[str, object]", decoded)


def _companyfacts_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("companyfacts_locator_unverified")
    return cast("dict[str, object]", value)


def _companyfacts_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("companyfacts_locator_unverified")
    return cast("list[object]", value)


def _load_complete_candidates(
    conn: sqlite3.Connection, logical_key: str, knowledge_cutoff: datetime
) -> tuple[_Candidate, ...]:
    rows = conn.execute(
        "SELECT link.observation_id, link.fact_table, link.fact_row_id, link.fact_revision, "
        "observation.numeric_value, observation.currency, observation.unit, "
        "observation.period_start, observation.period_end, observation.fiscal_period_type, "
        "link.source_tier, COALESCE(document.filing_date, document.fetched_at), "
        "observation.available_at, document.source_type "
        "FROM fact_observation_revisions AS link "
        "JOIN reported_observations AS observation USING (observation_id) "
        "JOIN documents AS document ON document.id = link.source_document_id "
        "WHERE link.logical_key = ? AND observation.available_at <= ? "
        "AND link.fact_revision = (SELECT MAX(latest.fact_revision) "
        "FROM fact_observation_revisions AS latest "
        "WHERE latest.fact_table = link.fact_table "
        "AND latest.fact_row_id = link.fact_row_id) "
        "ORDER BY link.observation_id",
        (logical_key, knowledge_cutoff),
    ).fetchall()
    candidates: list[_Candidate] = []
    for row in rows:
        fact_table = str(row[1])
        if fact_table not in _TABLES:
            raise RuntimeError(f"invalid captured fact table {fact_table!r}")
        numeric = _decimal(row[4], field="numeric_value")
        candidates.append(
            _Candidate(
                observation_id=str(row[0]),
                fact_table=fact_table,
                fact_row_id=int(row[2]),
                fact_revision=int(row[3]),
                numeric_value=numeric,
                currency=_optional_text(row[5]),
                unit=_required_text(row[6], "unit"),
                period_start=_datetime(row[7], field="period_start"),
                period_end=_datetime(row[8], field="period_end"),
                fiscal_period_type=_required_text(row[9], "fiscal_period_type"),
                source_tier=_required_text(row[10], "source_tier"),
                source_type=_required_text(row[13], "source_type"),
                source_effective_at=_datetime(row[11], field="source_effective_at"),
                available_at=_datetime(row[12], field="available_at"),
            )
        )
    return tuple(candidates)


def _candidate_checks(candidates: tuple[_Candidate, ...]) -> dict[str, bool]:
    units = {candidate.unit.casefold() for candidate in candidates}
    currencies = {
        None if candidate.currency is None else candidate.currency.upper()
        for candidate in candidates
    }
    periods = {
        (
            _utc_instant(candidate.period_start),
            _utc_instant(candidate.period_end),
            candidate.fiscal_period_type,
        )
        for candidate in candidates
    }
    tiers_known = all(candidate.source_tier in _TIER_RANK for candidate in candidates)
    authorities_known = all(_candidate_authority_rank(candidate) >= 0 for candidate in candidates)
    tables = {candidate.fact_table for candidate in candidates}
    return {
        "candidate_set_nonempty": bool(candidates),
        "source_authorities_known": authorities_known,
        "currency_consistent": len(currencies) == 1,
        "fact_kind_consistent": len(tables) == 1,
        "period_consistent": len(periods) == 1,
        "source_tiers_known": tiers_known,
        "unit_consistent": len(units) == 1,
    }


def _has_material_dissent(candidates: tuple[_Candidate, ...]) -> bool:
    if len(candidates) < 2:
        return False
    values = tuple(candidate.numeric_value for candidate in candidates)
    maximum = max(values)
    minimum = min(values)
    if maximum == minimum:
        return False
    denominator = max(abs(maximum), abs(minimum))
    if denominator == 0:
        return True
    return abs(maximum - minimum) / denominator > _MATERIAL_RELATIVE_DELTA


def _has_any_value_dissent(candidates: tuple[_Candidate, ...]) -> bool:
    return len({candidate.numeric_value for candidate in candidates}) > 1


def _candidate_authority(candidate: _Candidate) -> EvidenceAuthority | None:
    try:
        source_type = SourceType(candidate.source_type)
    except ValueError:
        return None
    return classification_for_source_type(source_type).authority


def _candidate_authority_rank(candidate: _Candidate) -> int:
    authority = _candidate_authority(candidate)
    return -1 if authority is None else _AUTHORITY_RANK.get(authority, -1)


def _current_resolution(conn: sqlite3.Connection, logical_key: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT resolution_id, revision, selected_observation_id, material_dissent "
        "FROM v_observation_resolution_current WHERE logical_key = ?",
        (logical_key,),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row, sqlite3.Row):
        raise RuntimeError("fact resolution requires sqlite3.Row row_factory")
    return row


def _same_current_decision(
    conn: sqlite3.Connection,
    *,
    current_resolution_id: str,
    candidate_ids: tuple[str, ...],
    selected_observation_id: str,
    status: ResolutionStatus,
    candidate_digest: str,
) -> bool:
    current = conn.execute(
        "SELECT resolution.selected_observation_id, resolution.policy_version, "
        "outcome.resolution_status, outcome.candidate_set_sha256 "
        "FROM observation_resolution_revisions AS resolution "
        "JOIN fact_resolution_outcomes AS outcome USING (resolution_id) "
        "WHERE resolution.resolution_id = ?",
        (current_resolution_id,),
    ).fetchone()
    if current is None:
        return False
    stored_candidates = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT observation_id FROM observation_resolution_candidates "
            "WHERE resolution_id = ? ORDER BY observation_id",
            (current_resolution_id,),
        ).fetchall()
    )
    return (
        stored_candidates == candidate_ids
        and str(current[0]) == selected_observation_id
        and str(current[1]) == _POLICY_VERSION
        and str(current[2]) == status
        and str(current[3]) == candidate_digest
    )


def _resolution_reason(
    *,
    selected: _Candidate,
    status: ResolutionStatus,
    material_dissent: bool,
    checks: dict[str, bool],
) -> str:
    failed = sorted(name for name, passed in checks.items() if not passed)
    details = {
        "failed_checks": failed,
        "material_dissent": material_dissent,
        "policy": _POLICY_VERSION,
        "selected_source_tier": selected.source_tier,
        "status": status,
    }
    return json.dumps(details, sort_keys=True, separators=(",", ":"))


def _next_rows(
    conn: sqlite3.Connection,
    checkpoint: FactCutoverCheckpoint,
    batch_size: int,
) -> tuple[list[tuple[FactTable, int]], FactCutoverCheckpoint]:
    """Return only unresolved rows without permanently skipping quarantines.

    The cursor makes one fair forward pass across both legacy tables.  When a
    pass reaches the end while unresolved rows remain behind the cursor, it
    wraps to the beginning for the next invocation.  A transiently
    quarantined row is therefore retried after the rest of the backlog, while
    rows already linked to observations disappear from the scan.
    """

    rows: list[tuple[FactTable, int]] = []
    table_index = checkpoint.table_index
    last_row_id = checkpoint.last_row_id
    while table_index < len(_TABLES) and len(rows) < batch_size:
        table = _TABLES[table_index]
        available = conn.execute(
            f"SELECT fact.id FROM {table} AS fact "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "WHERE fact.id > ? AND NOT EXISTS ("
            "SELECT 1 FROM fact_observation_revisions AS link "
            "WHERE link.fact_table = ? AND link.fact_row_id = fact.id"
            ") ORDER BY fact.id LIMIT ?",
            (last_row_id, table, batch_size - len(rows)),
        ).fetchall()
        rows.extend((table, int(row[0])) for row in available)
        if available:
            last_row_id = int(available[-1][0])
        has_more = conn.execute(
            f"SELECT 1 FROM {table} AS fact "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "WHERE fact.id > ? AND NOT EXISTS ("
            "SELECT 1 FROM fact_observation_revisions AS link "
            "WHERE link.fact_table = ? AND link.fact_row_id = fact.id"
            ") LIMIT 1",
            (last_row_id, table),
        ).fetchone()
        if has_more is not None:
            break
        table_index += 1
        last_row_id = 0
    if table_index >= len(_TABLES) and _has_unresolved_rows(conn):
        if not rows:
            return _next_rows(
                conn,
                FactCutoverCheckpoint(
                    table_index=0,
                    last_row_id=0,
                    updated_at=datetime.now(UTC),
                ),
                batch_size,
            )
        table_index = 0
        last_row_id = 0
    return (
        rows,
        FactCutoverCheckpoint(
            table_index=table_index,
            last_row_id=last_row_id,
            updated_at=datetime.now(UTC),
        ),
    )


def _is_complete(conn: sqlite3.Connection, checkpoint: FactCutoverCheckpoint) -> bool:
    del checkpoint
    return not _has_unresolved_rows(conn)


def _has_unresolved_rows(conn: sqlite3.Connection) -> bool:
    return any(
        conn.execute(
            f"SELECT 1 FROM {table} AS fact WHERE NOT EXISTS ("  # nosec B608 -- trusted internal SQL shape; values remain bound
            "SELECT 1 FROM fact_observation_revisions AS link "
            "WHERE link.fact_table = ? AND link.fact_row_id = fact.id"
            ") LIMIT 1",
            (table,),
        ).fetchone()
        is not None
        for table in _TABLES
    )


def _read_checkpoint(path: Path) -> FactCutoverCheckpoint:
    if not path.exists():
        return FactCutoverCheckpoint(table_index=0, last_row_id=0, updated_at=datetime.now(UTC))
    return FactCutoverCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))


def _write_checkpoint(path: Path, checkpoint: FactCutoverCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _logical_key(fact_table: FactTable, row: sqlite3.Row) -> str:
    return (
        f"{fact_table}:{str(row['ticker']).upper()}:{_concept_key(fact_table, row)}:"
        f"{str(row['period_end'])[:10]}:{str(row['fiscal_period_type']).upper()}"
    )


def _concept_key(fact_table: FactTable, row: sqlite3.Row) -> str:
    if fact_table == "financial_facts":
        return _required_text(row["line_item"], "line_item")
    return f"kpi_definition:{int(row['kpi_definition_id'])}"


def _dimensions(
    conn: sqlite3.Connection, fact_table: FactTable, row: sqlite3.Row
) -> tuple[ObservationDimension, ...]:
    if fact_table == "financial_facts":
        return ()
    values: dict[str, str] = {
        "kpi_definition_id": str(int(row["kpi_definition_id"])),
    }
    if _object_exists(conn, "kpi_fact_semantic_contexts", object_type="table"):
        columns = {
            str(column[1])
            for column in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
        }
        current_predicate = (
            " AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts successor "
            "WHERE successor.supersedes_context_id=context.id)"
            if {"revision", "supersedes_context_id"}.issubset(columns)
            else ""
        )
        context = conn.execute(
            "SELECT period_role,accounting_basis,consolidation_scope,dimensions_json,"  # nosec B608
            "unit_scale,status,metric_name_as_reported,publication_lane "
            "FROM kpi_fact_semantic_contexts context WHERE kpi_fact_id=?"
            + current_predicate
            + " ORDER BY id DESC LIMIT 1",
            (int(row["id"]),),
        ).fetchone()
        if context is not None:
            values.update(
                {
                    "semantic_status": str(context[5]),
                    "period_role": str(context[0]),
                    "accounting_basis": str(context[1]),
                    "consolidation_scope": str(context[2]),
                    "unit_scale": str(context[4]),
                    "metric_name_as_reported": str(context[6]),
                    "publication_lane": str(context[7]),
                }
            )
            try:
                dimensions = json.loads(str(context[3]))
            except json.JSONDecodeError:
                dimensions = {}
            if isinstance(dimensions, dict):
                typed_dimensions = cast("dict[str, object]", dimensions)
                values.update(
                    {f"scope:{key}": str(value) for key, value in typed_dimensions.items()}
                )
    return tuple(
        ObservationDimension(key=key, value=value) for key, value in sorted(values.items())
    )


def _observation_status(fact_table: FactTable, row: sqlite3.Row) -> Literal["reported", "derived"]:
    extracted_by = str(row["extracted_by"] or "").casefold()
    if "derived" in extracted_by:
        return "derived"
    if fact_table == "kpi_facts" and row["computed_from"] is not None:
        return "derived"
    return "reported"


def _observation_period_type(
    value: str,
) -> Literal["annual", "quarter", "year_to_date", "instant", "other"]:
    normalized = value.upper()
    if normalized in {"Q1", "Q2", "Q3", "Q4", "QUARTER"}:
        return "quarter"
    if normalized in {"FY", "ANNUAL"}:
        return "annual"
    if normalized in {"YTD", "TTM", "YEAR_TO_DATE"}:
        return "year_to_date"
    if normalized == "INSTANT":
        return "instant"
    return "other"


def _canonical_decimal(value: object) -> str:
    decimal = _decimal(value, field="value")
    normalized = format(decimal, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return decimal


def _datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO datetime") from exc
    raise ValueError(f"{field} must be an ISO datetime")


def _not_before(candidate: datetime, lower_bound: datetime) -> datetime:
    """Compare UTC clocks while preserving the stored clock's timezone shape."""

    if lower_bound.tzinfo is None:
        comparable = candidate.replace(tzinfo=None)
    elif candidate.tzinfo is None:
        comparable = candidate.replace(tzinfo=UTC).astimezone(lower_bound.tzinfo)
    else:
        comparable = candidate.astimezone(lower_bound.tzinfo)
    return max(comparable, lower_bound)


def _utc_instant(value: datetime) -> datetime:
    """Return a timezone-aware UTC comparison key for mixed legacy clocks."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _not_after(candidate: datetime, upper_bound: datetime) -> datetime:
    """Use a source period only when it does not exceed the fact's own end."""

    if upper_bound.tzinfo is None:
        comparable = candidate.replace(tzinfo=None)
    elif candidate.tzinfo is None:
        comparable = candidate.replace(tzinfo=UTC).astimezone(upper_bound.tzinfo)
    else:
        comparable = candidate.astimezone(upper_bound.tzinfo)
    return min(comparable, upper_bound)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _object_exists(
    conn: sqlite3.Connection, name: str, *, object_type: Literal["table", "view"]
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
        is not None
    )


def _require_cutover_schema(conn: sqlite3.Connection) -> None:
    required = (
        "reported_observations",
        "observation_resolution_revisions",
        "observation_resolution_candidates",
        "fact_observation_revisions",
        "fact_resolution_outcomes",
    )
    missing = [table for table in required if not _object_exists(conn, table, object_type="table")]
    if missing:
        raise RuntimeError(
            "financial fact cutover requires migration 0225; missing: " + ", ".join(missing)
        )


def _quarantine_reason(exc: Exception) -> str:
    text = str(exc)
    for reason in (
        "missing_evidence_document_anchor",
        "companyfacts_derived_fact_requires_input_lineage",
        "companyfacts_fact_match_proof_schema_required",
        "companyfacts_fact_evidence_match_required",
        "companyfacts_observation_match_proof_missing",
        "companyfacts_locator_unverified",
        "unknown_source_tier",
    ):
        if reason in text:
            return reason
    return "invalid_legacy_fact"


def _finding(summary: FactCutoverSummary, reason: str) -> None:
    summary.finding_counts[reason] = summary.finding_counts.get(reason, 0) + 1


def _emit_event(event: str, **fields: object) -> None:
    log.info(event, extra={"event": event, **fields})
