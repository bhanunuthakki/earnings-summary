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
import re
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


# Matches the processed-transcript file naming: ``..._Q1_2026.txt``.
_TRANSCRIPT_PERIOD_RE = re.compile(r"_Q([1-4])_((?:19|20)\d{2})", re.IGNORECASE)

# How many open notes ride along on an alert's evidence (P4.4) — watch items
# and questions lead, then the rest, newest first within each bucket.
_RELATED_NOTES_LIMIT = 5
_NOTE_KIND_RANK = {"watch": 0, "question": 1}


def _load_open_notes(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """The ticker's open analyst notes, lead-kinds first (P4.4).

    Returns lightweight dict rows ({id, kind, body, created_at}) — the
    drawer is a renderer; it doesn't need the full AnalystNoteRow."""
    try:
        rows = conn.execute(
            "SELECT id, kind, body, created_at FROM analyst_notes "
            "WHERE UPPER(ticker) = ? AND status = 'open' "
            "ORDER BY created_at DESC, id DESC",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    ordered = sorted(
        rows, key=lambda r: _NOTE_KIND_RANK.get(str(r[1]), 9)
    )  # stable: keeps newest-first within each kind bucket
    return [
        {
            "id": int(r[0]),
            "kind": str(r[1]),
            "body": str(r[2]),
            "created_at": str(r[3] or ""),
        }
        for r in ordered[:_RELATED_NOTES_LIMIT]
    ]


def _load_transcript_doc_ids(conn: sqlite3.Connection, ticker: str) -> dict[str, int]:
    """``{"Q1-2026": doc_id}`` for the ticker's registered call transcripts.

    Keys use the citation-period shape the earnings_tone trigger writes
    (``Q<n>-<year>``); the period is parsed from the processed-transcript
    file path. Latest registration (highest id) wins per period.
    """
    try:
        rows = conn.execute(
            "SELECT id, file_path FROM documents "
            "WHERE UPPER(ticker) = ? AND doc_type = 'earnings_call_transcript' "
            "ORDER BY id",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, int] = {}
    for doc_id, file_path in rows:
        m = _TRANSCRIPT_PERIOD_RE.search(str(file_path or ""))
        if m is None:
            continue
        out[f"Q{m.group(1)}-{m.group(2)}"] = int(doc_id)
    return out


def load_brief_provenance(ticker: str, *, db_path: Path) -> Mapping[str, object] | None:
    """Latest brief_provenance_log payload for `ticker`, drawer-shaped.

    Returns ``{"sources_used": <parsed JSON>, "transcript_docs": {...},
    "open_notes": [...]}`` — the shape ``render_evidence_drawer`` navigates
    for fact_id citation linking, the period → transcript-document map that
    lets transcript_line citations deep-link the ``/source/<doc_id>#L<n>``
    reader (P4.3), and the ticker's open analyst notes so alerts surface
    the owner's standing watch-items next to their evidence (P4.4). Any
    part may be absent; None only when none is available (missing DB, no
    rows). Callers (digest/feed/holding rail) look this up once per ticker.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT sources_used FROM brief_provenance_log "
                "WHERE UPPER(ticker) = ? ORDER BY generated_at DESC LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
        except sqlite3.Error:
            row = None
        transcript_docs = _load_transcript_doc_ids(conn, ticker)
        open_notes = _load_open_notes(conn, ticker)
    finally:
        conn.close()

    out: dict[str, object] = {}
    if row is not None and row[0] is not None:
        try:
            parsed = json.loads(row[0])
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            out["sources_used"] = cast("dict[str, object]", parsed)
    if transcript_docs:
        out["transcript_docs"] = transcript_docs
    if open_notes:
        out["open_notes"] = open_notes
    return out or None


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
    transcript_docs = _extract_transcript_docs(brief_provenance)

    body.append(_render_summary_section(parsed))
    body.append(_render_related_notes_section(brief_provenance))
    body.append(_render_citations_section(parsed, per_metric, transcript_docs))
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


def _render_related_notes_section(brief_provenance: Mapping[str, object] | None) -> str:
    """The owner's open notes for this ticker, attached to the evidence
    (P4.4) — watch items and questions lead so an alert lands next to what
    the analyst already said to watch for. Renders nothing when the payload
    carries no notes (cold ticker / pre-0074 DB)."""
    if brief_provenance is None:
        return ""
    raw = brief_provenance.get("open_notes")
    if not isinstance(raw, list):
        return ""
    notes = [
        cast("Mapping[str, object]", n) for n in cast("list[object]", raw) if isinstance(n, dict)
    ]
    if not notes:
        return ""
    rows: list[str] = [
        '<div class="evidence-section evidence-notes">'
        '<div class="evidence-section-title">Related notes</div>'
        '<ul class="evidence-notes-list">'
    ]
    for n in notes:
        kind = str(n.get("kind", "note"))
        note_body = str(n.get("body", ""))
        created = str(n.get("created_at", ""))[:10]
        created_html = f' <span class="muted">({_esc(created)})</span>' if created else ""
        rows.append(
            f'<li><span class="evidence-note-kind">{_esc(kind)}</span> '
            f"{_esc(note_body)}{created_html}</li>"
        )
    rows.append("</ul></div>")
    return "".join(rows)


def _render_citations_section(
    parsed: Mapping[str, object],
    per_metric: Mapping[str, object] | None,
    transcript_docs: Mapping[str, int] | None = None,
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
        viewer = _transcript_viewer_href(c, transcript_docs)
        # Citations the analyst can open render as real links — URL locators
        # (news_url etc.) directly, transcript lines into the in-app
        # /source/<doc_id>#L<n> reader (P4.3). A citation you can't open is
        # a dead end, not provenance.
        if c.locator.startswith(("http://", "https://")):
            locator_html = (
                f'<a href="{_esc(c.locator)}" target="_blank" rel="noopener">{_esc(c.locator)}</a>'
            )
        elif viewer is not None:
            locator_html = (
                f'<a href="{_esc(viewer)}" target="_blank" rel="noopener">{_esc(c.locator)}</a>'
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


def _transcript_viewer_href(
    citation: _Citation, transcript_docs: Mapping[str, int] | None
) -> str | None:
    """``/source/<doc_id>#L<n>`` for a transcript_line citation whose period
    resolves against the ticker's registered transcripts; else None."""
    if citation.kind != "transcript_line" or not transcript_docs:
        return None
    if citation.period is None:
        return None
    doc_id = transcript_docs.get(citation.period.upper())
    if doc_id is None:
        return None
    anchor = f"#L{citation.line_number}" if citation.line_number is not None else ""
    return f"/source/{doc_id}{anchor}"


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
    """One parsed citation entry — flat string fields, validated at parse time.

    ``period`` / ``line_number`` are kept structured (when the entry carries
    them) so transcript_line citations can resolve to the /source reader."""

    __slots__ = ("excerpt", "kind", "line_number", "locator", "period")

    def __init__(
        self,
        kind: str,
        locator: str,
        excerpt: str,
        period: str | None = None,
        line_number: int | None = None,
    ) -> None:
        self.kind = kind
        self.locator = locator
        self.excerpt = excerpt
        self.period = period
        self.line_number = line_number


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
        period_raw = entry_map.get("period")
        line_raw = entry_map.get("line_number")
        line_number: int | None = None
        if isinstance(line_raw, int):
            line_number = line_raw
        elif isinstance(line_raw, str) and line_raw.strip().isdigit():
            line_number = int(line_raw.strip())
        out.append(
            _Citation(
                kind=kind_raw,
                locator=str(locator_raw),
                excerpt=str(excerpt_raw),
                period=period_raw if isinstance(period_raw, str) and period_raw else None,
                line_number=line_number,
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


def _extract_transcript_docs(
    brief_provenance: Mapping[str, object] | None,
) -> Mapping[str, int] | None:
    """Pull the ``transcript_docs`` period → doc-id map (P4.3) when present."""
    if brief_provenance is None:
        return None
    raw = brief_provenance.get("transcript_docs")
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for key, value in cast("Mapping[str, object]", raw).items():
        if isinstance(value, int):
            out[str(key).upper()] = value
    return out or None


def _esc(text: str) -> str:
    return html.escape(text, quote=True)
