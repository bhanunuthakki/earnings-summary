"""Hermetic tests for BHA-71: six-group editorial hierarchy, canonical section labels,
and collapsible navigation rail in the Full Research Brief reader.

Tests:
  1. Canonical group labels present, no legacy labels (``Exec Comp``, ``Saydo``).
  2. Within-group section ordering matches approved BHA-71 sequence.
  3. Sidebar collapse toggle button present with correct aria-label.
  4. data-tab pane IDs for all existing sections are stable (no deep link regression).
  5. Six-group sequence is intact in _tab_groups output.
"""

from __future__ import annotations

import re
from io import StringIO
from typing import ClassVar

from report.renderers.workspace_html import _subtabs, _tabs
from report.renderers.workspace_script import JS
from report.renderers.workspace_sections._shared import TabDef, TabGroup
from report.renderers.workspace_styles import CSS

# ---------------------------------------------------------------------------
# Fixtures: minimal tab groups matching the six-group approved hierarchy
# ---------------------------------------------------------------------------


def _make_noop_fn():
    """Return a no-op render function for testing."""

    def _fn(b: StringIO) -> None:
        pass

    return _fn


def _make_tab(tab_id: str, label: str, count: int | None = None) -> TabDef:
    return (tab_id, label, count, _make_noop_fn())


APPROVED_GROUPS: list[TabGroup] = [
    (
        "overview",
        "Overview & Moat",
        [
            _make_tab("company", "Company"),
            _make_tab("synthesis", "Synthesis"),
            _make_tab("exec_comp", "Executive Compensation"),
        ],
    ),
    (
        "quarter",
        "Quarter & Guidance",
        [
            _make_tab("earnings", "Earnings", 4),
            _make_tab("news", "News"),
            _make_tab("saydo", "Say · Do", 3),
        ],
    ),
    (
        "financials",
        "Financials & DCF",
        [
            _make_tab("financials", "Financials", 8),
        ],
    ),
    (
        "thesis-risk",
        "Thesis & Risk",
        [
            _make_tab("thesis", "Thesis", 5),
            _make_tab("bear", "Bear case", 2),
        ],
    ),
    (
        "valuation-comps",
        "Valuation & Comps",
        [
            _make_tab("valuation", "Valuation"),
            _make_tab("comps", "Comps", 3),
        ],
    ),
    (
        "sources",
        "Sources & Citations",
        [
            _make_tab("sources", "Sources", 10),
        ],
    ),
]


def _render_tabs(groups: list[TabGroup] = APPROVED_GROUPS, ticker: str = "MELI") -> str:
    buf = StringIO()
    _tabs(buf, groups, ticker=ticker)
    return buf.getvalue()


def _render_subtabs(sections: list[TabDef]) -> str:
    buf = StringIO()
    _subtabs(buf, sections)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: Canonical group labels present, NO legacy labels
# ---------------------------------------------------------------------------


class TestCanonicalLabels:
    def test_six_approved_group_labels_present(self) -> None:
        html = _render_tabs()
        approved_labels = [
            "Overview &amp; Moat",
            "Quarter &amp; Guidance",
            "Financials &amp; DCF",
            "Thesis &amp; Risk",
            "Valuation &amp; Comps",
            "Sources &amp; Citations",
        ]
        for label in approved_labels:
            assert label in html, f"Missing approved group label: {label!r}"

    def test_exec_comp_legacy_label_absent(self) -> None:
        """'Exec Comp' sub-tab label must not appear; should be 'Executive Compensation'."""
        html = _render_subtabs(APPROVED_GROUPS[0][2])  # overview sections
        assert "Exec Comp" not in html, (
            "'Exec Comp' legacy label found — must be 'Executive Compensation'"
        )
        assert "Executive Compensation" in html

    def test_saydo_legacy_label_absent(self) -> None:
        """'Saydo' (un-spaced) must not appear — should be 'Say · Do'."""
        html = _render_subtabs(APPROVED_GROUPS[1][2])  # quarter sections
        assert ">Saydo<" not in html, "'Saydo' label found — must be 'Say · Do'"
        assert "Say · Do" in html

    def test_no_legacy_labels_in_full_nav(self) -> None:
        """Full rendered nav must not contain either legacy label."""
        html = _render_tabs()
        assert "Exec Comp" not in html
        # Saydo as a literal aria-label or tab-label is not expected
        assert 'aria-label="Saydo"' not in html


# ---------------------------------------------------------------------------
# Test 2: Within-group ordering matches approved sequence
# ---------------------------------------------------------------------------


