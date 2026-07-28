"""Fail-closed Ask retrieval over sealed, source-complete evidence corpora."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from search.embedding_promotion import (
    LocalVectorRuntimeConfig,
    promoted_vector_backend,
)
from search.grounded import EvidenceBundle, HybridRetriever, SearchFilter
from search.local_vector import LocalVectorCapabilityError

GroundedAskOutcome = Literal["ready", "coverage_incomplete", "unavailable"]
_RETRIEVAL_VERSION = "grounded-ask-retrieval@1"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GroundedAskItem(_Closed):
    rank: int = Field(gt=0)
    manifest_id: str
    chunk_id: str
    score: float
    text: str
    node_id: str
    node_kind: str
    locator: dict[str, object] | None
    document_version_id: str
    issuer_id: str
    ticker: str | None
    form_type: str
    source_url: str
    filing_at: datetime | None
    observed_at: datetime
    retrieved_at: datetime


class GroundedAskResult(_Closed):
    outcome: GroundedAskOutcome
    reason_code: str
    trace_id: str | None
    manifest_ids: tuple[str, ...]
    items: tuple[GroundedAskItem, ...]


def ask_item_bundle_sha256(item: GroundedAskItem) -> str:
    """Return the canonical digest persisted for one exact retrieval bundle."""

    return _sha_json(item.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class _Candidate:
    manifest_id: str
    bundle: EvidenceBundle


def retrieve_grounded_ask(
    conn: sqlite3.Connection,
    *,
    question: str,
    tickers: tuple[str, ...],
    created_at: datetime,
    limit: int = 12,
    persist_trace: bool = True,
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> GroundedAskResult:
    """Retrieve only when each issuer has a current, fully represented corpus."""

    normalized_question = " ".join(question.split())
    normalized_tickers = tuple(
        dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip())
    )
    if not normalized_question or not normalized_tickers:
        return _outcome(
            "unavailable",
            "empty_question_or_scope",
            (),
            (),
            conn,
            normalized_question,
            normalized_tickers,
            (),
            created_at,
            persist_trace,
        )
    if limit <= 0:
        raise ValueError("grounded Ask retrieval limit must be positive")
    manifests: list[str] = []
    issuer_ids: list[str] = []
    for ticker in normalized_tickers:
        manifest, issuer_id, reason = _manifest_for_ticker(conn, ticker)
        if manifest is None:
            outcome: GroundedAskOutcome = (
                "coverage_incomplete"
                if reason not in {"source_inventory_unavailable", "ticker_identity_ambiguous"}
                else "unavailable"
            )
            return _outcome(
                outcome,
                reason,
                tuple(manifests),
                (),
                conn,
                normalized_question,
                normalized_tickers,
                tuple(issuer_ids),
                created_at,
                persist_trace,
            )
        if issuer_id is None:
            raise RuntimeError("resolved grounded corpus unexpectedly lacks canonical issuer")
        manifests.append(manifest)
        issuer_ids.append(issuer_id)
    candidates: list[_Candidate] = []
    per_manifest_limit = max(limit, 4)
    runtime = (
        local_vector_runtime
        if local_vector_runtime is not None
        else LocalVectorRuntimeConfig.from_environment()
    )
    used_vector = False
    for issuer_id, manifest_id in zip(issuer_ids, manifests, strict=True):
        backend = None
        if runtime is not None:
            backend = promoted_vector_backend(
                conn,
                manifest_id=manifest_id,
                index_root=runtime.index_root,
                runtime_root=runtime.runtime_root,
            )
            if backend is None:
                raise LocalVectorCapabilityError(
                    "semantic retrieval is enabled but no verified local vector "
                    f"backend is available for manifest {manifest_id}"
                )
        used_vector = used_vector or backend is not None
        retriever = HybridRetriever(conn, backend)
        for bundle in retriever.search(
            normalized_question,
            manifest_id,
            SearchFilter(issuer_id=issuer_id),
            limit=per_manifest_limit,
        ):
            candidates.append(_Candidate(manifest_id=manifest_id, bundle=bundle))
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item.bundle.score,
            item.manifest_id,
            item.bundle.chunk_id,
        ),
    )[:limit]
    items = tuple(
        GroundedAskItem(
            rank=rank,
            manifest_id=item.manifest_id,
            chunk_id=item.bundle.chunk_id,
            score=item.bundle.score,
            text=item.bundle.text,
            node_id=item.bundle.node_id,
            node_kind=item.bundle.node_kind,
            locator=item.bundle.locator,
            document_version_id=item.bundle.document_version_id,
            issuer_id=item.bundle.issuer_id,
            ticker=item.bundle.ticker,
            form_type=item.bundle.form_type,
            source_url=item.bundle.source_url,
            filing_at=item.bundle.filing_at,
            observed_at=item.bundle.observed_at,
            retrieved_at=item.bundle.retrieved_at,
        )
        for rank, item in enumerate(ordered, start=1)
    )
    return _outcome(
        "ready",
        ("sealed_complete_hybrid_corpus" if used_vector else "sealed_complete_lexical_corpus"),
        tuple(manifests),
        items,
        conn,
        normalized_question,
        normalized_tickers,
        tuple(issuer_ids),
        created_at,
        persist_trace,
    )


def persist_answer_grounding(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
    prompt: str | None = None,
    prompt_sha256: str | None = None,
    answer: str,
    recorded_at: datetime,
    llm_call_id: str | None = None,
) -> str:
    """Append the exact prompt/answer digests associated with one retrieval."""

    if (prompt is None) == (prompt_sha256 is None):
        raise ValueError("provide exactly one of prompt or prompt_sha256")
    prompt_sha = _sha_text(prompt) if prompt is not None else str(prompt_sha256)
    if len(prompt_sha) != 64 or any(char not in "0123456789abcdef" for char in prompt_sha):
        raise ValueError("prompt_sha256 must be a lowercase SHA-256 digest")
    answer_sha = _sha_text(answer)
    seed = _sha_json(
        {
            "trace_id": trace_id,
            "prompt_sha256": prompt_sha,
            "answer_sha256": answer_sha,
            "llm_call_id": llm_call_id,
        }
    )
    grounding_id = f"ask-grounding:{seed}"
    values = (
        grounding_id,
        grounding_id,
        trace_id,
        prompt_sha,
        answer_sha,
        llm_call_id,
        recorded_at,
    )
    cursor = conn.execute(
        "INSERT INTO ask_answer_groundings "
        "(grounding_id,idempotency_key,trace_id,prompt_sha256,answer_sha256,"
        "llm_call_id,recorded_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        values,
    )
    if cursor.rowcount == 0:
        existing = conn.execute(
            "SELECT grounding_id,idempotency_key,trace_id,prompt_sha256,answer_sha256,"
            "llm_call_id,recorded_at FROM ask_answer_groundings WHERE idempotency_key = ?",
            (grounding_id,),
        ).fetchone()
        if existing is None or not _same(tuple(existing), values):
            raise ValueError("immutable Ask answer grounding conflicts with existing data")
    return grounding_id


def _manifest_for_ticker(
    conn: sqlite3.Connection, ticker: str
) -> tuple[str | None, str | None, str]:
    current = conn.execute(
        "SELECT current.snapshot_id, seal.completion_status, current.issuer_id "
        "FROM v_source_inventory_current AS current "
        "LEFT JOIN source_inventory_snapshot_seals AS seal "
        "ON seal.snapshot_id = current.snapshot_id "
        "WHERE current.ticker = ? ORDER BY current.inventory_key",
        (ticker,),
    ).fetchall()
    if not current:
        return None, None, "source_inventory_unavailable"
    issuer_ids = {str(row[2]) for row in current}
    if len(issuer_ids) != 1:
        return None, None, "ticker_identity_ambiguous"
    issuer_id = next(iter(issuer_ids))
    if any(row[1] != "complete" for row in current):
        return None, issuer_id, "current_source_inventory_incomplete"
    required = {str(row[0]) for row in current}
    candidates = conn.execute(
        "SELECT manifest.manifest_id, manifest.corpus_key, manifest.revision "
        "FROM search_corpus_manifests AS manifest "
        "JOIN search_corpus_manifest_seals AS seal "
        "ON seal.manifest_id = manifest.manifest_id "
        "WHERE seal.completion_status = 'complete' "
        "AND NOT EXISTS (SELECT 1 FROM search_corpus_manifests AS newer "
        "WHERE newer.corpus_key = manifest.corpus_key "
        "AND newer.revision > manifest.revision) "
        "ORDER BY manifest.recorded_at DESC, manifest.manifest_id",
    ).fetchall()
    for row in candidates:
        manifest_id = str(row[0])
        linked = {
            str(item[0])
            for item in conn.execute(
                "SELECT snapshot_id FROM search_manifest_source_inventories WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchall()
        }
        if linked == required:
            return manifest_id, issuer_id, "sealed_complete_corpus"
    return None, issuer_id, "no_manifest_for_current_source_universe"


def _outcome(
    outcome: GroundedAskOutcome,
    reason_code: str,
    manifest_ids: tuple[str, ...],
    items: tuple[GroundedAskItem, ...],
    conn: sqlite3.Connection,
    question: str,
    tickers: tuple[str, ...],
    issuer_ids: tuple[str, ...],
    created_at: datetime,
    persist_trace: bool,
) -> GroundedAskResult:
    question_sha = _sha_text(question)
    scope_sha = _sha_json({"tickers": tickers, "issuer_ids": issuer_ids})
    filters: dict[str, object] = {
        "tickers": tickers,
        "canonical_issuer_ids": issuer_ids,
    }
    config_sha = _sha_json(
        {
            "version": _RETRIEVAL_VERSION,
            "manifests": manifest_ids,
            "filters": filters,
        }
    )
    seed = _sha_json(
        {
            "question_sha256": question_sha,
            "scope_sha256": scope_sha,
            "retrieval_config_sha256": config_sha,
            "outcome": outcome,
            "reason_code": reason_code,
            "items": [(item.manifest_id, item.chunk_id, item.rank, item.score) for item in items],
        }
    )
    trace_id = f"ask-retrieval:{seed}"
    if persist_trace:
        _persist_trace(
            conn,
            trace_id=trace_id,
            question_sha=question_sha,
            scope_sha=scope_sha,
            config_sha=config_sha,
            outcome=outcome,
            reason_code=reason_code,
            manifest_ids=manifest_ids,
            filters=filters,
            items=items,
            created_at=created_at,
        )
    return GroundedAskResult(
        outcome=outcome,
        reason_code=reason_code,
        trace_id=trace_id if persist_trace else None,
        manifest_ids=manifest_ids,
        items=items,
    )


def _persist_trace(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
    question_sha: str,
    scope_sha: str,
    config_sha: str,
    outcome: GroundedAskOutcome,
    reason_code: str,
    manifest_ids: tuple[str, ...],
    filters: dict[str, object],
    items: tuple[GroundedAskItem, ...],
    created_at: datetime,
) -> None:
    values = (
        trace_id,
        trace_id,
        question_sha,
        scope_sha,
        config_sha,
        outcome,
        reason_code,
        json.dumps(manifest_ids, separators=(",", ":")),
        json.dumps(filters, sort_keys=True, separators=(",", ":")),
        created_at,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT INTO ask_retrieval_traces "
            "(trace_id,idempotency_key,question_sha256,scope_sha256,"
            "retrieval_config_sha256,outcome,reason_code,manifest_ids_json,"
            "filters_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT trace_id,idempotency_key,question_sha256,scope_sha256,"
                "retrieval_config_sha256,outcome,reason_code,manifest_ids_json,"
                "filters_json,created_at FROM ask_retrieval_traces "
                "WHERE idempotency_key = ?",
                (trace_id,),
            ).fetchone()
            if existing is None or not _same(tuple(existing), values):
                raise ValueError("immutable Ask retrieval trace conflicts with existing data")
        for item in items:
            item_values = (
                trace_id,
                item.rank,
                item.manifest_id,
                item.chunk_id,
                item.score,
                ask_item_bundle_sha256(item),
                created_at,
            )
            item_cursor = conn.execute(
                "INSERT INTO ask_retrieval_trace_items "
                "(trace_id,rank,manifest_id,chunk_id,score,bundle_sha256,recorded_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                item_values,
            )
            if item_cursor.rowcount == 0:
                existing_item = conn.execute(
                    "SELECT trace_id,rank,manifest_id,chunk_id,score,bundle_sha256,"
                    "recorded_at FROM ask_retrieval_trace_items "
                    "WHERE trace_id = ? AND rank = ?",
                    (trace_id, item.rank),
                ).fetchone()
                if existing_item is None or not _same(tuple(existing_item), item_values):
                    raise ValueError(
                        "immutable Ask retrieval trace item conflicts with existing data"
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    normalized_left = tuple(
        item.isoformat(sep=" ") if isinstance(item, datetime) else item for item in left
    )
    normalized_right = tuple(
        item.isoformat(sep=" ") if isinstance(item, datetime) else item for item in right
    )
    return normalized_left == normalized_right
