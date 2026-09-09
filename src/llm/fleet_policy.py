"""Thin adapter to the fleet-owned subscription-routing policy."""

from __future__ import annotations

import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

AGENT_INSTRUCTIONS_HOME_ENV_VAR = "AGENT_INSTRUCTIONS_HOME"
PRIMARY_BACKEND_ENV_VAR = "LLM_PRIMARY_SUBSCRIPTION_BACKEND"
FALLBACK_DISABLED_ENV_VAR = "LLM_SUBSCRIPTION_FALLBACK_DISABLED"
_SUPPORTED_BACKENDS = ("codex", "claude")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _candidate_roots() -> tuple[Path, ...]:
    configured = os.environ.get(AGENT_INSTRUCTIONS_HOME_ENV_VAR)
    sibling = Path(__file__).resolve().parents[3] / "agent-instructions"
    return tuple(path for path in (Path(configured) if configured else None, sibling) if path)


@lru_cache(maxsize=1)
def _policy_module() -> ModuleType:
    for root in _candidate_roots():
        module_path = root / "snippets" / "llm_policy.py"
        if not module_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_fleet_llm_policy", module_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    raise RuntimeError(
        f"fleet LLM policy not found; set {AGENT_INSTRUCTIONS_HOME_ENV_VAR} "
        "to the agent-instructions checkout"
    )


def subscription_route() -> tuple[str, ...]:
    """Return the ordinary application route as backend identifiers."""
    try:
        module = _policy_module()
    except RuntimeError:
        return _explicit_environment_route()
    return tuple(backend.value for backend in module.subscription_route())


def _explicit_environment_route() -> tuple[str, ...]:
    """Resolve an injected route when the fleet checkout is intentionally absent."""
    primary = os.environ.get(PRIMARY_BACKEND_ENV_VAR, "").strip().lower()
    if primary not in _SUPPORTED_BACKENDS:
        raise RuntimeError(
            f"fleet LLM policy not found and {PRIMARY_BACKEND_ENV_VAR} does not name "
            "a supported subscription backend"
        )
    fallback_disabled = (
        os.environ.get(FALLBACK_DISABLED_ENV_VAR, "").strip().lower() in _TRUE_VALUES
    )
    if fallback_disabled:
        return (primary,)
    return (primary, *tuple(backend for backend in _SUPPORTED_BACKENDS if backend != primary))


def policy_environment_names() -> tuple[str, str]:
    """Return canonical primary and fallback-control environment names."""
    return PRIMARY_BACKEND_ENV_VAR, FALLBACK_DISABLED_ENV_VAR
