"""Codex-first subscription routing with Claude as the operational fallback."""

from __future__ import annotations

import ast
import inspect
import json
import logging
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm_client
from llm import cli as llm_cli
from llm import codex_backend
from llm_call_ledger import usage_from_json_meta


def _good_cli_response(text: str = "ok") -> str:
    """Minimal `claude -p --output-format json` success envelope (mirrors the
    fixture in tests/test_llm_web_model_resolution.py — small enough to
    duplicate rather than share, per repo convention)."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            },
        }
    )


def test_empty_codex_response_is_ledgered_only_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []
    result = SimpleNamespace(
        text="   ",
        usage=SimpleNamespace(input_tokens=1, cached_input_tokens=0, output_tokens=0),
    )

    def call_empty(
        prompt: str, *, model: str, timeout_seconds: int, web_search: str = "disabled"
    ) -> SimpleNamespace:
        return result

    def load_empty_wrapper() -> object:
        return call_empty

    def record(**kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(codex_backend, "_load_wrapper", load_empty_wrapper)
    monkeypatch.setattr("llm.ledger.record_llm_call", record)

    with pytest.raises(RuntimeError, match="empty response"):
        codex_backend.call_codex_llm("question", purpose="bear_case")

    assert len(recorded) == 1
    assert recorded[0]["failure_class"] == "empty_response"
    assert recorded[0]["error"] == "[codex] RuntimeError: empty response"
    assert "response_text" not in recorded[0]


def test_codex_ledger_includes_public_api_equivalent_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []
    result = SimpleNamespace(
        text="answer",
        usage=SimpleNamespace(input_tokens=1_000, cached_input_tokens=200, output_tokens=100),
    )

    def call_result(
        _prompt: str, *, model: str, timeout_seconds: int, web_search: str = "disabled"
    ) -> SimpleNamespace:
        return result

    def record(**kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(codex_backend, "_load_wrapper", lambda: call_result)
    monkeypatch.setattr("llm.ledger.record_llm_call", record)

    assert (
        codex_backend.call_codex_llm(
            "question",
            purpose="bear_case",
            fallback_used="codex",
            fallback_from_provider="anthropic",
            fallback_from_transport="subscription_cli",
        )
        == "answer"
    )
    meta = recorded[0]["meta"]
    assert isinstance(meta, dict)
    assert meta["total_cost_usd"] == pytest.approx(0.00284)
    assert usage_from_json_meta(meta)["cost_estimate_usd"] == pytest.approx(0.00284)
    assert recorded[0]["fallback_used"] == "codex"
    assert recorded[0]["fallback_from_provider"] == "anthropic"


def test_codex_cost_estimate_applies_long_context_rates() -> None:
    assert codex_backend.estimate_api_equivalent_cost_usd(
        model="gpt-5.6-terra",
        input_tokens=273_000,
        cached_input_tokens=0,
        output_tokens=1_000,
    ) == pytest.approx(1.11)


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [
        ("gpt-5.6-luna", 0.000284),
        ("gpt-5.6-terra", 0.00284),
        ("gpt-5.6-sol", 0.0071),
    ],
)
def test_codex_cost_estimate_matches_current_public_rates(
    model: str,
    expected_cost: float,
) -> None:
    assert codex_backend.estimate_api_equivalent_cost_usd(
        model=model,
        input_tokens=1_000,
        cached_input_tokens=200,
        output_tokens=100,
    ) == pytest.approx(expected_cost)


def test_codex_unknown_model_blocks_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_load() -> object:
        raise AssertionError("must not dispatch")

    monkeypatch.setattr(
        codex_backend,
        "_load_wrapper",
        must_not_load,
    )

    with pytest.raises(ValueError, match="missing public API price"):
        codex_backend.call_codex_llm("question", model="gpt-future")


def test_default_purpose_routes_to_codex_before_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    seen: dict[str, object] = {}

    def fake_codex(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs, prompt=prompt)
        return "codex answer"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    monkeypatch.setattr(
        llm_cli,
        "_call_claude",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Claude must not run when the Codex primary succeeds")
        ),
    )

    assert llm_cli.call_llm("question", purpose="bear_case", ticker="NU") == "codex answer"
    assert seen["model"] == "gpt-5.6-terra"
    assert seen["purpose"] == "bear_case"
    assert seen["ticker"] == "NU"


def test_codex_primary_failure_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    calls: list[str] = []

    def fail_codex(prompt: str, **kwargs: object) -> str:
        calls.append("codex")
        raise RuntimeError("membership transport temporarily unavailable")

    def fake_claude(prompt: str, **kwargs: object) -> str:
        calls.append("claude")
        assert kwargs["fallback_used"] == "claude"
        assert kwargs["fallback_from_provider"] == "openai"
        assert kwargs["fallback_from_transport"] == "subscription_cli"
        return "claude fallback"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)
    monkeypatch.setattr(llm_cli, "_call_claude", fake_claude)

    assert llm_cli.call_llm("question", purpose="bear_case") == "claude fallback"
    assert calls == ["codex", "claude"]


def test_codex_then_claude_failure_does_not_reenter_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    calls: list[str] = []

    def fail_codex(prompt: str, **kwargs: object) -> str:
        calls.append("codex")
        raise RuntimeError("codex unavailable")

    def fail_claude(prompt: str, **kwargs: object) -> str:
        calls.append("claude")
        assert kwargs["allow_codex_fallback"] is False
        raise OSError("claude unavailable")

    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)
    monkeypatch.setattr(llm_cli, "_call_claude", fail_claude)

    with pytest.raises(
        RuntimeError,
        match=r"Both subscription LLM transports failed \(codex=RuntimeError, claude=OSError\)",
    ):
        llm_cli.call_llm("question", purpose="bear_case")
    assert calls == ["codex", "claude"]


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("capture_intent", "gpt-5.6-luna"),
        ("bear_case", "gpt-5.6-terra"),
        ("decision_draft_parse", "gpt-5.6-sol"),
    ],
)
def test_codex_primary_preserves_the_purpose_quality_tier(
    monkeypatch: pytest.MonkeyPatch, purpose: str, expected: str
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    seen: dict[str, object] = {}

    def fake_codex(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    llm_cli.call_llm("question", purpose=purpose)
    assert seen["model"] == expected


def test_explicit_claude_model_bypasses_codex_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    seen: dict[str, object] = {}

    def fake_claude(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "explicit Claude"

    monkeypatch.setattr(llm_cli, "_call_claude", fake_claude)
    monkeypatch.setattr(
        codex_backend,
        "call_codex_llm",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("an explicit Claude model is a forced-family request")
        ),
    )

    assert (
        llm_cli.call_llm("question", purpose="bear_case", model="claude-opus-4-8")
        == "explicit Claude"
    )
    assert seen["model"] == "claude-opus-4-8"


# --- call_llm_with_web: the SAME Codex-primary routing, mirrored -----------
#
# The guard hole this file exists to close: this file's name reads as "full
# Codex-primary routing coverage", but until now it only ever exercised
# call_llm. src/llm/cli.py has exactly two public entry points (call_llm and
# call_llm_with_web) and the web path had ZERO routing assertions — a purpose
# routed through call_llm_with_web (recent_developments, news_structuring,
# model_frontier_research, the research-task web pass) could silently stay
# Claude-first forever with this file's tests all green. These four tests
# mirror the call_llm block above one-for-one; the meta-guard further down
# makes a THIRD entry point impossible to add without matching coverage.


def _web_claude_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared setup for tests exercising call_llm_with_web's CLAUDE leg (either
    as the Codex fallback or the explicit-model bypass): bypass
    _verify_setup_once (avoids a real shutil.which) and no-op the quota
    breaker check, mirroring tests/test_llm_web_model_resolution.py's
    capture_web_cmd fixture."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")
    monkeypatch.setattr(llm_cli, "quota_block_active", lambda: None)


def test_web_default_purpose_routes_to_codex_before_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    seen: dict[str, object] = {}

    def fake_codex(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs, prompt=prompt)
        return "codex web answer https://reuters.com/x"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    monkeypatch.setattr(
        llm_cli.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Claude web subprocess must not run when Codex primary succeeds")
        ),
    )

    assert (
        llm_cli.call_llm_with_web("question", purpose="recent_developments", ticker="NU")
        == "codex web answer https://reuters.com/x"
    )
    assert seen["model"] == "gpt-5.6-terra"  # recent_developments -> DEFAULT_MODEL -> Terra tier
    assert seen["purpose"] == "recent_developments"
    assert seen["ticker"] == "NU"
    assert seen["web_search"] == "live"  # the whole point of routing web calls to Codex


def test_web_codex_primary_failure_falls_back_to_claude_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    _web_claude_fixture(monkeypatch)
    calls: list[str] = []

    def fail_codex(prompt: str, **kwargs: object) -> str:
        calls.append("codex")
        raise RuntimeError("membership transport temporarily unavailable")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append("claude_web")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_good_cli_response("claude web fallback"), stderr=""
        )

    records: list[dict[str, object]] = []
    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)
    monkeypatch.setattr(llm_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "record_llm_call", lambda **kwargs: records.append(kwargs))

    result = llm_cli.call_llm_with_web("question", purpose="recent_developments", ticker="NU")

    assert result == "claude web fallback"
    assert calls == ["codex", "claude_web"]
    (record,) = records
    assert record["fallback_used"] == "claude"
    assert record["fallback_from_provider"] == "openai"
    assert record["fallback_from_transport"] == "subscription_cli"


def test_web_codex_then_claude_web_failure_does_not_reenter_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    _web_claude_fixture(monkeypatch)
    calls: list[str] = []

    def fail_codex(prompt: str, **kwargs: object) -> str:
        calls.append("codex")
        raise RuntimeError("codex unavailable")

    def fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append("claude_web")
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1)

    def fake_plain_claude(prompt: str, **kwargs: object) -> str:
        calls.append("claude_plain")
        assert kwargs["allow_codex_fallback"] is False
        return "plain fallback, no codex retry"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)
    monkeypatch.setattr(llm_cli.subprocess, "run", fail_run)
    monkeypatch.setattr(llm_cli, "_call_claude", fake_plain_claude)
    monkeypatch.setattr(llm_cli, "record_llm_call", lambda **_kwargs: None)

    result = llm_cli.call_llm_with_web("question", purpose="recent_developments")

    assert result == "plain fallback, no codex retry"
    assert calls == ["codex", "claude_web", "claude_plain"]


def test_web_explicit_claude_model_bypasses_codex_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    _web_claude_fixture(monkeypatch)

    monkeypatch.setattr(
        codex_backend,
        "call_codex_llm",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("an explicit Claude model on the web path is a forced-family request")
        ),
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_good_cli_response("explicit claude web"), stderr=""
        )

    monkeypatch.setattr(llm_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(llm_cli, "record_llm_call", lambda **_kwargs: None)

    result = llm_cli.call_llm_with_web(
        "question", model="claude-opus-4-8", purpose="recent_developments"
    )
    assert result == "explicit claude web"


# --- META-GUARD: a third entry point cannot be added silently ---------------


def _module_call_entry_points(source: str) -> set[str]:
    """Top-level `def call_*` names in a module's source — the public LLM
    entry points that MUST have routing coverage in this file. This is the
    guard for the guard hole that let call_llm_with_web go unpinned: a file
    named 'codex primary routing' read as full coverage while only ever
    calling call_llm."""
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("call_")
    }


def _routing_covered_entry_points(test_source: str) -> set[str]:
    """Entry point names this test file actually CALLS (``<obj>.<name>(...)``)
    — a mention in a comment, docstring, or string literal doesn't count."""
    tree = ast.parse(test_source)
    covered: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            covered.add(node.func.attr)
    return covered


