"""Q&A roster topic-labeling must degrade gracefully on a transient empty /
unparseable LLM response instead of aborting the whole multi-section build.

Sibling guard to ``tests/test_bear_case_resilience.py``: ``_apply_llm_topics``
parses a live LLM JSON array (``generate_qa_topics`` → ``_parse_topics_response``)
to upgrade the regex-derived Q&A topic/tag labels. A transient empty / non-JSON /
wrong-shape response must fall back to the regex labels — the analyst panel still
renders — and never propagate an exception out of ``build_report``. These tests
drive the public ``build()`` across the bad-response matrix and assert the
degradation is exactly the offline (regex-only) result, with a positive control
proving the LLM path is actually exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from report.models import (
    AppendixSection,
    QARosterSection,
    SectionStatus,
    TranscriptEntry,
)
from report.sections import qa_roster

# A minimal aggregator-style Q&A transcript the regex parser turns into one
# QAEntry: an operator hand-off ("first question comes from ... with ....") then
# two ``<Initial> <Name>`` speaker blocks (analyst question, management answer).
_QA_TEXT = (
    "=== Q&A SEGMENT ===\n"
    "first question comes from Brian Nowak with Morgan Stanley.\n\n"
    "B Brian Nowak How are you thinking about CapEx going forward? "
    "We see big numbers ahead.\n\n"
    "S Sundar Pichai We are investing opportunistically with a clear ROIC framework.\n"
)


def _appendix() -> AppendixSection:
    return AppendixSection(
        status=SectionStatus.OK,
        transcripts=[
            TranscriptEntry(
                quarter="Q1",
                year=2026,
                source_path="/fake/NU_Q1_2026.txt",
                text=_QA_TEXT,
            )
        ],
    )


def _build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: str) -> QARosterSection:
    """Run qa_roster.build with the topic-labeling LLM stubbed to return
    ``response``. ``tmp_path`` has no on-disk topics cache so the stub always
    runs; qa_roster imports the generator at module top, so we patch it there."""

    def _fake_generate(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return response

    monkeypatch.setattr(qa_roster, "generate_qa_topics", _fake_generate)
    return qa_roster.build(appendix=_appendix(), ticker="NU", repo_root=tmp_path, enable_llm=True)


@pytest.mark.parametrize(
    "response",
    [
        "",  # the bear-case failure mode: empty completion
        "   \n  ",  # whitespace-only
        "```json\n```",  # fenced but empty after the fence is stripped
        "I could not label these questions.",  # prose, not JSON
        '[{"id": "0", "topic": ',  # truncated / malformed JSON
        '{"id": "0"}',  # valid JSON but an object, not the expected array
    ],
)
def test_build_degrades_to_regex_labels_on_unparseable_response(
    response: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build(monkeypatch, tmp_path, response)
    # The roster still builds (regex-derived labels) without raising.
    assert section.status == SectionStatus.OK
    assert section.quarters and section.quarters[0].entries
    entry = section.quarters[0].entries[0]
    assert entry.topic  # non-empty regex-derived topic survived
    assert entry.tag

    # Degradation is exactly the offline (regex-only) result — the bad LLM
    # response changed nothing.
    reference = qa_roster.build(
        appendix=_appendix(), ticker="NU", repo_root=tmp_path, enable_llm=False
    )
    assert section.quarters[0].entries == reference.quarters[0].entries


def test_build_applies_valid_llm_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Positive control: a well-formed array upgrades the regex labels, proving
    # the LLM path is reached (so the degradation test isn't vacuously passing
    # because the labeling never ran).
    section = _build(
        monkeypatch,
        tmp_path,
        '[{"id": "0", "topic": "Capex trajectory through 2027", "tag": "CAPEX"}]',
    )
    entry = section.quarters[0].entries[0]
    assert entry.topic == "Capex trajectory through 2027"
    assert entry.tag == "CAPEX"
