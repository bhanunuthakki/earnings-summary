"""Report layout-discipline guard (report spacing-rhythm pass, 2026-08-02).

Owner audit that triggered this file: "spacing in the workspace report...
too much space between vertical sections and too little between horizontal
sometimes... alignment of titles is jacked up." Two symptoms were pinned on
the live NU report and fixed in the same pass this guard protects:
``workspace_sections/earnings.py`` restated a full "Analyst Q&A" ``.panel``
per quarter card (identical title text, once per quarter on file — the
constant-metadata-restated drift design_language §6.2 names), and data
tables carried no explicit ``max-width`` guard against ever being squeezed
narrower than their column.

These are STATIC, CI-runnable proxies — there is no browser in CI, so a
computed ``getBoundingClientRect()`` (the actual visual gap/width/alignment)
is out of reach here; that remains manual/monthly-audit territory
(design_language §7.1, and see ``reference_ui_guard_geometry_blindspot`` in
project memory). Each check below is a structural signal that correlates
with one of the owner's complaints, not a substitute for eyeballing a real
render.

Scope note on (a): the report keeps its OWN spacing scale (``--pad-x/-y``,
``--panel-pad-x/-y``, ``--row-pad-y``, ``--gap``/``--gap-lg``,
``--section-gap``, ``--kpi-pad``, ``--table-pad-y`` — see the rhythm-tokens
comment block at the top of ``workspace_styles.py``), not the dashboard's
``--sp-1..6`` (design_language §6: "The report keeps its own density
tokens... they are layout, owned by the surface"). "Off-scale" below means
off THAT scale.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from report.models import (
    EarningsSection,
    FinancialsSection,
    QAEntry,
    QARosterQuarter,
    QARosterSection,
    QuarterlyEarningsCard,
    SectionStatus,
)
from report.renderers.workspace_sections._shared import _esc, _inline_md
from report.renderers.workspace_sections.earnings import _earnings_tab
from report.renderers.workspace_sections.saydo import _saydo_summary_table
from report.renderers.workspace_sections.thesis_risk import _failure_mode_card
from report.renderers.workspace_styles import CSS as WORKSPACE_CSS
from ui.controls import controls_css as _controls_css
from ui.tokens import palette_css as _palette_css

SRC = Path(__file__).resolve().parents[1] / "src"

# ---------------------------------------------------------------------------
# Shared scanning helpers — each has a self-test proving it discriminates
# real drift from clean CSS/HTML, not just a vacuous pass.
# ---------------------------------------------------------------------------

# The report's own rhythm tokens (design_language §6 scope note above).
_REPORT_SPACING_TOKENS = (
    "--pad-x",
    "--pad-y",
    "--panel-pad-x",
    "--panel-pad-y",
    "--row-pad-y",
    "--gap-lg",
    "--gap",
    "--section-gap",
    "--kpi-pad",
    "--table-pad-y",
)
_SPACING_PROP_RX = re.compile(
    r"(margin(?:-(?:top|bottom|left|right))?|padding(?:-(?:top|bottom|left|right))?"
    r"|gap|row-gap|column-gap):\s*([^;{}]+);"
)
_VAR_RX = re.compile(r"var\([^)]*\)")
_PX_RX = re.compile(r"[0-9.]+px")
_COMMENT_RX = re.compile(r"/\*.*?\*/", re.S)


def _workspace_local_css() -> str:
    """The report's OWN CSS, minus the shared palette/controls prefix every
    page rides (same scoping ``test_ui_controls.py``'s workspace-local test
    uses) — so this guard never flags the shared kit's tokens."""
    return WORKSPACE_CSS.replace(_palette_css("paper"), "").replace(_controls_css("paper"), "")


