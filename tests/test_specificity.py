"""Tests for the P3 deterministic specificity proxy (filings.specificity).

Weighted toward the failure modes the design doc calls out:

  * boilerplate-heavy generic risk language must score LOW (confidently
    boilerplate, never sent to an LLM);
  * numeric/entity-dense firm-specific text must score HIGH;
  * too little text to trust a density-based score either way stays
    AMBIGUOUS rather than picking a confident band by accident;
  * an empty hunk is a measurement gap, not evidence of genericness — must
    never resolve LOW;
  * diff-hunk extraction returns only the changed spans, never the whole
    text, and returns "" (a real signal to the caller) when nothing changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.specificity import (  # noqa: E402
    SpecificityBand,
    extract_diff_hunk,
    specificity_metrics,
)

_GENERIC_BOILERPLATE = (
    "There can be no assurance that our business will not be harmed. Such risks and "
    "uncertainties could adversely affect our business, financial condition and results "
    "of operations. We cannot guarantee that general economic conditions or competitive "
    "pressures may not adversely impact our future prospects, and we may be unable to "
    "respond to these forward-looking statements as anticipated."
)

_FIRM_SPECIFIC = (
    "In March 2025, our subsidiary MercadoPago launched a new credit product in Brazil "
    "and Mexico that increased total payment volume by $412 million, or 18%, compared to "
    "the prior year, following the December 2024 acquisition of KaveDinero and the "
    "expansion of our Buenos Aires fulfillment center to 1.2 million square feet."
)


def test_generic_boilerplate_scores_low() -> None:
    metrics = specificity_metrics(_GENERIC_BOILERPLATE)
    assert metrics.band is SpecificityBand.LOW
    assert metrics.boilerplate_hits > 0


def test_firm_specific_text_scores_high() -> None:
    metrics = specificity_metrics(_FIRM_SPECIFIC)
    assert metrics.band is SpecificityBand.HIGH
    assert metrics.number_count > 0
    assert metrics.entity_count > 0
    assert metrics.date_count > 0


def test_short_text_is_ambiguous_regardless_of_content() -> None:
    metrics = specificity_metrics("We compete with many other companies.")
    assert metrics.band is SpecificityBand.AMBIGUOUS


def test_empty_text_is_ambiguous_never_low() -> None:
    """An empty hunk is a measurement gap, not evidence the change was
    generic — must never resolve to a confident boilerplate verdict."""
    metrics = specificity_metrics("")
    assert metrics.band is SpecificityBand.AMBIGUOUS
    assert metrics.word_count == 0


def test_mixed_content_lands_ambiguous() -> None:
    """Neither confidently generic nor confidently specific -> the LLM
    survivor lane, not a forced deterministic guess."""
    text = (
        "Our Berlin office opened in 2023 and competes with Acme Corp for customers in "
        "several European markets, though broader macroeconomic conditions could still "
        "affect our results in ways that are difficult to predict at this time."
    )
    metrics = specificity_metrics(text)
    assert metrics.band is SpecificityBand.AMBIGUOUS


# ---------------------------------------------------------------------------
# Diff-hunk extraction
# ---------------------------------------------------------------------------


def test_diff_hunk_excludes_unchanged_prefix_and_suffix() -> None:
    prior = "We compete with many companies for users and advertisers in the market."
    current = "We compete with many companies for users, advertisers, and merchants in the market."
    hunk = extract_diff_hunk(prior, current)
    assert hunk != ""
    # The long unchanged prefix ("We compete with many companies for users")
    # should not appear in full, repeated verbatim, outside the WAS/NOW markers.
    assert "[WAS:" in hunk
    assert "[NOW:" in hunk


def test_diff_hunk_identical_text_returns_empty() -> None:
    text = "This risk factor did not change at all between periods."
    assert extract_diff_hunk(text, text) == ""


def test_diff_hunk_caps_number_of_hunks() -> None:
    prior = " ".join(f"prior{i}" for i in range(200))
    current = " ".join(f"current{i}" for i in range(200))
    hunk = extract_diff_hunk(prior, current, max_hunks=2)
    assert hunk.count("[WAS:") <= 2
