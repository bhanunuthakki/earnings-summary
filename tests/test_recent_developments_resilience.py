"""§8 recent-developments must not raise on a transient empty / odd LLM response.

Unlike the §6/§7 LLM sections, recent-developments performs NO structured parse —
``generate_recent_developments`` returns markdown that is stored verbatim in
``content_md``. This test pins that contract: an empty / non-JSON / odd response
is stored as-is and the section builds OK, so the bug class (an unguarded parse
of a live LLM response aborting ``build_report``) cannot be re-introduced here by
a future change that starts parsing the response.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from report.models import RecentDevelopmentsSection, SectionStatus
from report.sections import recent_developments


def _build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: str
) -> RecentDevelopmentsSection:
    """Run recent_developments.build with the news LLM stubbed to return
    ``response``. ``force_refresh=True`` bypasses the on-disk cache so the stub
    always runs; the anchor loaders read ``tmp_path`` and degrade to empty."""

    def _fake_generate(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return response

    monkeypatch.setattr(recent_developments, "generate_recent_developments", _fake_generate)
    return recent_developments.build(
        ticker="NU", repo_root=tmp_path, enable_llm=True, force_refresh=True
    )


@pytest.mark.parametrize(
    "response",
    [
        "",  # empty completion
        "   \n  ",  # whitespace-only
        "```json\n```",  # fenced but empty
        "[1, 2, 3]",  # JSON array — no parse, stored as-is
        "### Material news\n- **Thing happened** — implication. [Source: X]",  # normal
    ],
)
def test_build_stores_response_verbatim_without_raising(
    response: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build(monkeypatch, tmp_path, response)
    # No structured parse → whatever came back is stored as content; the build
    # never raises regardless of the response's shape.
    assert section.status == SectionStatus.OK
    assert section.content_md == response
    assert section.cached_at is not None


def test_existing_markdown_cache_replays_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / recent_developments.NEWS_CACHE_DIRNAME / "NU.json"
    cache_path.parent.mkdir(parents=True)
    content = "### Material news\n- **Existing event** — Recorded context. [Source: Issuer]"
    cache_path.write_text(
        json.dumps({"cached_at": datetime.now(UTC).isoformat(), "content_md": content}),
        encoding="utf-8",
    )

    def no_generation(*args: object, **kwargs: object) -> str:
        raise AssertionError("Completed Markdown cache must replay without a model call")

    monkeypatch.setattr(recent_developments, "generate_recent_developments", no_generation)
    section = recent_developments.build("NU", tmp_path, enable_llm=False)

    assert section.status == SectionStatus.OK
    assert section.content_md == content
