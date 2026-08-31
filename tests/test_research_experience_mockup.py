"""Structural contract for the front-end-only company research prototype."""

import re
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCKUP = REPO_ROOT / "mockups" / "company_research_experience.html"
DESIGN_LANGUAGE = REPO_ROOT / "directives" / "design_language.md"


class _MockupScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.layouts: set[str] = set()
        self.actions: set[str] = set()
        self.capabilities: set[str] = set()
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        values = dict(attrs)
        if element_id := values.get("id"):
            self.ids.add(element_id)
        if layout := values.get("data-layout"):
            self.layouts.add(layout)
        if action := values.get("data-action"):
            self.actions.add(action)
        if capability := values.get("data-capability"):
            self.capabilities.add(capability)


def _scan() -> _MockupScan:
    parser = _MockupScan()
    parser.feed(MOCKUP.read_text(encoding="utf-8"))
    return parser


def test_research_modes_are_distinct_and_not_nested() -> None:
    scan = _scan()

    assert {
        "screen-company-desk",
        "screen-brief-library",
        "screen-full-brief",
    } <= scan.ids
    assert {"decision-workbench", "report-library", "editorial-document"} <= scan.layouts
    assert "iframe" not in scan.tags


def test_mockup_covers_the_primary_research_interactions() -> None:
    scan = _scan()

    assert {
        "navigate",
        "switch-company",
        "switch-desk-tab",
        "open-brief",
        "open-drawer",
        "open-source",
        "toggle-ask",
        "close-overlay",
        "edit-card",
        "chat-card",
        "review-capabilities",
    } <= scan.actions


def test_brief_inherits_the_workspace_visual_language() -> None:
    html = MOCKUP.read_text(encoding="utf-8")
    design_language = DESIGN_LANGUAGE.read_text(encoding="utf-8")
    normalized_design_language = " ".join(design_language.split())

    assert "--reader:" not in html
    assert "--mark:" not in html
    assert "border-left-color:" not in html
    assert "font-family: var(--serif)" not in html
    assert ".k-doc {" in html
    assert "background: var(--surface)" in html
    assert "global tokens and controls" in normalized_design_language
    assert "Consumers must not add local visual CSS" in normalized_design_language


def test_mockup_uses_the_dashboard_type_and_density_rhythm() -> None:
    html = MOCKUP.read_text(encoding="utf-8")
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    literal_font_sizes = set(re.findall(r"--fs-[\w-]+:\s*([\d.]+px)", root))

    assert literal_font_sizes == {"20px", "15px", "13px", "11px"}
    assert "--fs-header-title: var(--fs-title)" in root
    assert "--fs-micro: var(--fs-caption)" in root
    assert "--fs-mono-sm: var(--fs-caption)" in root
    assert "var(--sp-7)" not in html
    assert "var(--sp-8)" not in html
    assert ".desk-stack { display: flex; flex-direction: column; gap: var(--sp-3); }" in css
    assert ".panel-body { padding: var(--sp-3); }" in css
    assert (
        ".brief-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3); }"
        in css
    )
    assert ".doc-section { padding: var(--sp-4) var(--sp-5);" in css
    normalized_design_language = " ".join(DESIGN_LANGUAGE.read_text(encoding="utf-8").split())
    assert "four visible roles: display, title, body, and meta" in normalized_design_language
    assert "Use the spacing ladder and registered grid" in normalized_design_language


def test_contextual_card_actions_are_accessible_and_self_describing() -> None:
    html = MOCKUP.read_text(encoding="utf-8")
    scan = _scan()

    assert ".k-card:hover .card-actions" in html
    assert ".k-card:focus-within .card-actions" in html
    assert "@media (hover: none)" in html
    assert {
        "research.change_feed.chat",
        "research.thesis_contracts.chat",
        "research.thesis_contracts.edit",
        "research.decision_kpis.chat",
        "research.open_questions.chat",
        "research.open_questions.edit",
        "research.catalysts.chat",
        "research.latest_brief.chat",
        "research.capability_catalog.review",
    } <= scan.capabilities
    assert "Ready now" in html
    assert "Adapter needed" in html
    assert "New governed" in html
    assert "Owner approval remains the write boundary" in html
    assert "docs/design/company_research_interaction_catalog.md" not in html