def _off_scale_spacing_declarations(css: str) -> list[tuple[str, str]]:
    """Every margin/padding/gap declaration carrying a bare px value that
    survives after stripping every ``var(...)`` reference — i.e. a literal
    px number outside any token, whether or not a token is ALSO present
    (catches ``padding: 12px var(--panel-pad-x)`` half-token declarations,
    not only fully-literal ones). CSS comments are stripped first so prose
    mentioning a property name never reads as a rule."""
    text = _COMMENT_RX.sub("", css)
    offenders: list[tuple[str, str]] = []
    for prop, value in _SPACING_PROP_RX.findall(text):
        if "px" not in value:
            continue
        residual = _VAR_RX.sub("", value)
        if _PX_RX.search(residual):
            offenders.append((prop, value.strip()))
    return offenders


def test_off_scale_spacing_scanner_fires_on_synthetic_drift() -> None:
    """Self-test: the scanner catches a literal px margin/padding/gap, and
    does NOT flag a declaration built entirely from report rhythm tokens."""
    dirty = ".x { margin-top: 14px; } .y { padding: 12px var(--panel-pad-x); }"
    found = _off_scale_spacing_declarations(dirty)
    assert ("margin-top", "14px") in found
    assert ("padding", "12px var(--panel-pad-x)") in found
    clean = ".x { margin-top: var(--gap-lg); } .y { padding: var(--row-pad-y) var(--panel-pad-x); }"
    assert _off_scale_spacing_declarations(clean) == []


# Baseline snapshot of PRE-EXISTING off-report-scale spacing literals, taken
# right after this pass's own cleanup (redundant .tab-body-duplicate margins
# deleted; --row-pad-y wired into the qa-head/ir-card/val-row/decision-card/
# failure/transcript-block row family — see the rhythm-tokens comment block
# in workspace_styles.py). A full migration of every remaining literal is a
# separate, much larger pass; this is the SAME shrink-only ratchet
# tests/test_ui_controls.py's QUARANTINE uses (test_quarantine_only_shrinks)
# — new drift must not push the count up, and a future cleanup pass that
# lowers it must lower this number too (grep the diff, don't just relax it).
_SPACING_BASELINE = 217


def test_report_spacing_literals_do_not_regress() -> None:
    """(a) No NEW off-report-scale margin/padding/gap px literal beyond the
    grandfathered baseline. Genuinely-needed exceptions get an inline CSS
    comment explaining why (e.g. the twk-panel's fixed-size micro controls)
    and bump this baseline explicitly, in the same PR, with the count in the
    commit message — never a silent creep."""
    offenders = _off_scale_spacing_declarations(_workspace_local_css())
    assert len(offenders) <= _SPACING_BASELINE, (
        f"{len(offenders)} off-scale spacing literals in the report's local CSS, "
        f"was {_SPACING_BASELINE} — new drift added a literal px margin/padding/gap "
        "instead of a report rhythm token (--pad-*/--panel-pad-*/--row-pad-y/--gap*/"
        f"--section-gap/--kpi-pad/--table-pad-y). New offenders: {offenders[:10]}"
    )


# ---------------------------------------------------------------------------
# (b) No fixed pixel width on a table-shaped selector.
# ---------------------------------------------------------------------------

_RULE_RX = re.compile(r"([^{}]+)\{([^{}]*)\}")
# The selector must BE a table element/class — not a descendant selector
# reaching INTO a table (`.kpi-ledger-table td:first-child { max-width:
# 300px }` legitimately caps one cell's text-wrap width; that is a
# different, valid concern from the whole table's own width and must NOT
# trip this check). Matched against each comma-split selector, trimmed,
# in full — `.match(...)` + `$` via fullmatch semantics below.
_TABLE_OWN_SELECTOR_RX = re.compile(
    r"^(?:table|\.tbl(?:-[\w-]+)?|\.cv2-matrix|\.[\w-]*-table)$", re.I
)
_WIDTH_PX_RX = re.compile(r"(?:^|;)\s*(width|max-width|min-width):\s*[0-9.]+px", re.I)


