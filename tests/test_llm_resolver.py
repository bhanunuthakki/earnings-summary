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
