"""Tests for src/llm/resolver.py: canonical model resolution, capability profiles, and forced fallback gates."""

import pytest

from llm.resolver import (
    CapabilityProfile,
    InvalidLLMPurposeError,
    is_forced_fallback_allowed,
    model_has_capabilities,
    resolve_model_and_backend,
)


def test_resolve_model_and_backend_requires_purpose_or_explicit_model():
    with pytest.raises(InvalidLLMPurposeError):
        resolve_model_and_backend(purpose=None)


def test_resolve_model_and_backend_explicit_model():
    model, backend = resolve_model_and_backend(purpose=None, model="gemini-3.1-pro-preview")
    assert model == "gemini-3.1-pro-preview"
    assert backend == "gemini"


def test_resolve_model_and_backend_openrouter():
    model, backend = resolve_model_and_backend(purpose=None, model="deepseek/deepseek-chat")
    assert model == "deepseek/deepseek-chat"
    assert backend == "openrouter"


def test_model_capabilities_validation():
    profile = CapabilityProfile(min_context_length=100_000, requires_vision=True)
    ok, reason = model_has_capabilities("claude-sonnet-4-6", profile)
    assert ok is True
    assert reason == "OK"

    too_large = CapabilityProfile(min_context_length=500_000, requires_vision=False)
    ok_claude, reason_claude = model_has_capabilities("claude-sonnet-4-6", too_large)
    assert ok_claude is False
    assert "context length" in reason_claude


def test_resolve_model_capability_profile_pass():
    profile = CapabilityProfile(min_context_length=50_000, requires_vision=False)
    model, backend = resolve_model_and_backend(
        purpose=None, model="claude-sonnet-4-6", capability_profile=profile
    )
    assert model == "claude-sonnet-4-6"
    assert backend in ("claude", "codex")


def test_resolve_model_capability_profile_fail():
    profile = CapabilityProfile(min_context_length=500_000, requires_vision=False)
    with pytest.raises(ValueError, match="capability check failed"):
        resolve_model_and_backend(
            purpose=None, model="claude-sonnet-4-6", capability_profile=profile
        )


def test_forced_fallback_allowed_env(monkeypatch: pytest.MonkeyPatch):

    monkeypatch.setenv("LLM_ALLOW_FORCED_FALLBACK", "1")
    assert is_forced_fallback_allowed() is True

    monkeypatch.setenv("LLM_ALLOW_FORCED_FALLBACK", "0")
    assert is_forced_fallback_allowed() is False


def test_sol_candidate_preserves_exact_id_without_purpose_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import cli, resolver

    incumbent = cli.LLM_MODELS["kpi_summary_enumerate"]

    def candidate_override(purpose: str, **kwargs: object) -> str:
        return "gpt-6.1-sol"

    monkeypatch.setattr(resolver, "active_override", candidate_override)
    model, backend = resolve_model_and_backend(
        "kpi_summary_enumerate",
        capability_profile=CapabilityProfile(
            min_context_length=1_050_000, requires_structured_output=True
        ),
    )
    assert (model, backend) == ("gpt-6.1-sol", "codex")
    assert cli.model_for_subscription_backend(model, backend) == "gpt-6.1-sol"
    assert cli.LLM_MODELS["kpi_summary_enumerate"] == incumbent
    assert not model_has_capabilities(model, CapabilityProfile(min_context_length=1_050_001))[0]


@pytest.mark.parametrize(
    "gemini_model",
    ["gemini-4-neon", "gemini-4-hydrogen", "gemini-4-carbon", "gemini-3.5-flash-lite"],
)
def test_gemini_4_models_resolve_with_unified_1m_capabilities(gemini_model: str) -> None:
    profile = CapabilityProfile(
        min_context_length=1_048_576,
        requires_vision=True,
        requires_structured_output=True,
    )
    model, backend = resolve_model_and_backend(
        purpose=None,
        model=gemini_model,
        capability_profile=profile,
    )
    assert model == gemini_model
    assert backend == "gemini"
    assert not model_has_capabilities(
        gemini_model, CapabilityProfile(min_context_length=1_048_577)
    )[0]


@pytest.mark.parametrize(
    "claude_model",
    ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"],
)
def test_claude_55_models_resolve_with_1m_capabilities(claude_model: str) -> None:
    profile = CapabilityProfile(
        min_context_length=1_000_000,
        requires_vision=True,
        requires_structured_output=True,
    )
    model, backend = resolve_model_and_backend(
        purpose=None,
        model=claude_model,
        capability_profile=profile,
    )
    assert model == claude_model
    assert backend == "claude"


def test_gemini_fallback_constants_avoid_deprecated_25_flash() -> None:
    from llm import fallback

    assert fallback.GEMINI_FALLBACK_MODEL == "gemini-4-hydrogen"
