"""§6 earnings cross-quarter themes split must degrade gracefully on a transient
empty / unparseable LLM response instead of aborting the whole multi-section
brief build.

Sibling guard to ``tests/test_bear_case_resilience.py``: the themes split is the
other §6 path that parses a live LLM JSON response (``extract_qa_vs_prepared_themes``
→ ``_parse_themes_response``). A transient empty / non-JSON / wrong-shape response
must collapse to empty theme rollups — the per-quarter cards still render — and
never propagate an exception out of ``build_report``. These tests drive the full
public ``build()`` across the bad-response matrix and assert that contract; the
last test pins the section-scope parse guard directly by forcing the parser to
raise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from report.models import EarningsSection, SectionStatus
from report.sections import earnings as earnings_section
from report.sections.earnings import build

_VALID_RESPONSE = (
    '{"prepared_themes": [{"theme_name": "Margin expansion narrative", '
    '"mentions_per_quarter": {"Q1 2026": 2}, "evidence": [{"period": "Q1 2026", '
    '"speaker": "CEO", "text": "margins expanded"}]}], "qa_themes": []}'
)


def _seed_prepared_transcript(repo_root: Path, ticker: str) -> None:
    """Write one prepared-remarks-only transcript so ``_build_themes`` reaches
    the live LLM path with a non-empty payload (no Q&A markers + no DB flag →
    the splitter classifies the whole file as prepared remarks)."""
    d = repo_root / "transcripts" / "processed"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_Q1_2026.txt").write_text(
        "Operator: welcome to the call. CEO: revenue grew 20%, margins expanded.",
        encoding="utf-8",
    )


def _build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: str) -> EarningsSection:
    """Run earnings.build with the themes-split LLM stubbed to return ``response``.

    ``enable_llm=True`` opts into the live themes path; ``tmp_path`` has no
    on-disk themes cache (or portfolio.db) so the stub always runs and the
    prompt-assembly helpers degrade to empty; the builder imports the generator
    lazily, so we patch it on the ``llm_client`` module.
    """
    import llm_client

    def _fake_extractor(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return response

    monkeypatch.setattr(llm_client, "extract_qa_vs_prepared_themes", _fake_extractor)
    _seed_prepared_transcript(tmp_path, "NU")
    return build("NU", tmp_path, enable_llm=True)


@pytest.mark.parametrize(
    "response",
    [
        "",  # the bear-case failure mode: empty completion
        "   \n  ",  # whitespace-only
        "```json\n```",  # fenced but empty after the fence is stripped
        "I could not produce a themes split.",  # prose, not JSON
        '{"prepared_themes": [',  # truncated / malformed JSON
        "[1, 2, 3]",  # valid JSON but not the expected object shape
    ],
)
def test_build_degrades_on_unparseable_themes_response(
    response: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build(monkeypatch, tmp_path, response)
    # The themes sub-feature degrades to empty rollups; the section itself still
    # builds (cards render) without raising and taking the whole build down.
    assert section.status in (SectionStatus.OK, SectionStatus.PARTIAL)
    assert section.prepared_remarks_themes == []
    assert section.qa_themes == []


def test_build_parses_valid_themes_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive control: a well-formed response still populates a theme rollup, so
    # the degradation test above can't be satisfied by a build that never
    # reaches/parses the LLM path.
    section = _build(monkeypatch, tmp_path, _VALID_RESPONSE)
    assert len(section.prepared_remarks_themes) == 1
    assert section.prepared_remarks_themes[0].theme_name == "Margin expansion narrative"


def test_build_survives_parser_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the section-scope parse guard directly: even if the (internally total)
    # parser is later changed to raise, the build must degrade to empty rollups
    # rather than abort. Mirrors the §7 bear-case fix applied at the §6 themes
    # path.
    def _raise(_raw: str) -> object:
        raise ValueError("simulated future parser regression")

    monkeypatch.setattr(earnings_section, "_parse_themes_response", _raise)

    import llm_client

    def _valid(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return _VALID_RESPONSE

    monkeypatch.setattr(llm_client, "extract_qa_vs_prepared_themes", _valid)
    _seed_prepared_transcript(tmp_path, "NU")
    section = build("NU", tmp_path, enable_llm=True)  # must not raise
    assert section.prepared_remarks_themes == []
    assert section.qa_themes == []
