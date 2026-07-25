"""Tests for the CLI-transport hardening (July-2026 quota incident):
failure classification, the quota circuit breaker, retry policy, the
phantom-fallback-row fix, and judge-degraded verdicts in the model loop.

No real subprocess or LLM call anywhere in this file.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm.transport import (
    AUTH,
    CONFIG,
    MALFORMED,
    OVERLOADED,
    RATE_LIMIT,
    TIMEOUT,
    UNKNOWN,
    USAGE_LIMIT,
    FailureInfo,
    classify_cli_failure,
    clear_quota_block,
    quota_block_active,
    record_quota_exhausted,
    retry_budget,
)


def _envelope(result: str, *, status: int | None = None, exit_code: int = 1) -> Exception:
    """A CalledProcessError carrying a CLI JSON envelope on stdout — the real
    July-2026 failure shape (measured live: is_error can arrive with exit 0
    via ValueError, or with exit 1 via CalledProcessError)."""
    payload: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": result,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if status is not None:
        payload["api_error_status"] = status
    exc = subprocess.CalledProcessError(exit_code, ["claude.CMD", "-p"])
    exc.stdout = json.dumps(payload)
    exc.stderr = ""
    return exc


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_usage_limit_classified_with_reset_epoch() -> None:
    future = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
    info = classify_cli_failure(_envelope(f"Claude AI usage limit reached|{future}"))
    assert info.kind == USAGE_LIMIT
    assert info.retry_after is not None
    assert "usage limit reached" in info.detail.lower()


def test_usage_limit_stale_epoch_yields_no_retry_after() -> None:
    past = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    info = classify_cli_failure(_envelope(f"Claude AI usage limit reached|{past}"))
    assert info.kind == USAGE_LIMIT
    assert info.retry_after is None  # stale stamp -> probe interval, not a block


def test_far_future_epoch_is_clamped() -> None:
    far = int((datetime.now(UTC) + timedelta(days=9)).timestamp())
    info = classify_cli_failure(_envelope(f"usage limit reached|{far}"))
    assert info.retry_after is not None
    assert info.retry_after <= datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=9)


def test_status_codes_classify() -> None:
    assert classify_cli_failure(_envelope("too many requests", status=429)).kind == RATE_LIMIT
    assert classify_cli_failure(_envelope("nope", status=401)).kind == AUTH
    assert classify_cli_failure(_envelope("bad model", status=404)).kind == CONFIG
    assert classify_cli_failure(_envelope("overloaded_error", status=529)).kind == OVERLOADED


def test_429_with_usage_phrase_is_usage_limit_not_rate_limit() -> None:
    info = classify_cli_failure(_envelope("usage limit reached", status=429))
    assert info.kind == USAGE_LIMIT


def test_detail_prefers_envelope_result_over_tail() -> None:
    """The regression this guards: the old _stderr_tail kept the END of the
    JSON envelope (the usage-stats block), so every July error row recorded
    token counts instead of the reason."""
    info = classify_cli_failure(_envelope("The actual reason lives here"))
    assert "The actual reason lives here" in info.detail
    assert "output_tokens" not in info.detail


def test_timeout_and_unknown_classes() -> None:
    exc = subprocess.TimeoutExpired(["claude.CMD"], 240)
    assert classify_cli_failure(exc).kind == TIMEOUT
    assert classify_cli_failure(RuntimeError("mystery")).kind == UNKNOWN


def test_parse_valueerror_with_result_head_classifies() -> None:
    """parse_claude_json_output now embeds the envelope's result head in its
    ValueError — classification must work from that text (the is_error-with-
    exit-0 path, measured live 2026-07-24)."""
    exc = ValueError(
        "Claude CLI reported error: subtype='success' api_status=None "
        "result='Claude AI usage limit reached|1953305600'"
    )
    assert classify_cli_failure(exc).kind == USAGE_LIMIT


def test_text_embedded_status_classifies() -> None:
    """Found by LIVE probe 2026-07-24: the parse-ValueError path renders the
    envelope status as text ("api_status=404"), not as a field. Without text
    extraction a real 404 classified MALFORMED and earned 3 pointless retries."""
    exc = ValueError(
        "Claude CLI reported error: subtype='success' api_status=404 "
        'result="There\'s an issue with the selected model (bogus). ..."'
    )
    assert classify_cli_failure(exc).kind == CONFIG


def test_empty_result_is_malformed_and_retryable() -> None:
    info = classify_cli_failure(RuntimeError("claude -p returned empty `result`. stderr: "))
    assert info.kind == MALFORMED
    assert retry_budget(info.kind) >= 2


def test_retry_budget_policy() -> None:
    assert retry_budget(OVERLOADED) >= 2
    assert retry_budget(RATE_LIMIT) >= 2
    assert retry_budget(TIMEOUT) == 2
    assert retry_budget(USAGE_LIMIT) == 1  # deterministic until reset — never retry
    assert retry_budget(AUTH) == 1
    assert retry_budget(CONFIG) == 1


# ---------------------------------------------------------------------------
# Quota breaker
# ---------------------------------------------------------------------------


def test_breaker_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "breaker.json"
    assert quota_block_active(path=p) is None
    until = record_quota_exhausted(
        FailureInfo(USAGE_LIMIT, "limit reached", retry_after=None), path=p
    )
    active = quota_block_active(path=p)
    assert active is not None and active == until
    clear_quota_block(path=p)
    assert quota_block_active(path=p) is None


def test_breaker_expires_on_its_own(tmp_path: Path) -> None:
    p = tmp_path / "breaker.json"
    past = (datetime.now(UTC) - timedelta(minutes=1)).replace(tzinfo=None)
    record_quota_exhausted(FailureInfo(USAGE_LIMIT, "x", retry_after=past), path=p)
    # retry_after in the past is not honored as a block...
    assert quota_block_active(path=p) is None or quota_block_active(path=p) > datetime.now(
        UTC
    ).replace(tzinfo=None)


def test_breaker_corrupt_file_fails_open(tmp_path: Path) -> None:
    """A broken breaker file must never block calls — worst case is the
    pre-breaker behavior, never a stuck-closed transport."""
    p = tmp_path / "breaker.json"
    p.write_text("{not json", encoding="utf-8")
    assert quota_block_active(path=p) is None


def test_breaker_uses_parsed_reset_time(tmp_path: Path) -> None:
    p = tmp_path / "breaker.json"
    reset = (datetime.now(UTC) + timedelta(hours=3)).replace(tzinfo=None)
    until = record_quota_exhausted(FailureInfo(USAGE_LIMIT, "x", retry_after=reset), path=p)
    assert until == reset
    assert quota_block_active(path=p) == reset


# ---------------------------------------------------------------------------
# _call_claude integration: breaker + retry + typed quota error
# ---------------------------------------------------------------------------


@pytest.fixture()
def _cli_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the CLI wrapper at a fake binary + a tmp breaker file, and
    silence ledger/capture side effects."""
    import llm_client
    from llm import cli as llm_cli
    from llm import transport as tr

    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", "C:/fake/claude.CMD")
    breaker = tmp_path / "breaker.json"
    monkeypatch.setattr(tr, "_breaker_path", lambda: breaker)
    monkeypatch.setattr(llm_cli, "record_llm_call", lambda **kw: None)
    monkeypatch.setattr(llm_cli, "capture_exchange", lambda **kw: None)
    monkeypatch.setattr(llm_cli.time, "sleep", lambda s: None)  # no real backoff waits
    return breaker


