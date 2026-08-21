"""Focused contract tests for the research/ledger visual family."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE, research_panel_style

_FAMILY = (
    "peeks",
    "explore_panel",
    "journal_panel",
    "decision_journal_panel",
    "ledger_panel",
    "ledger_console_panel",
    "thesis_ledger_panel",
    "source_viewers",
    "source_calls_panel",
)


def test_family_recipe_is_closed_and_token_only() -> None:
    assert research_panel_style() == RESEARCH_PANEL_STYLE
    assert RESEARCH_PANEL_STYLE.startswith("<style>")
    assert RESEARCH_PANEL_STYLE.endswith("</style>")
    assert RESEARCH_PANEL_STYLE.count("<style>") == 1
    assert RESEARCH_PANEL_STYLE.count("</style>") == 1
    assert "Panel renderers own data" not in RESEARCH_PANEL_STYLE
    assert "transcript reader + 10-K section reader" not in RESEARCH_PANEL_STYLE
    assert "var(--" in RESEARCH_PANEL_STYLE
    assert not any(token in RESEARCH_PANEL_STYLE for token in ("#fff", "#000", "rgba("))


def test_family_consumers_route_through_master_recipe() -> None:
    for name in _FAMILY:
        module = importlib.import_module(f"pipeline.{name}")
        module_file = module.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        assert "research_panel_styles" in source, name
        assert not re.search(r"^\s*\.[A-Za-z][\w-]*(?:\.[\w-]+)?\s*\{", source, re.MULTILINE), name
