"""Tests for src/allocation/recommendation_artifact.py — the P0.4a governed
generate + persist pipeline (PRD §7.4/§10).

``build_frontier`` is monkeypatched to a fixed ``DeterministicFrontier`` (this
module's own tests, tests/test_recommendation_frontier.py, already cover the
deterministic layer) so these tests exercise only the governed-selection
seam: the LLM call, schema + frontier validation, the corrective retry, the
deterministic fallback, and persistence. ``call_llm_structured`` is
monkeypatched at the module seam per the repo's never-spend convention — no
real LLM call anywhere in this file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

import allocation.recommendation_artifact as ra
from allocation.recommendation import (
    DeterministicFrontier,
    FrontierPlan,
    PlanAllocation,
)
from llm.cli import LLMBudgetExceeded, LLMSetupError

_CASH = 10_000.0


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER,
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        );
        CREATE TABLE owner_profile_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            narrative TEXT NOT NULL,
            provenance TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            affirmed_at TEXT,
            review_horizon_days INTEGER,
            source_detail TEXT,
            created_at TEXT NOT NULL,
            is_latest INTEGER NOT NULL DEFAULT 1,
            superseded_at TEXT,
            superseded_by_id INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _frontier(*, empty: bool = False) -> DeterministicFrontier:
    if empty:
        return DeterministicFrontier(
            as_of="2026-07-20T00:00:00",
            cash_usd=_CASH,
            plans=(
                FrontierPlan(
                    kind="retain_cash",
                    allocations=(),
                    cash_retained_usd=_CASH,
                    rationale_facts=(
                        "no eligible security clears the decision-ready evidence gate",
                    ),
                ),
            ),
            ineligible=(),
            eligible_tickers=(),
            input_sha="empty_sha",
            source_freshness={},
            degraded=(),
        )
    balanced = FrontierPlan(
        kind="balanced",
        allocations=(
            PlanAllocation(ticker="NU", dollars=6000.0, resulting_weight_pct=5.5, zone="ordinary"),
        ),
        cash_retained_usd=4000.0,
        rationale_facts=("NU has the best blended next-dollar score: +1.20",),
    )
    retain = FrontierPlan(
        kind="retain_cash",
        allocations=(),
        cash_retained_usd=_CASH,
        rationale_facts=("cash always retains the option to wait",),
    )
    return DeterministicFrontier(
        as_of="2026-07-20T00:00:00",
        cash_usd=_CASH,
        plans=(balanced, retain),
        ineligible=(("META", ("no usable DCF on file",)),),
        eligible_tickers=("NU", "WIX"),
        input_sha="fixed_sha_1",
        source_freshness={"dcf": "2026-07-20", "weights": "2026-07-15"},
        degraded=(),
    )


def _valid_payload() -> dict[str, object]:
    return {
        "status": "deploy_partial",
        "preferred_plan": {
            "allocations": [
                {
                    "ticker": "NU",
                    "dollars": 6000.0,
                    "pct_of_cash": 60.0,
                    "resulting_weight_pct": 5.5,
                    "zone": "ordinary",
                }
            ],
            "cash_retained_usd": 4000.0,
        },
        "best_alternative": None,
        "best_diversifier": None,
        "central_hypothesis": "NU has the best blended next-dollar score right now.",
        "personalization_why": "This deploys 60% of your new cash into the top-ranked name.",
        "supporting_evidence": ["NU has the best blended next-dollar score: +1.20"],
        "main_unknowns": ["how NU's next print reads on credit quality"],
        "disconfirming_evidence": ["a weak macro print could compress the multiple further"],
        "scenario_reasoning": "base case assumes stable credit trends",
        "confidence_verbal": "moderate",
        "confidence_basis": "The main reason I could be wrong is a macro shock hitting credit names.",
        "followup_research": [],
        "frontier_plan_ids": ["balanced"],
        "source_refs": ["dcf"],
        "risk_snapshot_ref": None,
    }


def _fake_build_frontier(*_a: object, **_kw: object) -> DeterministicFrontier:
    return _frontier()


def _fake_build_frontier_empty(*_a: object, **_kw: object) -> DeterministicFrontier:
    return _frontier(empty=True)


@pytest.fixture(autouse=True)
def _patch_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ra, "build_frontier", _fake_build_frontier)


def test_valid_llm_output_persists_as_llm_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    calls: list[str] = []

    def fake_call(prompt: str, **kw: object) -> object:
        calls.append(prompt)
        return _valid_payload()

    monkeypatch.setattr(ra, "call_llm_structured", fake_call)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "llm"
    assert result.artifact_id is not None
    assert len(calls) == 1
    assert result.recommendation.preferred_plan.allocations[0].ticker == "NU"


def test_ineligible_ticker_retries_then_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    calls: list[str] = []

    def fake_call(prompt: str, **kw: object) -> object:
        calls.append(prompt)
        bad = _valid_payload()
        bad["preferred_plan"] = {
            "allocations": [
                {
                    "ticker": "TSLA",  # not in the frontier's eligible/held set
                    "dollars": 6000.0,
                    "pct_of_cash": 60.0,
                    "resulting_weight_pct": 5.5,
                    "zone": "ordinary",
                }
            ],
            "cash_retained_usd": 4000.0,
        }
        return bad

    monkeypatch.setattr(ra, "call_llm_structured", fake_call)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "deterministic_fallback"
    assert len(calls) == 2  # one corrective retry, still bad
    assert any("rejected" in r for r in result.degraded_reasons)
    assert result.recommendation.preferred_plan.allocations[0].ticker == "NU"


def test_allocations_dont_sum_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _make_db(tmp_path)

    def fake_call(prompt: str, **kw: object) -> object:
        bad = _valid_payload()
        preferred_plan = cast("dict[str, object]", bad["preferred_plan"])
        preferred_plan["cash_retained_usd"] = 999.0  # 6000 + 999 != 10000
        return bad

    monkeypatch.setattr(ra, "call_llm_structured", fake_call)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "deterministic_fallback"


def test_numeric_probability_without_base_rate_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)

    def fake_call(prompt: str, **kw: object) -> object:
        bad = _valid_payload()
        bad["confidence_basis"] = "There's a 70% probability this play works out."
        return bad

    monkeypatch.setattr(ra, "call_llm_structured", fake_call)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "deterministic_fallback"


def test_budget_exceeded_falls_back_with_degraded_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)

    def raise_budget(prompt: str, **kw: object) -> object:
        raise LLMBudgetExceeded("over cap")

    monkeypatch.setattr(ra, "call_llm_structured", raise_budget)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "deterministic_fallback"
    assert any("budget" in r.lower() for r in result.degraded_reasons)
    assert result.artifact_id is not None


def test_hard_stop_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _make_db(tmp_path)

    def raise_setup(prompt: str, **kw: object) -> object:
        raise LLMSetupError("claude CLI not found")

    monkeypatch.setattr(ra, "call_llm_structured", raise_setup)

    with pytest.raises(LLMSetupError):
        ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)


def test_transient_failure_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _make_db(tmp_path)

    def raise_transient(prompt: str, **kw: object) -> object:
        raise TimeoutError("subprocess timed out")

    monkeypatch.setattr(ra, "call_llm_structured", raise_transient)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "deterministic_fallback"
    assert any("transient" in r.lower() for r in result.degraded_reasons)


def test_empty_frontier_skips_llm_entirely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _make_db(tmp_path)
    monkeypatch.setattr(ra, "build_frontier", _fake_build_frontier_empty)

    def fail_if_called(prompt: str, **kw: object) -> object:
        raise AssertionError("LLM must not be called for an empty frontier")

    monkeypatch.setattr(ra, "call_llm_structured", fail_if_called)

    result = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert result.selection_mode == "deterministic_fallback"
    assert result.recommendation.status == "retain_all"
    assert result.artifact_id is not None


def test_artifact_idempotency_same_frontier_sha_is_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)

    def fake_call(prompt: str, **kw: object) -> object:
        return _valid_payload()

    monkeypatch.setattr(ra, "call_llm_structured", fake_call)

    first = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)
    second = ra.generate_recommendation(db_path, tmp_path, cash_usd=_CASH)

    assert first.artifact_id == second.artifact_id
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_artifacts WHERE purpose='incremental_dollar_recommendation'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
