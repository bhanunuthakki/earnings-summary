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

from models.facts import FactLocator, LocatorKind
from report.models import CellSource

SOURCE_CHIP_ABBREV: dict[str, str] = {
    "sec_official": "SEC",
    "fmp_normalized": "FMP",
    "llm_extracted": "LLM",
    "yfinance_fallback": "YF",
    "s1_provisional": "S-1",
}

# Below this scored confidence (pipeline.confidence formula) the chip takes
# the subtle low-confidence affordance: warn-tinted dashed border. 0.8 sits
# between the deterministic FMP prior (0.94) and one-disagreement FMP (0.79),
# so a single unresolved cross-source disagreement is enough to flag a cell.
LOW_CONFIDENCE_THRESHOLD = 0.8


def confidence_pct(src: CellSource) -> int | None:
    """Whole-percent display value of a scored confidence; None when unscored."""
    if src.confidence is None:
        return None
    return round(max(0.0, min(1.0, src.confidence)) * 100)


def source_hover_title(src: CellSource) -> str:
    """Hover text for a sourced number: tier + fetched-at (P3.3 contract),
    plus the scored confidence % when the row carries one."""
    parts = [src.source]
    if src.fetched_at:
        parts.append(f"fetched {src.fetched_at[:10]}")
    pct = confidence_pct(src)
    if pct is not None:
        parts.append(f"conf {pct}%")
    return " · ".join(parts)


def _parse_locator(raw: str | None) -> FactLocator | None:
    """Defensive FactLocator parse for chip rendering: a malformed/legacy
    locator degrades to None (no click-through sharpening) rather than
    raising and breaking the whole report page over one bad row."""
    if not raw:
        return None
    try:
        return FactLocator.from_json(raw)
    except ValueError:
        return None


_LOCATOR_SNIPPET_MAX = 160


def _locator_row_html(raw_locator: str) -> str:
    """Popover locator row (section 6.1): a v2-parseable locator shows a
    compact "kind: <effective_kind>" line + a truncated verbatim snippet
    when carried, with the raw JSON demoted into a nested <details> rather
    than shown by default. An unparseable/legacy-shaped locator falls back
    to the original raw-JSON dump unchanged."""
    loc = _parse_locator(raw_locator)
    kind = loc.effective_kind() if loc is not None else None
    if loc is None or kind is None:
        return f'<div class="src-pop-row mono src-pop-locator">{_esc(raw_locator)}</div>'
    snippet = ""
    if loc.verbatim_snippet:
        text = loc.verbatim_snippet
        clipped = text if len(text) <= _LOCATOR_SNIPPET_MAX else text[:_LOCATOR_SNIPPET_MAX] + "…"
        snippet = f' &middot; "{_esc(clipped)}"'
    return (
        f'<div class="src-pop-row mono">kind: {_esc(kind.value)}{snippet}</div>'
        '<details class="src-pop-locator-raw">'
        '<summary class="src-pop-row mono">raw locator</summary>'
        f'<div class="src-pop-row mono src-pop-locator">{_esc(raw_locator)}</div></details>'
    )


# Locator kinds whose primary click target is the provenance peek dispatcher
# (/api/peek/provenance/<table>:<id>) rather than a raw /source link.
# `derived` (Phase C, §2.5/§6.1) joins this family: the peek is what renders
# the recursive formula tree -- a raw /source link has nothing to open (a
# derived value has no source_doc_id of its own worth linking to).
_PEEK_KINDS = (
    LocatorKind.FMP_JSON_TABLE,
    LocatorKind.VENDOR_FIELD,
    LocatorKind.PDF_SLIDE,
    LocatorKind.DERIVED,
)


def viewer_href(src: CellSource) -> str | None:
    """In-app click-through for a sourced cell (P4.3; extended for
    ``fmp_json_table``/``vendor_field`` kinds by the provenance
    click-through program's Phase A and ``pdf_slide`` by Phase B,
    docs/design/provenance_clickthrough.md section 6.1).

    ``fmp_json_table`` / ``vendor_field`` / ``pdf_slide`` locators (when the
    cell carries a ``fact_id``) link the peek dispatcher (``/api/peek/
    provenance/<fact_table>:<id>``) directly — the peek is the primary click
    target for these kinds, `/source/<doc_id>` stays the "open full
    document" escape valve. Everything else keeps the original P4.3
    behavior: ``transcript_line`` becomes the reader's ``#L<n>`` anchor,
    ``section`` the 10-K reader's ``?section=`` deep link, and a bare
    ``pdf_page`` on a cell WITHOUT a fact_id the PDF viewer's ``?page=``
    deep link (Phase B). None when the cell carries neither a
    fact_id-bearing peek target nor a document id.
    """
    loc = _parse_locator(src.locator)
    if loc is not None and src.fact_id is not None and loc.effective_kind() in _PEEK_KINDS:
        return f"/api/peek/provenance/{src.fact_table}:{src.fact_id}"
    if src.doc_id is None:
        return None
    suffix = ""
    if loc is not None:
        if loc.transcript_line is not None:
            suffix = f"#L{loc.transcript_line}"
        elif loc.section:
            suffix = f"?section={urllib.parse.quote(loc.section)}"
        elif loc.pdf_page is not None:
            suffix = f"?page={loc.pdf_page}"
    return f"/source/{src.doc_id}{suffix}"


