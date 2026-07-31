"""Replay-audit coverage for the paid-down LLM eval debt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.capture_quality import (
    _capture_paths,
    load_capture_quality_corpus,
    run_capture_quality_eval,
)
from evals.capture_quality_specs import CAPTURE_QUALITY_PURPOSES, CAPTURE_QUALITY_SPECS
from evals.coverage import GRANDFATHERED_UNCOVERED_PURPOSES, eval_coverage, eval_coverage_gate
from evals.harness import persist_summary
from llm.capture import capture_purpose_suffix


def _capture(
    purpose: str,
    *,
    prompt: str = "SOURCE: Management committed to launch by Q4.",
    response: str = '{"commitment": "Launch by Q4"}',
    captured_at: str = "2026-07-26T12:00:00",
) -> dict[str, object]:
    return {
        "captured_at": captured_at,
        "purpose": purpose,
        "prompt_version": "v1",
        "ticker": "NU",
        "model": "test-model",
        "backend": "claude",
        "prompt": prompt,
        "response": response,
        "prompt_sha256": f"sha-{purpose}-{prompt}",
    }


def _write_capture(repo: Path, rows: list[dict[str, object] | str]) -> None:
    directory = repo / "data" / "llm_capture"
    directory.mkdir(parents=True)
    path = directory / "capture_2026-07-26.jsonl"
    path.write_text(
        "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_registered_eval_mode_debt_is_fully_paid_down(tmp_path: Path) -> None:
    rows = eval_coverage(tmp_path / "missing.db")
    result = eval_coverage_gate(rows)

    assert not GRANDFATHERED_UNCOVERED_PURPOSES
    assert result.passed
    assert not result.new_uncovered
    registered_rows = [row for row in rows if row.model_pinned]
    assert registered_rows
    assert all(row.covered for row in registered_rows)


def test_specs_are_prioritized_and_performance_bounded() -> None:
    assert len(CAPTURE_QUALITY_SPECS) == 76  # 74 legacy purposes + lens:* + pre_earnings_brief.
    assert CAPTURE_QUALITY_SPECS[CAPTURE_QUALITY_PURPOSES[0]].priority == "P0"
    assert CAPTURE_QUALITY_SPECS[CAPTURE_QUALITY_PURPOSES[0]].traffic_tier == "hot"
    assert CAPTURE_QUALITY_SPECS["saydo_commitment_extract"].priority == "P0"
    assert CAPTURE_QUALITY_SPECS["saydo_commitment_extract"].traffic_tier == "hot"
    assert CAPTURE_QUALITY_SPECS["saydo_commitment_extract"].default_limit == 20
    assert CAPTURE_QUALITY_SPECS["advisor_swap_check"].priority == "P0"
    assert CAPTURE_QUALITY_SPECS["pressure_test_thesis"].priority == "P0"
    assert CAPTURE_QUALITY_SPECS["annual_letter"].priority == "P2"
    assert CAPTURE_QUALITY_SPECS["annual_letter"].default_limit == 5
    assert {spec.priority for spec in CAPTURE_QUALITY_SPECS.values()} == {"P0", "P1", "P2"}


def test_capture_loader_deduplicates_and_rolls_up_lenses(tmp_path: Path) -> None:
    duplicate = _capture("saydo_commitment_extract")
    _write_capture(
        tmp_path,
        [
            "{bad-json",
            duplicate,
            duplicate,
            _capture("lens:five_min_reread", prompt="lens-one"),
            _capture("lens:macro:rates_up", prompt="lens-two"),
        ],
    )

    saydo = load_capture_quality_corpus(tmp_path, "saydo_commitment_extract")
    lenses = load_capture_quality_corpus(tmp_path, "lens:*")

    assert len(saydo) == 1
    assert "PROMPT AND SOURCE MATERIAL" in saydo[0].content
    assert "RESPONSE UNDER AUDIT" in saydo[0].content
    assert len(lenses) == 2


def test_capture_loader_merges_pid_shards_by_timestamp(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "llm_capture"
    directory.mkdir(parents=True)
    (directory / "capture_2026-07-26_999.jsonl").write_text(
        json.dumps(_capture("annual_letter", prompt="older", captured_at="2026-07-26T12:00:00"))
        + "\n",
        encoding="utf-8",
    )
    (directory / "capture_2026-07-26_111.jsonl").write_text(
        json.dumps(_capture("annual_letter", prompt="newer", captured_at="2026-07-26T12:59:00"))
        + "\n",
        encoding="utf-8",
    )

    items = load_capture_quality_corpus(tmp_path, "annual_letter", limit=1)

    assert len(items) == 1
    assert "newer" in items[0].content


def test_capture_path_scan_is_partitioned_for_exact_purpose(tmp_path: Path) -> None:
    target = tmp_path / (f"capture_2026-07-26_1_p{capture_purpose_suffix('annual_letter')}.jsonl")
    unrelated = tmp_path / (
        f"capture_2026-07-26_1_p{capture_purpose_suffix('valuation_basis')}.jsonl"
    )
    legacy = tmp_path / "capture_2026-07-25_1.jsonl"
    for path in (target, unrelated, legacy):
        path.write_text("", encoding="utf-8")

    paths = set(_capture_paths(tmp_path, "annual_letter"))

    assert paths == {target, legacy}
    assert capture_purpose_suffix("lens:five_min_reread") == capture_purpose_suffix("lens:*")


def test_capture_loader_filters_age_before_deduplication(tmp_path: Path) -> None:
    _write_capture(
        tmp_path,
        [
            _capture("annual_letter", prompt="same", captured_at="2026-07-26T12:00:00"),
            _capture("annual_letter", prompt="same", captured_at="2020-01-01T12:00:00"),
        ],
    )

    items = load_capture_quality_corpus(tmp_path, "annual_letter", since_days=7)

    assert len(items) == 1
    assert items[0].produced_at is not None
    assert items[0].produced_at.year == 2026


def test_capture_loader_selects_one_exact_provenance_cohort(tmp_path: Path) -> None:
    older = _capture("annual_letter", prompt="old-model", captured_at="2026-07-26T12:00:00")
    older["model"] = "model-a"
    newer = _capture("annual_letter", prompt="new-model", captured_at="2026-07-26T12:01:00")
    newer["model"] = "model-b"
    _write_capture(tmp_path, [older, newer])

    items = load_capture_quality_corpus(
        tmp_path,
        "annual_letter",
        required_prompt_version="v1",
    )

    assert len(items) == 1
    assert items[0].model == "model-b"
    assert items[0].backend == "claude"


def test_capture_runner_uses_judge_only_and_bounded_hot_default(tmp_path: Path) -> None:
    _write_capture(
        tmp_path,
        [
            _capture(
                "saydo_commitment_extract",
                prompt=f"source-{index}",
                captured_at=f"2026-07-26T12:{index:02d}:00",
            )
            for index in range(25)
        ],
    )
    calls: list[dict[str, object]] = []

    def fake_judge(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return json.dumps(
            {
                "facet_scores": {
                    "source_fidelity": 1.0,
                    "completeness": 1.0,
                    "field_precision": 1.0,
                    "no_fabrication": 1.0,
                },
                "rationale": "All extracted fields are directly supported.",
            }
        )

    summary = run_capture_quality_eval(
        "saydo_commitment_extract",
        repo_root=tmp_path,
        code_root=tmp_path,
        caller=fake_judge,
    )

    assert summary.mode == "capture_audit"
    assert summary.n_cases == 20
    assert summary.n_pass == 20
    assert summary.model == "test-model"
    assert "priority=P0" in (summary.notes or "")
    assert calls and all(call["purpose"] == "eval_judge" for call in calls)
    assert all(
        case.prompt_text is None
        and case.response_text is None
        and case.judge_verdict is None
        and case.judge_rationale is None
        for case in summary.cases
    )


def test_sensitive_capture_text_never_reaches_logs_or_summary(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE_CAPTURE_SENTINEL_7f89"
    _write_capture(
        tmp_path,
        [
            _capture(
                "annual_letter",
                prompt=f"source {sentinel}",
                response=f"response {sentinel}",
            )
        ],
    )

    summary = run_capture_quality_eval(
        "annual_letter",
        repo_root=tmp_path,
        code_root=tmp_path,
        caller=lambda *_args, **_kwargs: f"unparseable echo {sentinel}",
    )

    assert sentinel not in caplog.text
    assert sentinel not in json.dumps(summary.to_json_dict())


def test_capture_persistence_strips_private_exchange_defense_in_depth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_capture(tmp_path, [_capture("annual_letter")])

    def fake_judge(_prompt: str, **_kwargs: object) -> str:
        return json.dumps(
            {
                "facet_scores": {
                    "source_fidelity": 1.0,
                    "prioritization": 1.0,
                    "balance": 1.0,
                    "decision_usefulness": 1.0,
                },
                "rationale": "Grounded.",
            }
        )

    summary = run_capture_quality_eval(
        "annual_letter",
        repo_root=tmp_path,
        code_root=tmp_path,
        caller=fake_judge,
    )
    summary.cases[0].prompt_text = "PRIVATE SOURCE"
    summary.cases[0].response_text = "PRIVATE RESPONSE"
    summary.cases[0].judge_verdict = "PRIVATE VERDICT"
    summary.cases[0].judge_rationale = "PRIVATE RATIONALE"

    from evals import harness, store

    def fake_write_run(observed, *, db_path: Path) -> int:
        assert db_path == tmp_path / "portfolio.db"
        assert observed.cases[0].prompt_text is None
        assert observed.cases[0].response_text is None
        assert observed.cases[0].judge_verdict is None
        assert observed.cases[0].judge_rationale is None
        return 7

    monkeypatch.setattr(store, "write_run", fake_write_run)
    monkeypatch.setattr(harness, "record_score", lambda *_args, **_kwargs: None)

    assert persist_summary(summary, db_path=tmp_path / "portfolio.db") == 7
