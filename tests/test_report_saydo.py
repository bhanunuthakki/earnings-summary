"""Tests for §6 Say-Do analysis report section."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from io import StringIO
from pathlib import Path

import pytest

from report.models import SayDoCard, SayDoSection, SectionStatus
from report.sections import saydo
from report.sections.saydo import (
    build as build_saydo,
)


def _parsed_card(tmp_path: Path, text: str) -> SayDoCard:
    cache = tmp_path / ".tmp"
    cache.mkdir(exist_ok=True)
    (cache / "SayDo_TEST_Q1_2026_Q2_2026.txt").write_text(text, encoding="utf-8")
    return build_saydo("TEST", tmp_path).cards[0]


def test_parse_rating(tmp_path: Path) -> None:
    text = """
### 3. Analyst Verdict
We think management **EXCEEDED** expectations.
"""
    assert _parsed_card(tmp_path, text).rating == "EXCEEDED"

    text_plain = """
### 3. Analyst Verdict
The performance was mixed.
"""
    assert _parsed_card(tmp_path, text_plain).rating == "MIXED"

    text_legacy = "Performance Rating: EXCEEDED"
    assert _parsed_card(tmp_path, text_legacy).rating == "EXCEEDED"


def test_parse_attribution(tmp_path: Path) -> None:
    text = """
### 4. Adversarial Loop
**Resolution:** This is a resolution text.

Some other text.
"""
    assert _parsed_card(tmp_path, text).attribution == "This is a resolution text."

    text_net_conv = """
### 4. Adversarial Loop
**Net Conviction: High conviction.**
"""
    assert _parsed_card(tmp_path, text_net_conv).attribution == "Net Conviction: High conviction."


def test_parse_thesis_view(tmp_path: Path) -> None:
    text = """
### 5. Thesis Impact
This is the thesis impact text.

Some other paragraph.
"""
    assert _parsed_card(tmp_path, text).thesis_view == "This is the thesis impact text."


def test_build_missing_data(tmp_path: Path) -> None:
    section = build_saydo("AMZN", tmp_path)
    assert section.status == SectionStatus.MISSING_DATA
    assert section.missing is not None
    assert section.missing.detail is not None
    assert "No SayDo_*.txt files found" in section.missing.detail


def test_build_ok(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()

    card_content = """
### 3. Analyst Verdict
**EXCEEDED**

### 4. Adversarial Loop
**Resolution:** Resolution content here.

