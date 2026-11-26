"""Scheduled provider policy keeps Codex disabled, Claude subscription-primary, and fails closed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import llm_client
from llm import cli as llm_cli
from llm import codex_backend, gemini_backend, openrouter_backend, resolver
from llm import transport as llm_transport


def _raise_assertion(message: str) -> None:
    raise AssertionError(message)


def test_disabled_codex_primary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")

    def fail_claude(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Claude must not run when an invalid/disabled primary is requested")

    monkeypatch.setattr(llm_cli, "_call_claude", fail_claude)

    with pytest.raises(llm_cli.LLMSetupError):
        llm_cli.call_llm("question", purpose="bear_case")


def test_scheduled_policy_can_forbid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")
    monkeypatch.setenv(llm_cli.SUBSCRIPTION_FALLBACK_DISABLED_ENV_VAR, "1")

    def fail_claude(_prompt: str, **_kwargs: object) -> str:
        raise RuntimeError("claude unavailable")

    def fail_codex(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Codex must not run")

    monkeypatch.setattr(llm_cli, "_call_claude", fail_claude)
    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)

    with pytest.raises(RuntimeError, match="claude unavailable"):
        llm_cli.call_llm("question", purpose="bear_case")


def test_streaming_ask_honors_claude_primary_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")
    which_calls: list[str] = []

    def no_budget(*_args: object, **_kwargs: object) -> None:
        return None

    def fail_buffered(*_args: object, **_kwargs: object) -> Iterator[dict[str, str]]:
        _raise_assertion("Claude-primary streaming must not take Codex buffered path")
        yield {}

    def fake_which(name: str) -> str | None:
        which_calls.append(name)
        return None

    def fail_codex(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Claude-primary streaming must not invoke Codex")

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", no_budget)
    monkeypatch.setattr(llm_cli, "_buffered_stream_answer", fail_buffered)
    monkeypatch.setattr(llm_cli.shutil, "which", fake_which)
    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)

    events = list(llm_cli.stream_llm("question", purpose="ask_answer"))

    assert which_calls == ["claude"]
    assert events == [{"type": "error", "error": "answer transport is not configured"}]


def test_scheduled_explicit_tier_uses_claude_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")
    monkeypatch.setenv("LLM_EXPLICIT_MODEL_POLICY", "primary-tier")
    seen: dict[str, object] = {}

    def fake_claude(_prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "scheduled Claude"

    def fail_gemini(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("scheduled tier pins must honor the configured primary provider")

    monkeypatch.setattr(llm_cli, "_call_claude", fake_claude)
    monkeypatch.setattr(gemini_backend, "call_gemini", fail_gemini)

    assert (
        llm_cli.call_llm("question", purpose="bear_case", model="gemini-4-neon")
        == "scheduled Claude"
    )
    assert seen["model"] == llm_cli.DEFAULT_MODEL


@pytest.mark.parametrize(
    ("primary", "model", "backend", "expected_model"),
    [
        ("claude", "claude-opus-4-8", "claude", "claude-opus-4-8"),
        ("claude", "gemini-3-flash-preview", "gemini", "claude-haiku-5-5"),
        ("claude", "deepseek/deepseek-chat", "openrouter", "claude-haiku-5-5"),
    ],
)
def test_scheduled_explicit_provider_pins_are_primary_provider_capability_requests(
    monkeypatch: pytest.MonkeyPatch,
    primary: str,
    model: str,
    backend: str,
    expected_model: str,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, primary)
    monkeypatch.setenv(resolver.EXPLICIT_MODEL_POLICY_ENV_VAR, resolver.PRIMARY_TIER_MODEL_POLICY)

    resolved_model, resolved_backend = resolver.resolve_model_and_backend(
        "bear_case",
        model=model,
        backend=backend,
    )

    assert resolved_model == expected_model
    assert resolved_backend == primary


def test_scheduled_database_model_override_cannot_escape_primary_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")
    monkeypatch.setenv(resolver.EXPLICIT_MODEL_POLICY_ENV_VAR, resolver.PRIMARY_TIER_MODEL_POLICY)

    def gemini_override(*_args: object, **_kwargs: object) -> str:
        return "gemini-3-flash-preview"

    monkeypatch.setattr(resolver, "active_override", gemini_override)
    seen: dict[str, object] = {}

    def fake_claude(_prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "scheduled Claude"

    def fail_other_provider(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("scheduled model overrides must not escape the primary provider")

    monkeypatch.setattr(llm_cli, "_call_claude", fake_claude)
    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_other_provider)
    monkeypatch.setattr(gemini_backend, "call_gemini", fail_other_provider)

    assert llm_cli.call_llm("question", purpose="bear_case") == "scheduled Claude"
    assert seen["model"] == llm_cli.FAST_CLASSIFIER_MODEL


@pytest.mark.parametrize(
    ("model", "provider_module", "call_name"),
    [
        ("gemini-3-flash-preview", gemini_backend, "call_gemini"),
        ("deepseek/deepseek-chat", openrouter_backend, "call_openrouter"),
    ],
)
def test_provider_wide_fallback_policy_blocks_metered_to_claude_fallback(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    provider_module: object,
    call_name: str,
) -> None:
    monkeypatch.delenv(resolver.EXPLICIT_MODEL_POLICY_ENV_VAR, raising=False)
    monkeypatch.setenv(llm_cli.SUBSCRIPTION_FALLBACK_DISABLED_ENV_VAR, "1")

    def fail_primary(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("provider unavailable")

    def fail_claude(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("fallback-disabled calls must not reach Claude")

    monkeypatch.setattr(provider_module, call_name, fail_primary)
    monkeypatch.setattr(llm_cli, "_call_claude", fail_claude)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        llm_cli.call_llm("question", purpose="bear_case", model=model)


def test_provider_wide_fallback_policy_blocks_claude_to_codex_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")
    monkeypatch.setenv(resolver.EXPLICIT_MODEL_POLICY_ENV_VAR, resolver.PRIMARY_TIER_MODEL_POLICY)
    monkeypatch.setenv(llm_cli.SUBSCRIPTION_FALLBACK_DISABLED_ENV_VAR, "1")
    monkeypatch.delenv("LLM_FALLBACK_DISABLED", raising=False)
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", "C:/fake/claude.CMD")

    def breaker_path() -> Path:
        return tmp_path / "breaker.json"

    def ignore_call(*_args: object, **_kwargs: object) -> None:
        return None

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(llm_transport, "_breaker_path", breaker_path)
    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", ignore_call)
    monkeypatch.setattr(llm_cli, "record_llm_call", ignore_call)
    monkeypatch.setattr(llm_cli, "capture_exchange", ignore_call)
    monkeypatch.setattr(llm_cli.time, "sleep", no_sleep)

    def fail_claude(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("claude unavailable")

    def fail_codex(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("fallback-disabled Claude calls must not reach Codex")

    monkeypatch.setattr(llm_cli.subprocess, "run", fail_claude)
    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)

    with pytest.raises(RuntimeError, match="claude unavailable"):
        llm_cli.call_llm("question", purpose="bear_case")


def test_scheduled_policy_can_forbid_claude_web_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")
    monkeypatch.setenv(llm_cli.SUBSCRIPTION_FALLBACK_DISABLED_ENV_VAR, "1")
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", "C:/fake/claude.CMD")

    def fail_claude_web(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("claude web unavailable")

    def fail_codex(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Codex must not run")

    monkeypatch.setattr(llm_cli.subprocess, "run", fail_claude_web)
    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)

    with pytest.raises(RuntimeError, match="claude web unavailable"):
        llm_cli.call_llm_with_web("question", purpose="recent_developments")