# Human noun for the pdf_slide chip hint, by the source document's doc_type
# (section 6.1: the tier abbreviation stays first — color-coding by trust
# tier is load-bearing — and the locator hint is appended, e.g.
# "FMP · IR deck p.14").
_PDF_HINT_NOUN: dict[str, str] = {
    "ir_presentation": "IR deck",
    "ir_event": "IR deck",
    "ir_supplement": "IR suppl",
    "ir_investor_update": "IR update",
    "ir_press_release": "IR release",
}


def _locator_hint(src: CellSource, loc: FactLocator | None) -> str | None:
    """Compact chip-label hint for locator kinds with a human-readable
    position (Phase B: pdf_slide → "IR deck p.14"; Phase C: derived →
    "derived · N inputs"). None = bare abbrev."""
    if loc is None:
        return None
    kind = loc.effective_kind()
    if kind == LocatorKind.PDF_SLIDE and loc.pdf_page is not None:
        noun = _PDF_HINT_NOUN.get(src.doc_type or "", "PDF")
        return f"{noun} p.{loc.pdf_page}"
    if kind == LocatorKind.DERIVED and loc.derived is not None:
        n = len(loc.derived.inputs)
        return f"derived · {n} input{'s' if n != 1 else ''}"
    return None


# Inputs shown in a "derived from" popover before truncating with "+N more"
# (ROE carries 5; anything longer is a misbehaving writer, not a UI case).
_MAX_LINEAGE_INPUTS = 6


def _lineage_rows(computed_from: str) -> list[str]:
    """Popover rows for a derived row's ``computed_from`` JSON (0087):
    "derived from: <display>" plus one mini-chip per input — tier-colored
    abbrev linking the /source viewer when the input carries doc_id.
    Defensive: malformed JSON renders nothing (lineage is enrichment)."""
    try:
        payload: object = json.loads(computed_from)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    payload_map = cast("dict[str, object]", payload)
    display = payload_map.get("display")
    raw_inputs = payload_map.get("inputs")
    rows: list[str] = []
    if isinstance(display, str) and display:
        rows.append(f'<div class="src-pop-row">derived from: {_esc(display)}</div>')
    if not isinstance(raw_inputs, list):
        return rows
    inputs = cast("list[object]", raw_inputs)
    for entry in inputs[:_MAX_LINEAGE_INPUTS]:
        if not isinstance(entry, dict):
            continue
        entry_map = cast("dict[str, object]", entry)
        item = entry_map.get("item")
        if not isinstance(item, str) or not item:
            continue
        period = entry_map.get("period_end")
        period_str = f" · {_esc(period)}" if isinstance(period, str) and period else ""
        tier = entry_map.get("tier")
        tier_str = tier if isinstance(tier, str) and tier else ""
        chip_abbrev = SOURCE_CHIP_ABBREV.get(tier_str, "SRC")
        chip_cls = f"src-chip src-{_esc(tier_str.replace('_', '-'))}" if tier_str else "src-chip"
        doc_id = entry_map.get("doc_id")
        if isinstance(doc_id, int):
            chip = (
                f'<a class="{chip_cls}" href="/source/{doc_id}" target="_blank" '
                f'rel="noopener" title="{_esc(tier_str or "source document")}">{_esc(chip_abbrev)}</a>'
            )
        else:
            chip = f'<span class="{chip_cls}">{_esc(chip_abbrev)}</span>'
        rows.append(f'<div class="src-pop-row src-pop-input">{chip} {_esc(item)}{period_str}</div>')
    if len(inputs) > _MAX_LINEAGE_INPUTS:
        rows.append(
            f'<div class="src-pop-row mono">+{len(inputs) - _MAX_LINEAGE_INPUTS} more inputs</div>'
        )
    return rows


