"""Adversarial coverage for capability enforcement at every public LLM seam."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter

from llm import cli, codex_backend, fallback, gemini_backend, ledger, openrouter_backend, structured
from llm.resolver import MODEL_CAPABILITIES, CapabilityProfile, resolve_model_and_backend


def _fail_transport(*_args: object, **_kwargs: object) -> str:
    pytest.fail("transport must not run")


def _fail_setup() -> None:
    pytest.fail("setup/transport must not run")


def _ignore_ledger_record(**_kwargs: object) -> None:
    return None


def _ignore_budget(
    _purpose: str | None,
    *,
    force_budget_bypass: bool = False,
) -> None:
    del force_budget_bypass


def test_unknown_model_fails_closed_even_for_plain_resolution() -> None:
    with pytest.raises(ValueError, match="unregistered capability metadata"):
        resolve_model_and_backend(None, model="vendor/unregistered-model")


def test_registered_codex_model_routes_to_codex_provider() -> None:
    _, backend = resolve_model_and_backend(None, model="gpt-5.6-sol")

    assert backend == "codex"


def test_call_llm_rejects_unknown_model_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_call_claude",
        _fail_transport,
    )

    with pytest.raises(ValueError, match="unregistered capability metadata"):
        cli.call_llm("prompt", model="unregistered-model", backend="claude")


def test_call_llm_with_web_rejects_unknown_model_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_verify_setup_once",
        _fail_setup,
    )

    with pytest.raises(ValueError, match="unregistered capability metadata"):
        cli.call_llm_with_web("prompt", model="unregistered-model")


@pytest.mark.parametrize(
    ("provider_call", "model"),
    [
        (gemini_backend.call_gemini, "gemini-unregistered"),
        (openrouter_backend.call_openrouter, "vendor/unregistered"),
        (codex_backend.call_codex_llm, "gpt-unregistered"),
    ],
)
def test_public_provider_facades_reject_unknown_model_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    provider_call: Any,
    model: str,
) -> None:
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", _fail_transport)

    with pytest.raises(ValueError, match="unregistered capability metadata"):
        provider_call("prompt", model=model)


@pytest.mark.parametrize(
    ("provider_call", "model"),
    [
        (gemini_backend.call_gemini, "gemini-3-flash-preview"),
        (openrouter_backend.call_openrouter, "deepseek/deepseek-chat"),
        (codex_backend.call_codex_llm, "gpt-5.6-sol"),
    ],
)
def test_public_provider_facades_enforce_explicit_requirements_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    provider_call: Any,
    model: str,
) -> None:
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", _fail_transport)

    with pytest.raises(ValueError, match="context length"):
        provider_call(
            "prompt",
            model=model,
            capability_profile=CapabilityProfile(min_context_length=2_000_000),
        )


def test_call_llm_with_web_propagates_profile_to_codex_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = CapabilityProfile(requires_vision=True)
    seen: list[CapabilityProfile] = []

    def fake_codex(_prompt: str, **kwargs: Any) -> str:
        seen.append(kwargs["capability_profile"])
        return "grounded answer https://example.test"

    monkeypatch.setattr(codex_backend, "call_codex_llm", fake_codex)
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", _ignore_budget)
    monkeypatch.setattr(cli, "capture_exchange", _ignore_ledger_record)

    result = cli.call_llm_with_web(
        "prompt",
        model="gpt-5.6-sol",
        capability_profile=profile,
    )

    assert result == "grounded answer https://example.test"
    assert seen == [profile]


def test_call_llm_propagates_profile_to_openrouter_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = CapabilityProfile(requires_structured_output=True)
    seen: list[CapabilityProfile] = []

    def fake_openrouter(_prompt: str, **kwargs: Any) -> str:
        seen.append(kwargs["capability_profile"])
        return "answer"

    monkeypatch.setattr(openrouter_backend, "call_openrouter", fake_openrouter)

    result = cli.call_llm(
        "prompt",
        purpose="test",
        model="deepseek/deepseek-chat",
        capability_profile=profile,
    )

    assert result == "answer"
    assert seen == [profile]


def test_try_gemini_fallback_propagates_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = CapabilityProfile(requires_structured_output=True)
    seen: list[CapabilityProfile] = []
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("LLM_FALLBACK_DISABLED", raising=False)

    def fake_gemini(_prompt: str, **kwargs: Any) -> str:
        seen.append(kwargs["capability_profile"])
        return "answer"

    monkeypatch.setattr(gemini_backend, "call_gemini", fake_gemini)

    result = fallback.try_gemini_fallback(
        "prompt",
        RuntimeError("primary down"),
        capability_profile=profile,
    )

    assert result == "answer"
    assert seen == [profile]


def test_logged_fallback_propagates_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = CapabilityProfile(requires_vision=True)
    seen: list[CapabilityProfile] = []
    monkeypatch.setattr(fallback, "fallback_available", lambda: True)
    monkeypatch.setattr(ledger, "record_llm_call", _ignore_ledger_record)

    def fake_fallback(
        _prompt: str,
        _error: Exception,
        *,
        capability_profile: CapabilityProfile | None = None,
    ) -> str:
        assert capability_profile is not None
        seen.append(capability_profile)
        return "answer"

    monkeypatch.setattr(ledger, "try_gemini_fallback", fake_fallback)

    result = ledger.fallback_call_logged(
        "prompt",
        RuntimeError("primary down"),
        prompt_sha="abc",
        purpose="test",
        ticker=None,
        scope=None,
        run_id="attempt",
        metered_fallback_authorized=True,
        capability_profile=profile,
    )

    assert result == "answer"
    assert seen == [profile]


def test_structured_facade_forces_structured_capability_on_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles: list[CapabilityProfile] = []
    responses = iter(("not json", '{"answer": 1}'))

    def fake_call_llm(_prompt: str, **kwargs: Any) -> str:
        profiles.append(kwargs["capability_profile"])
        return next(responses)

    monkeypatch.setattr(structured, "call_llm", fake_call_llm)

    result = structured.call_llm_structured("prompt", purpose="test", required_keys=("answer",))

    assert result == {"answer": 1}
    assert len(profiles) == 2
    assert all(profile.requires_structured_output for profile in profiles)


def test_structured_facade_rejects_unknown_model_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_call_claude",
        _fail_transport,
    )

    with pytest.raises(ValueError, match="unregistered capability metadata"):
        structured.call_llm_structured(
            "prompt",
            purpose="test",
            model="unregistered-model",
            backend="claude",
        )


def test_structured_with_raw_preserves_required_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles: list[CapabilityProfile] = []

    def fake_call_llm(_prompt: str, **kwargs: Any) -> str:
        profiles.append(kwargs["capability_profile"])
        return '{"answer": 1}'

    monkeypatch.setattr(structured, "call_llm", fake_call_llm)

    result = structured.call_llm_structured_with_raw(
        "prompt",
        purpose="test",
        schema=TypeAdapter(dict[str, int]),
        repair_prompt=lambda error: f"repair: {error}",
    )

    assert result.value == {"answer": 1}
    assert len(profiles) == 1
    assert profiles[0].requires_structured_output is True


def test_operational_fallback_cannot_bypass_required_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import gemini_backend

    def fail_gemini(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("outage")

    monkeypatch.setattr(gemini_backend, "call_gemini", fail_gemini)
    monkeypatch.setattr(
        cli,
        "_call_claude",
        _fail_transport,
    )
    monkeypatch.setitem(
        MODEL_CAPABILITIES,
        cli.DEFAULT_MODEL,
        CapabilityProfile(
            min_context_length=200_000,
            requires_vision=True,
            requires_structured_output=False,
        ),
    )

    with pytest.raises(ValueError, match="structured output"):
        cli.call_llm(
            "prompt",
            purpose="test",
            model="gemini-3-flash-preview",
            capability_profile=CapabilityProfile(requires_structured_output=True),
        )
