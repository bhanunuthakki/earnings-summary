"""``valuation_basis._coerce_multiple_payload`` must turn a transient empty /
non-JSON / wrong-shape multiple-selection response into ``None`` (→ a graceful
``skipped_reason`` upstream, which the §Valuation section maps to a loud
MISSING_DATA banner) rather than raising — closing the latent ``AttributeError``
on ``parsed.get(...)`` when the model returns a non-object JSON shape. Sibling
guard to ``tests/test_bear_case_resilience.py`` for the §Valuation compute layer.
"""

from __future__ import annotations

import pytest

from compute.valuation_basis import _coerce_multiple_payload


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty
        "   \n  ",  # whitespace-only
        "```json\n```",  # fenced but empty
        "I could not pick a multiple.",  # prose, not JSON
        '{"multiple": "EV/LTM Revenue"',  # truncated / malformed JSON
        "[1, 2, 3]",  # valid JSON array — NOT an object (the AttributeError case)
        '"EV/LTM Revenue"',  # valid JSON string — NOT an object
        "null",  # valid JSON null — NOT an object
    ],
)
def test_coerce_returns_none_on_bad_payload(raw: str) -> None:
    assert _coerce_multiple_payload(raw) is None


def test_coerce_parses_object_and_strips_fence() -> None:
    # Positive control: a well-formed (optionally fenced) object parses through.
    payload = _coerce_multiple_payload(
        '```json\n{"multiple": "EV/LTM Revenue", "rationale": "asset-light"}\n```'
    )
    assert payload is not None
    assert payload["multiple"] == "EV/LTM Revenue"
    assert payload["rationale"] == "asset-light"
