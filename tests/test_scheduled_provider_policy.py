"""Scheduled provider policy stays Codex-first and fails closed."""

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


def test_scheduled_policy_can_forbid_claude_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    monkeypatch.setenv(llm_cli.SUBSCRIPTION_FALLBACK_DISABLED_ENV_VAR, "1")

    def fail_codex(_prompt: str, **_kwargs: object) -> str:
        raise RuntimeError("codex unavailable")

    def fail_claude(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Claude must not run")

    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)
    monkeypatch.setattr(llm_cli, "_call_claude", fail_claude)

    with pytest.raises(RuntimeError, match="codex unavailable"):
        llm_cli.call_llm("question", purpose="bear_case")


def test_streaming_ask_honors_codex_primary_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    observed: dict[str, object] = {}

    def no_budget(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_buffered(
        prompt: str, *, purpose: str, backend: str | None = None
    ) -> Iterator[dict[str, str]]:
        observed.update(prompt=prompt, purpose=purpose, backend=backend)
        yield {"type": "delta", "text": "codex answer"}
        yield {"type": "final", "text": "codex answer"}

    def fail_which(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Codex-primary streaming must not resolve Claude")

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", no_budget)
    monkeypatch.setattr(llm_cli, "_buffered_stream_answer", fake_buffered)
    monkeypatch.setattr(llm_cli.shutil, "which", fail_which)

    events = list(llm_cli.stream_llm("question", purpose="ask_answer"))

    assert events[-1] == {"type": "final", "text": "codex answer"}
    assert observed == {
        "prompt": "question",
        "purpose": "ask_answer",
        "backend": "codex",
    }


def test_scheduled_explicit_claude_tier_uses_codex_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    monkeypatch.setenv("LLM_EXPLICIT_MODEL_POLICY", "primary-tier")
    seen: dict[str, object] = {}

    def fake_codex(_prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "scheduled Codex"

    def fail_claude(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("scheduled tier pins must honor the configured primary provider")

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    monkeypatch.setattr(llm_cli, "_call_claude", fail_claude)

    assert (
        llm_cli.call_llm("question", purpose="bear_case", model="claude-opus-4-8")
        == "scheduled Codex"
    )
    assert seen["model"] == "gpt-5.6-sol"


@pytest.mark.parametrize(
    ("primary", "model", "backend", "expected_model"),
    [
        ("codex", "claude-opus-4-8", "claude", "gpt-5.6-sol"),
        ("codex", "gemini-3-flash-preview", "gemini", "gpt-5.6-luna"),
        ("codex", "deepseek/deepseek-chat", "openrouter", "gpt-5.6-luna"),
        ("claude", "claude-opus-4-8", "claude", "claude-opus-4-8"),
        ("claude", "gemini-3-flash-preview", "gemini", "claude-haiku-4-5-20251001"),
        ("claude", "deepseek/deepseek-chat", "openrouter", "claude-haiku-4-5-20251001"),
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
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    monkeypatch.setenv(resolver.EXPLICIT_MODEL_POLICY_ENV_VAR, resolver.PRIMARY_TIER_MODEL_POLICY)

    def gemini_override(*_args: object, **_kwargs: object) -> str:
        return "gemini-3-flash-preview"

    monkeypatch.setattr(resolver, "active_override", gemini_override)
    seen: dict[str, object] = {}

    def fake_codex(_prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "scheduled Codex"

    def fail_other_provider(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("scheduled model overrides must not escape the primary provider")

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    monkeypatch.setattr(gemini_backend, "call_gemini", fail_other_provider)
    monkeypatch.setattr(llm_cli, "_call_claude", fail_other_provider)

    assert llm_cli.call_llm("question", purpose="bear_case") == "scheduled Codex"
    assert seen["model"] == "gpt-5.6-luna"


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
    monkeypatch.setenv(llm_cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    monkeypatch.setenv(llm_cli.SUBSCRIPTION_FALLBACK_DISABLED_ENV_VAR, "1")

    def fail_codex(_prompt: str, **_kwargs: object) -> str:
        raise RuntimeError("codex web unavailable")

    def fail_claude_web(*_args: object, **_kwargs: object) -> None:
        _raise_assertion("Claude web must not run")

    monkeypatch.setattr(codex_backend, "call_codex_llm", fail_codex)
    monkeypatch.setattr(llm_cli.subprocess, "run", fail_claude_web)

    with pytest.raises(RuntimeError, match="codex web unavailable"):
        llm_cli.call_llm_with_web("question", purpose="recent_developments")