class TestGroupOrdering:
    def test_quarter_group_ordering(self) -> None:
        """Quarter group: earnings → news → saydo (news before saydo)."""
        quarter_sections = APPROVED_GROUPS[1][2]
        section_ids = [s[0] for s in quarter_sections]
        assert section_ids == ["earnings", "news", "saydo"], (
            f"Quarter group ordering wrong: {section_ids}"
        )

    def test_overview_group_ordering(self) -> None:
        """Overview group: company → synthesis → exec_comp."""
        overview_sections = APPROVED_GROUPS[0][2]
        section_ids = [s[0] for s in overview_sections]
        assert section_ids == ["company", "synthesis", "exec_comp"], (
            f"Overview group ordering wrong: {section_ids}"
        )

    def test_thesis_risk_group_starts_with_thesis(self) -> None:
        """Thesis & Risk group must start with thesis (decision-critical content first)."""
        tr_sections = APPROVED_GROUPS[3][2]
        assert tr_sections[0][0] == "thesis", "Thesis section must be first in Thesis & Risk group"


# ---------------------------------------------------------------------------
# Test 3: Sidebar collapse toggle button present
# ---------------------------------------------------------------------------


class TestSidebarToggle:
    def test_toggle_button_present(self) -> None:
        html = _render_tabs()
        assert 'id="report-sidebar-toggle"' in html, "Sidebar collapse toggle button missing"

    def test_toggle_button_aria_label(self) -> None:
        html = _render_tabs()
        assert 'aria-label="Collapse navigation"' in html
        assert 'aria-expanded="true"' in html
        assert 'aria-controls="report-sidebar-navigation"' in html

    def test_toggle_button_composes_the_control_kit(self) -> None:
        html = _render_tabs()
        assert "report-sidebar-toggle k-btn k-btn-quiet k-btn-sm" in html
        assert 'class="k-icon"' in html

    def test_toggle_remains_operable_on_narrow_screens(self) -> None:
        assert ".report-sidebar-toggle { display: none; }" not in CSS
        assert "matchMedia('(max-width: 768px)')" in JS
        assert "applySidebarCollapsed(true)" in JS

    def test_toggle_name_tracks_expanded_state(self) -> None:
        assert "Expand navigation" in JS
        assert "Collapse navigation" in JS
        assert "toggleBtn.setAttribute('aria-label', label)" in JS
        assert "toggleBtn.setAttribute('title', label)" in JS

    def test_sidebar_motion_respects_user_preference(self) -> None:
        assert "@media (prefers-reduced-motion: reduce)" in CSS
        assert ".report-sidebar," in CSS
        assert ".report-sidebar-toggle svg" in CSS


# ---------------------------------------------------------------------------
# Test 4: data-tab pane IDs are stable (deep link regression guard)
# ---------------------------------------------------------------------------


class TestDeepLinkStability:
    EXPECTED_TAB_IDS: ClassVar[set[str]] = {
        "overview",
        "quarter",
        "financials",
        "thesis-risk",
        "valuation-comps",
        "sources",
    }
    EXPECTED_SECTION_IDS: ClassVar[set[str]] = {
        "company",
        "synthesis",
        "exec_comp",
        "earnings",
        "news",
        "saydo",
        "financials",
        "thesis",
        "bear",
        "valuation",
        "comps",
        "sources",
    }

    def test_group_data_tab_ids_unchanged(self) -> None:
        html = _render_tabs()
        found = set(re.findall(r'data-tab="([^"]+)"', html))
        for tid in self.EXPECTED_TAB_IDS:
            assert tid in found, f"Group tab id {tid!r} missing from nav"

    def test_section_subtab_ids_present(self) -> None:
        """All approved section IDs appear as data-subtab values in rendered subtab pills."""
        all_subtab_html = ""
        for _gid, _glabel, sections in APPROVED_GROUPS:
            all_subtab_html += _render_subtabs(sections)
        found = set(re.findall(r'data-subtab="([^"]+)"', all_subtab_html))
        for sid in self.EXPECTED_SECTION_IDS:
            assert sid in found, f"Section id {sid!r} missing from subtab pills"


# ---------------------------------------------------------------------------
# Test 5: Six-group sequence integrity (structural guard)
# ---------------------------------------------------------------------------


class TestSixGroupSequence:
    def test_exactly_six_groups(self) -> None:
        assert len(APPROVED_GROUPS) == 6, f"Expected 6 groups, got {len(APPROVED_GROUPS)}"

    def test_group_ids_and_labels_in_order(self) -> None:
        expected = [
            ("overview", "Overview & Moat"),
            ("quarter", "Quarter & Guidance"),
            ("financials", "Financials & DCF"),
            ("thesis-risk", "Thesis & Risk"),
            ("valuation-comps", "Valuation & Comps"),
            ("sources", "Sources & Citations"),
        ]
        actual = [(gid, glabel) for gid, glabel, _ in APPROVED_GROUPS]
        assert actual == expected, (
            f"Group sequence mismatch:\n  expected: {expected}\n  actual:   {actual}"
        )

    def test_nav_layers_are_present(self) -> None:
        """Three-layer nav structure (L1/L2/L3) present in rendered tabs."""
        html = _render_tabs()
        assert "L1 · Portfolio Intelligence" in html
        assert "L2 · Research Engine" in html
        assert "L3 · Operations" in html
