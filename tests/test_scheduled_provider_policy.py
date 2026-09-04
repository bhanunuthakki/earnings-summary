"""Scheduled provider policy stays Codex-first and fails closed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from llm import cli as llm_cli
from llm import codex_backend


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
