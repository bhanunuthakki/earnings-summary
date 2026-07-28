from __future__ import annotations

import json
from pathlib import Path

import pytest

from search.embedding_eval import (
    DEFAULT_CANDIDATES,
    EvalThresholds,
    VectorEvalCase,
    evaluate_embedding_candidates,
    load_embedding_golden,
)
from search.grounded import VectorCandidate

RUNTIME_HASHES = {
    model: f"{ordinal:x}" * 64 for ordinal, model in enumerate(DEFAULT_CANDIDATES, start=1)
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
    )
    result = artifact.results[0]
    assert result.recall_at_k == 0.5
    assert 0 < result.ndcg < 1


def test_golden_rejects_open_or_malformed_cases(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"purpose":"wrong","cases":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="purpose"):
        load_embedding_golden(path)