def test_every_public_cli_entry_point_has_routing_coverage() -> None:
    """META-GUARD: AST-enumerates src/llm/cli.py's public `call_*` entry
    points and asserts each is actually CALLED somewhere in this file. A
    third entry point (e.g. a future `call_llm_streaming` variant) added
    without a matching routing test here now fails CI instead of silently
    inheriting whatever the module happens to default to."""
    cli_source = inspect.getsource(llm_cli)
    entry_points = _module_call_entry_points(cli_source)
    assert entry_points >= {"call_llm", "call_llm_with_web"}, (
        "expected at least the two known entry points — did llm/cli.py move "
        "or get renamed without updating this guard?"
    )
    this_test_source = Path(__file__).read_text(encoding="utf-8")
    covered = _routing_covered_entry_points(this_test_source)
    missing = entry_points - covered
    assert not missing, (
        f"src/llm/cli.py has public entry point(s) {sorted(missing)} with NO "
        "routing test in tests/test_codex_primary_routing.py — add a "
        "Codex-primary/Claude-fallback/no-reentry/explicit-bypass test block "
        "for each, mirroring the call_llm and call_llm_with_web coverage above."
    )


def test_meta_guard_detects_an_unpinned_entry_point() -> None:
    """Self-test: a guard that cannot demonstrate detection of a KNOWN
    violation is a no-op guard (repo standing rule). Feed the meta-guard's
    own comparison a synthetic module exposing a THIRD entry point this file
    does not call, and assert the diff actually catches it — proving the
    guard above would have failed loudly on the exact defect class (a
    real-but-uncovered entry point) that motivated this file's expansion."""
    synthetic_source = (
        "def call_llm(*a, **k): ...\n"
        "def call_llm_with_web(*a, **k): ...\n"
        "def call_llm_via_teleport(*a, **k): ...\n"  # the unpinned violation
    )
    entry_points = _module_call_entry_points(synthetic_source)
    assert entry_points == {"call_llm", "call_llm_with_web", "call_llm_via_teleport"}

    this_test_source = Path(__file__).read_text(encoding="utf-8")
    covered = _routing_covered_entry_points(this_test_source)
    missing = entry_points - covered
    assert missing == {"call_llm_via_teleport"}, (
        f"meta-guard self-test failed to detect the synthetic violation: {missing!r} "
        "— the guard above would be a no-op"
    )


