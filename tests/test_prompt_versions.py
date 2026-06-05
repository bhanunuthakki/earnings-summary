"""Tests for the calibration prompt-version registry (src/llm/prompt_versions.py).

The registry is the single source of truth for the calibration A/B dimension.
Before it, graders hardcoded ``prompt_version="v1"``, so every score was v1 and
``summarize_by_prompt_version`` could never compare two versions.
"""

from __future__ import annotations

import pytest

from llm import prompt_versions
from llm.prompt_versions import prompt_version_for


def test_registered_purposes_default_v1() -> None:
    assert prompt_version_for("bear_case") == "v1"
    assert prompt_version_for("decision_audit") == "v1"


def test_unknown_purpose_defaults_v1() -> None:
    assert prompt_version_for("does_not_exist") == "v1"


def test_registry_is_the_single_bump_point(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bumping a graded prompt is a one-line change here; the new version then
    # flows into every record_score() for that purpose (no scattered literals).
    monkeypatch.setitem(prompt_versions._PROMPT_VERSIONS, "bear_case", "v2")
    assert prompt_version_for("bear_case") == "v2"
    # Other purposes are unaffected.
    assert prompt_version_for("decision_audit") == "v1"


def test_trigger_and_prediction_purposes_registered() -> None:
    """The four trigger artifact purposes + the prediction-extraction purpose are
    registered (so a bump is one place) and default to v1."""
    for purpose in (
        "earnings_tone_diff",
        "kpi_inflection_context",
        "material_news_classification",
        "saydo_due_context",
        "management_prediction",
    ):
        assert purpose in prompt_versions.registered_purposes()
        assert prompt_version_for(purpose) == "v1"


def test_triggers_source_prompt_version_from_registry() -> None:
    """Each trigger sensor's artifact ``_PROMPT_VERSION`` is sourced from the
    registry keyed by its own ``_ARTIFACT_PURPOSE`` — not a hardcoded literal —
    so a single registry bump moves both the cache key and the calibration tag
    (the v6 re-grade gap: triggers hardcoded ``"v1"`` and bypassed the registry)."""
    from triggers import earnings_tone, kpi_inflection, material_news, saydo_due

    for mod in (earnings_tone, kpi_inflection, material_news, saydo_due):
        assert prompt_version_for(mod._ARTIFACT_PURPOSE) == mod._PROMPT_VERSION