def source_chip_html(src: CellSource) -> str:
    """Clickable per-number source chip: hover = tier + fetched-at; click
    opens a JS-free <details> popover with the document identity (doc type,
    accession, filing date, sub-document locator), the scored confidence %,
    the extraction method, derived-from lineage with per-input mini-chips,
    any unresolved validation issues (⚠ rows), and the open links — the
    in-app /source viewer (P4.3) plus the original document URL.
    """
    abbrev = SOURCE_CHIP_ABBREV.get(src.source, src.source[:3].upper() or "?")
    hint = _locator_hint(src, _parse_locator(src.locator))
    chip_label = f"{abbrev} · {hint}" if hint else abbrev
    tier_slug = src.source.replace("_", "-")
    pct = confidence_pct(src)
    low_conf = (
        pct is not None and src.confidence is not None and src.confidence < LOW_CONFIDENCE_THRESHOLD
    )
    rows: list[str] = [f'<div class="src-pop-row"><b>{_esc(src.source)}</b></div>']
    if src.override:
        # Company-doc override (provenance-override P6): the displayed figure comes
        # from this filing, not FMP. Rendered prominently right under the source.
        rows.append(f'<div class="src-pop-row"><b>overridden by</b> {_esc(src.override)}</div>')
    if pct is not None:
        flag = " · below threshold" if low_conf else ""
        rows.append(f'<div class="src-pop-row">confidence {pct}%{flag}</div>')
    for issue in src.issues:
        rows.append(f'<div class="src-pop-row src-pop-warn">{_esc(issue)}</div>')
    if src.fetched_at:
        rows.append(f'<div class="src-pop-row">fetched {_esc(src.fetched_at[:10])}</div>')
    if src.doc_type:
        rows.append(f'<div class="src-pop-row">{_esc(src.doc_type)}</div>')
    if src.extracted_by:
        rows.append(f'<div class="src-pop-row mono">via {_esc(src.extracted_by)}</div>')
    if src.computed_from:
        rows.extend(_lineage_rows(src.computed_from))
    if src.accession_number:
        acc = _esc(src.accession_number)
        filed = f" · filed {_esc(src.filing_date)}" if src.filing_date else ""
        rows.append(f'<div class="src-pop-row mono">{acc}{filed}</div>')
    if src.locator:
        rows.append(_locator_row_html(src.locator))
    viewer = viewer_href(src)
    is_peek = viewer is not None and viewer.startswith("/api/peek/")
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
    low_cls = " src-lowconf" if low_conf else ""
    peek_attrs = (
        f' data-peek-url="{_esc(viewer)}" data-peek-title="{_esc(source_hover_title(src))}"'
        if is_peek and viewer is not None
        else ""
    )
    return (
        '<details class="src-pop">'
        f'<summary class="src-chip src-{_esc(tier_slug)}{low_cls}"{peek_attrs} '
        f'title="{_esc(source_hover_title(src))}">{_esc(chip_label)}</summary>'
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
  display: inline-block; font-size: var(--fs-caption); font-weight: 600;
  letter-spacing: 0.04em; line-height: 1.4; padding: 0 3px;
  border: 1px solid var(--border-2, var(--border)); border-radius: var(--radius);
  color: var(--muted); background: transparent;
  opacity: 0.65; user-select: none;
}
.src-chip:hover, .src-pop[open] .src-chip { opacity: 1; }
.src-sec-official { color: var(--ok); border-color: var(--ok); }
.src-fmp-normalized { color: var(--muted); border-color: var(--border-2); }
.src-llm-extracted { color: var(--warn); border-color: var(--warn); }
.src-yfinance-fallback, .src-s1-provisional { color: var(--muted); }
/* Scored confidence below LOW_CONFIDENCE_THRESHOLD: the subtle cell
   affordance — warn-tinted dashed border, overriding the tier color. */
.src-chip.src-lowconf { color: var(--warn); border-color: var(--warn); border-style: dashed; }
.src-pop-body {
  position: absolute; z-index: 40; top: calc(100% + 4px); left: 0;
  min-width: 220px; max-width: 340px; padding: 8px 10px;
  background: var(--surface); border: 1px solid var(--border-2, var(--border));
  border-radius: var(--radius); box-shadow: var(--shadow-pop);
  font-size: var(--fs-caption); text-align: left; white-space: normal;
}
.src-pop-row { padding: 1px 0; color: var(--fg); }
.src-pop-row.mono { font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }
.src-pop-locator { word-break: break-all; }
.src-pop-locator-raw summary { cursor: pointer; }
.src-pop-row a { color: var(--accent); }
/* S2 PR3: unresolved validation issues + derived-from input rows. */
.src-pop-warn { color: var(--warn); }
.src-pop-input { display: flex; align-items: center; gap: 5px; }
.src-pop-input .src-chip { opacity: 1; }
.src-pop-input a.src-chip { text-decoration: none; }
"""

# Escape-only dismissal (Law 3 / design_language §3.1). The chip's popover is a
# JS-free ``<details>`` — flow content split inside a table cell, NOT a modal —
# so it gets Escape-only via a CCOverlay dismisser, never the full triad (no
# scrim, no focus trap). Self-guarded + idempotent across the many surfaces that
# embed the chip; registers only when CCOverlay is present (shell + report
# iframe). A document that wants chip-Escape inlines this once after CCOverlay.
SOURCE_CHIP_JS = r"""
(function () {
  if (window.__ccSrcChipEsc || !window.CCOverlay) return;
  window.__ccSrcChipEsc = true;
  window.CCOverlay.addPopoverDismisser(function () {
    var open = document.querySelectorAll('details.src-pop[open]');
    if (open.length) { open[open.length - 1].removeAttribute('open'); return true; }
    return false;
  });
})();
"""
