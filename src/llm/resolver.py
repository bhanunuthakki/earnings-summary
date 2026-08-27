"""Single-point canonical model resolution, backend mapping, and capability validation.

Consolidates per-purpose model lookup, DB pin overrides, provider family dispatch,
and capability profile checks across all backends into one central module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

from llm.model_ladder import CLAUDE, GEMINI, OPENROUTER, family_of
from llm.model_overrides import active_override

log = logging.getLogger(__name__)

# Fallback escape hatch env var
ALLOW_FORCED_FALLBACK_ENV_VAR = "LLM_ALLOW_FORCED_FALLBACK"


class InvalidLLMPurposeError(ValueError):
    """Raised when an LLM call is not attributable to a governed purpose."""


@overload
def validate_purpose(
    purpose: str | None,
    *,
    model: str | None = None,
    allow_unbound_model: Literal[False] = False,
) -> str: ...


@overload
def validate_purpose(
    purpose: str | None,
    *,
    model: str | None = None,
    allow_unbound_model: Literal[True],
) -> str | None: ...


def validate_purpose(
    purpose: str | None,
    *,
    model: str | None = None,
    allow_unbound_model: bool = False,
) -> str | None:
    """Validate a purpose before model selection, budget checks, or transport.

    ``LLM_MODELS`` is the closed registry for ordinary calls. Lens instances
    are the one dynamic exception: they must carry an explicit model. The
    resolver retains a narrow ``purpose=None`` escape hatch for the internal
    model-family lookup used by the Gemini fallback; public call facades do
    not enable it.
    """
    if purpose is None:
        if allow_unbound_model and model is not None:
            from llm.cli import LLM_MODELS

            if model in set(LLM_MODELS.values()) or model in MODEL_CAPABILITIES:
                return purpose
        raise InvalidLLMPurposeError(
            "LLM purpose is required; pass a registered purpose or an explicit lens:<name> purpose"
        )
    if not purpose.strip() or purpose == "__default__":
        raise InvalidLLMPurposeError(f"invalid LLM purpose {purpose!r}")

    from llm.cli import LLM_MODELS

    if purpose in LLM_MODELS:
        return purpose
    if purpose.startswith("lens:") and purpose[5:].strip() and model is not None:
        return purpose
    raise InvalidLLMPurposeError(f"unknown LLM purpose {purpose!r}")


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """Capability requirements for an LLM call.

    Used by the resolver and fallback dispatcher to verify that a fallback or candidate
    model satisfies the hard constraints of the call before dispatching.
    """

    min_context_length: int = 0
    requires_vision: bool = False
    requires_structured_output: bool = False


# Minimal model capability registry (prices/ranks live in model_ladder.py)
MODEL_CAPABILITIES: dict[str, CapabilityProfile] = {
    # Claude models
    "claude-sonnet-4-6": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    "claude-sonnet-5": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    "claude-haiku-4-5-20251001": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    "claude-haiku-4-5": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    "claude-opus-4-8": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    "claude-opus-4-7": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    "claude-fable-5": CapabilityProfile(
        min_context_length=200_000, requires_vision=True, requires_structured_output=True
    ),
    # Gemini models
    "gemini-3.1-pro-preview": CapabilityProfile(
        min_context_length=1_000_000, requires_vision=True, requires_structured_output=True
    ),
    "gemini-3-flash-preview": CapabilityProfile(
        min_context_length=1_000_000, requires_vision=True, requires_structured_output=True
    ),
    "gemini-2.5-flash": CapabilityProfile(
        min_context_length=1_000_000, requires_vision=True, requires_structured_output=True
    ),
    "gemini-2.5-pro": CapabilityProfile(
        min_context_length=1_000_000, requires_vision=True, requires_structured_output=True
    ),
    # Codex models
    "gpt-5.6-terra": CapabilityProfile(
        min_context_length=128_000, requires_vision=True, requires_structured_output=True
    ),
    "gpt-5.6-luna": CapabilityProfile(
        min_context_length=128_000, requires_vision=True, requires_structured_output=True
    ),
    "gpt-5.6-sol": CapabilityProfile(
        min_context_length=128_000, requires_vision=True, requires_structured_output=True
    ),
    # OpenRouter models
    "deepseek/deepseek-chat": CapabilityProfile(
        min_context_length=64_000, requires_vision=False, requires_structured_output=True
    ),
    "qwen/qwen-2.5-72b-instruct": CapabilityProfile(
        min_context_length=32_000, requires_vision=False, requires_structured_output=True
    ),
}


def model_has_capabilities(model_id: str, profile: CapabilityProfile) -> tuple[bool, str]:
    """Check if model_id satisfies the required CapabilityProfile.

    Returns (is_capable, reason).
    """
    model_cap = MODEL_CAPABILITIES.get(model_id)
    if model_cap is None:
        return (
            False,
            f"model {model_id} has unregistered capability metadata",
        )

    if profile.min_context_length > model_cap.min_context_length:
        return (
            False,
            f"model {model_id} context length ({model_cap.min_context_length}) < required ({profile.min_context_length})",
        )
    if profile.requires_vision and not model_cap.requires_vision:
        return (False, f"model {model_id} does not support required vision capabilities")
    if profile.requires_structured_output and not model_cap.requires_structured_output:
        return (False, f"model {model_id} does not support structured output constraints")

    return (True, "OK")


def require_model_capabilities(model_id: str, profile: CapabilityProfile) -> None:
    """Fail closed unless ``model_id`` has registered evidence for ``profile``."""
    ok, reason = model_has_capabilities(model_id, profile)
    if not ok:
        raise ValueError(f"Model resolution capability check failed: {reason}")


def structured_capability_profile(
    profile: CapabilityProfile | None = None,
) -> CapabilityProfile:
    """Preserve caller constraints while making structured output mandatory."""
    profile = profile or CapabilityProfile()
    return CapabilityProfile(
        min_context_length=profile.min_context_length,
        requires_vision=profile.requires_vision,
        requires_structured_output=True,
    )


def is_forced_fallback_allowed() -> bool:
    """Return True if LLM_ALLOW_FORCED_FALLBACK is enabled in environment."""
    v = (os.environ.get(ALLOW_FORCED_FALLBACK_ENV_VAR) or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def resolve_model_and_backend(
    purpose: str | None,
    *,
    model: str | None = None,
    backend: str | None = None,
    capability_profile: CapabilityProfile | None = None,
    db_path: Path | str | None = None,
) -> tuple[str, str]:
    """Single-point canonical resolver for model ID and backend provider.

    Resolution order for model:
      1. Explicit `model` parameter if passed.
      2. Active DB override (`model_pin_overrides`) for `purpose`.
      3. Purpose pin in `LLM_MODELS`.

    Resolution order for backend:
      1. Explicit `backend` parameter if passed.
      2. Family mapping of resolved model (`family_of(resolved_model)`).
      3. Defaults to "claude".

    Every resolved model must have registered capability metadata. The optional
    profile adds call-specific hard constraints. Unknown models and violated
    constraints raise ValueError before dispatch.
    """
    from llm.cli import LLM_MODELS

    if model is None:
        purpose = validate_purpose(purpose)
    else:
        validate_purpose(purpose, model=model, allow_unbound_model=True)

    # 1. Resolve Model ID
    resolved_model: str
    if model is not None:
        resolved_model = model
    else:
        override = active_override(purpose, db_path=db_path)
        resolved_model = override if override is not None else LLM_MODELS[purpose]

    # 2. Resolve Backend Provider
    resolved_backend: str
    if backend is not None:
        resolved_backend = backend
    else:
        fam = family_of(resolved_model)
        if resolved_model.startswith("gpt-"):
            resolved_backend = "codex"
        elif fam == GEMINI:
            resolved_backend = "gemini"
        elif fam == OPENROUTER:
            resolved_backend = "openrouter"
        elif fam == CLAUDE:
            from llm.cli import PRIMARY_CODEX, primary_subscription_backend

            if model is None and primary_subscription_backend() == PRIMARY_CODEX:
                resolved_backend = "codex"
            else:
                resolved_backend = "claude"

        else:
            resolved_backend = "claude"

    # 3. Validate registry membership and call-specific requirements.
    effective_profile = capability_profile or CapabilityProfile()
    ok, reason = model_has_capabilities(resolved_model, effective_profile)
    if not ok:
        log.warning(
            {
                "event": "llm_resolver_capability_mismatch",
                "purpose": purpose,
                "model": resolved_model,
                "reason": reason,
            }
        )
        raise ValueError(f"Model resolution capability check failed: {reason}")

    return resolved_model, resolved_backend
