"""Structural and aesthetic contract for the Copilot workspace prototype."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

MOCKUP = Path(__file__).resolve().parents[1] / "mockups" / "copilot_conversation_prototype.html"


class _ContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.actions: set[str] = set()
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        values = dict(attrs)
        if element_id := values.get("id"):
            self.ids.add(element_id)
        if action := values.get("data-action"):
            self.actions.add(action)


def _parse() -> tuple[str, _ContractParser]:
    source = MOCKUP.read_text(encoding="utf-8")
    parser = _ContractParser()
    parser.feed(source)
    return source, parser


def test_copilot_is_one_workspace_with_deep_drawers() -> None:
    source, parser = _parse()

    assert {
        "app-sidebar",
        "copilot-workspace",
        "copilot-topbar",
        "thread-header",
        "conversation-thread",
        "copilot-composer",
        "history-drawer",
        "evidence-drawer",
        "approval-dialog",
        "walkthrough-bar",
        "composer-category",
        "focus-toggle",
    } <= parser.ids

    assert {
        "toggle-history",
        "toggle-evidence",
        "close-drawer",
        "collapse-sidebar",
        "toggle-focus",
        "start-tour",
        "next-question",
        "ask-fact",
        "review-proposal",
        "approve-proposal",
        "cancel-approval",
        "switch-security",
        "new-thread",
        "select-thread",
    } <= parser.actions

    assert "dialog" in parser.tags
    assert "iframe" not in parser.tags
    assert "history-rail" not in source
    assert "company-hero" not in source
    assert "context-grid" not in source
    assert "Stable security history" in source
    assert "Explicit approval required" in source


def test_copilot_uses_the_v6_compact_hierarchy() -> None:
    source, _ = _parse()

    assert "--fs-display: 20px" in source
    assert "--fs-title: 15px" in source
    assert "--fs-body: 13px" in source
    assert "--fs-caption: 11px" in source
    assert "--fs-hero" not in source
    assert "--fs-xl" not in source
    assert "--fs-lg" not in source
    assert "--radius-card: 10px" in source
    assert "--radius-drawer: 14px" in source


def test_copilot_uses_only_canonical_font_assets_and_no_data_calls() -> None:
    source, _ = _parse()

    assert "http://" not in source
    assert "fonts.googleapis.com" in source
    assert "fonts.gstatic.com" in source
    assert source.count("https://") == 3
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source
