"""Grading uses one explicit retained database, including internal LLM bookkeeping."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import bear_case_grader
import db
from db_paths import db_path_context, resolve_db_path
from execution import grade_bear_cases
from predictions_store import Prediction


@pytest.mark.parametrize("operation", ["materialize", "grade"])
def test_checkout_default_is_rejected_before_grading(
    operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout_default = Path(bear_case_grader.__file__).resolve().parents[1] / "data/portfolio.db"
    monkeypatch.setattr(db, "DB_PATH", str(checkout_default))
    with pytest.raises(RuntimeError, match="checkout-default"):
        if operation == "materialize":
            bear_case_grader.materialize_predictions(ticker="TEST", repo_root=tmp_path)
        else:
            bear_case_grader.grade_due_predictions(ticker="TEST", repo_root=tmp_path)
    assert not (tmp_path / "data/portfolio.db").exists()


def test_explicit_database_is_used_without_creating_checkout_state(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    target = migrated_db(tmp_path / "explicit.db")
    assert (
        bear_case_grader.materialize_predictions(ticker="TEST", repo_root=tmp_path, db_path=target)
        == 0
    )
    assert bear_case_grader.grade_due_predictions(
        ticker="TEST", repo_root=tmp_path, db_path=target
    ) == {"met": 0, "missed": 0, "mixed": 0, "unfalsifiable": 0}
    assert not (tmp_path / "data/portfolio.db").exists()


def test_explicit_database_scopes_and_restores_grading_bookkeeping(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = migrated_db(tmp_path / "explicit.db")
    outer = tmp_path / "outer.db"
    now = datetime.now(UTC)
    prediction = Prediction(
        id=9001,
        ticker="TEST",
        source_kind="llm_bear_case",
        prediction_md="Fixture hypothesis",
        made_at=now - timedelta(days=200),
        target_period=now - timedelta(days=100),
    )
    observed: list[Path | None] = []

    def history(**kwargs: object) -> list[Prediction]:
        assert kwargs["db_path"] == target
        return [prediction]

    def corpus(ticker: str, repo_root: Path, *, db_path: Path) -> dict[str, str]:
        assert ticker == "TEST" and repo_root == tmp_path and db_path == target
        return {}

    def grade(**kwargs: object) -> None:
        observed.append(resolve_db_path(None))
        assert kwargs["pred"] == prediction
        raise RuntimeError("fixture interrupted grading")

    monkeypatch.setattr(bear_case_grader, "prediction_history", history)
    monkeypatch.setattr(bear_case_grader, "_load_grading_corpus", corpus)
    monkeypatch.setattr(bear_case_grader, "_grade_one_prediction", grade)
    with db_path_context(outer):
        with pytest.raises(RuntimeError, match="fixture interrupted"):
            bear_case_grader.grade_due_predictions(
                ticker="TEST", repo_root=tmp_path, db_path=target
            )
        assert resolve_db_path(None) == outer
    assert observed == [target]


def test_cli_threads_explicit_database_to_both_stages(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = migrated_db(tmp_path / "explicit.db")
    calls: list[str] = []

    def materialize(**kwargs: object) -> int:
        assert kwargs["db_path"] == target and kwargs["ticker"] == "TEST"
        calls.append("materialize")
        return 0

    def grade(**kwargs: object) -> dict[str, int]:
        assert kwargs["db_path"] == target and kwargs["ticker"] == "TEST"
        calls.append("grade")
        return {"met": 0, "missed": 0, "mixed": 0, "unfalsifiable": 0}

    monkeypatch.setattr(grade_bear_cases, "materialize_predictions", materialize)
    monkeypatch.setattr(grade_bear_cases, "grade_due_predictions", grade)
    assert (
        grade_bear_cases.main(
            ["--ticker", "TEST", "--repo-root", str(tmp_path), "--db", str(target)]
        )
        == 0
    )
    assert calls == ["materialize", "grade"]
    assert not (tmp_path / "data/portfolio.db").exists()