def test_metered_fallback_requires_reviewed_purpose_and_hard_budget(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(llm_cli, "OPENROUTER_FALLBACK_PURPOSES", frozenset({"approved_purpose"}))
    monkeypatch.setattr(
        "llm_budget.check_budget",
        lambda purpose: SimpleNamespace(
            cap=Decimal("5.00"),
            hard_block=True,
            current_spend=Decimal("1.25"),
        ),
    )
    enforced: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        llm_cli,
        "_enforce_budget_pre_call",
        lambda purpose, *, force_budget_bypass: enforced.append((purpose, force_budget_bypass)),
    )

    with caplog.at_level(logging.ERROR):
        assert llm_cli._authorize_metered_openrouter_fallback(
            "approved_purpose", force_budget_bypass=False
        )

    assert enforced == [("approved_purpose", False)]
    assert "llm_metered_openrouter_fallback_authorized" in caplog.text


def test_metered_fallback_fails_closed_without_hard_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_cli, "OPENROUTER_FALLBACK_PURPOSES", frozenset({"approved_purpose"}))
    monkeypatch.setattr(
        "llm_budget.check_budget",
        lambda purpose: SimpleNamespace(
            cap=Decimal("5.00"),
            hard_block=False,
            current_spend=Decimal("0"),
        ),
    )

    with pytest.raises(llm_cli.LLMSetupError, match="lacks a positive hard-block budget"):
        llm_cli._authorize_metered_openrouter_fallback(
            "approved_purpose", force_budget_bypass=False
        )


