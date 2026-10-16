from pathlib import Path

import pytest

from research.method_contract import (
    ResearchInputLimitError,
    ResearchOutputContractError,
    load_research_method,
    validate_research_input,
    validate_research_markdown,
)


def test_method_is_canonical_and_mode_specific() -> None:
    earnings = load_research_method("earnings")
    assert "Q&A: assess the complete exchange" in earnings.instructions
    assert "Moat: test the economic mechanism" in earnings.instructions
    assert len(earnings.source_sha256) == 64
    assert earnings.as_dict()["mode"] == "earnings"


def test_source_edit_changes_identity(tmp_path: Path) -> None:
    original = load_research_method("company")
    source = tmp_path / "method.md"
    source.write_text(original.instructions)
    first = load_research_method("company", source_path=source)
    source.write_text(original.instructions + "\nNew source requirement.\n")
    assert load_research_method("company", source_path=source).source_sha256 != first.source_sha256


def test_incomplete_source_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "method.md"
    source.write_text("## Evidence to judgment\nSome evidence.\n")
    with pytest.raises(ResearchOutputContractError, match="missing"):
        load_research_method("earnings", source_path=source)


@pytest.mark.parametrize(
    "text",
    [
        "## First\nbody\n",
        "## Second\nbody\n## First\nbody\n",
        "## First\n\n## Second\nbody\n",
        "## First\nbody\n## Second\nbody\n## Extra\nbody",
    ],
)
def test_malformed_section_inventory_rejected(text: str) -> None:
    with pytest.raises(ResearchOutputContractError):
        validate_research_markdown(text, ["First", "Second"])


def test_valid_sections_with_nested_heading() -> None:
    validate_research_markdown(
        "## First\nbody\n### Evidence\nmore\n## Second\nbody", ["First", "Second"]
    )


def test_complete_input_bound_counts_unicode() -> None:
    validate_research_input("é" * 3, max_chars=3)
    with pytest.raises(ResearchInputLimitError):
        validate_research_input("é" * 4, max_chars=3)
