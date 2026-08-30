"""Build a bounded, source-first review queue for owner-visible KPI facts.

The planner is deliberately read-only.  It resolves legacy synthetic documents
to their attributable parents and surfaces exact evidence-node candidates, but
never infers semantic admission from a metric name or series shape.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.documents import SourceType
from pipeline.kpi_semantic_scope import portfolio_tickers, scoped_kpi_definitions
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.fulltext_extractor_identity import resolve_fulltext_extractor_identity

MAX_KPI_SEMANTIC_REVIEW_ITEMS = 250
MAX_EVIDENCE_CANDIDATES_PER_FACT = 8
MAX_EVIDENCE_NODES_PER_DOCUMENT = 2_048
MAX_EVIDENCE_TEXT_CHARS_PER_NODE = 250_000
MAX_EVIDENCE_TEXT_CHARS_PER_DOCUMENT = 4_000_000
MAX_EVIDENCE_MATCHES_SCANNED_PER_FACT = 4_096
OPERATIONS_GOVERNANCE_DISPOSITION = "no_surface_change_internal_read_only_kpi_review_preparation"
OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT = (
    "src/operations/registry.py:OperationsRegistry",
    "src/pipeline/operations_panel.py:visible_surface_dispositions",
)

_SUBSTANTIVE_NODE_KINDS = frozenset(
    {"section", "passage", "table", "table_row", "table_cell", "pdf_page"}
)
_REVIEWABLE_SOURCE_TYPES = frozenset(
    {
        SourceType.SEC_XBRL.value,
        SourceType.SEC_S1.value,
        SourceType.IR_DOC.value,
        SourceType.MANUAL_CSV.value,
        SourceType.MANUAL_ENTRY.value,
    }
)
_MULTI_PERIOD_DOC_TYPES = frozenset({"ir_historical_spreadsheet"})


class KpiSemanticReviewState(StrEnum):
    NEEDS_LEDGER_CAPTURE = "needs_ledger_capture"
    NEEDS_FULLTEXT_CAPTURE = "needs_fulltext_capture"
    NEEDS_CURRENT_BINDING = "needs_current_binding"
    SOURCE_DOCUMENT_MISSING = "source_document_missing"
    SOURCE_IDENTITY_MISSING = "source_identity_missing"
    SOURCE_ISSUER_MISMATCH = "source_issuer_mismatch"
    SOURCE_NOT_REVIEWABLE = "source_not_reviewable"
    EVIDENCE_BINDING_INVALID = "evidence_binding_invalid"
    EVIDENCE_SEARCH_INCOMPLETE = "evidence_search_incomplete"
    EVIDENCE_NO_NUMERIC_MATCH = "evidence_no_numeric_match"
    SOURCE_REVIEW_REQUIRED = "source_review_required"


class KpiEvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_node_id: str = Field(min_length=1, max_length=128)
    document_version_id: str = Field(min_length=1, max_length=128)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    node_kind: str = Field(min_length=1, max_length=64)
    locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_value_text: str = Field(min_length=1, max_length=80)
    excerpt: str = Field(min_length=1, max_length=640)
    match_start: int = Field(ge=0)
    match_end: int = Field(gt=0)
    excerpt_start: int = Field(ge=0)
    excerpt_end: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate_verbatim_offsets(self) -> Self:
        if not self.excerpt_start <= self.match_start < self.match_end <= self.excerpt_end:
            raise ValueError("evidence candidate offsets are inconsistent")
        relative_start = self.match_start - self.excerpt_start
        relative_end = self.match_end - self.excerpt_start
        if self.excerpt[relative_start:relative_end] != self.source_value_text:
            raise ValueError("source_value_text does not match the verbatim excerpt offsets")
        return self


class KpiSemanticReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    kpi_definition_id: int = Field(gt=0)
    kpi_name: str
    scope_reasons: tuple[str, ...]
    fact_id: int = Field(gt=0)
    period_end: str
    fiscal_period_type: str
    value: str
    unit: str
    context_status: str | None
    legacy_source_doc_id: int | None = Field(default=None, gt=0)
    source_doc_id: int | None = Field(default=None, gt=0)
    source_type: str | None
    doc_type: str | None
    source_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_observation_version: str | None
    source_period_end: str | None
    state: KpiSemanticReviewState
    state_reason_code: str = Field(min_length=1, max_length=128)
    evidence_candidate_total: int = Field(ge=0)
    evidence_candidates_truncated: bool
    evidence_search_incomplete: bool
    evidence_search_reason_codes: tuple[str, ...]
    evidence_candidates: tuple[KpiEvidenceCandidate, ...]

    @model_validator(mode="after")
    def _validate_candidate_summary(self) -> Self:
        if self.evidence_candidate_total < len(self.evidence_candidates):
            raise ValueError("evidence candidate total is smaller than the emitted candidates")
        if self.evidence_candidates_truncated != (
            self.evidence_candidate_total > len(self.evidence_candidates)
        ):
            raise ValueError("evidence candidate truncation flag is inconsistent")
        if (self.state is KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED) != bool(
            self.evidence_candidates
        ):
            raise ValueError("review-ready state and evidence candidates are inconsistent")
        if self.evidence_search_incomplete != bool(self.evidence_search_reason_codes):
            raise ValueError("evidence search completeness fields are inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class _EvidenceNodeSnapshot:
    node_id: str
    document_version_id: str
    extraction_run_id: str
    node_kind: str
    locator_sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class _EvidenceDocumentSnapshot:
    nodes: tuple[_EvidenceNodeSnapshot, ...]
    search_incomplete: bool
    search_reason_codes: tuple[str, ...]


class KpiSemanticReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi_semantic_review.v1"] = "kpi_semantic_review.v1"
    user_id: str
    ticker: str | None
    observed_at: datetime
    limit: int = Field(gt=0, le=MAX_KPI_SEMANTIC_REVIEW_ITEMS)
    total_items: int = Field(ge=0)
    truncated: bool
    state_counts: dict[str, int]
    items: tuple[KpiSemanticReviewItem, ...]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if self.total_items != len(self.items):
            raise ValueError("total_items does not match the review items")
        expected_counts: dict[str, int] = {}
        for item in self.items:
            expected_counts[item.state.value] = expected_counts.get(item.state.value, 0) + 1
            if self.ticker is not None and item.ticker != self.ticker:
                raise ValueError("review item ticker does not match the batch ticker")
        if self.state_counts != expected_counts:
            raise ValueError("state_counts do not match the review items")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != _payload_sha256(payload):
            raise ValueError("content_sha256 does not match the review payload")
        return self


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    if not _table_exists(conn, table):
        return frozenset()
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _decimal_tokens(value: str, unit: str) -> tuple[str, ...]:
    try:
        number = Decimal(value)
    except InvalidOperation:
        return (value.strip(),) if value.strip() else ()
    candidates = {_render_decimal(number)}
    if unit == "count":
        for divisor in (Decimal(1_000), Decimal(1_000_000), Decimal(1_000_000_000)):
            candidates.add(_render_decimal(number / divisor))
    expanded = set(candidates)
    for candidate in candidates:
        try:
            parsed = Decimal(candidate)
        except InvalidOperation:
            continue
        if parsed == parsed.to_integral_value():
            expanded.add(f"{int(parsed):,}")
    return tuple(sorted((item for item in expanded if item), key=lambda item: (-len(item), item)))


def _render_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _token_matches(text: str, token: str) -> Iterator[re.Match[str]]:
    prefix = r"(?<![\d.,])" if token.startswith(("+", "-")) else r"(?<![\d.,+\-])"
    pattern = rf"{prefix}{re.escape(token)}(?!\d|[.,]\d)"
    return re.finditer(pattern, text)


def _excerpt(text: str, match: re.Match[str]) -> tuple[str, int, int]:
    start = max(0, match.start() - 260)
    end = min(len(text), match.end() + 260)
    return text[start:end], start, end


def _current_context_join(conn: sqlite3.Connection) -> tuple[str, str]:
    columns = _columns(conn, "kpi_fact_semantic_contexts")
    if not columns:
        return "", "NULL"
    join = "LEFT JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id=fact.id"
    if {"revision", "supersedes_context_id"}.issubset(columns):
        join += (
            " AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts context_successor "
            "WHERE context_successor.supersedes_context_id=context.id)"
        )
    return join, "context.status"


def _source_document(
    conn: sqlite3.Connection, source_doc_id: int | None
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    if source_doc_id is None or not _table_exists(conn, "documents"):
        return None, None
    legacy = conn.execute("SELECT * FROM documents WHERE id=?", (source_doc_id,)).fetchone()
    if legacy is None:
        return None, None
    parent_id = legacy["parent_document_id"]
    if parent_id is None:
        return legacy, legacy
    parent = conn.execute("SELECT * FROM documents WHERE id=?", (parent_id,)).fetchone()
    return legacy, parent


def _review_result(
    state: KpiSemanticReviewState,
    reason_code: str,
    *,
    candidates: tuple[KpiEvidenceCandidate, ...] = (),
    candidate_total: int = 0,
    search_reason_codes: tuple[str, ...] = (),
) -> tuple[
    KpiSemanticReviewState,
    tuple[KpiEvidenceCandidate, ...],
    int,
    bool,
    str,
    bool,
    tuple[str, ...],
]:
    return (
        state,
        candidates,
        candidate_total,
        candidate_total > len(candidates),
        reason_code,
        bool(search_reason_codes),
        search_reason_codes,
    )


def _evidence_state_and_candidates(
    conn: sqlite3.Connection,
    *,
    legacy: sqlite3.Row | None,
    source: sqlite3.Row | None,
    ticker: str,
    definition_ticker: str,
    owner_tickers: frozenset[str],
    value: str,
    unit: str,
    snapshot_cache: dict[int, _EvidenceDocumentSnapshot],
) -> tuple[
    KpiSemanticReviewState,
    tuple[KpiEvidenceCandidate, ...],
    int,
    bool,
    str,
    bool,
    tuple[str, ...],
]:
    if (
        ticker.upper() != definition_ticker.upper()
        or ticker.upper() not in owner_tickers
        or (legacy is not None and str(legacy["ticker"] or "").upper() != ticker.upper())
    ):
        return _review_result(
            KpiSemanticReviewState.SOURCE_ISSUER_MISMATCH,
            "definition_fact_or_legacy_source_issuer_mismatch",
        )
    if source is None:
        return _review_result(KpiSemanticReviewState.SOURCE_DOCUMENT_MISSING, "source_missing")
    source_ticker = _optional_text(source["ticker"])
    if source_ticker is None or source_ticker.upper() != ticker.upper():
        return _review_result(
            KpiSemanticReviewState.SOURCE_ISSUER_MISMATCH,
            "source_document_issuer_mismatch",
        )
    source_sha = _optional_text(source["sha256"])
    source_observation_version = _optional_text(source["fetched_at"])
    source_period_end = _optional_text(source["period_end"])
    source_ref = _optional_text(source["file_path"])
    source_doc_type = _optional_text(source["doc_type"])
    if (
        source_sha is None
        or re.fullmatch(r"[0-9a-f]{64}", source_sha) is None
        or not source_observation_version
        or (source_doc_type not in _MULTI_PERIOD_DOC_TYPES and not source_period_end)
        or not source_ref
    ):
        return _review_result(
            KpiSemanticReviewState.SOURCE_IDENTITY_MISSING,
            "source_document_identity_incomplete",
        )
    source_type = str(source["source_type"])
    if source_type not in _REVIEWABLE_SOURCE_TYPES:
        return _review_result(
            KpiSemanticReviewState.SOURCE_NOT_REVIEWABLE,
            "source_type_not_reviewable",
        )
    source_doc_id = int(source["id"])
    if not _table_exists(conn, "evidence_document_versions"):
        return _review_result(
            KpiSemanticReviewState.NEEDS_LEDGER_CAPTURE,
            "evidence_document_version_missing",
        )
    version = conn.execute(
        "SELECT document_version_id FROM evidence_document_versions "
        "WHERE legacy_document_id=? ORDER BY version_sequence DESC LIMIT 1",
        (source_doc_id,),
    ).fetchone()
    if version is None:
        return _review_result(
            KpiSemanticReviewState.NEEDS_LEDGER_CAPTURE,
            "evidence_document_version_missing",
        )
    if not _table_exists(conn, "v_legacy_document_evidence_bindings_current"):
        return _review_result(
            KpiSemanticReviewState.NEEDS_CURRENT_BINDING,
            "current_document_binding_view_missing",
        )
    binding = conn.execute(
        "SELECT document_version_id,evidence_node_id,scope_content_sha256 "
        "FROM v_legacy_document_evidence_bindings_current "
        "WHERE legacy_document_id=?",
        (source_doc_id,),
    ).fetchone()
    if binding is None:
        return _review_result(
            KpiSemanticReviewState.NEEDS_CURRENT_BINDING,
            "current_document_binding_missing",
        )
    if not (
        _table_exists(conn, "evidence_extraction_runs") and _table_exists(conn, "evidence_nodes")
    ):
        return _review_result(
            KpiSemanticReviewState.NEEDS_FULLTEXT_CAPTURE,
            "fulltext_evidence_schema_missing",
        )
    bound = conn.execute(
        "SELECT node.node_kind,run.document_version_id FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
        "WHERE node.node_id=?",
        (str(binding["evidence_node_id"]),),
    ).fetchone()
    if (
        bound is None
        or str(bound["node_kind"]) != "document"
        or str(binding["document_version_id"]) != str(version["document_version_id"])
        or str(bound["document_version_id"]) != str(binding["document_version_id"])
        or str(binding["scope_content_sha256"]) != source_sha
    ):
        return _review_result(
            KpiSemanticReviewState.EVIDENCE_BINDING_INVALID,
            "current_document_binding_invalid",
        )
    bound_version = conn.execute(
        "SELECT document_version_id,blob_sha256,ticker FROM evidence_document_versions "
        "WHERE document_version_id=? AND legacy_document_id=?",
        (str(binding["document_version_id"]), source_doc_id),
    ).fetchone()
    if (
        bound_version is None
        or str(bound_version["blob_sha256"]) != source_sha
        or str(bound_version["ticker"] or "").upper() != ticker.upper()
    ):
        return _review_result(
            KpiSemanticReviewState.EVIDENCE_BINDING_INVALID,
            "evidence_document_version_identity_mismatch",
        )
    extractor = resolve_fulltext_extractor_identity(source_ref, None)
    runs = conn.execute(
        "SELECT extraction_run_id,input_sha256,extractor_name,extractor_config_sha256,"
        "extractor_code_version,outcome FROM evidence_extraction_runs "
        "WHERE document_version_id=? ORDER BY extraction_run_id",
        (str(binding["document_version_id"]),),
    ).fetchall()
    if not runs:
        return _review_result(
            KpiSemanticReviewState.NEEDS_FULLTEXT_CAPTURE,
            "fulltext_extraction_missing",
        )
    valid_run_ids = tuple(
        str(row["extraction_run_id"])
        for row in runs
        if str(row["outcome"]) == "succeeded"
        and str(row["input_sha256"]) == source_sha
        and str(row["extractor_name"]) == extractor.name
        and str(row["extractor_config_sha256"]) == extractor.config_sha256
        and str(row["extractor_code_version"]) == extractor.code_version
    )
    if not valid_run_ids:
        return _review_result(
            KpiSemanticReviewState.EVIDENCE_BINDING_INVALID,
            "promoted_fulltext_extraction_missing",
        )
    snapshot = snapshot_cache.get(source_doc_id)
    if snapshot is None:
        run_marks = ",".join("?" for _ in valid_run_ids)
        kind_marks = ",".join("?" for _ in _SUBSTANTIVE_NODE_KINDS)
        rows = conn.execute(
            "SELECT node.node_id,node.node_kind,node.locator_sha256,"  # nosec B608 -- SQL shape and placeholder counts come only from closed internal sets; all values remain bound
            "substr(node.text,1,?) AS bounded_text,length(node.text) AS text_length,"
            "node.extraction_run_id,run.document_version_id FROM evidence_nodes node "
            "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
            f"WHERE node.extraction_run_id IN ({run_marks}) "
            f"AND node.node_kind IN ({kind_marks}) "
            "AND NOT EXISTS (SELECT 1 FROM evidence_nodes newer "
            "WHERE newer.supersedes_node_id=node.node_id) ORDER BY node.node_id LIMIT ?",
            (
                MAX_EVIDENCE_TEXT_CHARS_PER_NODE + 1,
                *valid_run_ids,
                *sorted(_SUBSTANTIVE_NODE_KINDS),
                MAX_EVIDENCE_NODES_PER_DOCUMENT + 1,
            ),
        ).fetchall()
        search_reasons: set[str] = set()
        if len(rows) > MAX_EVIDENCE_NODES_PER_DOCUMENT:
            search_reasons.add("evidence_node_search_budget_exceeded")
        remaining_chars = MAX_EVIDENCE_TEXT_CHARS_PER_DOCUMENT
        nodes: list[_EvidenceNodeSnapshot] = []
        for row in rows[:MAX_EVIDENCE_NODES_PER_DOCUMENT]:
            bounded_text = str(row["bounded_text"] or "")
            allowed_chars = min(
                len(bounded_text),
                MAX_EVIDENCE_TEXT_CHARS_PER_NODE,
                max(remaining_chars, 0),
            )
            if int(row["text_length"] or 0) > allowed_chars:
                search_reasons.add("evidence_text_search_budget_exceeded")
            if allowed_chars <= 0:
                search_reasons.add("evidence_text_search_budget_exceeded")
                break
            text = bounded_text[:allowed_chars]
            remaining_chars -= len(text)
            nodes.append(
                _EvidenceNodeSnapshot(
                    node_id=str(row["node_id"]),
                    document_version_id=str(row["document_version_id"]),
                    extraction_run_id=str(row["extraction_run_id"]),
                    node_kind=str(row["node_kind"]),
                    locator_sha256=str(row["locator_sha256"] or ""),
                    text=text,
                )
            )
        snapshot = _EvidenceDocumentSnapshot(
            nodes=tuple(nodes),
            search_incomplete=bool(search_reasons),
            search_reason_codes=tuple(sorted(search_reasons)),
        )
        snapshot_cache[source_doc_id] = snapshot
    if not snapshot.nodes:
        if snapshot.search_incomplete:
            return _review_result(
                KpiSemanticReviewState.EVIDENCE_SEARCH_INCOMPLETE,
                "evidence_search_budget_exceeded",
                search_reason_codes=snapshot.search_reason_codes,
            )
        return _review_result(
            KpiSemanticReviewState.NEEDS_FULLTEXT_CAPTURE,
            "substantive_fulltext_evidence_missing",
        )
    candidates: list[KpiEvidenceCandidate] = []
    candidate_total = 0
    seen: set[tuple[str, int, int]] = set()
    match_budget_exceeded = False
    for node in snapshot.nodes:
        text = node.text
        locator_sha256 = node.locator_sha256
        if not re.fullmatch(r"[0-9a-f]{64}", locator_sha256):
            continue
        for token in _decimal_tokens(value, unit):
            for match in _token_matches(text, token):
                key = (node.node_id, match.start(), match.end())
                if key in seen:
                    continue
                seen.add(key)
                if candidate_total == MAX_EVIDENCE_MATCHES_SCANNED_PER_FACT:
                    candidate_total += 1
                    match_budget_exceeded = True
                    break
                candidate_total += 1
                if len(candidates) >= MAX_EVIDENCE_CANDIDATES_PER_FACT:
                    continue
                excerpt, excerpt_start, excerpt_end = _excerpt(text, match)
                candidates.append(
                    KpiEvidenceCandidate(
                        evidence_node_id=node.node_id,
                        document_version_id=node.document_version_id,
                        extraction_run_id=node.extraction_run_id,
                        node_kind=node.node_kind,
                        locator_sha256=locator_sha256,
                        source_value_text=match.group(0),
                        excerpt=excerpt,
                        match_start=match.start(),
                        match_end=match.end(),
                        excerpt_start=excerpt_start,
                        excerpt_end=excerpt_end,
                    )
                )
            if match_budget_exceeded:
                break
        if match_budget_exceeded:
            break
    search_reason_codes = set(snapshot.search_reason_codes)
    if match_budget_exceeded:
        search_reason_codes.add("evidence_match_search_budget_exceeded")
    if not candidates:
        if search_reason_codes:
            return _review_result(
                KpiSemanticReviewState.EVIDENCE_SEARCH_INCOMPLETE,
                "exact_numeric_evidence_search_incomplete",
                search_reason_codes=tuple(sorted(search_reason_codes)),
            )
        return _review_result(
            KpiSemanticReviewState.EVIDENCE_NO_NUMERIC_MATCH,
            "exact_numeric_evidence_missing",
        )
    return _review_result(
        KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED,
        "exact_numeric_evidence_candidates",
        candidates=tuple(candidates),
        candidate_total=candidate_total,
        search_reason_codes=tuple(sorted(search_reason_codes)),
    )


def build_kpi_semantic_review_batch(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str,
    ticker: str | None = None,
    limit: int = 250,
    observed_at: datetime | None = None,
) -> KpiSemanticReviewBatch:
    """Return one bounded queue of active, owner-visible facts needing review."""
    if not 0 < limit <= MAX_KPI_SEMANTIC_REVIEW_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_KPI_SEMANTIC_REVIEW_ITEMS}")
    conn.row_factory = sqlite3.Row
    fact_relation = canonical_fact_relation(conn, "kpi_facts")
    if fact_relation.selection_mode != "resolved_view":
        raise ValueError("KPI semantic review requires the resolved current-fact view")
    owner_tickers = portfolio_tickers(conn, user_id=user_id)
    if not owner_tickers:
        raise ValueError("owner portfolio scope is absent")
    owner_ticker_set = frozenset(owner_tickers)
    normalized_ticker = None if ticker is None else ticker.upper()
    if normalized_ticker is not None and normalized_ticker not in owner_tickers:
        raise ValueError("requested ticker is outside the owner portfolio scope")
    scoped = scoped_kpi_definitions(conn, repo_root=repo_root, user_id=user_id)
    reasons_by_definition = {
        int(row.kpi_definition_id): row.reasons
        for row in scoped
        if row.kpi_definition_id is not None
        and (normalized_ticker is None or row.ticker == normalized_ticker)
    }
    truncated = False
    if not reasons_by_definition:
        items: tuple[KpiSemanticReviewItem, ...] = ()
    else:
        context_join, context_status = _current_context_join(conn)
        marks = ",".join("?" for _ in reasons_by_definition)
        rows = conn.execute(
            "SELECT fact.id,fact.ticker,fact.period_end,fact.fiscal_period_type,"  # nosec B608 -- relation/context fragments are resolver-owned; ids and limits remain bound
            "fact.kpi_definition_id,fact.value,fact.unit,fact.source_doc_id,"
            f"definition.name,definition.ticker AS definition_ticker,"
            f"{context_status} AS context_status FROM {fact_relation.sql} fact "
            "JOIN kpi_definitions definition ON definition.id=fact.kpi_definition_id "
            f"{context_join} WHERE fact.kpi_definition_id IN ({marks}) "
            f"AND ({context_status} IS NULL OR {context_status}<>'admitted') "
            "ORDER BY UPPER(fact.ticker),definition.name,fact.period_end,fact.id LIMIT ?",
            (*sorted(reasons_by_definition), limit + 1),
        ).fetchall()
        truncated = len(rows) > limit
        built: list[KpiSemanticReviewItem] = []
        snapshot_cache: dict[int, _EvidenceDocumentSnapshot] = {}
        for row in rows[: limit + 1]:
            if len(built) >= limit:
                break
            legacy, source = _source_document(
                conn, None if row["source_doc_id"] is None else int(row["source_doc_id"])
            )
            (
                state,
                candidates,
                candidate_total,
                candidates_truncated,
                state_reason,
                search_incomplete,
                search_reason_codes,
            ) = _evidence_state_and_candidates(
                conn,
                legacy=legacy,
                source=source,
                ticker=str(row["ticker"]),
                definition_ticker=str(row["definition_ticker"]),
                owner_tickers=owner_ticker_set,
                value=str(row["value"]),
                unit=str(row["unit"]),
                snapshot_cache=snapshot_cache,
            )
            built.append(
                KpiSemanticReviewItem(
                    ticker=str(row["ticker"]).upper(),
                    kpi_definition_id=int(row["kpi_definition_id"]),
                    kpi_name=str(row["name"]),
                    scope_reasons=reasons_by_definition[int(row["kpi_definition_id"])],
                    fact_id=int(row["id"]),
                    period_end=str(row["period_end"]),
                    fiscal_period_type=str(row["fiscal_period_type"]),
                    value=str(row["value"]),
                    unit=str(row["unit"]),
                    context_status=(
                        None if row["context_status"] is None else str(row["context_status"])
                    ),
                    legacy_source_doc_id=(None if legacy is None else int(legacy["id"])),
                    source_doc_id=(None if source is None else int(source["id"])),
                    source_type=(None if source is None else _optional_text(source["source_type"])),
                    doc_type=(None if source is None else _optional_text(source["doc_type"])),
                    source_content_sha256=(
                        None if source is None else _optional_text(source["sha256"])
                    ),
                    source_observation_version=(
                        None if source is None else _optional_text(source["fetched_at"])
                    ),
                    source_period_end=(
                        None if source is None else _optional_text(source["period_end"])
                    ),
                    state=state,
                    state_reason_code=state_reason,
                    evidence_candidate_total=candidate_total,
                    evidence_candidates_truncated=candidates_truncated,
                    evidence_search_incomplete=search_incomplete,
                    evidence_search_reason_codes=search_reason_codes,
                    evidence_candidates=candidates,
                )
            )
        items = tuple(built)
    state_counts: dict[str, int] = {}
    for item in items:
        state_counts[item.state.value] = state_counts.get(item.state.value, 0) + 1
    at = observed_at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    at = at.astimezone(UTC)
    payload = {
        "schema_version": "kpi_semantic_review.v1",
        "user_id": user_id,
        "ticker": normalized_ticker,
        "observed_at": at.isoformat().replace("+00:00", "Z"),
        "limit": limit,
        "total_items": len(items),
        "truncated": truncated,
        "state_counts": state_counts,
        "items": [item.model_dump(mode="json") for item in items],
    }
    digest = _payload_sha256(payload)
    return KpiSemanticReviewBatch.model_validate({**payload, "content_sha256": digest})
