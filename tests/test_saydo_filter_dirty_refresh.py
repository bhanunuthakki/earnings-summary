"""Prepared Say/Do selection retains its cache, invalidation and strict refresh."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from report.models import SayDoCard
from report.sections import saydo


def _card(*, count: int = 1, quarter: str = "Q2") -> SayDoCard:
    return SayDoCard(
        current_quarter=quarter,
        current_year=2026,
        prior_quarter="Q1",
        prior_year=2026,
        saydo_md="| Metric | Guide | Actual | Direction |\n|---|---|---|---|\n"
        + "\n".join(f"| Revenue {i} | $1bn | $1.1bn | beat |" for i in range(count)),
    )


def _empty_anchor(*_args: object) -> str:
    return ""


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "load_thesis_anchor",
        "load_bear_anchor",
        "load_ir_anchor",
        "load_priors_anchor",
        "load_worldview_anchor",
        "load_owner_profile_anchor",
    ):
        monkeypatch.setattr(saydo, name, _empty_anchor)


def test_model_cache_and_forced_refresh_share_the_existing_key_and_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _card()
    calls: list[str] = []

    def generate(*_args: object, **_kwargs: object) -> str:
        calls.append("generated")
        return '["0"]'

    monkeypatch.setattr(saydo, "generate_saydo_filter", generate)
    saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
    assert card.selection_disposition == "model"
    assert card.selected_row_indices == [0]
    payload = [
        {
            "id": "0",
            "metric": "Revenue 0",
            "guide": "$1bn",
            "actual": "$1.1bn",
            "verdict": "EXCEEDED",
        }
    ]
    digest = hashlib.sha256()
    digest.update(b"Q22026\x00")
    digest.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    digest.update(b"\x00")
    saved = json.loads((tmp_path / "data/saydo_filter/NU.json").read_text())
    assert saved == {"by_key": {digest.hexdigest(): ["0"]}}

    saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
    assert card.selection_disposition == "cache"
    saydo.prepare_cards(
        "NU", tmp_path, [card], enable_llm=True, force_refresh=True, strict_refresh=True
    )
    assert card.selection_disposition == "model"
    assert calls == ["generated", "generated"]


@pytest.mark.parametrize("response", ["[]", '["999"]', '["0", "999"]'])
def test_strict_refresh_rejects_invalid_ids_without_writing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    def invalid(*_args: object, **_kwargs: object) -> str:
        return response

    monkeypatch.setattr(saydo, "generate_saydo_filter", invalid)
    with pytest.raises(ValueError, match="invalid commitment ids"):
        saydo.prepare_cards(
            "NU", tmp_path, [_card()], enable_llm=True, force_refresh=True, strict_refresh=True
        )
    assert not (tmp_path / "data/saydo_filter/NU.json").exists()


def test_provider_failure_is_fatal_only_for_strict_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _card(count=11)

    def fail(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("synthetic provider unavailable")

    monkeypatch.setattr(saydo, "generate_saydo_filter", fail)
    with pytest.raises(RuntimeError, match="synthetic provider unavailable"):
        saydo.prepare_cards(
            "NU", tmp_path, [card], enable_llm=True, force_refresh=True, strict_refresh=True
        )
    saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
    assert card.selection_disposition == "failed_fallback"
    assert card.selected_row_indices == list(range(8))
    assert len(card.print_vs_guide_rows) == 11
    assert not (tmp_path / "data/saydo_filter/NU.json").exists()


@pytest.mark.parametrize(
    "anchor_name",
    [
        "load_thesis_anchor",
        "load_bear_anchor",
        "load_ir_anchor",
        "load_priors_anchor",
        "load_worldview_anchor",
        "load_owner_profile_anchor",
    ],
)
def test_context_loads_once_per_section_and_invalidates_cached_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anchor_name: str
) -> None:
    anchors = {"thesis": "thesis A"}
    context_calls: list[str] = []
    generations: list[str] = []
    cards = [_card(), _card(quarter="Q3")]

    def thesis(*_args: object) -> str:
        context_calls.append("thesis")
        return anchors["thesis"]

    def generate(*_args: object, **_kwargs: object) -> str:
        generations.append("generated")
        return '["0"]'

    monkeypatch.setattr(saydo, anchor_name, thesis)
    monkeypatch.setattr(saydo, "generate_saydo_filter", generate)
    saydo.prepare_cards("NU", tmp_path, cards, enable_llm=True)
    saydo.prepare_cards("NU", tmp_path, cards, enable_llm=True)
    assert context_calls == ["thesis", "thesis"]
    assert generations == ["generated", "generated"]
    assert [card.selection_disposition for card in cards] == ["cache", "cache"]
    anchors["thesis"] = "thesis B"
    saydo.prepare_cards("NU", tmp_path, cards, enable_llm=True)
    assert len(generations) == 4
    cards[0].saydo_md += "\n| FCF | $2 | $3 | beat |"
    saydo.prepare_cards("NU", tmp_path, cards, enable_llm=True)
    assert len(generations) == 5


def test_non_llm_preparation_keeps_all_rows_without_context_or_cache_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _card(count=11)

    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("Non-LLM preparation must not load context, models or cache")

    monkeypatch.setattr(saydo, "load_thesis_anchor", forbidden)
    monkeypatch.setattr(saydo, "_load_saydo_filter_cache", forbidden)
    monkeypatch.setattr(saydo, "generate_saydo_filter", forbidden)
    saydo.prepare_cards("NU", tmp_path, [card])
    assert card.selection_disposition == "non_llm_fallback"
    assert card.selected_row_indices == list(range(8))
    assert len(card.print_vs_guide_rows) == 11


def test_no_parsable_rows_have_a_distinct_disposition_and_fail_strict_refresh(
    tmp_path: Path,
) -> None:
    card = _card(count=0)
    saydo.prepare_cards("NU", tmp_path, [card])
    assert card.print_vs_guide_rows == []
    assert card.selected_row_indices == []
    assert card.selection_disposition == "no_parsable_rows"
    with pytest.raises(RuntimeError, match="no parseable commitments"):
        saydo.prepare_cards(
            "NU", tmp_path, [card], enable_llm=True, force_refresh=True, strict_refresh=True
        )


def test_corrupt_cache_is_repaired_by_existing_selection_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "data/saydo_filter/NU.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("[]", encoding="utf-8")

    def generate(*_args: object, **_kwargs: object) -> str:
        return '["0"]'

    monkeypatch.setattr(saydo, "generate_saydo_filter", generate)
    card = _card()
    saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
    assert card.selection_disposition == "model"
    assert list(json.loads(cache.read_text())) == ["by_key"]


@pytest.mark.parametrize(
    ("response", "expected"), [('["2", "999", "0"]', [0, 2]), ('["0", "999"]', [0])]
)
def test_non_strict_selection_retains_valid_subset_and_caches_without_rebilling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str, expected: list[int]
) -> None:
    card = _card(count=11)
    generations: list[str] = []

    def generate(*_args: object, **_kwargs: object) -> str:
        generations.append("generated")
        return response

    monkeypatch.setattr(saydo, "generate_saydo_filter", generate)
    saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
    assert card.selection_disposition == "model"
    assert card.selected_row_indices == expected
    saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
    assert card.selection_disposition == "cache"
    assert card.selected_row_indices == expected
    assert len(card.print_vs_guide_rows) == 11
    assert generations == ["generated"]
    before = (tmp_path / "data/saydo_filter/NU.json").read_bytes()
    with pytest.raises(ValueError, match="invalid commitment ids"):
        saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True, strict_refresh=True)
    assert (tmp_path / "data/saydo_filter/NU.json").read_bytes() == before
    assert generations == ["generated"]


@pytest.mark.parametrize("response", ["[]", '["999"]'])
def test_non_strict_no_valid_ids_caches_the_response_and_preserves_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    card = _card(count=11)
    generations: list[str] = []

    def generate(*_args: object, **_kwargs: object) -> str:
        generations.append("generated")
        return response

    monkeypatch.setattr(saydo, "generate_saydo_filter", generate)
    for _ in range(2):
        saydo.prepare_cards("NU", tmp_path, [card], enable_llm=True)
        assert card.selection_disposition == "failed_fallback"
        assert card.selected_row_indices == list(range(8))
        assert len(card.print_vs_guide_rows) == 11
    assert generations == ["generated"]
