"""Reusable evidence-drawer HTML component for every alert card.

Every fired alert in ``alerts`` carries an ``evidence_json`` blob whose
shape is owned per ``trigger_kind`` (kpi_inflection / earnings_tone /
saydo_due / thesis_drift / material_news). This component renders that
blob into a default-expanded ``<details>`` block with three sections:

  1. "Why this fired"   — human-readable summary (``evidence.summary``)
  2. "Source citations" — table over ``evidence.citations[]``; fact_id
                           citations get linked to brief_provenance
                           per_metric when supplied
  3. "Raw evidence"     — collapsed JSON dump, for full-detail audit

The drawer degrades gracefully on malformed JSON: a structured notice
replaces the parsed sections rather than the renderer raising. Triggers
written in PR-N8+ will land their per-kind evidence shapes; the drawer
only requires the surface conventions documented above to render
something useful, with raw-JSON fallback for everything else.
"""

from __future__ import annotations

import html
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from alerts import AlertRow

_CITATION_KIND_LABELS: dict[str, str] = {
    "transcript_line": "Transcript line",
    "fact_id": "Financial fact",
    "news_url": "News article",
    "kpi_observation": "KPI observation",
    "filing_section": "Filing section",
    "dcf_run": "DCF run",
}


def load_brief_provenance(ticker: str, *, db_path: Path) -> Mapping[str, object] | None:
    """Latest brief_provenance_log payload for `ticker`, drawer-shaped.

    Returns ``{"sources_used": <parsed JSON>}`` — the row shape
    ``render_evidence_drawer`` navigates for fact_id citation linking —
    or None when the DB/table/row is missing or the JSON is malformed.
    Callers (digest/feed) look this up once per ticker so fact_id
    citations stop rendering the dead "no brief provenance" cell (P3.3).
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT sources_used FROM brief_provenance_log "
            "WHERE UPPER(ticker) = ? ORDER BY generated_at DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    try:
        parsed = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {"sources_used": cast("dict[str, object]", parsed)}


def render_evidence_drawer(
    alert: AlertRow,
    brief_provenance: Mapping[str, object] | None = None,
    *,
    default_open: bool = True,
) -> str:
    """Render the evidence drawer for one alert.

    ``brief_provenance`` is the full brief_provenance_log row payload (or
    None if not available) — the drawer navigates to
    ``sources_used.per_metric`` for fact_id citation linking. Pass the
    *row* shape (with ``sources_used`` at the top), not just per_metric
    itself, so the caller's lookup matches the table's column layout.

    ``default_open=False`` renders the drawer collapsed — for dense
    contexts (the Holding tab's side rail) where the full citation table
    should be one click away rather than unrolled per card. The digest and
    feed keep the default-expanded contract.
    """
    open_attr = " open" if default_open else ""
    body: list[str] = []
    body.append(f'<details{open_attr} class="evidence-drawer">')
    body.append('<summary class="evidence-summary">Evidence</summary>')
    body.append('<div class="evidence-body">')

    parsed = _parse_evidence(alert.evidence_json)
    if parsed is None:
        body.append(
            '<div class="evidence-malformed">'
            "<strong>Malformed evidence</strong> "
            "<span>The sensor wrote evidence that could not be parsed as JSON.</span>"
            "</div>"
        )
        raw = alert.evidence_json
        body.append(
            '<details class="evidence-raw"><summary>Raw evidence</summary>'
            f'<pre class="evidence-raw-pre">{_esc(raw)}</pre>'
            "</details>"
        )
        body.append("</div></details>")
        return "".join(body)

    per_metric = _extract_per_metric(brief_provenance)

    body.append(_render_summary_section(parsed))
    body.append(_render_citations_section(parsed, per_metric))
    body.append(_render_raw_section(parsed))

    body.append("</div></details>")
    return "".join(body)


# ----------------------------------------------------------------------------
# Section renderers
# ----------------------------------------------------------------------------


def _render_summary_section(parsed: Mapping[str, object]) -> str:
    raw_summary = parsed.get("summary")
    if raw_summary is None or raw_summary == "":
        return (
            '<div class="evidence-section evidence-why">'
            '<div class="evidence-section-title">Why this fired</div>'
            '<div class="muted">No summary supplied by the sensor.</div>'
            "</div>"
        )
    return (
        '<div class="evidence-section evidence-why">'
        '<div class="evidence-section-title">Why this fired</div>'
        f'<div class="evidence-summary-text">{_esc(str(raw_summary))}</div>'
        "</div>"
    )


def _render_citations_section(
    parsed: Mapping[str, object],
    per_metric: Mapping[str, object] | None,
) -> str:
    # Citations live at the top level for triggers that emit them there, AND
    # nested under each shift for earnings_tone (shifts[].citations). Gather
    # both so the richest evidence — the cited transcript lines — actually
    # renders instead of degrading to "No citations supplied".
    citations = _normalize_citations(parsed.get("citations"))
    citations.extend(_citations_from_shifts(parsed.get("shifts")))
    if not citations:
        return (
            '<div class="evidence-section evidence-citations">'
            '<div class="evidence-section-title">Source citations</div>'
            '<div class="muted">No citations supplied.</div>'
            "</div>"
        )

    rows: list[str] = []
    rows.append(
        '<div class="evidence-section evidence-citations">'
        '<div class="evidence-section-title">Source citations</div>'
        '<table class="evidence-citations-table">'
        "<thead><tr><th>Kind</th><th>Locator</th><th>Excerpt</th><th>Provenance</th></tr></thead>"
        "<tbody>"
    )
    for c in citations:
        kind = c.kind
        kind_label = _CITATION_KIND_LABELS.get(kind, kind)
        prov_html = _render_citation_provenance(c, per_metric)
        # URL locators (news_url etc.) render as real links — a citation the
        # analyst can't open is a dead end, not provenance.
        if c.locator.startswith(("http://", "https://")):
            locator_html = (
                f'<a href="{_esc(c.locator)}" target="_blank" rel="noopener">{_esc(c.locator)}</a>'
            )
        else:
            locator_html = _esc(c.locator)
        rows.append(
            "<tr>"
            f'<td class="cite-kind">{_esc(kind_label)}</td>'
            f'<td class="cite-locator mono">{locator_html}</td>'
            f'<td class="cite-excerpt">{_esc(c.excerpt)}</td>'
            f'<td class="cite-prov">{prov_html}</td>'
            "</tr>"
        )
    rows.append("</tbody></table></div>")
    return "".join(rows)


def _render_raw_section(parsed: Mapping[str, object]) -> str:
    pretty = json.dumps(dict(parsed), indent=2, sort_keys=True, default=str)
    return (
        '<details class="evidence-raw"><summary>Raw evidence</summary>'
        f'<pre class="evidence-raw-pre">{_esc(pretty)}</pre>'
        "</details>"
    )


# ----------------------------------------------------------------------------
# Citation parsing
# ----------------------------------------------------------------------------


class _Citation:
    """One parsed citation entry — flat string fields, validated at parse time."""

    __slots__ = ("excerpt", "kind", "locator")

    def __init__(self, kind: str, locator: str, excerpt: str) -> None:
        self.kind = kind
        self.locator = locator
        self.excerpt = excerpt


def _normalize_citations(raw: object) -> list[_Citation]:
    """Coerce ``evidence.citations`` into a list of _Citation.

    Tolerant of shape variance: skips entries that aren't dict-shaped or
    missing required keys, rather than raising — the drawer is a viewer,
    not a schema validator. Per-trigger sensors enforce their own shape
    on write; the drawer renders what's there.
    """
    if not isinstance(raw, list):
        return []
    # JSON-boundary cast: isinstance(..., list) just confirmed the runtime
    # shape; we treat each element as object until further narrowing.
    raw_list = cast("list[object]", raw)
    out: list[_Citation] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        entry_map = cast("Mapping[str, object]", entry)
        kind_raw = entry_map.get("kind")
        if not isinstance(kind_raw, str) or not kind_raw:
            continue
        locator_raw = entry_map.get("locator")
        if not (isinstance(locator_raw, str) and locator_raw):
            # No explicit locator — compose one from per-trigger fields, e.g.
            # earnings_tone cites a transcript line as {period, line_number}.
            locator_raw = _compose_locator(entry_map)
        excerpt_raw = entry_map.get("excerpt", "")
        out.append(
            _Citation(
                kind=kind_raw,
                locator=str(locator_raw),
                excerpt=str(excerpt_raw),
            )
        )
    return out


def _compose_locator(entry: Mapping[str, object]) -> str:
    """Best-effort locator string from per-trigger citation fields when the
    entry carries no explicit ``locator`` (earnings_tone writes ``period`` +
    ``line_number`` instead of a single locator)."""
    parts: list[str] = []
    period = entry.get("period")
    if isinstance(period, str) and period:
        parts.append(period)
    line_no = entry.get("line_number")
    if isinstance(line_no, int):
        parts.append(f"line {line_no}")
    elif isinstance(line_no, str) and line_no.strip():
        parts.append(f"line {line_no.strip()}")
    return " · ".join(parts)


def _citations_from_shifts(raw_shifts: object) -> list[_Citation]:
    """Gather citations nested under each shift. earnings_tone writes its
    citations per-shift (``shifts[].citations``) rather than at the top level,
    so the drawer must reach into the shifts to surface them."""
    if not isinstance(raw_shifts, list):
        return []
    out: list[_Citation] = []
    for shift in cast("list[object]", raw_shifts):
        if isinstance(shift, dict):
            shift_map = cast("Mapping[str, object]", shift)
            out.extend(_normalize_citations(shift_map.get("citations")))
    return out


def _render_citation_provenance(
    citation: _Citation, per_metric: Mapping[str, object] | None
) -> str:
    """For fact_id citations, surface (source, fetched_at) from per_metric.

    Other kinds get a sentinel "—" cell — the locator column already
    carries everything we can say about transcript / news / filing
    citations without a richer lookup table.
    """
    if citation.kind != "fact_id":
        return '<span class="muted">—</span>'
    if per_metric is None:
        return '<span class="muted">no brief provenance</span>'
    raw_entry = per_metric.get(citation.locator)
    if not isinstance(raw_entry, dict):
        return '<span class="muted">no match</span>'
    entry_map = cast("Mapping[str, object]", raw_entry)
    source = entry_map.get("source")
    fetched_at = entry_map.get("fetched_at")
    parts: list[str] = []
    if isinstance(source, str) and source:
        parts.append(f'<span class="prov-source">{_esc(source)}</span>')
    if isinstance(fetched_at, str) and fetched_at:
        parts.append(f'<span class="prov-fetched">{_esc(fetched_at)}</span>')
    if not parts:
        return '<span class="muted">—</span>'
    return " · ".join(parts)


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------


def _parse_evidence(raw: str) -> Mapping[str, object] | None:
    """Decode evidence_json into a dict, or return None on any parse failure.

    The drawer's contract: malformed JSON renders a structured notice
    rather than raising. ``None`` is the signal that we couldn't get a
    dict shape and should fall back to the malformed-notice path.
    """
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    # JSON-boundary cast: the isinstance check confirms the runtime shape;
    # the cast tells pyright we've validated dict[str, object] and can use
    # it without further narrowing (per project convention).
    return cast("dict[str, object]", loaded)


def _extract_per_metric(
    brief_provenance: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Pull ``sources_used.per_metric`` out of a brief_provenance_log payload.

    Returns None when the input is None, or when the navigation can't
    find a dict at ``sources_used.per_metric`` (callers may legitimately
    pass a payload without per_metric — e.g. an alert whose evidence
    doesn't reference any KPI/financial-fact citations).
    """
    if brief_provenance is None:
        return None
    sources_used = brief_provenance.get("sources_used")
    if not isinstance(sources_used, dict):
        return None
    sources_used_map = cast("Mapping[str, object]", sources_used)
    per_metric = sources_used_map.get("per_metric")
    if not isinstance(per_metric, dict):
        return None
    return cast("Mapping[str, object]", per_metric)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
