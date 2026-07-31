"""Tests for the calibration prompt-version registry (src/llm/prompt_versions.py).

The registry is the single source of truth for the calibration A/B dimension.
Before it, graders hardcoded ``prompt_version="v1"``, so every score was v1 and
``summarize_by_prompt_version`` could never compare two versions.
"""

from __future__ import annotations

import pytest

from llm import prompt_versions
from llm.prompt_versions import prompt_version_for


def test_registered_purposes_resolve_to_registered_version() -> None:
    # decision_audit is still v1; bear_case moved to v2 in the S9 sec-llm pass
    # (its IR anchor is now spotlighted) — both resolve via the registry, which
    # is the single bump-point the A/B dimension depends on.
    assert prompt_version_for("decision_audit") == "v1"
    assert prompt_version_for("bear_case") == "v2"


def test_unknown_purpose_defaults_v1() -> None:
    assert prompt_version_for("does_not_exist") == "v1"


def test_every_model_purpose_has_an_explicit_prompt_version() -> None:
    from llm.cli import LLM_MODELS

    assert set(LLM_MODELS) <= set(prompt_versions.registered_purposes())


def test_registry_is_the_single_bump_point(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bumping a graded prompt is a one-line change here; the new version then
    # flows into every record_score() for that purpose (no scattered literals).
    monkeypatch.setitem(prompt_versions._PROMPT_VERSIONS, "bear_case", "v2")
    assert prompt_version_for("bear_case") == "v2"
    # Other purposes are unaffected.
    assert prompt_version_for("decision_audit") == "v1"


def test_trigger_and_prediction_purposes_registered() -> None:
    """The four trigger artifact purposes + the prediction-extraction purpose are
    registered (so a bump is one place). Three trigger purposes moved to v2 in
    the S9 spotlighting pass (transcript / news / anchor inputs now wrapped);
    material_news_classification moved to v3 in the 2026-07-30 signal-quality
    rewrite (event-type taxonomy) and to v4 in the 2026-07-31 event_key
    clustering pass; management_prediction is untouched at v1."""
    for purpose in (
        "earnings_tone_diff",
        "kpi_inflection_context",
        "material_news_classification",
        "saydo_due_context",
        "management_prediction",
    ):
        assert purpose in prompt_versions.registered_purposes()
    assert prompt_version_for("management_prediction") == "v1"
    for trigger_purpose in (
        "earnings_tone_diff",
        "kpi_inflection_context",
        "saydo_due_context",
    ):
        assert prompt_version_for(trigger_purpose) == "v2"
    assert prompt_version_for("material_news_classification") == "v4"


def test_research_triage_is_registered() -> None:
    # B7 — the routing triage behind a positive wondering verdict.
    assert "research_triage" in prompt_versions.registered_purposes()
    assert prompt_version_for("research_triage") == "v1"


def test_triggers_source_prompt_version_from_registry() -> None:
    """Each trigger sensor's artifact ``_PROMPT_VERSION`` is sourced from the
    registry keyed by its own ``_ARTIFACT_PURPOSE`` — not a hardcoded literal —
    so a single registry bump moves both the cache key and the calibration tag
    (the v6 re-grade gap: triggers hardcoded ``"v1"`` and bypassed the registry)."""
    from triggers import earnings_tone, kpi_inflection, material_news, saydo_due

    for mod in (earnings_tone, kpi_inflection, material_news, saydo_due):
        assert prompt_version_for(mod._ARTIFACT_PURPOSE) == mod._PROMPT_VERSION
