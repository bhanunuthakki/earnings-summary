"""Shared per-number source chip (the P3.3 anatomy, extracted in P5.1).

One clickable provenance badge per displayed number: hover shows tier +
fetched-at, click opens a JS-free ``<details>`` popover carrying the
document identity (doc type, EDGAR accession, filing date, sub-document
locator) and the open links — the in-app ``/source/<doc_id>`` viewer plus
the original document URL.

Lived inside ``report.renderers.workspace_html`` until the ViewSpec engine
(master build P5.1) needed the identical chip on the dashboard's Explore
panel; the helpers moved here so both surfaces render one anatomy. The
workspace renderer re-imports them under its original private names, so
its call sites and the P3.3 tests are unchanged.

``SOURCE_CHIP_CSS`` carries the chip/popover rules for surfaces that do
not load the workspace stylesheet (the dashboard panels); it is a verbatim
copy of the ``.src-*`` block in ``workspace_styles.py`` and keys off the
same CSS variables, so it adapts to whichever theme the host surface sets.
"""

from __future__ import annotations

import json
import urllib.parse
from html import escape as _esc
from typing import cast

from report.models import CellSource

SOURCE_CHIP_ABBREV: dict[str, str] = {
    "sec_official": "SEC",
    "fmp_normalized": "FMP",
    "llm_extracted": "LLM",
    "yfinance_fallback": "YF",
    "s1_provisional": "S-1",
}


def source_hover_title(src: CellSource) -> str:
    """Hover text for a sourced number: tier + fetched-at (P3.3 contract)."""
    parts = [src.source]
    if src.fetched_at:
        parts.append(f"fetched {src.fetched_at[:10]}")
    return " · ".join(parts)


def viewer_href(src: CellSource) -> str | None:
    """In-app ``/source/<doc_id>`` link for a sourced cell (P4.3).

    The locator JSON sharpens the destination: ``transcript_line`` becomes
    the reader's ``#L<n>`` line anchor, ``section`` the 10-K reader's
    ``?section=`` deep link. None when the cell carries no document id —
    the chip then falls back to the raw source_url only.
    """
    if src.doc_id is None:
        return None
    suffix = ""
    if src.locator:
        try:
            loc: object = json.loads(src.locator)
        except (ValueError, TypeError):
            loc = None
        if isinstance(loc, dict):
            loc_map = cast("dict[str, object]", loc)
            line = loc_map.get("transcript_line")
            section = loc_map.get("section")
            if isinstance(line, int):
                suffix = f"#L{line}"
            elif isinstance(section, str) and section:
                suffix = f"?section={urllib.parse.quote(section)}"
    return f"/source/{src.doc_id}{suffix}"


def source_chip_html(src: CellSource) -> str:
    """Clickable per-number source chip: hover = tier + fetched-at; click
    opens a JS-free <details> popover with the document identity (doc type,
    accession, filing date, sub-document locator) and the open links — the
    in-app /source viewer (P4.3) plus the original document URL.
    """
    abbrev = SOURCE_CHIP_ABBREV.get(src.source, src.source[:3].upper() or "?")
    tier_slug = src.source.replace("_", "-")
    rows: list[str] = [f'<div class="src-pop-row"><b>{_esc(src.source)}</b></div>']
    if src.fetched_at:
        rows.append(f'<div class="src-pop-row">fetched {_esc(src.fetched_at[:10])}</div>')
    if src.doc_type:
        rows.append(f'<div class="src-pop-row">{_esc(src.doc_type)}</div>')
    if src.accession_number:
        acc = _esc(src.accession_number)
        filed = f" · filed {_esc(src.filing_date)}" if src.filing_date else ""
        rows.append(f'<div class="src-pop-row mono">{acc}{filed}</div>')
    if src.locator:
        rows.append(f'<div class="src-pop-row mono src-pop-locator">{_esc(src.locator)}</div>')
    viewer = viewer_href(src)
    if viewer:
        rows.append(
            f'<div class="src-pop-row"><a href="{_esc(viewer)}" target="_blank" '
            'rel="noopener">open in viewer ↗</a></div>'
        )
    if src.source_url:
        label = "original document ↗" if viewer else "open source ↗"
        rows.append(
            f'<div class="src-pop-row"><a href="{_esc(src.source_url)}" target="_blank" '
            f'rel="noopener">{label}</a></div>'
        )
    return (
        '<details class="src-pop">'
        f'<summary class="src-chip src-{_esc(tier_slug)}" '
        f'title="{_esc(source_hover_title(src))}">{_esc(abbrev)}</summary>'
        f'<div class="src-pop-body">{"".join(rows)}</div>'
        "</details>"
    )


# Verbatim copy of the .src-* block in workspace_styles.py for surfaces that
# don't load the workspace stylesheet. Keep the two in sync when the chip
# anatomy changes (they share the markup above, so drift breaks both alike).
SOURCE_CHIP_CSS = """
.src-pop { display: inline-block; position: relative; vertical-align: baseline; }
.src-pop > summary { list-style: none; cursor: pointer; }
.src-pop > summary::-webkit-details-marker { display: none; }
.src-chip {
  display: inline-block; font-size: 8.5px; font-weight: 700;
  letter-spacing: 0.04em; line-height: 1.4; padding: 0 3px;
  border: 1px solid var(--border-2, var(--border)); border-radius: 3px;
  color: var(--muted-2, var(--muted)); background: transparent;
  opacity: 0.65; user-select: none;
}
.src-chip:hover, .src-pop[open] .src-chip { opacity: 1; }
.src-sec-official { color: var(--ok); border-color: var(--ok); }
.src-fmp-normalized { color: var(--accent); border-color: var(--accent); }
.src-llm-extracted { color: var(--warn); border-color: var(--warn); }
.src-yfinance-fallback, .src-s1-provisional { color: var(--muted-2, var(--muted)); }
.src-pop-body {
  position: absolute; z-index: 40; top: calc(100% + 4px); left: 0;
  min-width: 220px; max-width: 340px; padding: 8px 10px;
  background: var(--surface); border: 1px solid var(--border-2, var(--border));
  border-radius: var(--radius); box-shadow: var(--shadow-pop);
  font-size: var(--fs-caption); text-align: left; white-space: normal;
}
.src-pop-row { padding: 1px 0; color: var(--fg); }
.src-pop-row.mono { font-family: var(--mono); font-size: var(--fs-micro); color: var(--muted); }
.src-pop-locator { word-break: break-all; }
.src-pop-row a { color: var(--accent); }
"""
