"""Deterministic legacy-fact relocation inside immutable CompanyFacts blobs.

The matcher is deliberately offline.  It consumes the exact current
``legacy_document_evidence`` binding, verifies the immutable blob and the
binding's accession-scoped digest, and appends only match-ledger revisions.
It never changes legacy facts, observations, or evidence bindings.

The private ``pipeline.sec_xbrl`` helpers imported below are the canonical
legacy extraction semantics.  Parity tests lock their unit, fiscal-period, and
sign behavior to this backfill so historical proof cannot silently diverge
from the extractor that created the rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

import pipeline.sec_xbrl as sec_xbrl
import provenance.sec_companyfacts_capture as companyfacts_capture
from pipeline.sec_xbrl import (
    TAG_LADDERS,
    FiscalPeriodType,
    LadderKind,
    LineItemLadder,
    Unit,
)
from provenance.legacy_fact_evidence_match import (
    CanonicalJSONObject,
    CompanyFactsCandidateManifestV1,
    CompanyFactsCandidateV1,
    CompanyFactsRelocatedLocator,
    FactPayloadV1,
    FinancialFactPayloadV1,
    KpiFactPayloadV1,
    LegacyFactEvidenceMatchLedger,
    LegacyFactEvidenceMatchRevision,
    OriginalFactLocator,
)
from provenance.sec_companyfacts_capture import CompanyFactsPayload, parse_companyfacts_body

_Mode = Literal["dry_run", "apply"]
_FactTable = Literal["financial_facts", "kpi_facts"]
_Outcome = Literal["accepted", "retryable", "terminal"]
_BlobCache = dict[
    tuple[str, str],
    tuple[CompanyFactsPayload, dict[str, bytes]],
]
_MATCHER_NAME = "deterministic-companyfacts-relocator"
_MATCHER_VERSION = "1"
_MATCHER_CONFIG: dict[str, JsonValue] = {
    "candidate_manifest": "all_accession_ladder_entries",
    "decimal_comparison": "exact_signed",
    "derived_kpi_policy": "terminal_not_document_matchable",
    "fiscal_semantics": "pipeline.sec_xbrl@current",
    "locator_policy": "old_index_is_hint",
    "matcher_name": _MATCHER_NAME,
    "matcher_version": _MATCHER_VERSION,
    "scope_verification": "canonical_accession_scope_sha256",
    "unit_semantics": "pipeline.sec_xbrl@current",
}
_MATCHER_CONFIG_JSON = json.dumps(
    _MATCHER_CONFIG,
    sort_keys=True,
    separators=(",", ":"),
)
MATCHER_CONFIG_SHA256 = hashlib.sha256(_MATCHER_CONFIG_JSON.encode()).hexdigest()
_currency_of_unit_code = cast(
    "Callable[[str, LadderKind], str | None]",
    vars(sec_xbrl)["_currency_of_unit_code"],
)
_infer_fye_month = cast(
    "Callable[[dict[str, object]], int]",
    vars(sec_xbrl)["_infer_fye_month"],
)
_modal_currency = cast(
    "Callable[[dict[str, object], LadderKind], str | None]",
    vars(sec_xbrl)["_modal_currency"],
)
_resolve_fiscal_period_type = cast(
    "Callable[..., FiscalPeriodType | None]",
    vars(sec_xbrl)["_resolve_fiscal_period_type"],
)
_same_doc_pick_key = cast(
    "Callable[[Mapping[str, object], Decimal], tuple[str, int, Decimal]]",
    vars(sec_xbrl)["_same_doc_pick_key"],
)
_accession_scopes = cast(
    "Callable[[CompanyFactsPayload], dict[str, bytes]]",
    vars(companyfacts_capture)["_accession_scopes"],
)
_REQUIRED_TABLES = frozenset(
    {
        "documents",
        "evidence_content_blobs",
        "evidence_document_versions",
        "evidence_source_observations",
        "financial_facts",
        "kpi_facts",
        "legacy_document_evidence_binding_revisions",
        "legacy_fact_evidence_match_revisions",
    }
)
_REQUIRED_VIEWS = frozenset(
    {
        "v_evidence_blob_locations_current",
        "v_evidence_document_versions_canonical",
        "v_issuer_identifiers_canonical",
        "v_legacy_document_evidence_bindings_current",
        "v_legacy_fact_evidence_matches_current",
    }
)


class CompanyFactsFactMatcherError(RuntimeError):
    """The matcher cannot safely continue."""


class CompanyFactsFactMatcherRequest(BaseModel):
    """Explicit bounded controls for one offline plan or append-only batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    blob_root: Path
    checkpoint_root: Path
    apply: bool = False
    batch_size: int = Field(default=500, ge=1, le=10_000)
    task_id: str = Field(
        default="legacy-companyfacts-fact-match",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    fact_tables: tuple[_FactTable, ...] = ("financial_facts", "kpi_facts")

    @field_validator("fact_tables")
    @classmethod
    def _fact_tables(
        cls,
        values: tuple[_FactTable, ...],
    ) -> tuple[_FactTable, ...]:
        if not values:
            raise ValueError("at least one fact table is required")
        if len(values) != len(set(values)):
            raise ValueError("fact tables must be unique")
        return tuple(sorted(values))


class CompanyFactsFactMatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_table: _FactTable
    fact_row_id: int = Field(gt=0)
    binding_revision_id: str
    outcome: Literal["planned", "accepted", "retryable", "terminal"]
    reason_code: str
    candidate_count: int = Field(ge=0)
    matched_candidate_count: int = Field(ge=0)
    revision_created: bool = False


class CompanyFactsFactMatchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    mode: _Mode
    dry_run: bool
    run_at: datetime
    matcher_name: str
    matcher_version: str
    matcher_config_sha256: str
    batch_size: int
    candidates_total: int = Field(ge=0)
    already_current: int | None = Field(default=None, ge=0)
    considered: int = Field(ge=0)
    accepted: int = Field(ge=0)
    retryable: int = Field(ge=0)
    terminal: int = Field(ge=0)
    revisions_created: int = Field(ge=0)
    revisions_replayed: int = Field(ge=0)
    has_more: bool
    items: tuple[CompanyFactsFactMatchItem, ...]


class CompanyFactsFactMatchCheckpoint(BaseModel):
    """Observability only; readiness is always recomputed from current views."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    matcher_config_sha256: str
    last_run_at: datetime
    last_considered: tuple[str, ...]


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_table: _FactTable
    fact_row_id: int
    issuer_id: str
    binding_revision_id: str
    binding_revision: int
    binding_scope_content_sha256: str
    evidence_node_id: str
    binding_effective_at: datetime
    binding_knowledge_at: datetime
    scope_locator_json: str
    document_version_id: str
    blob_sha256: str
    storage_uri: str
    retrieved_at: datetime
    normalized_cik: str | None
    canonical_cik_count: int = Field(ge=0)
    fact_payload: FactPayloadV1
    prior_match_revision_id: str | None
    prior_revision: int
    prior_outcome: _Outcome | None
    exact_current: bool

    @property
    def key(self) -> str:
        return f"{self.fact_table}:{self.fact_row_id}"


class _CandidateEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: CompanyFactsCandidateV1
    context: bool
    unit: bool
    sign: bool
    fiscal_period: bool
    value: bool

    @property
    def matches(self) -> bool:
        return self.context and self.unit and self.sign and self.fiscal_period and self.value


class _RawCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    candidate: CompanyFactsCandidateV1
    rung_index: int = Field(ge=0)
    unit_code: str
    entry: dict[str, JsonValue]
    end: str
    fiscal_labels: frozenset[str]
    modal_currency: str | None
    parsed_currency: str | None
    signed_value: Decimal
    pick_key: tuple[str, int, Decimal]


def emit_structured_event(event: str, **fields: object) -> None:
    """Emit one machine-readable event to stderr."""

    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def match_legacy_companyfacts_evidence(
    conn: sqlite3.Connection,
    request: CompanyFactsFactMatcherRequest,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CompanyFactsFactMatchSummary:
    """Plan or append one bounded readiness-derived match batch."""

    _require_schema(conn)
    _register_payload_fingerprint_function(conn)
    run_at = _timeline(now())
    if request.apply:
        conn.execute("BEGIN IMMEDIATE")
    try:
        targets, has_more = _load_targets(conn, request)
        results: list[CompanyFactsFactMatchItem] = []
        ledger = LegacyFactEvidenceMatchLedger(conn) if request.apply else None
        blob_cache: _BlobCache = {}
        for target in targets:
            record = _match_target(
                target,
                request.blob_root,
                blob_cache=blob_cache,
            )
            persisted = ledger.persist(record) if ledger is not None else None
            results.append(
                CompanyFactsFactMatchItem(
                    fact_table=target.fact_table,
                    fact_row_id=target.fact_row_id,
                    binding_revision_id=target.binding_revision_id,
                    outcome=record.outcome,
                    reason_code=record.reason_code,
                    candidate_count=record.candidate_count or 0,
                    matched_candidate_count=record.matched_candidate_count,
                    revision_created=(persisted.created if persisted is not None else False),
                )
            )
        if request.apply:
            conn.commit()
    except Exception:
        if request.apply:
            conn.rollback()
        raise

    if not request.apply:
        return _summary(
            request,
            run_at=run_at,
            candidates_total=len(targets),
            already_current=None,
            has_more=has_more,
            items=tuple(results),
        )

    checkpoint = CompanyFactsFactMatchCheckpoint(
        task_id=request.task_id,
        matcher_config_sha256=MATCHER_CONFIG_SHA256,
        last_run_at=run_at,
        last_considered=tuple(target.key for target in targets),
    )
    _write_checkpoint(
        request.checkpoint_root / request.task_id / "state.json",
        checkpoint,
    )
    return _summary(
        request,
        run_at=run_at,
        candidates_total=len(targets),
        already_current=None,
        has_more=has_more,
        items=tuple(results),
    )


def _load_targets(
    conn: sqlite3.Connection,
    request: CompanyFactsFactMatcherRequest,
) -> tuple[list[_Target], bool]:
    selected, has_more = _select_ready_fact_ids(conn, request)
    rows: list[sqlite3.Row | tuple[object, ...]] = []
    for fact_table in request.fact_tables:
        fact_ids = tuple(
            fact_row_id for selected_table, fact_row_id in selected if selected_table == fact_table
        )
        if fact_ids:
            rows.extend(
                conn.execute(
                    _target_sql(fact_table, fact_ids),
                    fact_ids,
                ).fetchall()
            )
    loaded = [_target_from_row(row) for row in rows]
    grouped: dict[tuple[_FactTable, int], list[_Target]] = {}
    for target in loaded:
        grouped.setdefault((target.fact_table, target.fact_row_id), []).append(target)
    targets = [
        _select_blob_location(group, request.blob_root) for _, group in sorted(grouped.items())
    ]
    by_key = {(target.fact_table, target.fact_row_id): target for target in targets}
    ordered = [by_key[key] for key in selected if key in by_key]
    return ordered, has_more


def _register_payload_fingerprint_function(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "legacy_fact_payload_sha256",
        17,
        _sql_payload_fingerprint,
        deterministic=True,
    )


def _sql_payload_fingerprint(*values: object) -> str:
    if len(values) != 17:
        raise CompanyFactsFactMatcherError("legacy fact payload fingerprint requires 17 arguments")
    (
        fact_table_raw,
        fact_row_id,
        ticker,
        period_end,
        fiscal_period_type,
        value,
        unit,
        source_doc_id,
        extracted_by,
        locator,
        line_item,
        currency,
        kpi_definition_id,
        source_excerpt,
        computed_from,
        formula_id,
        formula_version,
    ) = values
    fact_table = cast("_FactTable", str(fact_table_raw))
    row: dict[str, object] = {
        "id": fact_row_id,
        "ticker": ticker,
        "period_end": period_end,
        "fiscal_period_type": fiscal_period_type,
        "value": value,
        "unit": unit,
        "source_doc_id": source_doc_id,
        "extracted_by": extracted_by,
        "locator": locator,
        "line_item": line_item,
        "currency": currency,
        "kpi_definition_id": kpi_definition_id,
        "source_excerpt": source_excerpt,
        "computed_from": computed_from,
        "formula_id": formula_id,
        "formula_version": formula_version,
    }
    return _fact_payload(fact_table, row).canonical_sha256


def _payload_fingerprint_sql(fact_table: _FactTable) -> str:
    financial_values = "fact.line_item, fact.currency, NULL, NULL, NULL, NULL, NULL"
    kpi_values = (
        "NULL, NULL, fact.kpi_definition_id, fact.source_excerpt, "
        "fact.computed_from, fact.formula_id, fact.formula_version"
    )
    table_values = financial_values if fact_table == "financial_facts" else kpi_values
    return (
        "legacy_fact_payload_sha256("
        f"'{fact_table}', fact.id, fact.ticker, fact.period_end, "
        "fact.fiscal_period_type, fact.value, fact.unit, fact.source_doc_id, "
        f"fact.extracted_by, fact.locator, {table_values})"
    )


def _select_ready_fact_ids(
    conn: sqlite3.Connection,
    request: CompanyFactsFactMatcherRequest,
) -> tuple[list[tuple[_FactTable, int]], bool]:
    selected: list[tuple[_FactTable, int]] = []
    lanes = ("unseen", "stale", "retryable")
    for lane_index, lane in enumerate(lanes):
        remaining = request.batch_size - len(selected)
        if remaining <= 0:
            return selected, True
        lane_rows: list[tuple[int, _FactTable, int]] = []
        for fact_table in request.fact_tables:
            sql, params = _ready_key_sql(
                fact_table,
                lane=lane,
                limit=remaining + 1,
            )
            for row in conn.execute(sql, params):
                if not isinstance(row, sqlite3.Row):
                    raise CompanyFactsFactMatcherError("matcher requires sqlite3.Row row_factory")
                lane_rows.append(
                    (
                        int(row["prior_revision"]),
                        fact_table,
                        int(row["fact_row_id"]),
                    )
                )
        lane_rows.sort(
            key=(
                (lambda item: (item[0], item[1], item[2]))
                if lane == "retryable"
                else (lambda item: (item[1], item[2]))
            )
        )
        chosen = lane_rows[:remaining]
        selected.extend((fact_table, fact_row_id) for _, fact_table, fact_row_id in chosen)
        if len(lane_rows) > remaining:
            return selected, True
        if len(selected) == request.batch_size:
            return selected, lane_index < len(lanes) - 1
    return selected, False


def _ready_key_sql(
    fact_table: _FactTable,
    *,
    lane: str,
    limit: int,
) -> tuple[str, tuple[object, ...]]:
    derived_clause = (
        ""
        if fact_table == "financial_facts"
        else (
            "AND (fact.computed_from IS NOT NULL OR fact.formula_id IS NOT NULL "
            "OR fact.formula_version IS NOT NULL "
            "OR LOWER(COALESCE(fact.extracted_by, '')) LIKE '%derived%') "
        )
    )
    exact = (
        "prior.legacy_binding_revision_id = binding.binding_revision_id "
        "AND prior.matcher_config_sha256 = ? "
        "AND prior.fact_payload_fingerprint_sha256 = "
        f"{_payload_fingerprint_sql(fact_table)}"
    )
    if lane == "unseen":
        predicate = "prior.match_revision_id IS NULL"
        params: tuple[object, ...] = (limit,)
    elif lane == "stale":
        predicate = f"prior.match_revision_id IS NOT NULL AND NOT ({exact})"
        params = (MATCHER_CONFIG_SHA256, limit)
    elif lane == "retryable":
        predicate = f"prior.outcome = 'retryable' AND ({exact})"
        params = (MATCHER_CONFIG_SHA256, limit)
    else:
        raise CompanyFactsFactMatcherError(f"unsupported readiness lane: {lane}")
    order = "prior.revision, fact.id" if lane == "retryable" else "fact.id"
    sql = (
        "SELECT fact.id AS fact_row_id, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "COALESCE(prior.revision, 0) AS prior_revision "
        f"FROM {fact_table} AS fact "
        "JOIN documents AS legacy ON legacy.id = fact.source_doc_id "
        "JOIN v_legacy_document_evidence_bindings_current AS binding "
        "ON binding.legacy_document_id = fact.source_doc_id "
        "JOIN evidence_document_versions AS document "
        "ON document.document_version_id = binding.document_version_id "
        "LEFT JOIN v_legacy_fact_evidence_matches_current AS prior "
        f"ON prior.fact_table = '{fact_table}' "
        "AND prior.fact_row_id = fact.id "
        "WHERE legacy.source_type = 'sec_xbrl' "
        "AND document.document_type = 'companyfacts_snapshot' "
        f"{derived_clause}"
        f"AND ({predicate}) "
        f"ORDER BY {order} LIMIT ?"
    )
    return sql, params


def _target_sql(
    fact_table: _FactTable,
    fact_ids: tuple[int, ...] | None = None,
) -> str:
    derived_clause = (
        ""
        if fact_table == "financial_facts"
        else (
            "AND (fact.computed_from IS NOT NULL OR fact.formula_id IS NOT NULL "
            "OR fact.formula_version IS NOT NULL "
            "OR LOWER(COALESCE(fact.extracted_by, '')) LIKE '%derived%') "
        )
    )
    return (
        "SELECT "  # nosec B608 -- trusted internal SQL shape; values remain bound
        f"'{fact_table}' AS fact_table, fact.*, "
        "binding.binding_revision_id, binding.revision AS binding_revision, "
        "binding.scope_content_sha256, binding.evidence_node_id, "
        "binding.effective_at AS binding_effective_at, "
        "binding.knowledge_at AS binding_knowledge_at, "
        "binding.scope_locator_json, binding.document_version_id, "
        "document.issuer_id, document.blob_sha256, source.retrieved_at, "
        "location.storage_uri, "
        "(SELECT MIN(identifier.normalized_value) "
        "FROM v_issuer_identifiers_canonical AS identifier "
        "WHERE identifier.issuer_id = document.issuer_id "
        "AND identifier.identifier_type = 'sec_cik') AS normalized_cik, "
        "(SELECT COUNT(*) "
        "FROM v_issuer_identifiers_canonical AS identifier "
        "WHERE identifier.issuer_id = document.issuer_id "
        "AND identifier.identifier_type = 'sec_cik') AS canonical_cik_count, "
        "prior.match_revision_id AS prior_match_revision_id, "
        "COALESCE(prior.revision, 0) AS prior_revision, "
        "prior.outcome AS prior_outcome, "
        "prior.fact_payload_fingerprint_sha256 AS prior_fact_sha, "
        "prior.legacy_binding_revision_id AS prior_binding_id, "
        "prior.matcher_config_sha256 AS prior_matcher_config "
        f"FROM {fact_table} AS fact "
        "JOIN documents AS legacy ON legacy.id = fact.source_doc_id "
        "JOIN v_legacy_document_evidence_bindings_current AS binding "
        "ON binding.legacy_document_id = fact.source_doc_id "
        "JOIN v_evidence_document_versions_canonical AS document "
        "ON document.document_version_id = binding.document_version_id "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = document.observation_id "
        "JOIN v_evidence_blob_locations_current AS location "
        "ON location.blob_sha256 = document.blob_sha256 "
        "AND location.availability_state = 'present' "
        "AND location.verified_sha256 = document.blob_sha256 "
        "LEFT JOIN v_legacy_fact_evidence_matches_current AS prior "
        f"ON prior.fact_table = '{fact_table}' "
        "AND prior.fact_row_id = fact.id "
        "WHERE legacy.source_type = 'sec_xbrl' "
        "AND document.document_type = 'companyfacts_snapshot' "
        f"{derived_clause}"
        + ("AND fact.id IN (" + ",".join("?" for _ in fact_ids) + ") " if fact_ids else "")
        + "ORDER BY fact.id, location.storage_uri"
    )


def _target_from_row(row_raw: sqlite3.Row | tuple[object, ...]) -> _Target:
    if not isinstance(row_raw, sqlite3.Row):
        raise CompanyFactsFactMatcherError("matcher requires sqlite3.Row row_factory")
    row = dict(row_raw)
    fact_table = cast("_FactTable", str(row["fact_table"]))
    payload = _fact_payload(fact_table, row)
    prior_fact_sha = row.get("prior_fact_sha")
    exact_current = (
        prior_fact_sha == payload.canonical_sha256
        and row.get("prior_binding_id") == row["binding_revision_id"]
        and row.get("prior_matcher_config") == MATCHER_CONFIG_SHA256
    )
    prior_outcome_raw = row.get("prior_outcome")
    prior_outcome = None if prior_outcome_raw is None else cast("_Outcome", str(prior_outcome_raw))
    return _Target(
        fact_table=fact_table,
        fact_row_id=int(row["id"]),
        issuer_id=str(row["issuer_id"]),
        binding_revision_id=str(row["binding_revision_id"]),
        binding_revision=int(row["binding_revision"]),
        binding_scope_content_sha256=str(row["scope_content_sha256"]),
        evidence_node_id=str(row["evidence_node_id"]),
        binding_effective_at=_parse_time(row["binding_effective_at"]),
        binding_knowledge_at=_parse_time(row["binding_knowledge_at"]),
        scope_locator_json=str(row["scope_locator_json"]),
        document_version_id=str(row["document_version_id"]),
        blob_sha256=str(row["blob_sha256"]),
        storage_uri=str(row["storage_uri"]),
        retrieved_at=_parse_time(row["retrieved_at"]),
        normalized_cik=(
            None if row.get("normalized_cik") is None else str(row["normalized_cik"]).zfill(10)
        ),
        canonical_cik_count=int(row["canonical_cik_count"]),
        fact_payload=payload,
        prior_match_revision_id=(
            None
            if row.get("prior_match_revision_id") is None
            else str(row["prior_match_revision_id"])
        ),
        prior_revision=int(row["prior_revision"]),
        prior_outcome=prior_outcome,
        exact_current=exact_current,
    )


def _fact_payload(
    fact_table: _FactTable,
    row: Mapping[str, object],
) -> FinancialFactPayloadV1 | KpiFactPayloadV1:
    locator_raw = row.get("locator")
    locator = (
        None if locator_raw is None else OriginalFactLocator.model_validate_json(str(locator_raw))
    )
    common: dict[str, object] = {
        "fact_row_id": int(cast("int", row["id"])),
        "ticker": str(row["ticker"]),
        "period_end": str(row["period_end"]),
        "fiscal_period_type": str(row["fiscal_period_type"]),
        "value": str(row["value"]),
        "unit": str(row["unit"]),
        "source_doc_id": int(cast("int", row["source_doc_id"])),
        "extracted_by": (None if row.get("extracted_by") is None else str(row["extracted_by"])),
        "locator": locator,
    }
    if fact_table == "financial_facts":
        return FinancialFactPayloadV1.model_validate(
            {
                **common,
                "schema_version": "financial_fact_payload.v1",
                "fact_table": "financial_facts",
                "line_item": str(row["line_item"]),
                "currency": (None if row.get("currency") is None else str(row["currency"])),
            }
        )
    return KpiFactPayloadV1.model_validate(
        {
            **common,
            "schema_version": "kpi_fact_payload.v1",
            "fact_table": "kpi_facts",
            "kpi_definition_id": int(cast("int", row["kpi_definition_id"])),
            "source_excerpt": (
                None if row.get("source_excerpt") is None else str(row["source_excerpt"])
            ),
            "computed_from": (
                None if row.get("computed_from") is None else str(row["computed_from"])
            ),
            "formula_id": (
                None if row.get("formula_id") is None else int(cast("int", row["formula_id"]))
            ),
            "formula_version": (
                None
                if row.get("formula_version") is None
                else int(cast("int", row["formula_version"]))
            ),
        }
    )


def _match_target(
    target: _Target,
    blob_root: Path,
    *,
    blob_cache: _BlobCache | None = None,
) -> LegacyFactEvidenceMatchRevision:
    revision = target.prior_revision + 1
    identity = _stable_digest(
        target.fact_table,
        str(target.fact_row_id),
        target.fact_payload.canonical_sha256,
        target.binding_revision_id,
        MATCHER_CONFIG_SHA256,
        str(revision),
    )
    base: dict[str, object] = {
        "match_revision_id": f"companyfacts-match:{identity}",
        "idempotency_key": f"companyfacts-match:{identity}",
        "fact_table": target.fact_table,
        "fact_row_id": target.fact_row_id,
        "issuer_id": target.issuer_id,
        "revision": revision,
        "fact_payload": target.fact_payload,
        "original_locator": target.fact_payload.locator,
        "legacy_binding_revision_id": target.binding_revision_id,
        "legacy_binding_revision": target.binding_revision,
        "binding_scope_content_sha256": target.binding_scope_content_sha256,
        "evidence_node_id": target.evidence_node_id,
        "matcher_name": _MATCHER_NAME,
        "matcher_version": _MATCHER_VERSION,
        "matcher_config_sha256": MATCHER_CONFIG_SHA256,
        "effective_at": target.binding_effective_at,
        "knowledge_at": max(target.binding_knowledge_at, target.retrieved_at),
        "recorded_at": max(target.binding_knowledge_at, target.retrieved_at),
        "supersedes_match_revision_id": target.prior_match_revision_id,
    }
    try:
        accession = _binding_accession(target)
        if target.canonical_cik_count != 1 or target.normalized_cik is None:
            raise CompanyFactsFactMatcherError("issuer lacks one canonical SEC CIK")
        payload, scopes = _verified_payload_and_scopes(
            target,
            blob_root,
            expected_cik=target.normalized_cik,
            cache=blob_cache,
        )
        scope_bytes = scopes.get(accession)
        if scope_bytes is None:
            raise CompanyFactsFactMatcherError(
                "binding accession is absent from canonical CompanyFacts scope"
            )
        if hashlib.sha256(scope_bytes).hexdigest() != target.binding_scope_content_sha256:
            raise CompanyFactsFactMatcherError(
                "binding accession scope digest conflicts with immutable blob"
            )
    except (OSError, ValueError, CompanyFactsFactMatcherError) as exc:
        return _record(
            base,
            relocated_locator=None,
            matched_entry_sha256=None,
            candidate_manifest=_empty_manifest(),
            matched_candidate_count=0,
            issuer_check="not_evaluated",
            context_check="not_evaluated",
            unit_check="not_evaluated",
            sign_check="not_evaluated",
            fiscal_period_check="not_evaluated",
            value_check="not_evaluated",
            outcome="retryable",
            reason_code="companyfacts_blob_or_scope_unavailable",
            reason_details=CanonicalJSONObject(root={"error_type": type(exc).__name__}),
        )

    if isinstance(target.fact_payload, KpiFactPayloadV1):
        return _record(
            base,
            relocated_locator=None,
            matched_entry_sha256=None,
            candidate_manifest=_empty_manifest(),
            matched_candidate_count=0,
            issuer_check="pass",
            context_check="not_evaluated",
            unit_check="not_evaluated",
            sign_check="not_evaluated",
            fiscal_period_check="not_evaluated",
            value_check="not_evaluated",
            outcome="terminal",
            reason_code="derived_kpi_not_document_matchable",
            reason_details=CanonicalJSONObject(
                root={"lineage_required": "input_observation_edges"}
            ),
        )

    ladder = _ladder_for(target.fact_payload.line_item)
    evaluations, manifest = _evaluate_candidates(
        payload,
        accession=accession,
        fact=target.fact_payload,
        ladder=ladder,
    )
    matches = [evaluation for evaluation in evaluations if evaluation.matches]
    checks = _aggregate_checks(evaluations)
    if len(matches) == 1:
        selected = matches[0].candidate
        return _record(
            base,
            relocated_locator=selected.relocated_locator,
            matched_entry_sha256=selected.entry_sha256,
            candidate_manifest=manifest,
            matched_candidate_count=1,
            issuer_check="pass",
            **checks,
            outcome="accepted",
            reason_code="unique_companyfacts_entry",
            reason_details=CanonicalJSONObject(root={"original_locator_used_as": "hint_only"}),
        )
    if len(matches) > 1:
        return _record(
            base,
            relocated_locator=None,
            matched_entry_sha256=None,
            candidate_manifest=manifest,
            matched_candidate_count=len(matches),
            issuer_check="pass",
            context_check="pass",
            unit_check="pass",
            sign_check="pass",
            fiscal_period_check="pass",
            value_check="pass",
            outcome="terminal",
            reason_code="ambiguous_companyfacts_candidates",
            reason_details=CanonicalJSONObject(root={"matching_candidates": len(matches)}),
        )
    failed = [name for name, value in checks.items() if value == "fail"]
    return _record(
        base,
        relocated_locator=None,
        matched_entry_sha256=None,
        candidate_manifest=manifest,
        matched_candidate_count=0,
        issuer_check="pass",
        **checks,
        outcome="terminal",
        reason_code=(
            "no_companyfacts_candidates" if not evaluations else "no_exact_companyfacts_match"
        ),
        reason_details=CanonicalJSONObject(root={"failed_checks": cast("list[JsonValue]", failed)}),
    )


def _record(
    base: Mapping[str, object],
    **decision: object,
) -> LegacyFactEvidenceMatchRevision:
    return LegacyFactEvidenceMatchRevision.model_validate({**base, **decision})


def _binding_accession(target: _Target) -> str:
    locator_raw: object = json.loads(target.scope_locator_json)
    if not isinstance(locator_raw, dict):
        raise CompanyFactsFactMatcherError("binding scope locator is not an object")
    locator = cast("dict[str, object]", locator_raw)
    accession_raw = locator.get("accession_number")
    if not isinstance(accession_raw, str) or not accession_raw:
        raise CompanyFactsFactMatcherError("binding scope locator lacks an accession")
    return accession_raw


def _verified_blob(target: _Target, blob_root: Path) -> bytes:
    root = blob_root.resolve()
    storage_path = _file_uri_path(target.storage_uri)
    try:
        storage_path.relative_to(root)
    except ValueError as exc:
        raise CompanyFactsFactMatcherError(
            "current blob location is outside the explicit blob root"
        ) from exc
    expected = root / target.blob_sha256[:2] / f"{target.blob_sha256}.json"
    if storage_path.resolve() != expected.resolve():
        raise CompanyFactsFactMatcherError(
            "current blob location does not match the content-addressed path"
        )
    raw_body = storage_path.read_bytes()
    if hashlib.sha256(raw_body).hexdigest() != target.blob_sha256:
        raise CompanyFactsFactMatcherError("immutable CompanyFacts blob digest verification failed")
    return raw_body


def _verified_payload_and_scopes(
    target: _Target,
    blob_root: Path,
    *,
    expected_cik: str,
    cache: _BlobCache | None,
) -> tuple[CompanyFactsPayload, dict[str, bytes]]:
    key = (target.blob_sha256, expected_cik)
    if cache is not None and key in cache:
        return cache[key]
    raw_body = _verified_blob(target, blob_root)
    payload = parse_companyfacts_body(raw_body, expected_cik=expected_cik)
    result = (payload, _accession_scopes(payload))
    if cache is not None:
        cache[key] = result
    return result


def _select_blob_location(
    candidates: list[_Target],
    blob_root: Path,
) -> _Target:
    if not candidates:
        raise CompanyFactsFactMatcherError("fact target has no blob location")
    matching = [
        target for target in candidates if _is_content_addressed_location(target, blob_root)
    ]
    if len(matching) > 1:
        raise CompanyFactsFactMatcherError(
            "fact target has duplicate current content-addressed blob locations"
        )
    return matching[0] if matching else candidates[0]


def _is_content_addressed_location(target: _Target, blob_root: Path) -> bool:
    try:
        actual = _file_uri_path(target.storage_uri)
    except CompanyFactsFactMatcherError:
        return False
    expected = blob_root.resolve() / target.blob_sha256[:2] / f"{target.blob_sha256}.json"
    return actual == expected.resolve()


def _evaluate_candidates(
    payload: CompanyFactsPayload,
    *,
    accession: str,
    fact: FinancialFactPayloadV1,
    ladder: LineItemLadder,
) -> tuple[list[_CandidateEvaluation], CompanyFactsCandidateManifestV1]:
    payload_dict = cast(
        "dict[str, object]",
        payload.model_dump(mode="json", by_alias=True),
    )
    fye_month = _infer_fye_month(payload_dict)
    claimed = _claimed_rungs(payload, ladder=ladder, fye_month=fye_month)
    raw_candidates: list[_RawCandidate] = []
    for rung_index, (namespace, concept_name) in enumerate(ladder.rungs):
        concept = payload.facts.get(namespace, {}).get(concept_name)
        if concept is None:
            continue
        units_raw = cast(
            "dict[str, object]",
            {
                unit: [entry.model_dump(mode="json", exclude_none=False) for entry in entries]
                for unit, entries in concept.units.items()
            },
        )
        modal_currency = _modal_currency(units_raw, ladder.kind)
        for unit_code, entries in concept.units.items():
            for entry_index, typed_entry in enumerate(entries):
                if typed_entry.accn != accession:
                    continue
                entry = typed_entry.model_dump(mode="json", exclude_none=False)
                locator = CompanyFactsRelocatedLocator(
                    accession_number=accession,
                    namespace=namespace,
                    concept=concept_name,
                    unit=unit_code,
                    entry_index=entry_index,
                    json_path=(
                        f"facts.{namespace}.{concept_name}.units.{unit_code}[{entry_index}]"
                    ),
                )
                entry_bytes = json.dumps(
                    entry,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                candidate = CompanyFactsCandidateV1(
                    entry_sha256=hashlib.sha256(entry_bytes).hexdigest(),
                    relocated_locator=locator,
                )
                currency = _currency_of_unit_code(unit_code, ladder.kind)
                signed_value = Decimal(str(typed_entry.val)) * ladder.sign
                resolved = _resolve_fiscal_period_type(
                    fp=typed_entry.fp,
                    start_date=typed_entry.start,
                    end_date=typed_entry.end,
                    fye_month=fye_month,
                )
                fiscal_labels: set[str] = (
                    {FiscalPeriodType.FY.value, FiscalPeriodType.Q4.value}
                    if typed_entry.start is None and resolved is FiscalPeriodType.FY
                    else ({resolved.value} if resolved is not None else set())
                )
                raw_candidates.append(
                    _RawCandidate(
                        candidate=candidate,
                        rung_index=rung_index,
                        unit_code=unit_code,
                        entry=cast("dict[str, JsonValue]", entry),
                        end=typed_entry.end,
                        fiscal_labels=frozenset(fiscal_labels),
                        modal_currency=modal_currency,
                        parsed_currency=currency,
                        signed_value=signed_value,
                        pick_key=_same_doc_pick_key(entry, signed_value),
                    )
                )
    fact_value = _decimal(fact.value)
    eligible = [
        raw
        for raw in raw_candidates
        if fact.fiscal_period_type in raw.fiscal_labels
        and raw.parsed_currency == raw.modal_currency
        and raw.parsed_currency is not None
        and claimed.get((raw.end, fact.fiscal_period_type)) == raw.rung_index
    ]
    winning_pick = max(
        (raw.pick_key for raw in eligible if raw.end == fact.period_end[:10]),
        default=None,
    )
    evaluations = [
        _CandidateEvaluation(
            candidate=raw.candidate,
            context=(
                raw.end == fact.period_end[:10]
                and fact.fiscal_period_type in raw.fiscal_labels
                and claimed.get((raw.end, fact.fiscal_period_type)) == raw.rung_index
                and raw.pick_key == winning_pick
            ),
            unit=(
                raw.parsed_currency is not None
                and raw.parsed_currency == raw.modal_currency
                and (Unit.COUNT.value if ladder.kind == "shares" else Unit.ACTUAL.value)
                == fact.unit.lower()
                and (raw.parsed_currency or None) == fact.currency
            ),
            sign=_sign(raw.signed_value) == _sign(fact_value),
            fiscal_period=fact.fiscal_period_type in raw.fiscal_labels,
            value=raw.signed_value == fact_value,
        )
        for raw in raw_candidates
    ]
    candidates = tuple(
        sorted(
            (evaluation.candidate for evaluation in evaluations),
            key=lambda candidate: (
                candidate.entry_sha256,
                candidate.relocated_locator.canonical_json,
            ),
        )
    )
    manifest = CompanyFactsCandidateManifestV1(
        schema_version="companyfacts_candidate_manifest.v1",
        candidates=candidates,
    )
    return evaluations, manifest


def _claimed_rungs(
    payload: CompanyFactsPayload,
    *,
    ladder: LineItemLadder,
    fye_month: int,
) -> dict[tuple[str, str], int]:
    """Replay the extractor's global first-rung claim for every logical period."""

    claimed: dict[tuple[str, str], int] = {}
    for rung_index, (namespace, concept_name) in enumerate(ladder.rungs):
        concept = payload.facts.get(namespace, {}).get(concept_name)
        if concept is None:
            continue
        units_raw = cast(
            "dict[str, object]",
            {
                unit: [entry.model_dump(mode="json", exclude_none=False) for entry in entries]
                for unit, entries in concept.units.items()
            },
        )
        modal_currency = _modal_currency(units_raw, ladder.kind)
        if modal_currency is None:
            continue
        for unit_code, entries in concept.units.items():
            if _currency_of_unit_code(unit_code, ladder.kind) != modal_currency:
                continue
            for entry in entries:
                resolved = _resolve_fiscal_period_type(
                    fp=entry.fp,
                    start_date=entry.start,
                    end_date=entry.end,
                    fye_month=fye_month,
                )
                if resolved is None:
                    continue
                labels = (
                    (FiscalPeriodType.FY.value, FiscalPeriodType.Q4.value)
                    if entry.start is None and resolved is FiscalPeriodType.FY
                    else (resolved.value,)
                )
                for label in labels:
                    claimed.setdefault((entry.end, label), rung_index)
    return claimed


def _aggregate_checks(
    evaluations: list[_CandidateEvaluation],
) -> dict[
    Literal[
        "context_check",
        "unit_check",
        "sign_check",
        "fiscal_period_check",
        "value_check",
    ],
    Literal["pass", "fail"],
]:
    return {
        "context_check": (
            "pass" if any(candidate.context for candidate in evaluations) else "fail"
        ),
        "unit_check": ("pass" if any(candidate.unit for candidate in evaluations) else "fail"),
        "sign_check": ("pass" if any(candidate.sign for candidate in evaluations) else "fail"),
        "fiscal_period_check": (
            "pass" if any(candidate.fiscal_period for candidate in evaluations) else "fail"
        ),
        "value_check": ("pass" if any(candidate.value for candidate in evaluations) else "fail"),
    }


def _ladder_for(line_item: str) -> LineItemLadder:
    matches = [ladder for ladder in TAG_LADDERS if ladder.line_item == line_item]
    if len(matches) != 1:
        raise CompanyFactsFactMatcherError(
            f"line item has no unique CompanyFacts ladder: {line_item}"
        )
    return matches[0]


def _empty_manifest() -> CompanyFactsCandidateManifestV1:
    return CompanyFactsCandidateManifestV1(
        schema_version="companyfacts_candidate_manifest.v1",
        candidates=(),
    )


def _decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise CompanyFactsFactMatcherError("fact value is not an exact decimal") from exc
    if not result.is_finite():
        raise CompanyFactsFactMatcherError("fact value must be finite")
    return result


def _sign(value: Decimal) -> int:
    if value == 0:
        return 0
    return -1 if value < 0 else 1


def _file_uri_path(storage_uri: str) -> Path:
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file":
        raise CompanyFactsFactMatcherError("CompanyFacts blob location is not file://")
    path_text = unquote(parsed.path)
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    if os.name == "nt" and path_text.startswith("/") and len(path_text) > 2 and path_text[2] == ":":
        path_text = path_text[1:]
    return Path(path_text).resolve()


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return _timeline(value)
    return _timeline(datetime.fromisoformat(str(value)))


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stable_digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _require_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    views = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()
    }
    missing = sorted((_REQUIRED_TABLES - tables) | (_REQUIRED_VIEWS - views))
    if missing:
        raise CompanyFactsFactMatcherError(
            "CompanyFacts fact matcher schema is incomplete: " + ", ".join(missing)
        )
    if conn.row_factory is not sqlite3.Row:
        raise CompanyFactsFactMatcherError(
            "CompanyFacts fact matcher requires sqlite3.Row row_factory"
        )


def _summary(
    request: CompanyFactsFactMatcherRequest,
    *,
    run_at: datetime,
    candidates_total: int,
    already_current: int | None,
    has_more: bool,
    items: tuple[CompanyFactsFactMatchItem, ...],
) -> CompanyFactsFactMatchSummary:
    return CompanyFactsFactMatchSummary(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        run_at=run_at,
        matcher_name=_MATCHER_NAME,
        matcher_version=_MATCHER_VERSION,
        matcher_config_sha256=MATCHER_CONFIG_SHA256,
        batch_size=request.batch_size,
        candidates_total=candidates_total,
        already_current=already_current,
        considered=len(items),
        accepted=sum(item.outcome == "accepted" for item in items),
        retryable=sum(item.outcome == "retryable" for item in items),
        terminal=sum(item.outcome == "terminal" for item in items),
        revisions_created=sum(item.revision_created for item in items),
        revisions_replayed=(
            sum(not item.revision_created for item in items) if request.apply else 0
        ),
        has_more=has_more,
        items=items,
    )


def _write_checkpoint(
    path: Path,
    checkpoint: CompanyFactsFactMatchCheckpoint,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(checkpoint.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        os.replace(temporary, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