def _fixed_width_table_rules(css: str) -> list[str]:
    """Any CSS rule whose selector — after splitting a comma-grouped rule
    into its parts — IS a table element/class on its own (``table``, the
    canonical ``.tbl``/``.tbl-*``, ``.cv2-matrix``, or a ``*-table`` class)
    and whose body pins width/max-width/min-width to a literal px — the
    "locked at ~427px regardless of container" symptom the owner named. A
    table must stay fluid (``100%`` / unset) within whatever column or
    panel it renders in."""
    text = _COMMENT_RX.sub("", css)
    offenders: list[str] = []
    for selector_group, body in _RULE_RX.findall(text):
        if not _WIDTH_PX_RX.search(body):
            continue
        for raw_sel in selector_group.split(","):
            sel = raw_sel.strip()
            if _TABLE_OWN_SELECTOR_RX.match(sel):
                offenders.append(f"{sel} {{ {body.strip()} }}")
    return offenders


def test_fixed_width_table_scanner_fires_on_synthetic_drift() -> None:
    """Self-test: the scanner catches a table selector pinned to a literal
    px width, does not flag a fluid one or an unrelated selector, and —
    the actual false-positive this check's first draft hit — does not flag
    a DESCENDANT selector that caps one cell's width for text-wrap, which
    is a legitimate, different concern from the table's own width."""
    dirty = ".coverage-table { width: 427px; border-collapse: collapse; }"
    assert _fixed_width_table_rules(dirty) != []
    fluid = ".tbl { width: 100%; max-width: 100%; }"
    assert _fixed_width_table_rules(fluid) == []
    unrelated = ".stable-panel { width: 300px; }"  # "table" substring trap
    assert _fixed_width_table_rules(unrelated) == []
    cell_wrap_cap = ".kpi-ledger-table td:first-child { max-width: 300px; }"
    assert _fixed_width_table_rules(cell_wrap_cap) == []


def test_no_fixed_width_table_in_report_css() -> None:
    """(b) Every table-shaped selector across the report's CSS surfaces
    (workspace_styles.py + the charts_v2 YoY matrix) stays fluid. Zero
    tolerance — unlike (a), there is no pre-existing baseline to grandfather
    here; every `.tbl`/`.cv2-matrix`/`.fin-drill-table` rule is already
    width:100%/max-width:100% as of this pass."""
    offenders: list[str] = []
    for rel in (
        "report/renderers/workspace_styles.py",
        "report/renderers/charts_v2.py",
    ):
        module_text = (SRC / rel).read_text(encoding="utf-8")
        # Pull the CSS string constants the same crude way the scanners in
        # this file operate — good enough here since both files hold ONE CSS
        # payload each and neither embeds a stray literal "table" in prose
        # inside a triple-quoted CSS block in a way that would produce a
        # false table-selector match (checked at authoring time).
        offenders.extend(_fixed_width_table_rules(module_text))
    assert offenders == [], f"table selector(s) pinned to a fixed px width: {offenders}"


# ---------------------------------------------------------------------------
# (c) No repeated identical panel title inside one rendered tab.
# ---------------------------------------------------------------------------


def _panel_title_texts(html: str) -> list[str]:
    """Every ``.panel-title`` element's visible text, in document order —
    the constant-metadata-restated-per-item drift (design_language §6.2)
    shows up as the SAME text recurring, whether or not the repeats sit
    DOM-adjacent (a toggled quarter-card wrapper can interleave a distinct
    sibling title between two occurrences of a repeated one, so this checks
    "appears more than once" rather than strict adjacency — the earnings.py
    bug this guard protects against was exactly that interleaved shape)."""
    soup = BeautifulSoup(html, "html.parser")
    return [el.get_text(strip=True) for el in soup.select(".panel-title")]


def _duplicate_panel_titles(html: str) -> list[str]:
    texts = _panel_title_texts(html)
    seen: set[str] = set()
    dupes: list[str] = []
    for t in texts:
        if t in seen and t not in dupes:
            dupes.append(t)
        seen.add(t)
    return dupes


