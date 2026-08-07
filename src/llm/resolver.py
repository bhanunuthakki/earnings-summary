"""Single-point canonical model resolution, backend mapping, and capability validation.

Consolidates per-purpose model lookup, DB pin overrides, provider family dispatch,
and capability profile checks across all backends into one central module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from llm.model_ladder import CLAUDE, GEMINI, OPENROUTER, family_of
from llm.model_overrides import active_override

log = logging.getLogger(__name__)

# Fallback escape hatch env var
ALLOW_FORCED_FALLBACK_ENV_VAR = "LLM_ALLOW_FORCED_FALLBACK"


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
        # Default assumption for unknown models: default to 32k context, vision=False
        model_cap = CapabilityProfile(
            min_context_length=32_000, requires_vision=False, requires_structured_output=True
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
      4. `DEFAULT_MODEL` fallback ("claude-sonnet-4-6").

    Resolution order for backend:
      1. Explicit `backend` parameter if passed.
      2. Family mapping of resolved model (`family_of(resolved_model)`).
      3. Defaults to "claude".

    If `capability_profile` is supplied, validates that the resolved model meets
    all constraints. Raises ValueError if constraints are violated.
    """
    from llm.cli import DEFAULT_MODEL, LLM_MODELS

    # 1. Resolve Model ID
    resolved_model: str
    if model is not None:
        resolved_model = model
    elif purpose is not None:
        override = active_override(purpose, db_path=db_path)
        if override is not None:
            resolved_model = override
        else:
            resolved_model = LLM_MODELS.get(purpose, DEFAULT_MODEL)
    else:
        resolved_model = DEFAULT_MODEL

    # 2. Resolve Backend Provider
    resolved_backend: str
    if backend is not None:
        resolved_backend = backend
    else:
        fam = family_of(resolved_model)
        if fam == GEMINI:
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

    # 3. Validate Capability Profile if provided
    if capability_profile is not None:
        ok, reason = model_has_capabilities(resolved_model, capability_profile)
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
