"""Fail-closed purpose enforcement at the public LLM seams."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from llm import cli
from llm.resolver import (
    InvalidLLMPurposeError,
    resolve_model_and_backend,
    validate_purpose,
)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: cli.call_llm("prompt", purpose=None),
        lambda: cli.call_llm("prompt", purpose="__default__", model=cli.DEFAULT_MODEL),
        lambda: cli.call_llm("prompt", purpose="unknown-purpose", model=cli.DEFAULT_MODEL),
        lambda: cli.call_llm_with_web("prompt", purpose="unknown-purpose"),
        lambda: list(cli.stream_llm("prompt", purpose="unknown-purpose")),
    ],
)
def test_invalid_purpose_fails_before_budget_or_transport(
    monkeypatch: pytest.MonkeyPatch, invoke: Callable[[], object]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", lambda *_a, **_k: calls.append("budget"))
    monkeypatch.setattr(cli, "_call_claude", lambda *_a, **_k: calls.append("transport"))

    with pytest.raises(InvalidLLMPurposeError):
        invoke()
    assert calls == []


def test_dynamic_lens_requires_explicit_model() -> None:
    model = cli.DEFAULT_MODEL
    assert validate_purpose("lens:five_min_reread", model=model) == "lens:five_min_reread"

    with pytest.raises(InvalidLLMPurposeError):
        validate_purpose("lens:five_min_reread")


def test_registered_purpose_routes_through_the_canonical_registry() -> None:
    model, backend = resolve_model_and_backend("bear_case")
    assert model == cli.LLM_MODELS["bear_case"]
    assert backend in {"claude", "codex"}


def test_resolver_allows_only_its_registered_model_family_escape_hatch() -> None:
    # fallback.py uses this narrow resolver-only lookup; public facades still
    # reject purpose=None before they can reach it.
    assert validate_purpose(None, model="gemini-2.5-flash", allow_unbound_model=True) is None
    with pytest.raises(InvalidLLMPurposeError):
        validate_purpose(None, model="vendor/arbitrary", allow_unbound_model=True)
