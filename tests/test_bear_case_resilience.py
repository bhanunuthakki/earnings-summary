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


@pytest.mark.parametrize(
    "response",
    [
        _VALID_RESPONSE + "\n\nThis bear case centers on NIM compression.",
        "```json\n" + _VALID_RESPONSE + "\n```",
        "```json\n" + _VALID_RESPONSE + "\n```\n\nThe analysis above is grounded in the filings.",
        "Here is the bear case:\n```json\n" + _VALID_RESPONSE + "\n```",
    ],
)
def test_build_parses_response_with_surrounding_prose(
    response: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model intermittently appends commentary after the JSON object and/or
    wraps it in ``` fences. A bare ``json.loads`` raised "Extra data" on the
    trailing text and silently degraded the section to MISSING_DATA — observed
    live on NVO (6504-char response, "Extra data: line 1 column 13"). The first
    JSON object must still parse cleanly into an OK section."""
    section = _build(monkeypatch, tmp_path, response)
    assert section.status == SectionStatus.OK
    assert len(section.failure_modes) == 1
    assert section.failure_modes[0].leading_indicator == "cost of risk"


def _build_no_llm(tmp_path: Path) -> BearCaseSection:
    return bear_case.build(
        ticker="NU",
        repo_root=tmp_path,
        enable_llm=False,  # the default dev / non-LLM build
        thesis=_thesis(),
        financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
        segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
        earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
        force_refresh=False,  # honor the on-disk cache
    )


def test_build_serves_cache_without_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-LLM build must serve a valid on-disk cache instead of blanking the
    bear tab with LLM_PENDING. Regression for the gate-ordering bug where the
    `enable_llm` short-circuit ran BEFORE the cache read, so a present, valid
    data/bear_case/<T>.json was never consulted on the default (non-LLM) build —
    which is exactly why NU's bear tab rendered empty while 16KB of cached
    failure modes sat on disk."""
    cache = tmp_path / "data" / "bear_case"
    cache.mkdir(parents=True)
    (cache / "NU.json").write_text(_VALID_RESPONSE, encoding="utf-8")

    # The LLM must NOT be called when a fresh cache exists.
    def _boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("LLM must not be called when a valid cache exists")

    monkeypatch.setattr("report.sections.bear_case.generate_bear_case", _boom)

    section = _build_no_llm(tmp_path)
    assert section.status == SectionStatus.OK
    assert len(section.failure_modes) == 1
    assert section.failure_modes[0].leading_indicator == "cost of risk"


def test_build_pending_without_llm_when_no_cache(tmp_path: Path) -> None:
    """Cold path unchanged: no cache + no LLM still returns LLM_PENDING."""
    section = _build_no_llm(tmp_path)
    assert section.status == SectionStatus.LLM_PENDING