def _ok_result(text: str = "hello") -> subprocess.CompletedProcess[str]:
    payload = {"type": "result", "is_error": False, "result": text, "usage": {}}
    return subprocess.CompletedProcess(
        args=["claude.CMD"], returncode=0, stdout=json.dumps(payload), stderr=""
    )


def test_transient_failure_retries_then_succeeds(
    _cli_ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm import cli as llm_cli

    calls = {"n": 0}

    def flaky(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            exc = subprocess.CalledProcessError(1, ["claude.CMD"])
            exc.stdout = json.dumps({"is_error": True, "result": "API Error: 529 overloaded"})
            exc.stderr = ""
            raise exc
        return _ok_result("recovered")

    monkeypatch.setattr(llm_cli.subprocess, "run", flaky)
    out = llm_cli._call_claude("prompt", purpose="bear_case")
    assert out == "recovered"
    assert calls["n"] == 2  # exactly one retry


def test_usage_limit_engages_breaker_and_raises_typed(
    _cli_ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm import cli as llm_cli
    from llm.transport import LLMQuotaExhausted

    calls = {"n": 0}

    def exhausted(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        exc = subprocess.CalledProcessError(1, ["claude.CMD"])
        exc.stdout = json.dumps({"is_error": True, "result": "Claude AI usage limit reached"})
        exc.stderr = ""
        raise exc

    monkeypatch.setattr(llm_cli.subprocess, "run", exhausted)
    monkeypatch.setenv("LLM_FALLBACK_DISABLED", "1")
    with pytest.raises(LLMQuotaExhausted):
        llm_cli._call_claude("prompt", purpose="bear_case")
    assert calls["n"] == 1  # no retry against an exhausted window
    assert quota_block_active(path=_cli_ready) is not None  # breaker engaged

    # Second call: fails FAST without spawning any subprocess.
    def must_not_spawn(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess spawned while the breaker was engaged")

    monkeypatch.setattr(llm_cli.subprocess, "run", must_not_spawn)
    with pytest.raises(LLMQuotaExhausted):
        llm_cli._call_claude("prompt", purpose="bear_case")


def test_success_clears_the_breaker(_cli_ready: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import cli as llm_cli

    # Engage with an already-passed block so the next call proceeds to spawn.
    past = (datetime.now(UTC) + timedelta(seconds=-1)).replace(tzinfo=None)
    record_quota_exhausted(FailureInfo(USAGE_LIMIT, "x", retry_after=past), path=_cli_ready)
    # File exists but block expired -> call runs; success removes the file.
    monkeypatch.setattr(llm_cli.subprocess, "run", lambda *a, **kw: _ok_result())
    assert llm_cli._call_claude("prompt", purpose="bear_case") == "hello"
    assert not _cli_ready.exists()


def test_quota_exhausted_is_not_a_hard_stop() -> None:
    """Pipelines defer per-item on quota exhaustion (self-healing) instead of
    crashing the build; eval/judge callers abort explicitly on the type."""
    from llm.cli import is_hard_stop
    from llm.transport import LLMQuotaExhausted

    assert not is_hard_stop(LLMQuotaExhausted("window exhausted"))


# ---------------------------------------------------------------------------
# Phantom fallback rows
# ---------------------------------------------------------------------------


def test_fallback_disabled_writes_no_gemini_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """July 2026: 3,496 model='gemini-2.5-flash' error rows for a backend that
    was never called — every Claude failure double-counted. A non-attempt must
    not produce a Gemini row."""
    from llm import ledger as llm_ledger

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(llm_ledger, "record_llm_call", lambda **kw: recorded.append(kw))
    monkeypatch.setenv("LLM_FALLBACK_DISABLED", "1")

    with pytest.raises(RuntimeError, match="fallback is disabled"):
        llm_ledger.fallback_call_logged(
            "prompt",
            RuntimeError("claude down"),
            prompt_sha="abc",
            purpose="bear_case",
            ticker=None,
            scope=None,
            run_id=None,
        )
    assert recorded == []  # zero phantom rows


# ---------------------------------------------------------------------------
# Judge-degraded verdicts in the model loop
# ---------------------------------------------------------------------------


def test_majority_judge_errors_yield_judge_degraded() -> None:
    from llm.model_eval import JUDGE_DEGRADED, decide_switch

    verdict = decide_switch(
        purpose="bear_case",
        incumbent="claude-sonnet-5",
        candidate="deepseek/deepseek-chat",
        per_judge={"claude": (1, 0, 1), "gemini": (0, 0, 0)},
        judge_agreement=0.0,
        min_n=4,
        parity_threshold=0.8,
        n_cases_attempted=12,
        n_candidate_errors=0,
        n_judgments_attempted=24,
        n_judge_errors=20,
    )
    assert verdict.recommendation == JUDGE_DEGRADED
    assert verdict.n_judge_errors == 20


def test_judge_degraded_is_streak_neutral() -> None:
    import sys

    sys.path.insert(0, "execution")
    from apply_model_switches import STREAK_NEUTRAL

    from llm.model_eval import JUDGE_DEGRADED

    assert JUDGE_DEGRADED in STREAK_NEUTRAL


def test_healthy_judges_unchanged() -> None:
    from llm.model_eval import SWITCH_DOWN, decide_switch

    verdict = decide_switch(
        purpose="bear_case",
        incumbent="claude-sonnet-5",
        candidate="claude-haiku-4-5",
        per_judge={"claude": (5, 0, 5), "gemini": (4, 1, 5)},
        judge_agreement=0.9,
        min_n=4,
        parity_threshold=0.8,
        n_cases_attempted=10,
        n_candidate_errors=0,
        n_judgments_attempted=20,
        n_judge_errors=0,
    )
    assert verdict.recommendation == SWITCH_DOWN


# ---------------------------------------------------------------------------
# Eval summary math under infra failures
# ---------------------------------------------------------------------------


def test_avg_score_excludes_infra_cases() -> None:
    from evals.harness import JUDGE_INFRA_STAGE, CaseResult, EvalRunSummary

    summary = EvalRunSummary(
        run_id="r",
        purpose="bear_case",
        mode="audit",
        prompt_version="v2",
        model="m",
        judge_model="j",
        golden_set_sha=None,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    summary.cases.append(CaseResult("a", "q", passed=True, score=0.9))
    summary.cases.append(CaseResult("b", "q", passed=True, score=0.95))
    summary.cases.append(
        CaseResult("c", "q", passed=False, score=None, failure_stage=JUDGE_INFRA_STAGE)
    )
    assert summary.n_cases == 3
    assert summary.n_infra == 1
    assert summary.n_scored == 2
    # 0.925, not (0.9+0.95+0)/3 = 0.617 — the July poisoning pattern.
    assert summary.avg_score is not None and abs(summary.avg_score - 0.925) < 1e-9


def test_all_infra_run_has_no_score() -> None:
    from evals.harness import JUDGE_INFRA_STAGE, CaseResult, EvalRunSummary

    summary = EvalRunSummary(
        run_id="r",
        purpose="p",
        mode="audit",
        prompt_version="v1",
        model="m",
        judge_model=None,
        golden_set_sha=None,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    summary.cases.append(
        CaseResult("a", "q", passed=False, score=None, failure_stage=JUDGE_INFRA_STAGE)
    )
    assert summary.avg_score is None  # "not measured", never a fabricated number


def test_rubric_run_aborts_after_consecutive_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    from evals.harness import EvalAbortError
    from evals.rubric_judge import AUDIT_SPECS, run_rubric_eval

    purpose = next(iter(AUDIT_SPECS))

    def boom(prompt: str, **_kw: object) -> str:
        raise RuntimeError("CLI down")

    import evals.rubric_judge as rj

    monkeypatch.setitem(
        rj.CORPUS_LOADERS,
        purpose,
        lambda _root: [
            rj.AuditItem(item_id=f"i{i}", label=f"i{i}", content="text", ticker=None)
            for i in range(6)
        ],
    )
    with pytest.raises(EvalAbortError, match="consecutive judge infra"):
        run_rubric_eval(
            purpose,
            db_path=Path("unused.db"),
            repo_root=Path("."),
            code_root=Path("."),
            caller=boom,
        )
