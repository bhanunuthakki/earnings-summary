"""Cross-section helpers for the workspace section renderers.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

import html
from collections.abc import Callable
from io import StringIO
from typing import TypeAlias

from report.models import CellSource, MissingReason, SectionStatus
from report.renderers.workspace_data import quarter_short
from ui.prose import render_prose
from ui.source_chip import source_chip_html, source_hover_title

__all__ = [
    "_STATUS_EMPTY_REASON",
    "TabDef",
    "TabGroup",
    "TabRenderFn",
    "_empty_panel",
    "_esc",
    "_fmt_pct",
    "_fmt_price",
    "_fmt_usd",
    "_inline_md",
    "_missing_panel",
    "_panel_head",
    "_quarter_selector",
    "_render_markdown",
    "_source_chip_html",
    "_source_hover_title",
    "_xlink_html",
]


# A tab tuple: (id, label, optional badge count, render-function-into-body).
TabRenderFn: TypeAlias = Callable[[StringIO], None]


TabDef: TypeAlias = tuple[str, str, int | None, TabRenderFn]


# A grouped top-level tab: (group_id, label, section tabs rendered inside).
TabGroup: TypeAlias = tuple[str, str, list[TabDef]]


# P3.3 chip anatomy — shared with the dashboard's Explore panel since P5.1:
# the implementation lives in ui.source_chip; these aliases keep the
# renderer's call sites (and the P3.3 tests' private-name imports) stable.
_source_hover_title = source_hover_title


_source_chip_html = source_chip_html


def _quarter_selector(body: StringIO, labels: list[str], group: str) -> None:
    if not labels:
        return
    body.write(f'<div class="quarter-select" data-quarter-group="{_esc(group)}">')
    body.write('<div class="quarter-select-label">Quarter</div>')
    body.write('<div class="quarter-select-btns">')
    for i, lbl in enumerate(labels):
        cls = "qbtn active" if i == 0 else "qbtn"
        body.write(
            f'<button class="{cls}" data-quarter="{_esc(lbl)}">{_esc(quarter_short(lbl))}</button>'
        )
    body.write("</div></div>")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _panel_head(
    title: str,
    *,
    sub: str | None = None,
    sub_html: str | None = None,
    as_of: str | None = None,
    chip: CellSource | None = None,
    links: str = "",
    classes: str = "",
    attrs: str = "",
    title_html: str | None = None,
    panel_id: str | None = None,
) -> str:
    """Canonical section-header anatomy (P4.1): title · as-of · source chip,
    with the descriptive sub on the right edge. Returns the OPENED panel —
    ``<div class="panel …"><div class="panel-head">…</div>`` — so the caller
    writes the panel body and the closing ``</div>``.

    ``title``/``sub``/``as_of`` are escaped; ``title_html``/``sub_html``/
    ``attrs``/``links`` are emitted as-is for call sites that embed links,
    pills, or data-anchor attributes. ``links`` carries cross-tab links
    (P4.3, see ``_xlink_html``); ``panel_id`` makes the panel a cross-link
    target. Hand-rolled ``panel-head`` markup should not exist outside this
    helper (and the <summary> variants that mirror it).
    """
    cls = f"panel {classes}".strip()
    t = title_html if title_html is not None else _esc(title)
    meta: list[str] = []
    if as_of:
        meta.append(f'<span class="panel-asof">as of {_esc(as_of)}</span>')
    if chip is not None:
        meta.append(_source_chip_html(chip))
    if links:
        meta.append(links)
    if sub_html is not None:
        meta.append(sub_html)
    elif sub:
        meta.append(f'<span class="panel-sub">{_esc(sub)}</span>')
    meta_html = f'<span class="panel-meta">{"".join(meta)}</span>' if meta else ""
    attrs_s = f" {attrs}" if attrs else ""
    id_s = f' id="{_esc(panel_id)}"' if panel_id else ""
    return (
        f'<div class="{cls}"{id_s}{attrs_s}><div class="panel-head">'
        f'<span class="panel-title">{t}</span>{meta_html}</div>'
    )


def _xlink_html(tab: str, label: str, anchor: str | None = None) -> str:
    """A cross-tab link (P4.3): switches the workspace to ``tab`` and, when
    ``anchor`` names a panel id there, scrolls it into view. Wired by the
    ``data-xtab`` handler in workspace_script.JS."""
    anchor_attr = f' data-anchor="{_esc(anchor)}"' if anchor else ""
    return f'<a class="panel-xlink" href="#" data-xtab="{_esc(tab)}"{anchor_attr}>{_esc(label)}</a>'


def _empty_panel(
    body: StringIO,
    title: str,
    message: str,
    *,
    reason: str = "no data",
    classes: str = "",
) -> None:
    """P4.1 empty-state anatomy: a collapsed one-line <details> panel that
    expands to an analyst-language explanation.

    Empty sections collapse to a single muted line instead of stacking
    full-height stub panels, and the copy never references CLI commands,
    migration ids, or pipeline internals — reports speak analyst; operations
    live under Governance.
    """
    cls = f"panel panel-empty {classes}".strip()
    body.write(
        f'<details class="{cls}"><summary class="panel-head">'
        f'<span class="panel-title">{_esc(title)}</span>'
        f'<span class="panel-meta"><span class="panel-sub">{_esc(reason)}</span></span>'
        f'</summary><div class="panel-empty-body">{_esc(message)}</div></details>'
    )


def _fmt_usd(v: float | None) -> str:
    """Full-precision dollar (2 decimals). Used for cost basis / P&L amounts."""
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _fmt_price(v: float | None) -> str:
    """Zero-decimal price ($388). Used in the identity strip and valuation summary."""
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


# Analyst-language readings of SectionStatus for the empty-state summary line
# (P4.1): short right-edge reason + default expanded message. The
# ``MissingReason.fix_command`` CLI string deliberately never renders here.
_STATUS_EMPTY_REASON: dict[SectionStatus, tuple[str, str]] = {
    SectionStatus.MISSING_DATA: (
        "no data yet",
        "No data on file for this section yet.",
    ),
    SectionStatus.PARTIAL: (
        "partial coverage",
        "Only part of this section's data is on file yet.",
    ),
    SectionStatus.LLM_PENDING: (
        "analysis pending",
        "This read hasn't been written yet — it lands with the next full analysis build.",
    ),
    SectionStatus.NOT_APPLICABLE: (
        "not applicable",
        "This section doesn't apply to this name.",
    ),
    SectionStatus.BUDGET_SKIPPED: (
        "Forgone — budget",
        "Forgone to stay under the monthly analysis budget. Raise the cap or "
        "override from the settings drawer, then rebuild.",
    ),
}


def _missing_panel(
    body: StringIO,
    status: SectionStatus,
    missing: MissingReason | None,
    *,
    title: str = "Section data",
) -> None:
    """Section-level miss rendered as the collapsed empty-state panel.

    ``missing.detail`` is analyst prose and is surfaced when present; the
    ``fix_command`` CLI string is NOT (reports speak analyst — the operational
    fix lives under Governance)."""
    reason, default_msg = _STATUS_EMPTY_REASON.get(
        status, (status.value.replace("_", " "), "Section returned no data.")
    )
    message = missing.detail if missing is not None and missing.detail else default_msg
    is_budget = status == SectionStatus.BUDGET_SKIPPED
    _empty_panel(
        body,
        title,
        message,
        reason=reason,
        classes="panel-budget" if is_budget else "",
    )


# Markdown → HTML for the workspace report's stored prose. The real renderer
# now lives in :mod:`ui.prose` (the one prose render boundary — Instrument
# Paradigm "one render per content-kind"); these keep the historical workspace
# names resolving. New code imports ``ui.prose.render_prose`` directly.
_render_markdown = render_prose


def _inline_md(text: str) -> str:
    """Inline-only markdown (bold/italic/code) for table cells and labels."""
    return render_prose(text, inline=True)
