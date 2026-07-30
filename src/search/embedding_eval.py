"""Closed-set, deterministic evaluation for local evidence-vector retrieval.

This is deliberately a recommendation artifact, not model routing.  A caller
may inspect its output and separately authorize any routing change.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from search.grounded import SearchFilter, VectorCandidate

PURPOSE = "evidence_vector_retrieval"
DEFAULT_CANDIDATES = ("BAAI/bge-small-en-v1.5", "BAAI/bge-base-en-v1.5")


class VectorEvalCase(BaseModel):
    """A closed retrieval judgment; relevant IDs are immutable search chunks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1)
    relevant_chunk_ids: tuple[str, ...] = Field(min_length=1)
    filters: SearchFilter = Field(default_factory=SearchFilter)

    @field_validator("relevant_chunk_ids")
    @classmethod
    def _unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("relevant_chunk_ids must be unique non-empty IDs")
        return values


class EvalThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_cases: int = Field(default=30, gt=0)
    min_recall_at_k: float = Field(default=0.75, ge=0, le=1)
    min_mrr: float = Field(default=0.65, ge=0, le=1)
    min_ndcg: float = Field(default=0.7, ge=0, le=1)
    max_mean_latency_ms: float = Field(default=1500.0, ge=0)
    parity_tolerance: float = Field(default=0.02, ge=0, le=1)


class CandidateMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    case_count: int = Field(ge=0)
    recall_at_k: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    ndcg: float = Field(ge=0, le=1)
    mean_latency_ms: float = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    runtime_artifact_sha256: str | None = None

    @field_validator("runtime_artifact_sha256")
    @classmethod
    def _runtime_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("runtime artifact hash must be lowercase SHA-256")
        return value


class CandidateEvaluationCoordinate(BaseModel):
    """Exact sealed candidate projection used to produce one metric row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1, max_length=128)
    index_run_id: str = Field(min_length=1, max_length=128)
    manifest_id: str = Field(min_length=1, max_length=128)
    projection_seal_id: str = Field(min_length=1, max_length=128)
    projection_records_sha256: str
    artifact_set_sha256: str
    config_sha256: str
    chunk_count: int = Field(gt=0)
    chunk_set_sha256: str
    runtime_registration_id: str = Field(min_length=1, max_length=128)
    runtime_artifact_sha256: str
    sealed_at: datetime

    @field_validator(
        "projection_records_sha256",
        "artifact_set_sha256",
        "config_sha256",
        "chunk_set_sha256",
        "runtime_artifact_sha256",
    )
    @classmethod
    def _hashes(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("candidate coordinate hashes must be lowercase SHA-256")
        return value


class EmbeddingRecommendationArtifact(BaseModel):
    """An auditable result that intentionally does not modify model selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str = PURPOSE
    golden_sha256: str = Field(min_length=64, max_length=64)
    k: int = Field(gt=0)
    thresholds: EvalThresholds
    results: tuple[CandidateMetrics, ...]
    candidate_coordinates: tuple[CandidateEvaluationCoordinate, ...]
    evaluated_at: datetime
    recommended_model: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _candidate_contract(self) -> EmbeddingRecommendationArtifact:
        coordinate_models = tuple(item.model for item in self.candidate_coordinates)
        result_models = tuple(item.model for item in self.results)
        if coordinate_models != tuple(sorted(coordinate_models)) or len(coordinate_models) != len(
            set(coordinate_models)
        ):
            raise ValueError("candidate coordinates must be unique and sorted by model")
        if set(coordinate_models) != set(DEFAULT_CANDIDATES) or len(coordinate_models) != len(
            DEFAULT_CANDIDATES
        ):
            raise ValueError("candidate coordinates differ from the governed candidate policy")
        runtime_coordinates = {item.runtime_registration_id for item in self.candidate_coordinates}
        projection_coordinates = {item.projection_seal_id for item in self.candidate_coordinates}
        if len(runtime_coordinates) != len(coordinate_models) or len(projection_coordinates) != len(
            coordinate_models
        ):
            raise ValueError("candidate runtime and projection coordinates must be unique")
        if result_models != coordinate_models:
            raise ValueError("candidate metrics and coordinates must cover the same models")
        corpus_coordinates = {
            (item.manifest_id, item.chunk_count, item.chunk_set_sha256)
            for item in self.candidate_coordinates
        }
        if len(corpus_coordinates) != 1:
            raise ValueError("embedding candidates must evaluate the same sealed corpus")
        runtime_by_model = {
            item.model: item.runtime_artifact_sha256 for item in self.candidate_coordinates
        }
        if any(
            item.runtime_artifact_sha256 != runtime_by_model[item.model] for item in self.results
        ):
            raise ValueError("candidate metrics runtime differs from its sealed coordinate")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )


class VectorRetriever(Protocol):
    def __call__(self, case: VectorEvalCase, limit: int) -> Sequence[VectorCandidate]: ...


def load_embedding_golden(path: Path) -> list[VectorEvalCase]:
    """Load a finite, schema-validated evaluation set; no live labels are accepted."""
    try:
        raw_value: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"golden file unreadable at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"golden file is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw_value, dict):
        raise ValueError("golden file must be a JSON object")
    raw = cast(dict[str, object], raw_value)
    if raw.get("purpose") != PURPOSE:
        raise ValueError(f"golden file purpose must be {PURPOSE!r}")
    cases_value = raw.get("cases")
    if not isinstance(cases_value, list) or not cases_value:
        raise ValueError("golden file requires a non-empty closed cases list")
    cases = cast(list[object], cases_value)
    try:
        parsed = [VectorEvalCase.model_validate(case) for case in cases]
    except ValueError as exc:
        raise ValueError(f"golden file has invalid cases: {exc}") from exc
    if len({case.case_id for case in parsed}) != len(parsed):
        raise ValueError("golden case IDs must be unique")
    return parsed


