"""Bear-case build must degrade gracefully on a transient empty / unparseable
LLM response instead of aborting the whole multi-section brief build.

Regression guard for the empty-response crash: ``generate_bear_case`` returned
``""`` (a transient ``claude -p`` hiccup on NU's large bear-case prompt),
``_parse_response`` then ran ``json.loads("")`` and the resulting
``JSONDecodeError`` propagated out of ``build_report`` — killing every other
section and producing no artifact at all. The fix catches the parse failure at
section scope and returns a loud MISSING_DATA banner; these tests pin that
behavior so the transient failure is caught here, not re-discovered in prod.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from report.models import (
    BearCaseSection,
    EarningsSection,
    FinancialsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
)
from report.sections import bear_case

_VALID_RESPONSE = (
    '{"failure_modes": [{"hypothesis": "Brazil risk-adjusted NIM compresses as '
    'the book mixes into thinner-spread products", "evidence_in_data": '
    '"risk-adjusted NIM -120bps YoY", "leading_indicator": "cost of risk", '
    '"quantitative_impact": "~$400M PPOP headwind", "refutation_criteria": '
    '"NIM stable for two quarters"}], "most_underweighted": "Mexico credit '
    'seasoning", "out_of_scope_flags": []}'
)


def _thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK,
        thesis_full="NU is LatAm's leading digital bank; the bet is durable Brazil ROE.",
        break_conditions=["ROE sustains below 25% for two quarters"],
    )


def _build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: str) -> BearCaseSection:
    """Run bear_case.build with the LLM stubbed to return ``response``.

    ``force_refresh`` skips the on-disk cache so the stub always runs, and
    ``tmp_path`` has no portfolio.db so every prompt-assembly helper degrades to
    an empty string — the minimal sections below are the only content in play.
    """

    def _fake_generate(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return response

    monkeypatch.setattr("report.sections.bear_case.generate_bear_case", _fake_generate)
    return bear_case.build(
        ticker="NU",
        repo_root=tmp_path,
        enable_llm=True,
        thesis=_thesis(),
        financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
        segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
        earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
        force_refresh=True,
    )


@pytest.mark.parametrize(
    "response",
    [
        "",  # the exact failure observed: empty completion
        "   \n  ",  # whitespace-only
        "```json\n```",  # fenced but empty after the fence is stripped
        "I could not produce a bear case.",  # prose, not JSON
        '{"failure_modes": [',  # truncated / malformed JSON
        "[1, 2, 3]",  # valid JSON but not the expected object shape
    ],
)
def test_build_degrades_on_unparseable_llm_response(
    response: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build(monkeypatch, tmp_path, response)
    # Degrades loudly — a MISSING_DATA banner with a reason — instead of raising
    # and taking the whole build down with it.
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None
    assert section.failure_modes == []


def test_build_parses_valid_llm_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Positive control: a well-formed response still yields an OK section, so the
    # degradation test above can't be satisfied by a build that never parses.
    section = _build(monkeypatch, tmp_path, _VALID_RESPONSE)
    assert section.status == SectionStatus.OK
    assert len(section.failure_modes) == 1
    assert section.failure_modes[0].leading_indicator == "cost of risk"
