"""Qualification cannot pass from a missing, duplicate or wrong hazard result."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.kpi_repair_judge import QualificationAnswer, QualificationDataset, qualify_repair_judge


def test_complete_case_coverage_and_wrong_answer_gate() -> None:
    dataset = QualificationDataset.model_validate_json(
        (Path(__file__).resolve().parents[1] / "evals/golden/kpi_repair_judge.json").read_bytes()
    )
    answers = tuple(
        QualificationAnswer(
            case_id=case.case_id,
            verdict=case.expected_verdict,
            rationale="Synthetic expected answer",
        )
        for case in dataset.cases
    )

    # Keep the closed purpose literal visible to static checking.
    def run(values: tuple[QualificationAnswer, ...]) -> object:
        return qualify_repair_judge(
            dataset,
            values,
            purpose="kpi_source_repair",
            model_id="synthetic-model",
            run_evidence_sha256="a" * 64,
            evaluated_at=datetime(2026, 10, 3, tzinfo=UTC),
        )

    assert run(answers)
    with pytest.raises(ValueError, match="exactly cover"):
        run(answers[:-1])
    with pytest.raises(ValueError, match="exactly cover"):
        run((*answers, answers[0]))
    with pytest.raises(ValueError, match="failed"):
        run((answers[0].model_copy(update={"verdict": "BLOCK"}), *answers[1:]))
