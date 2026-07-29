"""Typed append-only persistence for sealed Ask answers and claim provenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)

    def _default(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", exclude_none=True)
        raise TypeError(f"{type(item).__name__} is not JSON serializable")

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def retrieval_query_sha256(query_text: str) -> str:
    normalized = " ".join(query_text.split())
    if not normalized:
        raise ValueError("retrieval query must not be blank")
    return digest_text(
        canonical_json(
            {
                "query_text": normalized,
                "query_version": "heterogeneous_query.v1",
            }
        )
    )


def _sha(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("must be a lowercase SHA-256 digest")
    return normalized


def _time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _time(value).replace(tzinfo=None).isoformat(sep=" ")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswerContextTurn(_Frozen):
    """Immutable identity of one authoritative ``ask_turns`` row."""

    turn_id: int = Field(gt=0)
    session_id: str = Field(min_length=1)
    role: str = Field(pattern=r"^(user|assistant)$")
    text_sha256: str
    created_at: str = Field(min_length=1)

    _text_hash = field_validator("text_sha256")(_sha)


class RetrievalAssemblyItem(_Frozen):
    """Exact ordered prompt input reconstructed from one sealed trace result."""

    citation_number: int = Field(gt=0)
    trace_id: str = Field(min_length=1, max_length=128)
    result_ordinal: int = Field(ge=0)
    candidate_kind: str = Field(pattern=r"^(narrative|fact)$")
    candidate_id: str = Field(min_length=1, max_length=128)
    source_commitment_sha256: str
    prompt_text_sha256: str

    _hashes = field_validator(
        "source_commitment_sha256",
        "prompt_text_sha256",
    )(_sha)


class CitationAuditPayload(_Frozen):
    """Citation identity that can be re-derived solely from its sealed trace."""

    n: int = Field(gt=0)
    trace_id: str = Field(min_length=1, max_length=128)
    result_ordinal: int = Field(ge=0)
    candidate_kind: str = Field(pattern=r"^(narrative|fact)$")
    candidate_id: str = Field(min_length=1, max_length=128)
    source_commitment_sha256: str

    _source_hash = field_validator("source_commitment_sha256")(_sha)


class AnswerAuditRecord(_Frozen):
    answer_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = None
    surface: str = Field(pattern=r"^(portfolio|ticker|report|api)$")
    query_sha256: str
    prompt_sha256: str
    prompt_template_id: str = Field(min_length=1, max_length=128)
    prompt_template_version: str = Field(min_length=1, max_length=64)
    context_turns: tuple[AnswerContextTurn, ...] = ()
    retrieval_assembly: tuple[RetrievalAssemblyItem, ...]
    answer_text: str = Field(min_length=1)
    llm_purpose: str = Field(min_length=1, max_length=128)
    llm_model: str = Field(min_length=1, max_length=128)
    llm_provider: str = Field(min_length=1, max_length=64)
    llm_transport: str = Field(min_length=1, max_length=64)
    llm_call_id: int = Field(gt=0)
    claim_auditor_version: str = Field(min_length=1, max_length=64)
    claim_audit_purpose: str = Field(min_length=1, max_length=128)
    claim_audit_template_id: str = Field(min_length=1, max_length=128)
    claim_audit_template_version: str = Field(min_length=1, max_length=64)
    claim_auditor_model: str = Field(min_length=1, max_length=128)
    claim_audit_provider: str = Field(min_length=1, max_length=64)
    claim_audit_transport: str = Field(min_length=1, max_length=64)
    claim_audit_prompt_sha256: str
    claim_audit_response_sha256: str
    claim_audit_llm_call_id: int = Field(gt=0)
    no_claim_exemption: Literal["deterministic_non_substantive.v1"] | None = None
    recorded_at: datetime

    _hashes = field_validator(
        "query_sha256",
        "prompt_sha256",
        "claim_audit_prompt_sha256",
        "claim_audit_response_sha256",
    )(_sha)

    @model_validator(mode="after")
    def _session_contract(self) -> Self:
        if self.surface == "portfolio" and self.session_id is None:
            raise ValueError("portfolio Ask answers require an authoritative session")
        if self.session_id is not None:
            if not self.context_turns:
                raise ValueError("session Ask answers require authoritative context turns")
            if any(item.session_id != self.session_id for item in self.context_turns):
                raise ValueError("context turns must belong to the answer session")
            ids = tuple(item.turn_id for item in self.context_turns)
            if ids != tuple(sorted(set(ids))):
                raise ValueError("context turns must be unique and ordered")
            if self.context_turns[-1].role != "user":
                raise ValueError("the final authoritative context turn must be the user request")
        elif self.context_turns:
            raise ValueError("sessionless answers cannot assert Ask context turns")
        return self


class AnswerRetrieval(_Frozen):
    trace_ordinal: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)
    query_sha256: str
    promotion_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    trace_sha256: str
    research_snapshot_sha256: str
    recorded_at: datetime

    _hashes = field_validator(
        "query_sha256", "trace_sha256", "research_snapshot_sha256"
    )(_sha)


class AnswerCitation(_Frozen):
    citation_number: int = Field(gt=0)
    trace_id: str = Field(min_length=1, max_length=128)
    result_ordinal: int = Field(ge=0)
    candidate_kind: str = Field(pattern=r"^(narrative|fact)$")
    candidate_id: str = Field(min_length=1, max_length=128)
    source_commitment_sha256: str
    citation: CitationAuditPayload
    recorded_at: datetime

    _source_hash = field_validator("source_commitment_sha256")(_sha)


class AnswerClaim(_Frozen):
    claim_ordinal: int = Field(ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    claim_text: str = Field(min_length=1)
    supported: bool
    recorded_at: datetime

    @model_validator(mode="after")
    def _span(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("claim span must be non-empty")
        if len(self.claim_text) != self.char_end - self.char_start:
            raise ValueError("claim text length must equal its exact answer span")
        return self


class AnswerClaimCitation(_Frozen):
    claim_ordinal: int = Field(ge=0)
    citation_number: int = Field(gt=0)
    recorded_at: datetime


class AnswerAuditPackage(_Frozen):
    record: AnswerAuditRecord
    retrievals: tuple[AnswerRetrieval, ...]
    citations: tuple[AnswerCitation, ...]
    claims: tuple[AnswerClaim, ...]
    claim_citations: tuple[AnswerClaimCitation, ...]
    sealed_at: datetime

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not self.retrievals:
            raise ValueError("a sealed Ask answer requires at least one retrieval")
        if tuple(item.trace_ordinal for item in self.retrievals) != tuple(
            range(len(self.retrievals))
        ):
            raise ValueError("retrieval ordinals must be contiguous")
        if tuple(item.citation_number for item in self.citations) != tuple(
            range(1, len(self.citations) + 1)
        ):
            raise ValueError("citation numbers must be contiguous")
        if tuple(item.claim_ordinal for item in self.claims) != tuple(range(len(self.claims))):
            raise ValueError("claim ordinals must be contiguous")
        if tuple(item.citation_number for item in self.record.retrieval_assembly) != tuple(
            range(1, len(self.record.retrieval_assembly) + 1)
        ):
            raise ValueError("retrieval assembly ordinals must be contiguous")
        trace_ids = {item.trace_id for item in self.retrievals}
        if any(
            item.request_id != self.record.request_id
            or item.query_sha256 != self.record.query_sha256
            for item in self.retrievals
        ):
            raise ValueError("retrieval traces must bind the exact answer request and query")
        if any(item.trace_id not in trace_ids for item in self.citations):
            raise ValueError("every citation must belong to a bound retrieval")
        assembly_by_number = {
            item.citation_number: item for item in self.record.retrieval_assembly
        }
        for citation in self.citations:
            assembly = assembly_by_number.get(citation.citation_number)
            payload = citation.citation
            if (
                assembly is None
                or payload.n != citation.citation_number
                or payload.trace_id != citation.trace_id
                or payload.result_ordinal != citation.result_ordinal
                or payload.candidate_kind != citation.candidate_kind
                or payload.candidate_id != citation.candidate_id
                or payload.source_commitment_sha256
                != citation.source_commitment_sha256
                or assembly.trace_id != citation.trace_id
                or assembly.result_ordinal != citation.result_ordinal
                or assembly.candidate_kind != citation.candidate_kind
                or assembly.candidate_id != citation.candidate_id
                or assembly.source_commitment_sha256
                != citation.source_commitment_sha256
            ):
                raise ValueError("citation must equal its exact retrieval assembly item")
        claims = {item.claim_ordinal: item for item in self.claims}
        citations = {item.citation_number for item in self.citations}
        edges_by_claim: dict[int, set[int]] = {}
        for edge in self.claim_citations:
            if edge.claim_ordinal not in claims or edge.citation_number not in citations:
                raise ValueError("claim citation edge references an unknown endpoint")
            edges_by_claim.setdefault(edge.claim_ordinal, set()).add(edge.citation_number)
        for ordinal, claim in claims.items():
            if self.record.answer_text[claim.char_start : claim.char_end] != claim.claim_text:
                raise ValueError("claim text must equal its exact answer span")
            has_edges = bool(edges_by_claim.get(ordinal))
            if claim.supported != has_edges:
                raise ValueError("supported claims need citations and unsupported claims need none")
        if self.claims:
            if self.record.no_claim_exemption is not None:
                raise ValueError("claim-bearing answers cannot carry a no-claim exemption")
            if not self.citations:
                raise ValueError("substantive sealed answers require at least one citation")
        else:
            exemption = deterministic_no_claim_exemption(self.record.answer_text)
            if exemption is None or self.record.no_claim_exemption != exemption:
                raise ValueError(
                    "zero-claim sealed answers require the deterministic "
                    "non-substantive exemption"
                )
        if _time(self.sealed_at) < _time(self.record.recorded_at):
            raise ValueError("answer seal cannot predate its record")
        return self


def deterministic_no_claim_exemption(
    answer_text: str,
) -> Literal["deterministic_non_substantive.v1"] | None:
    """Return the one narrow, deterministic exemption for non-answers.

    This is intentionally not a fuzzy classifier.  A substantive response,
    including an unsupported answer, must carry audited claims and citations.
    Only bounded acknowledgements or explicit fail-closed no-answer messages
    qualify.
    """

    normalized = " ".join(answer_text.strip().lower().split())
    exact = {
        "thanks.",
        "thank you.",
        "understood.",
        "i don't have enough sealed evidence to answer that.",
        "i do not have enough sealed evidence to answer that.",
        "no grounded answer is available from the sealed evidence.",
    }
    return "deterministic_non_substantive.v1" if normalized in exact else None


class VerifiedAnswerAudit(_Frozen):
    answer_id: str
    audit_sha256: str
    retrieval_count: int
    citation_count: int
    claim_count: int
    unsupported_claim_count: int
    claim_citation_count: int
    sealed_at: datetime

    _audit_hash = field_validator("audit_sha256")(_sha)


class AnswerAuditIntegrity(_Frozen):
    unsealed_answer_ids: tuple[str, ...]
    invalid_sealed_answer_ids: tuple[str, ...] = ()
    orphan_child_rows: int = Field(ge=0)
    orphan_seal_rows: int = Field(ge=0)

    @property
    def ready(self) -> bool:
        return (
            not self.unsealed_answer_ids
            and not self.invalid_sealed_answer_ids
            and self.orphan_child_rows == 0
            and self.orphan_seal_rows == 0
        )


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _insert_exact(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    *,
    identity_column: str,
    identity_value: object,
) -> None:
    select_sql = (
        f"SELECT {','.join(columns)} FROM {table} WHERE {identity_column}=?"  # nosec B608 -- closed internal schema
    )
    existing = conn.execute(select_sql, (identity_value,)).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise ValueError(f"immutable {table} replay conflicts with existing data")
        return
    placeholders = ",".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) "  # nosec B608 -- closed internal schema
        f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
        values,
    )
    if cursor.rowcount == 1:
        return
    row = conn.execute(select_sql, (identity_value,)).fetchone()
    if row is None or tuple(row) != values:
        raise ValueError(f"immutable {table} replay conflicts with existing data")


def _record_values(record: AnswerAuditRecord) -> tuple[tuple[str, ...], tuple[object, ...]]:
    context_json = canonical_json(record.context_turns)
    assembly_json = canonical_json(record.retrieval_assembly)
    columns = (
        "answer_id",
        "idempotency_key",
        "request_id",
        "session_id",
        "surface",
        "query_sha256",
        "prompt_sha256",
        "prompt_template_id",
        "prompt_template_version",
        "context_turn_set_json",
        "context_turn_set_sha256",
        "retrieval_assembly_json",
        "retrieval_assembly_sha256",
        "answer_text",
        "answer_sha256",
        "llm_purpose",
        "llm_model",
        "llm_provider",
        "llm_transport",
        "llm_call_id",
        "claim_auditor_version",
        "claim_audit_purpose",
        "claim_audit_template_id",
        "claim_audit_template_version",
        "claim_auditor_model",
        "claim_audit_provider",
        "claim_audit_transport",
        "claim_audit_prompt_sha256",
        "claim_audit_response_sha256",
        "claim_audit_llm_call_id",
        "no_claim_exemption",
        "recorded_at",
    )
    return columns, (
        record.answer_id,
        record.idempotency_key,
        record.request_id,
        record.session_id,
        record.surface,
        record.query_sha256,
        record.prompt_sha256,
        record.prompt_template_id,
        record.prompt_template_version,
        context_json,
        digest_text(context_json),
        assembly_json,
        digest_text(assembly_json),
        record.answer_text,
        digest_text(record.answer_text),
        record.llm_purpose,
        record.llm_model,
        record.llm_provider,
        record.llm_transport,
        record.llm_call_id,
        record.claim_auditor_version,
        record.claim_audit_purpose,
        record.claim_audit_template_id,
        record.claim_audit_template_version,
        record.claim_auditor_model,
        record.claim_audit_provider,
        record.claim_audit_transport,
        record.claim_audit_prompt_sha256,
        record.claim_audit_response_sha256,
        record.claim_audit_llm_call_id,
        record.no_claim_exemption,
        _db_time(record.recorded_at),
    )


def _record_sha256(columns: tuple[str, ...], values: tuple[object, ...]) -> str:
    return digest_text(canonical_json(dict(zip(columns, values, strict=True))))


def _verify_llm_call(
    conn: sqlite3.Connection,
    *,
    call_id: int,
    purpose: str,
    model: str,
    provider: str,
    transport: str,
    template_id: str,
    template_version: str,
    prompt_sha256: str,
    response_sha256: str,
    label: str,
) -> None:
    row = conn.execute(
        "SELECT purpose,model,provider,transport,template_id,template_version,"
        "prompt_sha256,response_sha256,error "
        "FROM llm_calls WHERE id=?",
        (call_id,),
    ).fetchone()
    expected = (
        purpose,
        model,
        provider,
        transport,
        template_id,
        template_version,
        prompt_sha256,
        response_sha256,
        None,
    )
    if row is None or tuple(row) != expected:
        raise ValueError(f"{label} does not match its governed llm_calls row")


def _verify_record_references(
    conn: sqlite3.Connection,
    record: AnswerAuditRecord,
) -> None:
    if record.session_id is not None:
        session = conn.execute(
            "SELECT scope FROM ask_sessions WHERE id=?",
            (record.session_id,),
        ).fetchone()
        if session is None or str(session[0]) != record.surface:
            raise ValueError("Ask answer session identity mismatch")
        for item in record.context_turns:
            turn = conn.execute(
                "SELECT session_id,role,text,created_at FROM ask_turns WHERE id=?",
                (item.turn_id,),
            ).fetchone()
            expected_turn = (
                item.session_id,
                item.role,
                item.text_sha256,
                item.created_at,
            )
            actual_turn = (
                None
                if turn is None
                else (str(turn[0]), str(turn[1]), digest_text(str(turn[2])), str(turn[3]))
            )
            if actual_turn != expected_turn:
                raise ValueError("Ask answer context turn identity mismatch")
    _verify_llm_call(
        conn,
        call_id=record.llm_call_id,
        purpose=record.llm_purpose,
        model=record.llm_model,
        provider=record.llm_provider,
        transport=record.llm_transport,
        template_id=record.prompt_template_id,
        template_version=record.prompt_template_version,
        prompt_sha256=record.prompt_sha256,
        response_sha256=digest_text(record.answer_text),
        label="Ask answer LLM call",
    )
    _verify_llm_call(
        conn,
        call_id=record.claim_audit_llm_call_id,
        purpose=record.claim_audit_purpose,
        model=record.claim_auditor_model,
        provider=record.claim_audit_provider,
        transport=record.claim_audit_transport,
        template_id=record.claim_audit_template_id,
        template_version=record.claim_audit_template_version,
        prompt_sha256=record.claim_audit_prompt_sha256,
        response_sha256=record.claim_audit_response_sha256,
        label="Ask claim-audit LLM call",
    )


def _verify_retrieval_reference(
    conn: sqlite3.Connection,
    record: AnswerAuditRecord,
    retrieval: AnswerRetrieval,
) -> None:
    row = conn.execute(
        "SELECT header.idempotency_key,header.query_sha256,"
        "header.research_snapshot_sha256,seal.trace_sha256,"
        "promotion.research_snapshot_id=header.research_snapshot_id,"
        "promotion.fact_generation_id=header.fact_generation_id "
        "FROM heterogeneous_retrieval_trace_headers header "
        "JOIN heterogeneous_retrieval_trace_seals seal "
        "ON seal.trace_id=header.trace_id "
        "JOIN ask_retrieval_scope_promotions promotion "
        "ON promotion.promotion_id=? "
        "WHERE header.trace_id=?",
        (retrieval.promotion_id, retrieval.trace_id),
    ).fetchone()
    expected_key = (
        f"ask-request:{record.request_id}:{record.query_sha256}:"
        f"{retrieval.promotion_id}"
    )
    expected = (
        expected_key,
        record.query_sha256,
        retrieval.research_snapshot_sha256,
        retrieval.trace_sha256,
        1,
        1,
    )
    if row is None or tuple(row) != expected:
        raise ValueError("answer retrieval does not match its exact request-bound trace")


def _verify_assembly_reference(
    conn: sqlite3.Connection,
    assembly: RetrievalAssemblyItem,
) -> None:
    row = conn.execute(
        "SELECT candidate.candidate_kind,candidate.candidate_id,"
        "candidate.source_commitment_sha256 "
        "FROM heterogeneous_retrieval_trace_results result "
        "JOIN heterogeneous_retrieval_trace_candidates candidate "
        "ON candidate.trace_id=result.trace_id "
        "AND candidate.candidate_ordinal=result.candidate_ordinal "
        "WHERE result.trace_id=? AND result.result_ordinal=?",
        (assembly.trace_id, assembly.result_ordinal),
    ).fetchone()
    expected = (
        assembly.candidate_kind,
        assembly.candidate_id,
        assembly.source_commitment_sha256,
    )
    if row is None or tuple(row) != expected:
        raise ValueError("retrieval assembly does not match its exact sealed trace result")


def persist_answer_audit(
    conn: sqlite3.Connection,
    package: AnswerAuditPackage,
) -> VerifiedAnswerAudit:
    """Atomically append and seal one exact answer audit; exact replay is a no-op."""

    package = AnswerAuditPackage.model_validate(package.model_dump())
    answer_id = package.record.answer_id
    columns, values = _record_values(package.record)
    with _savepoint(conn, "persist_ask_answer_audit"):
        _verify_record_references(conn, package.record)
        for item in package.retrievals:
            _verify_retrieval_reference(conn, package.record, item)
        for item in package.record.retrieval_assembly:
            _verify_assembly_reference(conn, item)
        _insert_exact(
            conn,
            "ask_answer_audit_records",
            columns,
            values,
            identity_column="answer_id",
            identity_value=answer_id,
        )
        for item in package.retrievals:
            child = (
                answer_id,
                item.trace_ordinal,
                item.request_id,
                item.query_sha256,
                item.promotion_id,
                item.trace_id,
                item.trace_sha256,
                item.research_snapshot_sha256,
                _db_time(item.recorded_at),
            )
            _insert_exact(
                conn,
                "ask_answer_audit_retrievals",
                (
                    "answer_id",
                    "trace_ordinal",
                    "request_id",
                    "query_sha256",
                    "promotion_id",
                    "trace_id",
                    "trace_sha256",
                    "research_snapshot_sha256",
                    "recorded_at",
                ),
                child,
                identity_column="answer_id || ':' || trace_ordinal",
                identity_value=f"{answer_id}:{item.trace_ordinal}",
            )
        for item in package.citations:
            payload_json = canonical_json(item.citation)
            child = (
                answer_id,
                item.citation_number,
                item.trace_id,
                item.result_ordinal,
                item.candidate_kind,
                item.candidate_id,
                item.source_commitment_sha256,
                payload_json,
                digest_text(payload_json),
                _db_time(item.recorded_at),
            )
            _insert_exact(
                conn,
                "ask_answer_audit_citations",
                (
                    "answer_id",
                    "citation_number",
                    "trace_id",
                    "result_ordinal",
                    "candidate_kind",
                    "candidate_id",
                    "source_commitment_sha256",
                    "citation_json",
                    "citation_sha256",
                    "recorded_at",
                ),
                child,
                identity_column="answer_id || ':' || citation_number",
                identity_value=f"{answer_id}:{item.citation_number}",
            )
        for item in package.claims:
            claim_json = canonical_json(
                {
                    "char_end": item.char_end,
                    "char_start": item.char_start,
                    "claim_ordinal": item.claim_ordinal,
                    "claim_sha256": digest_text(item.claim_text),
                    "supported": item.supported,
                }
            )
            child = (
                answer_id,
                item.claim_ordinal,
                item.char_start,
                item.char_end,
                item.claim_text,
                digest_text(item.claim_text),
                int(item.supported),
                claim_json,
                digest_text(claim_json),
                _db_time(item.recorded_at),
            )
            _insert_exact(
                conn,
                "ask_answer_audit_claims",
                (
                    "answer_id",
                    "claim_ordinal",
                    "char_start",
                    "char_end",
                    "claim_text",
                    "claim_sha256",
                    "supported",
                    "claim_json",
                    "claim_json_sha256",
                    "recorded_at",
                ),
                child,
                identity_column="answer_id || ':' || claim_ordinal",
                identity_value=f"{answer_id}:{item.claim_ordinal}",
            )
        for item in package.claim_citations:
            child = (
                answer_id,
                item.claim_ordinal,
                item.citation_number,
                _db_time(item.recorded_at),
            )
            _insert_exact(
                conn,
                "ask_answer_audit_claim_citations",
                ("answer_id", "claim_ordinal", "citation_number", "recorded_at"),
                child,
                identity_column=(
                    "answer_id || ':' || claim_ordinal || ':' || citation_number"
                ),
                identity_value=(
                    f"{answer_id}:{item.claim_ordinal}:{item.citation_number}"
                ),
            )
        seal_columns, seal_values = _seal_values(package)
        _insert_exact(
            conn,
            "ask_answer_audit_seals",
            seal_columns,
            seal_values,
            identity_column="answer_id",
            identity_value=answer_id,
        )
        return verify_answer_audit(conn, answer_id)


def _seal_values(
    package: AnswerAuditPackage,
) -> tuple[tuple[str, ...], tuple[object, ...]]:
    record_columns, record_values = _record_values(package.record)
    record_sha256 = _record_sha256(record_columns, record_values)
    retrievals: list[dict[str, object]] = [
        {
            "promotion_id": item.promotion_id,
            "query_sha256": item.query_sha256,
            "research_snapshot_sha256": item.research_snapshot_sha256,
            "request_id": item.request_id,
            "trace_id": item.trace_id,
            "trace_ordinal": item.trace_ordinal,
            "trace_sha256": item.trace_sha256,
        }
        for item in package.retrievals
    ]
    citations = [
        {
            "candidate_id": item.candidate_id,
            "candidate_kind": item.candidate_kind,
            "citation_number": item.citation_number,
            "citation_sha256": digest_text(canonical_json(item.citation)),
            "result_ordinal": item.result_ordinal,
            "source_commitment_sha256": item.source_commitment_sha256,
            "trace_id": item.trace_id,
        }
        for item in package.citations
    ]
    claims = [
        {
            "char_end": item.char_end,
            "char_start": item.char_start,
            "claim_ordinal": item.claim_ordinal,
            "claim_sha256": digest_text(item.claim_text),
            "supported": item.supported,
        }
        for item in package.claims
    ]
    edges = [
        {
            "citation_number": item.citation_number,
            "claim_ordinal": item.claim_ordinal,
        }
        for item in package.claim_citations
    ]
    retrieval_json = canonical_json(retrievals)
    citation_json = canonical_json(citations)
    claim_json = canonical_json(claims)
    edge_json = canonical_json(edges)
    audit = {
        "answer_id": package.record.answer_id,
        "answer_sha256": digest_text(package.record.answer_text),
        "citation_set_sha256": digest_text(citation_json),
        "claim_citation_set_sha256": digest_text(edge_json),
        "claim_set_sha256": digest_text(claim_json),
        "record_sha256": record_sha256,
        "retrieval_set_sha256": digest_text(retrieval_json),
        "version": "ask_answer_audit.v1",
    }
    audit_json = canonical_json(audit)
    columns = (
        "answer_id",
        "retrieval_count",
        "citation_count",
        "claim_count",
        "unsupported_claim_count",
        "claim_citation_count",
        "record_sha256",
        "retrieval_set_sha256",
        "citation_set_sha256",
        "claim_set_sha256",
        "claim_citation_set_sha256",
        "audit_json",
        "audit_sha256",
        "sealed_at",
    )
    return columns, (
        package.record.answer_id,
        len(retrievals),
        len(citations),
        len(claims),
        sum(not item.supported for item in package.claims),
        len(edges),
        record_sha256,
        digest_text(retrieval_json),
        digest_text(citation_json),
        digest_text(claim_json),
        digest_text(edge_json),
        audit_json,
        digest_text(audit_json),
        _db_time(package.sealed_at),
    )


def verify_answer_audit(conn: sqlite3.Connection, answer_id: str) -> VerifiedAnswerAudit:
    """Recompute every stored answer-audit commitment without caller callbacks."""

    conn.row_factory = sqlite3.Row
    record = conn.execute(
        "SELECT * FROM ask_answer_audit_records WHERE answer_id=?",
        (answer_id,),
    ).fetchone()
    seal = conn.execute(
        "SELECT * FROM ask_answer_audit_seals WHERE answer_id=?",
        (answer_id,),
    ).fetchone()
    if record is None or seal is None:
        raise ValueError("Ask answer audit is not fully sealed")
    if (
        digest_text(str(record["answer_text"])) != str(record["answer_sha256"])
        or digest_text(str(record["context_turn_set_json"]))
        != str(record["context_turn_set_sha256"])
        or digest_text(str(record["retrieval_assembly_json"]))
        != str(record["retrieval_assembly_sha256"])
    ):
        raise ValueError("Ask answer header commitment mismatch")
    try:
        context_payload = json.loads(str(record["context_turn_set_json"]))
        assembly_payload = json.loads(str(record["retrieval_assembly_json"]))
        context_turns = tuple(AnswerContextTurn.model_validate(item) for item in context_payload)
        assembly_items = tuple(
            RetrievalAssemblyItem.model_validate(item) for item in assembly_payload
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("Ask answer header JSON is invalid") from exc
    if canonical_json(context_turns) != str(record["context_turn_set_json"]) or canonical_json(
        assembly_items
    ) != str(record["retrieval_assembly_json"]):
        raise ValueError("Ask answer header JSON is not canonical")
    if record["session_id"] is not None:
        session = conn.execute(
            "SELECT scope FROM ask_sessions WHERE id=?",
            (record["session_id"],),
        ).fetchone()
        if session is None or str(session[0]) != str(record["surface"]):
            raise ValueError("Ask answer session identity mismatch")
        for item in context_turns:
            turn = conn.execute(
                "SELECT session_id,role,text,created_at FROM ask_turns WHERE id=?",
                (item.turn_id,),
            ).fetchone()
            if turn is None or tuple(
                (str(turn[0]), str(turn[1]), digest_text(str(turn[2])), str(turn[3]))
            ) != (item.session_id, item.role, item.text_sha256, item.created_at):
                raise ValueError("Ask answer context turn identity mismatch")
    for item in assembly_items:
        _verify_assembly_reference(conn, item)
    _verify_llm_call(
        conn,
        call_id=int(record["llm_call_id"]),
        purpose=str(record["llm_purpose"]),
        model=str(record["llm_model"]),
        provider=str(record["llm_provider"]),
        transport=str(record["llm_transport"]),
        template_id=str(record["prompt_template_id"]),
        template_version=str(record["prompt_template_version"]),
        prompt_sha256=str(record["prompt_sha256"]),
        response_sha256=str(record["answer_sha256"]),
        label="Ask answer LLM call",
    )
    _verify_llm_call(
        conn,
        call_id=int(record["claim_audit_llm_call_id"]),
        purpose=str(record["claim_audit_purpose"]),
        model=str(record["claim_auditor_model"]),
        provider=str(record["claim_audit_provider"]),
        transport=str(record["claim_audit_transport"]),
        template_id=str(record["claim_audit_template_id"]),
        template_version=str(record["claim_audit_template_version"]),
        prompt_sha256=str(record["claim_audit_prompt_sha256"]),
        response_sha256=str(record["claim_audit_response_sha256"]),
        label="Ask claim-audit LLM call",
    )
    retrieval_rows = conn.execute(
        "SELECT * FROM ask_answer_audit_retrievals "
        "WHERE answer_id=? ORDER BY trace_ordinal",
        (answer_id,),
    ).fetchall()
    citation_rows = conn.execute(
        "SELECT * FROM ask_answer_audit_citations "
        "WHERE answer_id=? ORDER BY citation_number",
        (answer_id,),
    ).fetchall()
    claim_rows = conn.execute(
        "SELECT * FROM ask_answer_audit_claims "
        "WHERE answer_id=? ORDER BY claim_ordinal",
        (answer_id,),
    ).fetchall()
    edge_rows = conn.execute(
        "SELECT * FROM ask_answer_audit_claim_citations "
        "WHERE answer_id=? ORDER BY claim_ordinal,citation_number",
        (answer_id,),
    ).fetchall()
    for row in retrieval_rows:
        header = conn.execute(
            "SELECT header.idempotency_key,header.query_sha256,"
            "header.research_snapshot_sha256,trace.trace_sha256,"
            "promotion.research_snapshot_id=header.research_snapshot_id,"
            "promotion.fact_generation_id=header.fact_generation_id "
            "FROM heterogeneous_retrieval_trace_headers header "
            "JOIN heterogeneous_retrieval_trace_seals trace "
            "ON trace.trace_id=header.trace_id "
            "JOIN ask_retrieval_scope_promotions promotion "
            "ON promotion.promotion_id=? WHERE header.trace_id=?",
            (row["promotion_id"], row["trace_id"]),
        ).fetchone()
        expected_key = (
            f"ask-request:{record['request_id']}:{record['query_sha256']}:"
            f"{row['promotion_id']}"
        )
        expected_trace = (
            expected_key,
            record["query_sha256"],
            row["research_snapshot_sha256"],
            row["trace_sha256"],
            1,
            1,
        )
        if (
            row["request_id"] != record["request_id"]
            or row["query_sha256"] != record["query_sha256"]
            or header is None
            or tuple(header) != expected_trace
        ):
            raise ValueError("Ask answer retrieval request identity mismatch")
    retrievals = [
        {
            "promotion_id": row["promotion_id"],
            "query_sha256": row["query_sha256"],
            "research_snapshot_sha256": row["research_snapshot_sha256"],
            "request_id": row["request_id"],
            "trace_id": row["trace_id"],
            "trace_ordinal": int(row["trace_ordinal"]),
            "trace_sha256": row["trace_sha256"],
        }
        for row in retrieval_rows
    ]
    citations: list[dict[str, object]] = []
    for row in citation_rows:
        citation_json = str(row["citation_json"])
        try:
            payload = CitationAuditPayload.model_validate_json(citation_json)
        except ValueError as exc:
            raise ValueError("Ask citation payload is invalid") from exc
        if (
            digest_text(citation_json) != str(row["citation_sha256"])
            or canonical_json(payload) != citation_json
            or (
                payload.n,
                payload.trace_id,
                payload.result_ordinal,
                payload.candidate_kind,
                payload.candidate_id,
                payload.source_commitment_sha256,
            )
            != (
                int(row["citation_number"]),
                str(row["trace_id"]),
                int(row["result_ordinal"]),
                str(row["candidate_kind"]),
                str(row["candidate_id"]),
                str(row["source_commitment_sha256"]),
            )
        ):
            raise ValueError("Ask citation commitment mismatch")
        citations.append(
            {
                "candidate_id": row["candidate_id"],
                "candidate_kind": row["candidate_kind"],
                "citation_number": int(row["citation_number"]),
                "citation_sha256": row["citation_sha256"],
                "result_ordinal": int(row["result_ordinal"]),
                "source_commitment_sha256": row["source_commitment_sha256"],
                "trace_id": row["trace_id"],
            }
        )
    claims: list[dict[str, object]] = []
    answer_text = str(record["answer_text"])
    for row in claim_rows:
        claim_text = str(row["claim_text"])
        claim_json = str(row["claim_json"])
        if (
            digest_text(claim_text) != str(row["claim_sha256"])
            or digest_text(claim_json) != str(row["claim_json_sha256"])
            or answer_text[int(row["char_start"]) : int(row["char_end"])]
            != claim_text
        ):
            raise ValueError("Ask claim commitment mismatch")
        claims.append(
            {
                "char_end": int(row["char_end"]),
                "char_start": int(row["char_start"]),
                "claim_ordinal": int(row["claim_ordinal"]),
                "claim_sha256": row["claim_sha256"],
                "supported": bool(row["supported"]),
            }
        )
    edges: list[dict[str, object]] = [
        {
            "citation_number": int(row["citation_number"]),
            "claim_ordinal": int(row["claim_ordinal"]),
        }
        for row in edge_rows
    ]
    edges_by_claim = {
        int(row["claim_ordinal"]) for row in edge_rows
    }
    if any(
        bool(row["supported"]) != (int(row["claim_ordinal"]) in edges_by_claim)
        for row in claim_rows
    ):
        raise ValueError("Ask answer support graph is incomplete")
    hashes = (
        digest_text(canonical_json(retrievals)),
        digest_text(canonical_json(citations)),
        digest_text(canonical_json(claims)),
        digest_text(canonical_json(edges)),
    )
    record_names = record.keys()
    record_payload: dict[str, object] = dict(
        zip(
            (str(name) for name in record_names),
            tuple(record),
            strict=True,
        )
    )
    record_commitment_sha256 = digest_text(canonical_json(record_payload))
    expected_audit = canonical_json(
        {
            "answer_id": answer_id,
            "answer_sha256": record["answer_sha256"],
            "citation_set_sha256": hashes[1],
            "claim_citation_set_sha256": hashes[3],
            "claim_set_sha256": hashes[2],
            "record_sha256": record_commitment_sha256,
            "retrieval_set_sha256": hashes[0],
            "version": "ask_answer_audit.v1",
        }
    )
    expected_counts = (
        len(retrievals),
        len(citations),
        len(claims),
        sum(not bool(row["supported"]) for row in claim_rows),
        len(edges),
    )
    exemption = (
        None
        if record["no_claim_exemption"] is None
        else str(record["no_claim_exemption"])
    )
    if expected_counts[2] > 0:
        if expected_counts[1] == 0 or exemption is not None:
            raise ValueError("substantive Ask answer claim/citation contract is incomplete")
    else:
        expected_exemption = deterministic_no_claim_exemption(answer_text)
        if expected_exemption is None or exemption != expected_exemption:
            raise ValueError("Ask no-claim exemption is invalid")
    stored_counts = (
        int(seal["retrieval_count"]),
        int(seal["citation_count"]),
        int(seal["claim_count"]),
        int(seal["unsupported_claim_count"]),
        int(seal["claim_citation_count"]),
    )
    if (
        stored_counts != expected_counts
        or str(seal["record_sha256"]) != record_commitment_sha256
        or tuple(str(seal[name]) for name in (
            "retrieval_set_sha256",
            "citation_set_sha256",
            "claim_set_sha256",
            "claim_citation_set_sha256",
        ))
        != hashes
        or str(seal["audit_json"]) != expected_audit
        or str(seal["audit_sha256"]) != digest_text(expected_audit)
    ):
        raise ValueError("Ask answer final audit seal mismatch")
    return VerifiedAnswerAudit(
        answer_id=answer_id,
        audit_sha256=str(seal["audit_sha256"]),
        retrieval_count=expected_counts[0],
        citation_count=expected_counts[1],
        claim_count=expected_counts[2],
        unsupported_claim_count=expected_counts[3],
        claim_citation_count=expected_counts[4],
        sealed_at=datetime.fromisoformat(str(seal["sealed_at"])),
    )


def audit_answer_audit_integrity(conn: sqlite3.Connection) -> AnswerAuditIntegrity:
    """Detect interrupted/direct-SQL writes that never reached an immutable seal."""

    unsealed = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT record.answer_id FROM ask_answer_audit_records record "
            "LEFT JOIN ask_answer_audit_seals seal ON seal.answer_id=record.answer_id "
            "WHERE seal.answer_id IS NULL ORDER BY record.answer_id"
        ).fetchall()
    )
    orphan_children = 0
    for table in (
        "ask_answer_audit_retrievals",
        "ask_answer_audit_citations",
        "ask_answer_audit_claims",
        "ask_answer_audit_claim_citations",
    ):
        orphan_children += int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} child "  # nosec B608 - closed table set
                "LEFT JOIN ask_answer_audit_records record "
                "ON record.answer_id=child.answer_id "
                "WHERE record.answer_id IS NULL"
            ).fetchone()[0]
        )
    orphan_seals = int(
        conn.execute(
            "SELECT COUNT(*) FROM ask_answer_audit_seals seal "
            "LEFT JOIN ask_answer_audit_records record "
            "ON record.answer_id=seal.answer_id "
            "WHERE record.answer_id IS NULL"
        ).fetchone()[0]
    )
    invalid: list[str] = []
    for row in conn.execute(
        "SELECT answer_id FROM ask_answer_audit_seals ORDER BY answer_id"
    ).fetchall():
        answer_id = str(row[0])
        try:
            verify_answer_audit(conn, answer_id)
        except (sqlite3.Error, ValueError):
            invalid.append(answer_id)
    return AnswerAuditIntegrity(
        unsealed_answer_ids=unsealed,
        invalid_sealed_answer_ids=tuple(invalid),
        orphan_child_rows=orphan_children,
        orphan_seal_rows=orphan_seals,
    )


__all__ = [
    "AnswerAuditIntegrity",
    "AnswerAuditPackage",
    "AnswerAuditRecord",
    "AnswerCitation",
    "AnswerClaim",
    "AnswerClaimCitation",
    "AnswerContextTurn",
    "AnswerRetrieval",
    "CitationAuditPayload",
    "RetrievalAssemblyItem",
    "VerifiedAnswerAudit",
    "audit_answer_audit_integrity",
    "canonical_json",
    "deterministic_no_claim_exemption",
    "digest_text",
    "persist_answer_audit",
    "retrieval_query_sha256",
    "verify_answer_audit",
]
