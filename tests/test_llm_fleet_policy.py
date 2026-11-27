"""Hermetic tests for the fleet-policy adapter."""

from __future__ import annotations

import pytest

from llm import fleet_policy


def _missing_policy() -> None:
    raise RuntimeError("fleet checkout unavailable")


def test_explicit_environment_route_supports_isolated_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fleet_policy, "_policy_module", _missing_policy)
    monkeypatch.setenv(fleet_policy.PRIMARY_BACKEND_ENV_VAR, "claude")
    monkeypatch.delenv(fleet_policy.FALLBACK_DISABLED_ENV_VAR, raising=False)

    assert fleet_policy.subscription_route() == ("claude",)
    assert fleet_policy.preferred_provider_order() == ("gemini", "claude")
    assert fleet_policy.subscription_fallback_disabled() is False

    monkeypatch.setenv(fleet_policy.FALLBACK_DISABLED_ENV_VAR, "1")
    assert fleet_policy.subscription_fallback_disabled() is True


def test_explicit_environment_route_rejects_disabled_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fleet_policy, "_policy_module", _missing_policy)
    monkeypatch.setenv(fleet_policy.PRIMARY_BACKEND_ENV_VAR, "codex")
    monkeypatch.setenv(fleet_policy.FALLBACK_DISABLED_ENV_VAR, "1")

    with pytest.raises(RuntimeError, match=fleet_policy.PRIMARY_BACKEND_ENV_VAR):
        fleet_policy.subscription_route()


def test_missing_policy_requires_an_explicit_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fleet_policy, "_policy_module", _missing_policy)
    monkeypatch.delenv(fleet_policy.PRIMARY_BACKEND_ENV_VAR, raising=False)

    with pytest.raises(RuntimeError, match=fleet_policy.PRIMARY_BACKEND_ENV_VAR):
        fleet_policy.subscription_route()
