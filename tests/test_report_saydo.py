"""Tests for §6 Say-Do analysis report section."""

from __future__ import annotations

from pathlib import Path

from report.models import SectionStatus
from report.sections.saydo import (
    build as build_saydo,
    _parse_rating,
    _parse_attribution,
    _parse_thesis_view,
)

def test_parse_rating() -> None:
    text = """
### 3. Analyst Verdict
We think management **EXCEEDED** expectations.
"""
    assert _parse_rating(text) == "EXCEEDED"

    text_plain = """
### 3. Analyst Verdict
The performance was mixed.
"""
    assert _parse_rating(text_plain) == "MIXED"

    text_legacy = "Performance Rating: EXCEEDED"
    assert _parse_rating(text_legacy) == "EXCEEDED"

def test_parse_attribution() -> None:
    text = """
### 4. Adversarial Loop
**Resolution:** This is a resolution text.

Some other text.
"""
    assert _parse_attribution(text) == "This is a resolution text."

    text_net_conv = """
### 4. Adversarial Loop
**Net Conviction: High conviction.**
"""
    assert _parse_attribution(text_net_conv) == "Net Conviction: High conviction."

def test_parse_thesis_view() -> None:
    text = """
### 5. Thesis Impact
This is the thesis impact text.

Some other paragraph.
"""
    assert _parse_thesis_view(text) == "This is the thesis impact text."

def test_build_missing_data(tmp_path: Path) -> None:
    section = build_saydo("AMZN", tmp_path)
    assert section.status == SectionStatus.MISSING_DATA
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