def test_metered_fallback_never_honors_budget_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_cli, "OPENROUTER_FALLBACK_PURPOSES", frozenset({"approved_purpose"}))
    with pytest.raises(llm_cli.LLMBudgetExceeded, match="cannot bypass"):
        llm_cli._authorize_metered_openrouter_fallback("approved_purpose", force_budget_bypass=True)


# ---------------------------------------------------------------------------
# Groundedness gate on the Codex web leg (measured defect, 2026-08-03).
#
# Codex returned the recent_developments template's own sanctioned escape
# hatch ("*No material news in the last 7 days.*", 0 URLs) for NU/MELI/UBER
# while Claude found real material news for all three the same day. The call
# SUCCEEDED — exit 0, well-formatted, confidently wrong — so neither the
# routing guard nor the operational fallback could see it. These pin the gate
# that converts "cited nothing" into a loud fallback.
# ---------------------------------------------------------------------------


def test_uncited_codex_web_answer_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production shape: Codex 'succeeds' with the template's
    say-nothing output and zero citations -> treated as operational failure,
    Claude serves the answer."""
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    # The Claude leg runs for real here, so it needs the same CLI-setup bypass
    # the other Claude-leg tests use — without it this passes on a machine that
    # HAS the claude binary and fails in CI, which is exactly what it did.
    _web_claude_fixture(monkeypatch)
    calls: list[str] = []

    def fake_codex(prompt: str, **kwargs: object) -> str:
        calls.append("codex")
        return "### Material news\n\n*No material news in the last 7 days.*"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)

    # After the gate fires the CLAUDE WEB leg runs (a subprocess), not
    # _call_claude — mock that seam the way the routing tests above do.
    def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        calls.append("claude")
        return subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=_good_cli_response("### Material news - **Real** https://reuters.com/x"),
            stderr="",
        )

    monkeypatch.setattr(llm_cli.subprocess, "run", fake_run)

    out = llm_cli.call_llm_with_web("brief please", purpose="recent_developments")
    assert calls == ["codex", "claude"], f"expected codex->claude fallback, got {calls}"
    assert "reuters.com" in out, "the served answer must be the GROUNDED one"


def test_cited_codex_web_answer_is_served_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discriminating half: a Codex answer that DOES cite is served directly —
    proving the gate keys on grounding, not on 'is it Codex'."""
    monkeypatch.setenv("LLM_PRIMARY_SUBSCRIPTION_BACKEND", "codex")
    calls: list[str] = []

    def fake_codex(prompt: str, **kwargs: object) -> str:
        calls.append("codex")
        return "### Material news\n- **Item** [Source: FT, https://ft.com/a]"

    def fake_claude(prompt: str, **kwargs: object) -> str:  # pragma: no cover
        calls.append("claude")
        return "should not be reached"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    monkeypatch.setattr(
        llm_cli.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("grounded Codex answer must not reach the Claude web leg")
        ),
    )

    out = llm_cli.call_llm_with_web("brief please", purpose="recent_developments")
    assert calls == ["codex"], f"grounded Codex answer must not fall back, got {calls}"
    assert "ft.com" in out