def golden_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_embedding_candidates(
    cases: Sequence[VectorEvalCase],
    retrievers: Mapping[str, VectorRetriever],
    *,
    k: int = 10,
    thresholds: EvalThresholds | None = None,
    golden_digest: str | None = None,
    runtime_artifact_sha256: Mapping[str, str],
    candidate_coordinates: Mapping[str, CandidateEvaluationCoordinate],
    evaluated_at: datetime,
    clock: Callable[[], float] = time.perf_counter,
) -> EmbeddingRecommendationArtifact:
    """Measure fixed retrieval metrics and recommend only a clear eligible winner."""
    if k <= 0:
        raise ValueError("k must be positive")
    if not cases:
        raise ValueError("evaluation needs at least one closed golden case")
    if set(retrievers) != set(DEFAULT_CANDIDATES) or len(retrievers) != len(DEFAULT_CANDIDATES):
        raise ValueError(
            "evaluation candidates differ from the governed candidate policy: "
            + ", ".join(DEFAULT_CANDIDATES)
        )
    if set(runtime_artifact_sha256) != set(retrievers):
        raise ValueError("evaluation requires one runtime artifact hash per candidate")
    if set(candidate_coordinates) != set(retrievers):
        raise ValueError("evaluation requires one sealed coordinate per candidate")
    for digest in runtime_artifact_sha256.values():
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("candidate runtime artifact hash must be lowercase SHA-256")
    thresholds = EvalThresholds() if thresholds is None else thresholds
    results = tuple(
        _evaluate_one(
            model,
            retrievers[model],
            cases,
            k,
            clock,
            runtime_artifact_sha256[model],
        )
        for model in sorted(retrievers)
    )
    digest = golden_digest or _cases_digest(cases)
    winner, reason = _choose_winner(results, thresholds)
    coordinates = tuple(candidate_coordinates[model] for model in sorted(candidate_coordinates))
    return EmbeddingRecommendationArtifact(
        golden_sha256=digest,
        k=k,
        thresholds=thresholds,
        results=results,
        candidate_coordinates=coordinates,
        evaluated_at=evaluated_at,
        recommended_model=winner,
        reason=reason,
    )


def _evaluate_one(
    model: str,
    retrieve: VectorRetriever,
    cases: Sequence[VectorEvalCase],
    k: int,
    clock: Callable[[], float],
    runtime_artifact_sha256: str,
) -> CandidateMetrics:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    latencies: list[float] = []
    nonempty = 0
    for case in cases:
        started = clock()
        candidates = list(retrieve(case, k))
        elapsed = max(0.0, clock() - started)
        latencies.append(elapsed * 1000.0)
        ids: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.chunk_id not in seen:
                seen.add(candidate.chunk_id)
                ids.append(candidate.chunk_id)
            if len(ids) == k:
                break
        if ids:
            nonempty += 1
        relevant = set(case.relevant_chunk_ids)
        hits = [chunk_id in relevant for chunk_id in ids]
        recalls.append(sum(hits) / len(relevant))
        first = next((rank for rank, hit in enumerate(hits, start=1) if hit), None)
        reciprocal_ranks.append(0.0 if first is None else 1.0 / first)
        ndcgs.append(_ndcg(hits, len(relevant), k))
    count = len(cases)
    return CandidateMetrics(
        model=model,
        case_count=count,
        recall_at_k=sum(recalls) / count,
        mrr=sum(reciprocal_ranks) / count,
        ndcg=sum(ndcgs) / count,
        mean_latency_ms=sum(latencies) / count,
        coverage=nonempty / count,
        runtime_artifact_sha256=runtime_artifact_sha256,
    )


def _ndcg(hits: Sequence[bool], relevant_count: int, k: int) -> float:
    dcg = sum(1.0 / math.log2(rank + 1) for rank, hit in enumerate(hits, start=1) if hit)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(k, relevant_count) + 1))
    return 0.0 if ideal == 0 else dcg / ideal


def _choose_winner(
    results: Sequence[CandidateMetrics], thresholds: EvalThresholds
) -> tuple[str | None, str]:
    if not results:
        return None, "no candidate results"
    if any(result.case_count < thresholds.minimum_cases for result in results):
        return None, "minimum_cases not met for every required candidate"
    eligible = [
        result
        for result in results
        if result.recall_at_k >= thresholds.min_recall_at_k
        and result.mrr >= thresholds.min_mrr
        and result.ndcg >= thresholds.min_ndcg
        and result.mean_latency_ms <= thresholds.max_mean_latency_ms
    ]
    if not eligible:
        return None, "no candidate met retrieval and latency thresholds"
    ordered = sorted(
        eligible, key=lambda item: (-item.ndcg, -item.mrr, -item.recall_at_k, item.model)
    )
    if len(ordered) > 1:
        top, second = ordered[0], ordered[1]
        if abs(top.ndcg - second.ndcg) <= thresholds.parity_tolerance:
            return None, "eligible candidates remain within configured parity tolerance"
    return ordered[
        0
    ].model, "clear eligible winner; routing remains unchanged pending owner approval"


def _cases_digest(cases: Sequence[VectorEvalCase]) -> str:
    payload = [case.model_dump(mode="json") for case in cases]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
