"""Held-out discipline in execution/backtest_prompt_candidate.py: case loading
respects the train/test split, and the combined verdict flags a candidate that
wins train but not the held-out test.

No LLM calls — only the split filter and the pure verdict function.
"""

from __future__ import annotations

import json
from pathlib import Path

from execution.backtest_prompt_candidate import (
    OVERFIT_SUSPECTED,
    BacktestResult,
    holdout_verdict,
    load_historical_cases,
)
from llm.prompt_reflect import case_group_key, case_split
from llm.prompt_registry import PromptTemplate

_TEMPLATE = PromptTemplate(
    template_id="t.backtest",
    body="Analyze {ticker} now.",
    variables=("ticker",),
)


def _write_captures(capture_dir: Path, tickers: list[str]) -> None:
    lines = [
        json.dumps(
            {
                "purpose": "bear_case",
                "prompt": f"Analyze {t} now.",
                "response": f"resp {t}",
                "prompt_sha256": f"{i:064x}",
                "ticker": t,
            }
        )
        for i, t in enumerate(tickers)
    ]
    (capture_dir / "capture_2026-09-01.jsonl").write_text("\n".join(lines), encoding="utf-8")


def test_loader_returns_only_the_requested_split(tmp_path: Path) -> None:
    tickers = [f"T{i}" for i in range(40)]
    _write_captures(tmp_path, tickers)

    train, _ = load_historical_cases(tmp_path, "bear_case", _TEMPLATE, limit=100, split="train")
    test, _ = load_historical_cases(tmp_path, "bear_case", _TEMPLATE, limit=100, split="test")
    every, _ = load_historical_cases(tmp_path, "bear_case", _TEMPLATE, limit=100)

    assert train and test
    assert {c.ticker for c in train}.isdisjoint({c.ticker for c in test})
    assert len(train) + len(test) == len(every) == len(tickers)
    for case in test:
        assert case_split("bear_case", case_group_key(case.ticker, case.prompt_sha)) == "test"
    assert all(c.variables == {"ticker": c.ticker} for c in every)


def test_limit_applies_within_the_split(tmp_path: Path) -> None:
    _write_captures(tmp_path, [f"T{i}" for i in range(40)])
    test, _ = load_historical_cases(tmp_path, "bear_case", _TEMPLATE, limit=3, split="test")
    assert len(test) == 3


def _result(verdict: str, wins: int, losses: int, ties: int = 0) -> BacktestResult:
    return BacktestResult(
        purpose="bear_case",
        template_id="t",
        baseline_version="v1",
        candidate_version="v2",
        n_cases=wins + losses + ties,
        candidate_wins=wins,
        baseline_wins=losses,
        ties=ties,
        judge_agreement=1.0,
        n_candidate_errors=0,
        n_baseline_errors=0,
        n_judge_errors=0,
        candidate_mean_output_chars=0.0,
        baseline_mean_output_chars=0.0,
        verdict=verdict,
        reason="r",
    )


def test_train_win_without_test_win_is_overfitting() -> None:
    verdict, reason = holdout_verdict(
        _result("CANDIDATE_BETTER", 7, 1), _result("INCONCLUSIVE", 3, 3, 2)
    )
    assert verdict == OVERFIT_SUSPECTED
    assert "revert" in reason


def test_held_out_win_is_the_only_acceptance() -> None:
    verdict, _ = holdout_verdict(
        _result("CANDIDATE_BETTER", 7, 1), _result("CANDIDATE_BETTER", 6, 1)
    )
    assert verdict == "CANDIDATE_BETTER"
    verdict, _ = holdout_verdict(_result("INCONCLUSIVE", 3, 3), _result("BASELINE_BETTER", 1, 5))
    assert verdict == "BASELINE_BETTER"


def test_degraded_test_split_is_never_relabelled() -> None:
    """An outage on the test side measured nothing; it must not read as
    overfitting or as a win."""
    verdict, _ = holdout_verdict(_result("CANDIDATE_BETTER", 7, 1), _result("JUDGE_DEGRADED", 0, 0))
    assert verdict == "JUDGE_DEGRADED"
