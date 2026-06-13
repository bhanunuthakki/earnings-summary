"""The prose render boundary (Instrument Paradigm rule 6 — src/ui/prose.py).

The directive names exactly ONE renderer per content-kind: every stored
analyst/LLM body / narrative / memo / note rendered to HTML passes through
``ui.prose.render_prose`` (the inline variant for table cells). This guard pins
that contract so the "markdown leaking into rendered prose" class of miss — born
of THREE divergent renderers + ~7 bare-``escape()`` body sites — cannot regress:

1. ``render_prose`` is a strict superset of the three renderers it replaced
   (headings, bold, italic, inline code, bullets, pipe tables, ``<hr>``), always
   escapes its input, and the inline variant emits no block tags.
2. Opt-out denial: no module outside an explicit, documented allowlist contains
   a markdown→HTML renderer (the bold-substitution signature). New renderers are
   caught; the four sanctioned exceptions each carry a reason.
3. The three former server renderers are now thin re-exports of the boundary.
4. The enumerated genuinely-markdown surfaces route their prose field through
   ``render_prose`` and no longer bare-``escape()`` it.
5. The two deterministic non-markdown fields (attribution narrative, evals judge
   rationale) deliberately STAY ``escape()``d — ``render_prose`` would corrupt
   them — so the carve-out is asserted, not assumed.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.prose import render_prose  # noqa: E402

# The tell-tale of a markdown→HTML renderer: the bold-span regex. Present in the
# Python ``re`` form and the JS ``.replace`` form alike, so one substring scan
# catches a re-rolled renderer in either language.
_RENDERER_SIGNATURE = r"\*\*([^*]+)\*\*"

# Opt-out allowlist — the ONLY files permitted to carry the signature, each with
# a documented reason. An unlisted hit fails CI (a new fourth renderer); a new
# exception must be added here WITH its rationale.
_SIGNATURE_ALLOWLIST: dict[str, str] = {
    "src/ui/prose.py": "the canonical prose render boundary",
    "src/pipeline/ask_dock.py": (
        "pinned JS inline-subset mirror — Ask streams tokens + threads cite-marks "
        "client-side, so it cannot be server-rendered"
    ),
    "src/report/renderers/workspace_chat.py": (
        "pinned JS inline-subset mirror — the iframe chat streams tokens "
        "client-side (same category as ask_dock); cannot be server-rendered"
    ),
    "src/report/renderers/workspace_data.py": (
        "markdown STRIPPER (**x** -> plaintext x for a news-tile gloss), not an "
        "HTML renderer — emits no tags"
    ),
}


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. render_prose — the superset contract
# ---------------------------------------------------------------------------


def test_block_pass_renders_the_full_markdown_subset() -> None:
    assert "<strong>b</strong>" in render_prose("**b**")
    assert "<em>i</em>" in render_prose("*i*")
    assert "<code>c</code>" in render_prose("`c`")
    assert render_prose("plain").startswith("<p>")
    # Headings shift two levels (a panel already owns the <h2>): # -> h3, ## -> h4.
    assert "<h3>Top</h3>" in render_prose("# Top")
    assert "<h4>Sub</h4>" in render_prose("## Sub")
    assert "<ul><li>a</li><li>b</li></ul>" in render_prose("- a\n- b")


def test_pipe_tables_and_hr_are_part_of_the_superset() -> None:
    # Pipe tables came from the workspace renderer; <hr> from the dashboard one.
    # render_prose must carry BOTH so no surface loses a capability on collapse.
    table = render_prose("| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert '<table class="tbl">' in table
    assert "<th>A</th>" in table and "<td>1</td>" in table
    assert render_prose("---") == "<hr>"


def test_render_prose_always_escapes_its_input() -> None:
    out = render_prose("<script>alert(1)</script> **x**")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>x</strong>" in out  # still renders markdown around escaped text


def test_inline_variant_emits_spans_but_no_block_tags() -> None:
    out = render_prose("**bold** then ## not-a-heading\n- not-a-bullet", inline=True)
    assert "<strong>bold</strong>" in out
    for block in ("<p>", "<h3", "<h4", "<ul>", "<li>", "<table"):
        assert block not in out, f"inline output must not contain {block!r}"
    # Block markers survive as literal (escaped) text rather than break a cell.
    assert "## not-a-heading" in out


def test_empty_input_is_empty_output() -> None:
    assert render_prose("") == ""
    assert render_prose("", inline=True) == ""


# ---------------------------------------------------------------------------
# 2. Opt-out denial — exactly one renderer family, the rest documented
# ---------------------------------------------------------------------------


def test_no_undeclared_markdown_renderer_outside_ui_prose() -> None:
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(PROJECT_ROOT).as_posix()
        if rel in _SIGNATURE_ALLOWLIST:
            continue
        if _RENDERER_SIGNATURE in py.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, (
        "Markdown renderer signature found outside ui.prose. Route stored prose "
        f"through ui.prose.render_prose, or document the exception in this test's "
        f"_SIGNATURE_ALLOWLIST. Offenders: {offenders}"
    )


def test_allowlist_entries_still_exist_and_carry_the_signature() -> None:
    # Keep the allowlist honest: a stale entry (file moved/renamed, or the
    # signature genuinely removed) should be pruned, not left rotting.
    for rel in _SIGNATURE_ALLOWLIST:
        assert (PROJECT_ROOT / rel).exists(), f"allowlisted file missing: {rel}"
        assert _RENDERER_SIGNATURE in _read(rel), (
            f"allowlist entry {rel} no longer carries the signature — prune it"
        )


# ---------------------------------------------------------------------------
# 3. The three former renderers are now thin re-exports of the boundary
# ---------------------------------------------------------------------------


def test_workspace_render_markdown_is_the_boundary() -> None:
    from report.renderers.workspace_sections import _shared

    assert _shared._render_markdown is render_prose
    assert _shared._inline_md("**b**") == render_prose("**b**", inline=True)


def test_dashboard_light_markdown_delegates_to_boundary() -> None:
    from pipeline.analytical_dashboard_html import light_markdown_to_html

    # Output identity is the real contract; the source check guards against a
    # future re-fork of the body.
    assert light_markdown_to_html("## H\n**b**") == render_prose("## H\n**b**")
    src = _read("src/pipeline/analytical_dashboard_html.py")
    assert "def light_markdown_to_html" in src
    assert "render_prose(md)" in src


# ---------------------------------------------------------------------------
# 4. The enumerated genuinely-markdown surfaces route through render_prose
# ---------------------------------------------------------------------------

# (rel path, must-appear render_prose call, must-be-GONE bare-escape fragment)
_ROUTED_SURFACES: list[tuple[str, str, str | None]] = [
    ("src/pipeline/advisor_memos_panel.py", "render_prose(m.body_md", None),
    ("src/pipeline/peeks.py", "render_prose(body_md", None),
    ("src/pipeline/journal_panel.py", "render_prose(n.body)", 'jr-body">{escape(n.body)}'),
    (
        "src/pipeline/ticker_command_center.py",
        "render_prose(n.body)",
        'rail-note-body">{escape(n.body)}',
    ),
    (
        "src/pipeline/allocation_decisions_panel.py",
        "render_prose(e.body, inline=True)",
        'ad-body">{escape(e.body)}',
    ),
    (
        "src/pipeline/thesis_ledger_panel.py",
        "render_prose(e.body, inline=True)",
        'tl-body">{escape(e.body)}',
    ),
]


def test_enumerated_prose_surfaces_route_through_render_prose() -> None:
    for rel, must_have, must_be_gone in _ROUTED_SURFACES:
        text = _read(rel)
        assert must_have in text, f"{rel} should render its prose via {must_have!r}"
        if must_be_gone is not None:
            assert must_be_gone not in text, (
                f"{rel} still bare-escapes its prose field ({must_be_gone!r}); "
                "route it through render_prose"
            )


def test_report_sections_render_through_the_boundary() -> None:
    # The report section renderers call the workspace _render_markdown alias,
    # which IS render_prose — so sampling a couple keeps that wiring asserted.
    for rel in (
        "src/report/renderers/workspace_sections/synthesis.py",
        "src/report/renderers/workspace_sections/earnings.py",
    ):
        assert "_render_markdown(" in _read(rel)


# ---------------------------------------------------------------------------
# 5. Deterministic non-markdown fields STAY escape()d (the explicit carve-out)
# ---------------------------------------------------------------------------


def test_deterministic_fields_keep_escape() -> None:
    # render_prose would corrupt these machine-authored, non-markdown strings,
    # so they are deliberately excluded from the boundary.
    attr = _read("src/pipeline/attribution_panel.py")
    assert "escape(a.narrative)" in attr
    assert "render_prose(a.narrative" not in attr

    evals = _read("src/pipeline/evals_panel.py")
    assert "escape(c.judge_rationale)" in evals
    assert "render_prose(c.judge_rationale" not in evals