### 5. Thesis Impact
Thesis impact content here.
"""
    # Filename: SayDo_{TICKER}_Q{prev}_{prev_yr}_Q{curr}_{curr_yr}.txt
    card_file = tmp_dir / "SayDo_AMZN_Q3_2024_Q4_2024.txt"
    card_file.write_text(card_content, encoding="utf-8")

    section = build_saydo("AMZN", tmp_path)
    assert section.status == SectionStatus.OK
    assert len(section.cards) == 1
    card = section.cards[0]
    assert card.current_quarter == "Q4"
    assert card.current_year == 2024
    assert card.prior_quarter == "Q3"
    assert card.prior_year == 2024
    assert card.rating == "EXCEEDED"
    assert card.attribution == "Resolution content here."
    assert card.thesis_view == "Thesis impact content here."


@pytest.mark.parametrize("leader", ["Metric", "Commitment", "Item", "Promise", "Topic"])
@pytest.mark.parametrize("columns", [3, 4, 5])
def test_preparation_preserves_supported_header_families_and_full_population(
    tmp_path: Path, leader: str, columns: int
) -> None:
    header = " | ".join([leader] + [f"Column {i}" for i in range(1, columns)])
    cells = ["Original label", "Prior wording", "Current wording", "Extra context", "beat"]
    if columns == 3:
        cells = ["Original label", "Current wording", "beat"]
    elif columns == 4:
        cells = ["Original label", "Prior wording", "Current wording", "beat"]
    card = SayDoCard(
        current_quarter="Q2",
        current_year=2026,
        prior_quarter="Q1",
        prior_year=2026,
        saydo_md=f"| {header} |\n| "
        + " | ".join(["---"] * columns)
        + " |\n"
        + "\n".join("| " + " | ".join(cells) + " |" for _ in range(11)),
    )
    original = card.saydo_md
    saydo.prepare_cards("TEST", tmp_path, [card])
    assert len(card.print_vs_guide_rows) == 11
    assert card.selected_row_indices == list(range(8))
    assert card.selection_disposition == "non_llm_fallback"
    row = card.print_vs_guide_rows[0]
    assert row.metric == "Original label"
    assert row.guide == ("" if columns == 3 else "Prior wording")
    assert row.actual == ("Current wording · Extra context" if columns == 5 else "Current wording")
    assert row.verdict == "EXCEEDED"
    assert card.saydo_md == original
    restored = SayDoCard.model_validate_json(card.model_dump_json())
    assert restored.print_vs_guide_rows == card.print_vs_guide_rows
    assert restored.selected_row_indices == card.selected_row_indices


def test_repeated_saydo_render_requires_no_analysis_cache_or_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report.renderers.workspace_sections.saydo import _saydo_tab

    card = SayDoCard(
        current_quarter="Q2",
        current_year=2026,
        prior_quarter="Q1",
        prior_year=2026,
        saydo_md="| Metric | Guide | Actual | Verdict |\n|---|---|---|---|\n"
        "| Revenue [source](https://example.com/issuer) | $1bn | $1.1bn | beat |\n\n"
        "Full argument and source context remain available.",
        rating="EXCEEDED",
        thesis_view="Thesis argument.",
        attribution="Attribution argument.",
    )
    saydo.prepare_cards("TEST", tmp_path, [card])
    section = SayDoSection(status=SectionStatus.OK, cards=[card])

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Rendering must consume the prepared section only")

    for name in (
        "parse_print_vs_guide",
        "prepare_cards",
        "generate_saydo_filter",
        "_load_saydo_filter_cache",
        "_save_saydo_filter_cache",
        "load_thesis_anchor",
        "load_saydo_verdicts",
        "open_repo_db",
    ):
        monkeypatch.setattr(saydo, name, forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    first, second = StringIO(), StringIO()
    _saydo_tab(first, section)
    _saydo_tab(second, section)
    assert first.getvalue() == second.getvalue()
    for expected in (
        "Print vs guide",
        "$1bn",
        "$1.1bn",
        "Thesis argument.",
        "Attribution argument.",
        "Full argument and source context remain available.",
        'href="https://example.com/issuer"',
    ):
        assert expected in first.getvalue()


def test_section_verdicts_distinguish_unconfigured_unavailable_and_empty(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    assert build_saydo("TEST", tmp_path).verdicts_availability == "not_configured"
    with sqlite3.connect(":memory:") as unavailable:
        section = build_saydo("TEST", tmp_path, conn=unavailable)
        assert section.verdicts_availability == "unavailable"
        assert section.verdicts == []
    database = migrated_db(tmp_path / "synthetic.sqlite")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        section = build_saydo("TEST", tmp_path, conn=conn)
        assert section.verdicts_availability == "empty"
        assert section.verdicts == []
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_section_prepares_verdicts_and_preserves_borrowed_connection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "synthetic.sqlite")
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO management_commitments "
            "(ticker, period_made, transcript_segment_id, period_target, kpi_name, comparator, "
            "target_value, unit, narrative, realized_value, outcome, evaluated_at, created_at) "
            "VALUES ('TEST', '2025-10-30', 1, '2025-12-31', 'Cloud growth', 'ge', 30, 'percent', "
            "'Original management wording', 32, 'met', '2026-01-30', '2025-10-30')"
        )
        section = build_saydo("TEST", tmp_path, conn=conn)
        assert section.verdicts_availability == "present"
        assert len(section.verdicts) == 1
        assert section.verdicts[0].narrative == "Original management wording"
        assert section.verdicts[0].realized_value == 32
        assert conn.in_transaction
        assert conn.row_factory is sqlite3.Row
        encoded = section.model_dump_json()
    restored = SayDoSection.model_validate_json(encoded)
    assert restored.verdicts == section.verdicts
