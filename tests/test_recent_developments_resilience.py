"""Recent-development output remains typed and failures do not populate cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm.contracts import RecentDevelopmentsPayload
from report.models import RecentDevelopmentsSection, SectionStatus
from report.renderers.workspace_data import structure_news_by_section
from report.sections import recent_developments


def _payload() -> RecentDevelopmentsPayload:
    return RecentDevelopmentsPayload.model_validate(
        {
            "search_status": "events_found",
            "window_start": "2026-06-01",
            "window_end": "2026-06-08",
            "queries_run": ["NU recent issuer news"],
            "sources_opened": ["https://example.com/news"],
            "events": [
                {
                    "category": "material_news",
                    "tag": "EARNINGS",
                    "headline": "NU reports results",
                    "published_at": "2026-06-06",
                    "publisher": "Issuer IR",
                    "url": "https://example.com/news",
                    "factual_summary": "The issuer reported its quarterly results.",
                    "thesis_implication": "The report changes the credit growth watch.",
                    "tone": "neu",
                }
            ],
        }
    )


def _build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: RecentDevelopmentsPayload,
) -> RecentDevelopmentsSection:
    def _fake_generate(*args: object, **kwargs: object) -> RecentDevelopmentsPayload:
        del args, kwargs
        return response

    monkeypatch.setattr(recent_developments, "generate_recent_developments", _fake_generate)
    return recent_developments.build(
        ticker="NU", repo_root=tmp_path, enable_llm=True, force_refresh=True
    )


def test_build_stores_typed_output_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _payload()
    section = _build(monkeypatch, tmp_path, response)
    assert section.status == SectionStatus.OK
    assert section.structured_output == response
    assert section.events[0].url == "https://example.com/news"
    assert section.content_md is not None
    cache_path = tmp_path / recent_developments.NEWS_CACHE_DIRNAME / "NU.json"
    cached = getattr(recent_developments, "_read_cache")(tmp_path, cache_path, ttl_days=7)
    assert cached is not None
    assert cached.structured_output == response
    assert section.cached_at is not None


def test_legacy_markdown_cache_requires_explicit_refresh(tmp_path: Path) -> None:
    cache_path = tmp_path / recent_developments.NEWS_CACHE_DIRNAME / "NU.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        '{"cached_at":"2026-06-01T00:00:00+00:00","content_md":"old"}',
        encoding="utf-8",
    )
    section = recent_developments.build("NU", tmp_path, enable_llm=True)
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None
    assert "--refresh-news" in (section.missing.fix_command or "")


def test_workspace_news_uses_typed_events_not_markdown_parse() -> None:
    section = RecentDevelopmentsSection(
        status=SectionStatus.OK,
        content_md="### Legal\n- **untrusted text** — not a typed event",
        structured_output=_payload(),
    )
    tiles = structure_news_by_section(section)
    assert list(tiles) == ["Material events"]
    assert tiles["Material events"][0].tag == "EARNINGS"
    assert tiles["Material events"][0].url == "https://example.com/news"


def test_invalid_event_date_or_unopened_source_fails_contract() -> None:
    payload = _payload().model_dump()
    payload["events"][0]["published_at"] = "2026-99-06"
    with pytest.raises(ValueError):
        RecentDevelopmentsPayload.model_validate(payload)

    payload = _payload().model_dump()
    payload["events"][0]["url"] = "https://example.com/other"
    with pytest.raises(ValueError):
        RecentDevelopmentsPayload.model_validate(payload)
