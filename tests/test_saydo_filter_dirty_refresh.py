"""Dirty-drain refresh behavior for the native Say-Do filter cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from report.models import SayDoCard
from report.renderers import workspace_data
from report.renderers.workspace_data import PrintVsGuideRow


def _card() -> SayDoCard:
    return SayDoCard(
        current_quarter="Q2",
        current_year=2026,
        prior_quarter="Q1",
        prior_year=2026,
        saydo_md="| Metric | Guide | Actual | Direction |\n|---|---|---|---|",
    )


def _rows() -> list[PrintVsGuideRow]:
    return [
        PrintVsGuideRow(
            metric="Revenue",
            guide="$1bn",
            actual="$1.1bn",
            verdict="EXCEEDED",
        )
    ]


def test_force_refresh_bypasses_existing_saydo_filter_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "data" / "saydo_filter" / "NU.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({"by_key": {"stale": ["0"]}}), encoding="utf-8")
    calls: list[str] = []

    def _generate(*args: object, **kwargs: object) -> str:
        del args, kwargs
        calls.append("generated")
        return '["0"]'

    monkeypatch.setattr(workspace_data, "generate_saydo_filter", _generate)

    result = workspace_data.filter_important_print_vs_guide(
        ticker="NU",
        repo_root=tmp_path,
        card=_card(),
        rows=_rows(),
        enable_llm=True,
        force_refresh=True,
        strict_refresh=True,
    )

    assert calls == ["generated"]
    assert [row.metric for row in result] == ["Revenue"]


def test_strict_refresh_propagates_saydo_filter_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(workspace_data, "generate_saydo_filter", _fail)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        workspace_data.filter_important_print_vs_guide(
            ticker="NU",
            repo_root=tmp_path,
            card=_card(),
            rows=_rows(),
            enable_llm=True,
            force_refresh=True,
            strict_refresh=True,
        )


def test_strict_refresh_rejects_unmatched_ids_without_writing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _invalid_selection(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return '["999"]'

    monkeypatch.setattr(workspace_data, "generate_saydo_filter", _invalid_selection)

    with pytest.raises(ValueError, match="invalid commitment ids"):
        workspace_data.filter_important_print_vs_guide(
            ticker="NU",
            repo_root=tmp_path,
            card=_card(),
            rows=_rows(),
            enable_llm=True,
            force_refresh=True,
            strict_refresh=True,
        )

    assert not (tmp_path / "data" / "saydo_filter" / "NU.json").exists()