def test_duplicate_panel_title_scanner_fires_on_synthetic_drift() -> None:
    """Self-test: reproduces the OLD per-quarter shape this pass removed —
    a title restated once per quarter, with a distinct sibling title
    interleaved between the repeats (so a naive "consecutive" check would
    miss it; this one doesn't)."""
    old_shape = (
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Q1 2026 — prepared remarks</span></div></div>'
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Analyst Q&amp;A</span></div></div>'
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Q4 2025 — prepared remarks</span></div></div>'
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Analyst Q&amp;A</span></div></div>'
    )
    assert _duplicate_panel_titles(old_shape) == ["Analyst Q&A"]
    clean_shape = (
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Analyst Q&amp;A</span></div></div>'
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Q1 2026 — prepared remarks</span></div></div>'
    )
    assert _duplicate_panel_titles(clean_shape) == []


def _render_two_quarter_earnings_tab() -> str:
    """Minimal direct fixture — two full quarters + a matching two-quarter
    Q&A roster — through the actual production renderer, no golden-fixture
    machinery required."""
    cards = [
        QuarterlyEarningsCard(quarter="Q1", year=2026, summary_md="Q1 take.", is_recent=True),
        QuarterlyEarningsCard(quarter="Q4", year=2025, summary_md="Q4 take.", is_recent=False),
    ]
    section = EarningsSection(status=SectionStatus.OK, full_quarters=cards)
    financials = FinancialsSection(status=SectionStatus.OK)
    qa = QARosterSection(
        status=SectionStatus.OK,
        quarters=[
            QARosterQuarter(
                quarter="Q1",
                year=2026,
                entries=[
                    QAEntry(
                        analysts="A. Analyst (Firm)",
                        topic="topic",
                        tag="TAG",
                        question="Q?",
                        answers=[("CEO", "A.")],
                    )
                ],
            ),
            QARosterQuarter(
                quarter="Q4",
                year=2025,
                entries=[
                    QAEntry(
                        analysts="B. Analyst (Firm)",
                        topic="topic",
                        tag="TAG",
                        question="Q?",
                        answers=[("CEO", "A.")],
                    )
                ],
            ),
        ],
    )
    body = StringIO()
    _earnings_tab(body, section, financials, qa, ticker="TEST", repo_root=".")
    return body.getvalue()


def test_earnings_tab_never_repeats_a_panel_title_per_quarter() -> None:
    """(c) The regression this pass fixes: two quarters on file used to
    produce two identical "Analyst Q&A" panel-heads (design_language §6.2 —
    constant metadata restated per item instead of stated once with the
    quarter as the per-item label). ``_qa_roster_panel`` now emits ONE
    panel-head shared across every quarter's toggle body."""
    html = _render_two_quarter_earnings_tab()
    assert html.count(">Analyst Q&amp;A<") == 1
    assert _duplicate_panel_titles(html) == []


# ---------------------------------------------------------------------------
# (d) No raw markdown marker survives into rendered HTML from the Say-Do /
#     thesis-risk section renderers (coordinator scope addition, 2026-08-02:
#     "**Risk-adj. NIM**" / "**Priority #1 — Mexico momentum**" / "**NPL
#     trajectory**" observed live on /reports/NU).
# ---------------------------------------------------------------------------

_RAW_MARKDOWN_RX = re.compile(r"\*\*[^*]|^#{1,6}\s", re.M)


def _rendered_text_has_raw_markdown(html: str) -> bool:
    """Scan only the rendered TEXT NODES for a leaked marker — never
    attribute values. A machine-readable attribute like the failure-mode
    card's ``data-anchor-key`` legitimately carries the raw, un-rendered
    string (design_language §10: an anchor key is a plain-text handle, not
    prose — it is correct for it to skip the render_prose boundary), so
    scanning the whole HTML string would false-positive on that attribute
    even when the VISIBLE text is correctly rendered."""
    text = BeautifulSoup(html, "html.parser").get_text()
    return bool(_RAW_MARKDOWN_RX.search(text))


