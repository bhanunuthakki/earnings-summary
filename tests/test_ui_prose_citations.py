"""Static-prose citations (Close the Loops L12 — src/ui/prose.py + cite_marks).

Per-claim ``[n]`` citations used to exist ONLY on the interactive Ask surfaces,
where ``ui.cite_marks``'s JS ``linkify`` ran client-side at stream close. The
static LLM prose (thesis memo, bear case, synthesis lenses) is server-rendered
once through ``render_prose`` with no stream and no JS, so its ``[n]`` markers
stayed inert text while the fact cell beside them was fully sourced.

This pins the server-side seam: ``render_prose(md, citations=…)`` linkifies
``[n]`` into the SAME ``.cite-wrap`` / ``.cite-mark`` / ``.cite-pop`` anatomy the
JS emits (so the shared ``CITE_MARKS_CSS`` styles both), while the default
(no payload) path is byte-for-byte unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.cite_marks import citation_items, linkify  # noqa: E402
from ui.prose import render_prose  # noqa: E402

# A two-item evidence payload in EvidenceItem.chip_payload's shape: one with a
# relative /source href + confidence, one resolved to no href (a link-less chip).
_ITEMS = [
    {
        "n": 1,
        "kind": "10-K",
        "label": "FY24 10-K · Item 1A",
        "href": "/source/42",
        "source_url": None,
        "confidence": 0.79,
    },
    {
        "n": 2,
        "kind": "transcript",
        "label": "Q3 call",
        "href": None,
        "source_url": None,
        "confidence": None,
    },
]
# The full citations-event dict (ask.claims.build_citations_payload shape).
_PAYLOAD = {"items": _ITEMS, "claims": [], "grounding": "per_claim"}


# ---------------------------------------------------------------------------
# Default path is unchanged — the whole point of "optional".
# ---------------------------------------------------------------------------


def test_no_payload_is_byte_for_byte_unchanged() -> None:
    md = "Revenue grew **20%** [1] and margins held [2].\n\n## Risks\n- churn [3]"
    assert render_prose(md) == render_prose(md, citations=None)
    # Markers stay literal text when no payload is passed.
    assert "[1]" in render_prose(md)
    assert "cite-wrap" not in render_prose(md)


def test_empty_or_unusable_payload_leaves_markers_literal() -> None:
    md = "A claim [1]."
    payloads: tuple[object, ...] = (None, [], {"items": []}, {"items": "nope"}, "garbage")
    for payload in payloads:
        out = render_prose(md, citations=payload)  # type: ignore[arg-type]
        assert "cite-wrap" not in out
        assert "[1]" in out


# ---------------------------------------------------------------------------
# With a payload — chips resolve to the shared anatomy.
# ---------------------------------------------------------------------------


def test_matched_marker_becomes_a_cite_chip_with_absolute_resolvable_href() -> None:
    out = render_prose("Margins held [1].", citations=_PAYLOAD, cite_href_base="http://h:7421")
    assert '<span class="cite-wrap" tabindex="0">' in out
    # Relative /source href is prefixed with the base so a file:// report resolves it.
    assert 'href="http://h:7421/source/42"' in out
    assert 'class="cite-mark"' in out
    # Popover carries label + kind + rounded confidence (mirror of the JS popHtml).
    assert "FY24 10-K · Item 1A" in out
    assert "10-K" in out
    assert "confidence 79%" in out


def test_linkless_item_renders_a_span_mark_not_an_anchor() -> None:
    out = render_prose("On the call [2].", citations=_PAYLOAD)
    assert '<span class="cite-wrap" tabindex="0"><span class="cite-mark">[2]</span>' in out
    assert "Q3 call" in out
    # No confidence row when the item carries none.
    assert "confidence" not in out


def test_unmatched_marker_is_left_literal() -> None:
    # [9] has no backing item — stays as plain text, never a broken chip.
    out = render_prose("Unbacked claim [9].", citations=_PAYLOAD)
    assert "[9]" in out
    assert "cite-wrap" not in out


def test_absolute_href_is_used_as_is_not_prefixed() -> None:
    items = [{"n": 1, "label": "ext", "href": "https://sec.gov/x", "kind": "filing"}]
    out = render_prose("See [1].", citations=items, cite_href_base="http://h:7421")
    assert 'href="https://sec.gov/x"' in out
    assert "http://h:7421https://" not in out


def test_inline_mode_linkifies_without_block_tags() -> None:
    out = render_prose("cell [1]", inline=True, citations=_PAYLOAD)
    assert '<span class="cite-wrap"' in out
    for block in ("<p>", "<h3", "<ul>", "<li>", "<table"):
        assert block not in out


def test_markers_in_headings_and_tables_are_linkified() -> None:
    heading = render_prose("# Top [1]", citations=_PAYLOAD)
    assert "<h3>" in heading and "cite-wrap" in heading
    table = render_prose("| A | B |\n| --- | --- |\n| x [1] | y |", citations=_PAYLOAD)
    assert '<table class="tbl">' in table and "cite-wrap" in table


# ---------------------------------------------------------------------------
# Safety — escaping survives linkify; no double-linkify of generated chips.
# ---------------------------------------------------------------------------


def test_linkify_does_not_reopen_an_xss_hole() -> None:
    # render_prose escapes first; linkify only swaps [n] -> chip, never un-escapes.
    out = render_prose("<script>alert(1)</script> claim [1]", citations=_PAYLOAD)
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out
    assert "cite-wrap" in out


def test_malicious_chip_fields_are_escaped() -> None:
    items = [{"n": 1, "label": "<img src=x onerror=alert(1)>", "href": '"><script>x'}]
    out = render_prose("Claim [1].", citations=items)
    assert "<img src=x" not in out
    assert "<script>x" not in out
    assert "&lt;img" in out


def test_generated_chip_marker_is_not_re_matched() -> None:
    # The chip the replacer emits contains a literal [1]; a single left-to-right
    # re.sub pass must not rescan it into a nested chip.
    out = linkify("only [1] here", _ITEMS)
    assert out.count('class="cite-wrap"') == 1


def test_citation_items_normalizes_both_payload_forms() -> None:
    assert citation_items(_ITEMS) == _ITEMS
    assert citation_items(_PAYLOAD) == _ITEMS
    assert citation_items(None) == []
    assert citation_items({"items": 5}) == []  # type: ignore[arg-type]
    assert citation_items("nope") == []  # type: ignore[arg-type]
