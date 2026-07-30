from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from search.embedding_eval import (
    DEFAULT_CANDIDATES,
    CandidateEvaluationCoordinate,
    EvalThresholds,
    VectorEvalCase,
    evaluate_embedding_candidates,
    load_embedding_golden,
)
from search.grounded import VectorCandidate

RUNTIME_HASHES = {
    model: f"{ordinal:x}" * 64 for ordinal, model in enumerate(DEFAULT_CANDIDATES, start=1)
}
EVALUATED_AT = datetime(2026, 7, 29, tzinfo=UTC)


def _coordinates() -> dict[str, CandidateEvaluationCoordinate]:
    return {
        model: CandidateEvaluationCoordinate(
            model=model,
            index_run_id=f"run-{ordinal}",
            manifest_id="manifest-1",
            projection_seal_id=f"seal-{ordinal}",
            projection_records_sha256=f"{ordinal + 2:x}" * 64,
            artifact_set_sha256=f"{ordinal + 4:x}" * 64,
            config_sha256=f"{ordinal + 6:x}" * 64,
            chunk_count=2,
            chunk_set_sha256="e" * 64,
            runtime_registration_id=f"runtime-{ordinal}",
            runtime_artifact_sha256=RUNTIME_HASHES[model],
            sealed_at=EVALUATED_AT,
        )
        for ordinal, model in enumerate(DEFAULT_CANDIDATES, start=1)
    }


def _golden(path: Path) -> Path:
    payload = {
        "purpose": "evidence_vector_retrieval",
        "cases": [
            {"case_id": "revenue", "query": "revenue growth", "relevant_chunk_ids": ["c1"]},
            {"case_id": "margin", "query": "margin", "relevant_chunk_ids": ["c2"]},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_closed_golden_and_metrics_select_clear_winner(tmp_path: Path) -> None:
    cases = load_embedding_golden(_golden(tmp_path / "golden.json"))
    assert [case.case_id for case in cases] == ["revenue", "margin"]

    def winner(case: VectorEvalCase, limit: int) -> list[VectorCandidate]:
        return [VectorCandidate("c1" if "revenue" in case.query else "c2", 1.0, "run")]

    def loser(case: VectorEvalCase, limit: int) -> list[VectorCandidate]:
        return [VectorCandidate("wrong", 1.0, "run")]

    artifact = evaluate_embedding_candidates(
        cases,
        {
            DEFAULT_CANDIDATES[0]: winner,
            DEFAULT_CANDIDATES[1]: loser,
        },
        thresholds=EvalThresholds(minimum_cases=2, min_recall_at_k=0.5, min_mrr=0.5, min_ndcg=0.5),
        clock=lambda: 1.0,
        runtime_artifact_sha256=RUNTIME_HASHES,
        candidate_coordinates=_coordinates(),
        evaluated_at=EVALUATED_AT,
    )
    assert artifact.recommended_model == DEFAULT_CANDIDATES[0]
    metrics = {result.model: result for result in artifact.results}
    assert metrics[DEFAULT_CANDIDATES[0]].recall_at_k == 1.0
    assert metrics[DEFAULT_CANDIDATES[1]].coverage == 1.0


def test_eval_refuses_default_without_minimum_sample_or_clear_parity() -> None:
    case = VectorEvalCase(case_id="one", query="q", relevant_chunk_ids=("c",))

    def same(case: VectorEvalCase, limit: int) -> list[VectorCandidate]:
        return [VectorCandidate("c", 1.0, "run")]

    artifact = evaluate_embedding_candidates(
        [case],
        {model: same for model in DEFAULT_CANDIDATES},
        thresholds=EvalThresholds(minimum_cases=2),
        clock=lambda: 1.0,
        runtime_artifact_sha256=RUNTIME_HASHES,
        candidate_coordinates=_coordinates(),
        evaluated_at=EVALUATED_AT,
    )
    assert artifact.recommended_model is None
    assert "minimum_cases" in artifact.reason


def test_eval_deduplicates_hits_and_uses_full_ideal_ranking_depth() -> None:
    case = VectorEvalCase(
        case_id="multiple",
        query="revenue and margin",
        relevant_chunk_ids=("revenue", "margin"),
    )

    def duplicates(case: VectorEvalCase, limit: int) -> list[VectorCandidate]:
        return [
            VectorCandidate("revenue", 1.0, "run"),
            VectorCandidate("revenue", 0.9, "run"),
        ]

    artifact = evaluate_embedding_candidates(
        [case, case],
        {model: duplicates for model in DEFAULT_CANDIDATES},
        k=2,
        thresholds=EvalThresholds(
            minimum_cases=2,
            min_recall_at_k=0,
            min_mrr=0,
            min_ndcg=0,
        ),
        clock=lambda: 1.0,
        runtime_artifact_sha256=RUNTIME_HASHES,
        candidate_coordinates=_coordinates(),
        evaluated_at=EVALUATED_AT,
    )
    result = artifact.results[0]
    assert result.recall_at_k == 0.5
    assert 0 < result.ndcg < 1


def test_golden_rejects_open_or_malformed_cases(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"purpose":"wrong","cases":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="purpose"):
        load_embedding_golden(path)


def test_eval_rejects_candidates_outside_governed_policy() -> None:
    case = VectorEvalCase(case_id="one", query="q", relevant_chunk_ids=("c",))

    def retrieve(case: VectorEvalCase, limit: int) -> list[VectorCandidate]:
        return [VectorCandidate("c", 1.0, "run")]

    extra_model = "unreviewed/embedding-model"
    coordinates = _coordinates()
    coordinates[extra_model] = coordinates[DEFAULT_CANDIDATES[0]].model_copy(
        update={
            "model": extra_model,
            "index_run_id": "run-extra",
            "projection_seal_id": "seal-extra",
            "runtime_registration_id": "runtime-extra",
            "runtime_artifact_sha256": "f" * 64,
        }
    )
    with pytest.raises(ValueError, match="governed candidate policy"):
        evaluate_embedding_candidates(
            [case],
            {**{model: retrieve for model in DEFAULT_CANDIDATES}, extra_model: retrieve},
            thresholds=EvalThresholds(minimum_cases=1),
            runtime_artifact_sha256={**RUNTIME_HASHES, extra_model: "f" * 64},
            candidate_coordinates=coordinates,
            evaluated_at=EVALUATED_AT,
        )


def test_artifact_rejects_reused_runtime_or_projection_coordinates() -> None:
    coordinates = _coordinates()
    first, second = sorted(coordinates)
    duplicated = coordinates[second].model_copy(
        update={
            "runtime_registration_id": coordinates[first].runtime_registration_id,
            "projection_seal_id": coordinates[first].projection_seal_id,
        }
    )
    with pytest.raises(ValueError, match="runtime and projection coordinates"):
        evaluate_embedding_candidates(
            [VectorEvalCase(case_id="one", query="q", relevant_chunk_ids=("c",))],
            {
                model: (lambda _case, _limit: [VectorCandidate("c", 1.0, "run")])
                for model in DEFAULT_CANDIDATES
            },
            thresholds=EvalThresholds(minimum_cases=1),
            runtime_artifact_sha256=RUNTIME_HASHES,
            candidate_coordinates={**coordinates, second: duplicated},
            evaluated_at=EVALUATED_AT,
        )