def test_raw_markdown_scanner_fires_on_the_old_bare_escape_path() -> None:
    """Self-test proving the check discriminates: the OLD code path (bare
    ``_esc``, no prose boundary) leaves the literal markers in; the fix
    (``_inline_md`` — ``ui.prose.render_prose(..., inline=True)``) does not.
    """
    poisoned = "**Risk-adj. NIM**"
    old_path_output = _esc(poisoned)  # what every fixed call site used to do
    assert _RAW_MARKDOWN_RX.search(old_path_output), (
        "self-test is broken: the OLD bare-_esc path should still leak markdown "
        "markers, or this test isn't proving anything"
    )
    fixed_output = _inline_md(poisoned)
    assert not _RAW_MARKDOWN_RX.search(fixed_output)
    assert "<strong>Risk-adj. NIM</strong>" in fixed_output


def test_rendered_text_scanner_ignores_attributes_but_not_text_nodes() -> None:
    """Self-test for the attribute/text-node distinction
    ``_rendered_text_has_raw_markdown`` draws: a raw marker sitting ONLY in
    an attribute value (the legitimate ``data-anchor-key`` case) must not
    fire, but the same marker in the element's visible text must."""
    attr_only = '<div data-anchor-key="**NPL trajectory**">NPL trajectory</div>'
    assert not _rendered_text_has_raw_markdown(attr_only)
    leaked_text = '<div data-anchor-key="NPL trajectory">**NPL trajectory**</div>'
    assert _rendered_text_has_raw_markdown(leaked_text)


@pytest.mark.parametrize(
    "poisoned",
    [
        "**Risk-adj. NIM**",
        "**Priority #1 — Mexico momentum**",
        "**NPL trajectory**",
    ],
)
def test_saydo_metric_cell_strips_raw_markdown(poisoned: str) -> None:
    """(d) Say-Do table metric/attribution/thesis-view cells route stored
    LLM/analyst text through the inline prose boundary — design_language
    §9's "one render boundary per content-kind", scoped to table-cell /
    label contexts via ``_inline_md`` (the block form would inject an
    illegal nested ``<p>`` inside these ``<td>``/``<p>`` containers)."""
    from report.models import SayDoCard

    card = SayDoCard(
        current_quarter="Q1",
        current_year=2026,
        prior_quarter="Q4",
        prior_year=2025,
        saydo_md="n/a",
        attribution=poisoned,
        thesis_view=poisoned,
    )
    body = StringIO()
    _saydo_summary_table(body, [card])
    html = body.getvalue()
    assert not _rendered_text_has_raw_markdown(html), f"raw markdown leaked: {html}"
    assert "<strong>" in html or "<em>" in html or "<code>" in html


@pytest.mark.parametrize(
    "poisoned",
    [
        "**Priority #1 — Mexico momentum**",
        "**Priority #3 — High-income segment**",
        "**NPL trajectory**",
    ],
)
def test_failure_mode_card_strips_raw_markdown(poisoned: str) -> None:
    """(d) The bear-case "Failure modes" panel — the report's risk-narrative
    surface — routes ``FailureMode.hypothesis`` (and its meta-row values)
    through the same inline prose boundary."""
    from report.models import FailureMode

    fm = FailureMode(
        hypothesis=poisoned,
        evidence_in_data=poisoned,
        leading_indicator="n/a",
        quantitative_impact="n/a",
        refutation_criteria="n/a",
    )
    body = StringIO()
    _failure_mode_card(body, 0, fm)
    html = body.getvalue()
    # data-anchor-key legitimately carries the raw, un-rendered string (a
    # plain-text handle, not prose — design_language §10); only the VISIBLE
    # text must be clean.
    assert not _rendered_text_has_raw_markdown(html), f"raw markdown leaked: {html}"
    assert "<strong>" in html
