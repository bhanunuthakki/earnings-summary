"""PR 4 of directives/llm_evals_plan.md: classifier golden sets (mode A,
no judge), the eval-coverage report, the shared structured-output helper
(retry-with-feedback, loud on failure), and the hard-stop/silent-empty
fixes in the production classifier wrappers + risk-factor extractor.

All LLM calls are injected or monkeypatched — the suite never spends.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TypeVar

import pytest

import evals.coverage as coverage_module
import llm.structured as structured
from evals.coverage import (
    GRANDFATHERED_UNCOVERED_PURPOSES,
    META_PURPOSES,
    build_eval_run_receipt,
    eval_coverage,
    eval_coverage_gate,
    load_latest_eval_run_receipt,
    persist_eval_run_receipt,
    render_coverage_gate_text,
    render_coverage_text,
)
from evals.golden_classifiers import (
    CLASSIFIER_PURPOSES,
    ClassifierCase,
    grade_intake_classifier_case,
    grade_news_structuring_case,
    grade_transcript_metadata_case,
    load_intake_classifier_golden,
    load_news_structuring_golden,
    load_transcript_metadata_golden,
    run_classifier_eval,
    validate_news_row,
)
from evals.harness import EvalAbortError
from llm.cli import LLMBudgetExceeded
from llm.prompt_versions import registered_purposes
from llm.structured import StructuredParseError, call_llm_structured, parse_json_payload

_T = TypeVar("_T")


def test_eval_run_receipt_requires_graded_evidence_and_separates_errors(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    receipt = build_eval_run_receipt(
        ["INSUFFICIENT_FRAME", "INSUFFICIENT_DATA", "CANDIDATE_ERRORED", "UNKNOWN"],
        run_id="run-1",
        started_at=now,
        finished_at=now,
    )

    assert (receipt.attempted, receipt.graded, receipt.insufficient, receipt.errors) == (4, 0, 2, 2)
    assert receipt.status == "alert" and receipt.alert_count == 2

    path = persist_eval_run_receipt(tmp_path, receipt)
    assert path == tmp_path / "data" / "model_eval_runs" / "run-1.json"
    assert load_latest_eval_run_receipt(tmp_path) == receipt


def test_eval_run_receipt_passes_only_with_grade_and_zero_errors() -> None:
    now = datetime.now(UTC)
    receipt = build_eval_run_receipt(
        ["KEEP_INCUMBENT", "HOLD", "INSUFFICIENT_FRAME"],
        run_id="run-2",
        started_at=now,
        finished_at=now,
    )
    assert (receipt.graded, receipt.insufficient, receipt.errors) == (2, 1, 0)
    assert receipt.status == "passed" and receipt.alert_count == 0


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = PROJECT_ROOT / "evals" / "golden"


def _load_execution_module(name: str, alias: str) -> ModuleType:
    src = PROJECT_ROOT / "execution" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(alias, src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _returning(value: _T) -> Callable[..., _T]:
    """Typed constant-function factory (strict pyright rejects bare lambdas
    against Callable[..., X] targets)."""

    def fn(*_a: object, **_k: object) -> _T:
        return value

    return fn


# ----------------------------------------------------------------------------
# checked-in golden sets + wiring
# ----------------------------------------------------------------------------


def test_checked_in_classifier_golden_sets_load() -> None:
    tm = load_transcript_metadata_golden(GOLDEN_DIR / "transcript_metadata.json")
    assert len(tm) >= 4
    assert any(c.expected == "UNKNOWN" for c in tm)  # the negative case
    ic = load_intake_classifier_golden(GOLDEN_DIR / "intake_classifier.json")
    assert len(ic) >= 4
    from typing import cast

    doc_types = {str(cast("dict[str, object]", c.expected)["doc_type"]) for c in ic}
    assert "ir_event" in doc_types  # the non-quarterly shape is covered
    ns = load_news_structuring_golden(GOLDEN_DIR / "news_structuring.json")
    assert 1 <= len(ns) <= 8  # live web spend — the set stays small


def test_pr4_wiring_is_consistent() -> None:
    runner = _load_execution_module("run_llm_evals", "run_llm_evals_pr4_check")
    assert set(CLASSIFIER_PURPOSES) <= set(runner.GOLDEN_PURPOSES)
    from pipeline.evals_panel import RUNNABLE_PURPOSES

    assert set(RUNNABLE_PURPOSES) == set(runner.PURPOSES)
    assert set(CLASSIFIER_PURPOSES) <= registered_purposes()


def test_load_rejects_bad_golden(tmp_path: Path) -> None:
    bad = tmp_path / "g.json"
    bad.write_text(
        json.dumps(
            {
                "purpose": "transcript_metadata",
                "cases": [
                    {"id": "a", "text": "x", "expected": "NOT_A_FORMAT"},
                    {"id": "a", "text": "", "expected": "NU_Q1_2026"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_transcript_metadata_golden(bad)
    msg = str(exc.value)
    assert "duplicate id" in msg and "not TICKER_QX_YYYY" in msg and "missing/empty `text`" in msg


# ----------------------------------------------------------------------------
# per-purpose grading
# ----------------------------------------------------------------------------


def test_grade_transcript_metadata_exact_match() -> None:
    case = ClassifierCase("tm-x", {"text_snippet": "NVIDIA Q1 FY2026..."}, "NVDA_Q1_2026")
    ok = grade_transcript_metadata_case(case, fn=_returning("NVDA_Q1_2026"))
    assert ok.passed and ok.score == 1.0
    miss = grade_transcript_metadata_case(case, fn=_returning("NVDA_Q2_2026"))
    assert not miss.passed and miss.failure_stage == "mismatch"
    assert miss.judge_rationale is not None and "NVDA_Q2_2026" in miss.judge_rationale


def test_grade_transcript_metadata_hard_stop_aborts() -> None:
    case = ClassifierCase("tm-x", {"text_snippet": "..."}, "UNKNOWN")

    def capped(_t: str) -> str:
        raise LLMBudgetExceeded("over cap")

    with pytest.raises(EvalAbortError, match="hard stop"):
        grade_transcript_metadata_case(case, fn=capped)


def test_grade_intake_partial_credit() -> None:
    case = ClassifierCase(
        "ic-x",
        {"filename": "f.pdf", "text": "...", "hint": {}},
        {"ticker": "NU", "doc_type": "ir_press_release", "period_end": "2026-03-31"},
    )
    full_payload: dict[str, object] = {
        "ticker": "NU",
        "doc_type": "ir_press_release",
        "period_end": "2026-03-31",
        "confidence": 0.97,
        "reasoning": "clear",
    }
    full = grade_intake_classifier_case(case, fn=_returning(full_payload))
    assert full.passed and full.score == 1.0
    partial_payload: dict[str, object] = {
        "ticker": "NU",
        "doc_type": "ir_presentation",
        "period_end": "2026-03-31",
    }
    partial = grade_intake_classifier_case(case, fn=_returning(partial_payload))
    assert not partial.passed and partial.score == pytest.approx(2 / 3)
    assert partial.judge_rationale is not None and "doc_type" in partial.judge_rationale
    degraded = grade_intake_classifier_case(case, fn=_returning(None))
    assert not degraded.passed and degraded.score == 0.0 and degraded.failure_stage == "call"


def test_news_contract_validation() -> None:
    now = datetime(2026, 6, 11, 12, 0, 0)
    good = {
        "headline": "Nu launches X",
        "url": "https://reuters.com/a",
        "published_at": "2026-06-10 14:00:00",
        "published_tz": "UTC",
        "snippet": "s",
        "source": "Reuters",
    }
    assert validate_news_row(good, now=now) is None
    assert validate_news_row({**good, "url": "reuters.com/a"}, now=now) is not None
    assert validate_news_row({**good, "published_at": "2026-06-10T14:00:00Z"}, now=now) is not None
    assert validate_news_row({**good, "published_tz": "PST"}, now=now) is not None
    future = validate_news_row({**good, "published_at": "2026-06-20 14:00:00"}, now=now)
    assert future is not None and "future" in future
    assert validate_news_row({**good, "source": ""}, now=now) is not None
    assert validate_news_row("not an object", now=now) is not None


def test_grade_news_structuring_scores_fraction() -> None:
    case = ClassifierCase("ns-x", {"ticker": "NU", "news_days": 2}, None)
    good_row = {
        "headline": "h",
        "url": "https://x.com/a",
        "published_at": "2020-01-01 00:00:00",
        "published_tz": "UTC",
        "snippet": "s",
        "source": "X",
    }
    no_rows: list[object] = []
    empty = grade_news_structuring_case(case, fn=_returning(no_rows))
    assert empty.passed and empty.score == 1.0  # "no determinable news" is valid
    mixed_rows: list[object] = [good_row, {"headline": "no url"}]
    mixed = grade_news_structuring_case(case, fn=_returning(mixed_rows))
    assert not mixed.passed and mixed.score == 0.5
    assert mixed.failure_stage == "contract"
    assert mixed.actual_json is not None and "violations" in mixed.actual_json


def test_run_classifier_eval_end_to_end(tmp_path: Path) -> None:
    summary = run_classifier_eval(
        "transcript_metadata",
        golden_path=GOLDEN_DIR / "transcript_metadata.json",
        code_root=tmp_path,  # not a git repo -> git_sha None, still valid
        fn=_returning("NVDA_Q1_2026"),  # right for tm-001, wrong elsewhere
    )
    assert summary.mode == "live" and summary.judge_model is None
    assert summary.n_cases >= 4 and summary.n_pass == 1
    assert summary.golden_set_sha is not None
    with pytest.raises(ValueError, match="no classifier eval"):
        run_classifier_eval("bear_case", golden_path=GOLDEN_DIR / "x.json", code_root=tmp_path)


# ----------------------------------------------------------------------------
# the coverage report
# ----------------------------------------------------------------------------


def test_eval_coverage_finds_gaps_and_rolls_up_lenses(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE llm_calls (purpose TEXT, called_at TEXT)")
    rows = [
        ("viewspec_compile",),
        ("bear_case",),
        ("lens:five_min_reread",),
        ("lens:macro_scenario:rates_up",),
        ("some_orphan_purpose",),
    ]
    conn.executemany("INSERT INTO llm_calls (purpose) VALUES (?)", rows)
    conn.commit()
    conn.close()

    cov = {r.purpose: r for r in eval_coverage(db)}
    assert "golden" in cov["viewspec_compile"].modes
    assert "golden" in cov["transcript_metadata"].modes
    assert set(cov["bear_case"].modes) >= {"audit", "outcome"}
    assert cov["transcript_summary"].modes == ("audit",)
    assert cov["eval_judge"].modes == ("meta",)
    # The orphan purpose (observed in calls, no pin, no eval) is the headline.
    orphan = cov["some_orphan_purpose"]
    assert not orphan.covered and not orphan.model_pinned and orphan.observed_calls == 1
    # lens:* rolls up into one synthetic row.
    assert "lens:five_min_reread" not in cov
    assert cov["lens:*"].observed_calls == 2

    text = render_coverage_text(eval_coverage(db))
    assert "uncovered" in text and "some_orphan_purpose" in text and "NONE" in text
    # Uncovered rows sort before covered ones.
    assert text.index("some_orphan_purpose") < text.index("viewspec_compile")


def test_eval_coverage_without_db(tmp_path: Path) -> None:
    rows = eval_coverage(tmp_path / "missing.db")
    assert rows  # registry-derived universe still reports
    assert all(r.observed_calls == 0 for r in rows)
    assert {r.purpose for r in rows} >= META_PURPOSES


def test_eval_coverage_gate_accepts_only_the_explicit_existing_debt(
    tmp_path: Path,
) -> None:
    result = eval_coverage_gate(eval_coverage(tmp_path / "missing.db"))
    assert result.passed
    assert not result.new_uncovered
    assert not result.stale_grandfathered
    assert not GRANDFATHERED_UNCOVERED_PURPOSES
    assert "PASS" in render_coverage_gate_text(result)


def test_eval_coverage_gate_blocks_new_model_and_prompt_registrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        coverage_module.LLM_MODELS,
        "new_model_without_quality_eval",
        "test-model",
    )
    registered = coverage_module.registered_purposes()
    monkeypatch.setattr(
        coverage_module,
        "registered_purposes",
        lambda: registered | {"new_prompt_without_quality_eval"},
    )

    result = eval_coverage_gate(eval_coverage(tmp_path / "missing.db"))

    assert result.new_uncovered == (
        "new_model_without_quality_eval",
        "new_prompt_without_quality_eval",
    )
    assert not result.passed
    text = render_coverage_gate_text(result)
    assert "golden/audit/capture_audit/outcome/meta" in text
    assert "empty capture declaration alone is not a quality eval" in text


def test_eval_coverage_gate_has_no_exemption_when_capture_eval_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    purpose = "annual_letter"
    assert purpose not in GRANDFATHERED_UNCOVERED_PURPOSES
    monkeypatch.delitem(coverage_module.CAPTURE_QUALITY_SPECS, purpose)

    result = eval_coverage_gate(eval_coverage(tmp_path / "missing.db"))

    assert result.new_uncovered == (purpose,)
    assert not result.passed


def test_coverage_cli_report_stays_nonblocking_but_gate_fails_new_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _load_execution_module("run_llm_evals", "run_llm_evals_coverage_gate_check")
    monkeypatch.setitem(
        coverage_module.LLM_MODELS,
        "new_cli_gap",
        "test-model",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_llm_evals.py",
            "--coverage",
            "--repo-root",
            str(tmp_path),
        ],
    )
    assert runner.main() == 0
    assert "new_cli_gap" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_llm_evals.py",
            "--coverage-gate",
            "--repo-root",
            str(tmp_path),
        ],
    )
    assert runner.main() == 4
    output = capsys.readouterr().out
    assert "Eval coverage gate: FAIL" in output
    assert "new_cli_gap" in output


def test_capture_audit_cli_fails_loudly_when_explicitly_bounded_to_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _load_execution_module("run_llm_evals", "run_llm_evals_empty_capture_check")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio.db").touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_llm_evals.py",
            "--purpose",
            "annual_letter",
            "--repo-root",
            str(tmp_path),
            "--limit",
            "0",
            "--no-persist",
        ],
    )

    assert runner.main() == 2
    assert "has no executable cases" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# llm.structured — parse + retry-with-feedback, loud on failure
# ----------------------------------------------------------------------------


def test_parse_json_payload_shapes() -> None:
    assert parse_json_payload('{"a": 1}', expect="object") == {"a": 1}
    assert parse_json_payload("```json\n[1, 2]\n```", expect="array") == [1, 2]
    with pytest.raises(ValueError, match="expected a JSON object"):
        parse_json_payload("[1]", expect="object")
    with pytest.raises(ValueError, match="expected a JSON array"):
        parse_json_payload("{}", expect="array")
    with pytest.raises(ValueError, match="missing required key"):
        parse_json_payload('{"a": 1}', expect="object", required_keys=("a", "b"))
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_json_payload("prose, not json", expect="object")


def test_parse_json_payload_tolerates_trailing_prose() -> None:
    # A chatty model wraps the value in a fence AND appends an explanation, so
    # the closing fence + prose trail the JSON. json.loads rejects that as
    # "Extra data"; the parser must still recover the leading value instead of
    # burning a retry (the qualitative_conditions_extract outage, 2026-07-16).
    assert (
        parse_json_payload(
            "```json\n[]\n```\n\nThe section describes tactical trades, "
            "not falsifiable conditions.",
            expect="array",
        )
        == []
    )
    assert parse_json_payload(
        '{"verdict": "hold"}\n\nRationale: nothing material changed.',
        expect="object",
    ) == {"verdict": "hold"}
    # Genuine garbage (no JSON value at all) still raises loudly so the
    # retry-with-feedback layer fires.
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_json_payload("no json here at all", expect="array")


def test_call_llm_structured_retries_with_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    outputs = ["I think the answer is...", '{"k": "v"}']

    def fake_call(prompt: str, **_kw: object) -> str:
        prompts.append(prompt)
        return outputs[len(prompts) - 1]

    monkeypatch.setattr(structured, "call_llm", fake_call)
    out = call_llm_structured("PROMPT", purpose="p", expect="object", required_keys=("k",))
    assert out == {"k": "v"}
    assert len(prompts) == 2
    assert prompts[1].startswith("IMPORTANT:") and prompts[1].endswith("PROMPT")


def test_call_llm_structured_raises_after_two_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured, "call_llm", _returning("never json"))
    with pytest.raises(StructuredParseError, match="both attempts"):
        call_llm_structured("PROMPT", purpose="p")


def test_call_llm_structured_propagates_call_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def capped(_p: str, **_k: object) -> str:
        raise LLMBudgetExceeded("over cap")

    monkeypatch.setattr(structured, "call_llm", capped)
    with pytest.raises(LLMBudgetExceeded):
        call_llm_structured("PROMPT", purpose="p")


# ----------------------------------------------------------------------------
# production wrappers: hard stops propagate; risk classify fails loudly
# ----------------------------------------------------------------------------


def test_production_wrappers_propagate_hard_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_client

    def capped(*_a: object, **_k: object) -> str:
        raise LLMBudgetExceeded("over cap")

    monkeypatch.setattr(llm_client, "call_llm", capped)
    monkeypatch.setattr(llm_client, "call_llm_with_web", capped)
    # classify_intake_document now routes through call_llm_structured (Track B
    # seam 10), so the hard-stop must be injected at that transport too.
    monkeypatch.setattr(llm_client, "call_llm_structured", capped)
    with pytest.raises(LLMBudgetExceeded):
        llm_client.identify_transcript_metadata("some cover page")
    with pytest.raises(LLMBudgetExceeded):
        llm_client.classify_intake_document("f.pdf", "text", {})
    with pytest.raises(LLMBudgetExceeded):
        llm_client.structure_recent_news_json("NU")

    # Transient failures still degrade exactly as before.
    def flaky(*_a: object, **_k: object) -> str:
        raise RuntimeError("timeout")

    monkeypatch.setattr(llm_client, "call_llm", flaky)
    monkeypatch.setattr(llm_client, "call_llm_with_web", flaky)
    monkeypatch.setattr(llm_client, "call_llm_structured", flaky)
    with pytest.raises(RuntimeError, match="timeout"):
        llm_client.identify_transcript_metadata("x")
    assert llm_client.classify_intake_document("f.pdf", "t", {}) is None
    assert llm_client.structure_recent_news_json("NU") == []


def test_risk_classify_failure_skips_ticker_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed classify call must SKIP the ticker with a visible status —
    not persist every uncategorized risk as a fabricated 'other'."""
    mod = _load_execution_module("extract_risk_factors", "extract_risk_factors_pr4")

    def parse_fail(*_a: object, **_k: object) -> object:
        raise StructuredParseError("p: LLM returned unusable JSON on both attempts: x")

    monkeypatch.setattr(mod, "call_llm_structured", parse_fail)
    with pytest.raises(StructuredParseError):
        mod._llm_classify_risks(ticker="NU", fiscal_year=2025, risks=[("h", "b")])
    assert mod._llm_classify_risks(ticker="NU", fiscal_year=2025, risks=[]) == {}

    good = {"0": "regulatory"}
    monkeypatch.setattr(mod, "call_llm_structured", _returning(good))
    out = mod._llm_classify_risks(ticker="NU", fiscal_year=2025, risks=[("h", "b")])
    assert out == {0: "regulatory"}
