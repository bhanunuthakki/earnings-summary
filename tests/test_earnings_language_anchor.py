"""Owner watch context survives a long thesis without changing legacy anchors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest

from llm import anchors


def _no_statistics(_repo_root: Path, _ticker: str, _payload: dict[str, object]) -> list[str]:
    return []


@pytest.fixture(autouse=True)
def isolate_statistics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anchors, "statistical_patterns_block", _no_statistics)


def _write_holdings(root: Path, payload: dict[str, object]) -> None:
    folder = root / "micro_thesis" / "holdings"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SYN.json").write_text(json.dumps(payload), encoding="utf-8")


class WatchRecord(TypedDict):
    topic: str
    phrases: list[str]
    question: str


def _six_watches() -> list[WatchRecord]:
    return [
        {
            "topic": f"Owner watch topic {index}",
            "phrases": [f"complete phrase {index}.{phrase}" for phrase in range(7)],
            "question": f"What changed in observation {index}, and what evidence supports it?",
        }
        for index in range(6)
    ]


def test_all_six_watches_survive_long_thesis_and_composition(tmp_path: Path) -> None:
    watches = _six_watches()
    _write_holdings(
        tmp_path,
        {"thesis": "Long owner narrative. " * 1000, "earnings_language_watchlist": watches},
    )
    thesis = anchors.load_thesis_anchor(tmp_path, "SYN")
    composed = anchors.compose_anchor_block(thesis, "bear " * 2000, "IR " * 2000)
    for watch in watches:
        assert str(watch["topic"]) in thesis
        assert str(watch["question"]) in composed
        for phrase in watch["phrases"]:
            assert phrase in thesis
            assert phrase in composed
    assert thesis.index("Owner watch topic 5") < thesis.index("**Thesis statement:**")
    assert "not reported facts or automatic detections" in thesis
    assert "[...truncated]" in thesis
    assert len(thesis) <= anchors.ANCHOR_BLOCK_CHAR_CAP + len("\n[...truncated]")
    assert "Watch records omitted" not in thesis


def test_legacy_small_anchor_output_is_exact(tmp_path: Path) -> None:
    _write_holdings(
        tmp_path,
        {
            "thesis": "The owner thesis.",
            "key_driver": "Demand",
            "tier_1_kpis": [{"name": "Metric A", "break_condition": "observed decline"}],
            "business_model_rules": [{"narrative": "Review comparable quarters."}],
        },
    )
    assert anchors.load_thesis_anchor(tmp_path, "SYN") == (
        "## THESIS ANCHOR (analyst's own framing of this name)\n"
        "\n**Thesis statement:**\nThe owner thesis.\n"
        "\n**Key driver tracked:** Demand\n"
        "\n**Tier-1 KPIs (with break conditions):**\n"
        "- **Metric A** — breaks if observed decline\n"
        "\n**Quantitative thesis-breakers:**\n- Review comparable quarters."
    )


def test_legacy_long_anchor_truncation_is_exact(tmp_path: Path) -> None:
    thesis = "legacy " * 1000
    _write_holdings(tmp_path, {"thesis": thesis})
    untrimmed = (
        "## THESIS ANCHOR (analyst's own framing of this name)\n"
        f"\n**Thesis statement:**\n{thesis.strip()}"
    )
    assert anchors.load_thesis_anchor(tmp_path, "SYN") == (
        untrimmed[: anchors.ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    )


@pytest.mark.parametrize("watchlist", [None, []])
def test_empty_watch_field_keeps_legacy_output(tmp_path: Path, watchlist: object) -> None:
    _write_holdings(tmp_path, {"thesis": "Owner thesis."})
    legacy = anchors.load_thesis_anchor(tmp_path, "SYN")
    _write_holdings(tmp_path, {"thesis": "Owner thesis.", "earnings_language_watchlist": watchlist})
    assert anchors.load_thesis_anchor(tmp_path, "SYN") == legacy


def test_invalid_watch_format_is_visible_and_tolerated(tmp_path: Path) -> None:
    _write_holdings(
        tmp_path,
        {"thesis": "Owner thesis.", "earnings_language_watchlist": {"topic": "wrong shape"}},
    )
    result = anchors.load_thesis_anchor(tmp_path, "SYN")
    assert "Watch list omitted: invalid format" in result
    assert "Owner thesis." in result


def test_malformed_records_are_omitted_whole_with_count(tmp_path: Path) -> None:
    valid = {"topic": "Valid topic", "phrases": ["whole valid phrase"], "question": "Evidence?"}
    _write_holdings(
        tmp_path,
        {
            "earnings_language_watchlist": [
                valid,
                "wrong shape",
                {"topic": "Missing question", "phrases": ["not admitted cue"]},
                {"topic": "Invalid phrase", "phrases": [1, "other cue"], "question": "Why?"},
            ],
        },
    )
    result = anchors.load_thesis_anchor(tmp_path, "SYN")
    assert "whole valid phrase" in result
    assert "3 invalid; 0 exceed" in result
    assert "Missing question" not in result
    assert "other cue" not in result


def test_oversized_record_is_not_cut_and_later_record_remains(tmp_path: Path) -> None:
    _write_holdings(
        tmp_path,
        {
            "earnings_language_watchlist": [
                {
                    "topic": "Oversized topic",
                    "phrases": ["giant-phrase-" * 200],
                    "question": "Why?",
                },
                {
                    "topic": "Later topic",
                    "phrases": ["later whole phrase"],
                    "question": "Which evidence?",
                },
            ],
        },
    )
    result = anchors.load_thesis_anchor(tmp_path, "SYN")
    assert "Oversized topic" not in result
    assert "giant-phrase" not in result
    assert "later whole phrase" in result
    assert "Which evidence?" in result
    assert "0 invalid; 1 exceed" in result
    assert len(result) < anchors.ANCHOR_BLOCK_CHAR_CAP


def test_many_records_have_explicit_bounded_omissions(tmp_path: Path) -> None:
    _write_holdings(tmp_path, {"earnings_language_watchlist": _six_watches() * 20})
    result = anchors.load_thesis_anchor(tmp_path, "SYN")
    assert "Watch records omitted" in result
    assert "Review the saved holdings file." in result
    assert "[...truncated]" not in result
    assert len(result) < anchors.ANCHOR_BLOCK_CHAR_CAP
