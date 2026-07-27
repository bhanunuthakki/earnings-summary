"""Codex-first subscription routing with Claude as the operational fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm import cli as llm_cli
from llm import codex_backend


def test_empty_codex_response_is_ledgered_only_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []
    result = SimpleNamespace(
        text="   ",
        usage=SimpleNamespace(input_tokens=1, cached_input_tokens=0, output_tokens=0),
    )

    def call_empty(prompt: str, *, model: str, timeout_seconds: int) -> SimpleNamespace:
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
