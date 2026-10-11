"""In-app source viewers (P3.5): transcript reader + 10-K section reader.

The provenance substrate (0075 locators, accession/filing identity, source
chips) gives every number a position inside a source document; these pages
are the destination that position points at:

  * ``render_transcript_page`` — a registered transcript document rendered
    as numbered lines, each carrying ``id="L<n>"`` so ``#L42`` deep-links
    and highlights the cited line (the evidence drawer's transcript_line
    citations and ``FactLocator.transcript_line`` use this anchor shape).
  * ``render_form10k_page`` — a parsed FMP form-10-K/10-Q JSON document
    (section-keyed text) with a section nav; ``?section=<key>`` deep-links
    the section a ``FactLocator.section`` names.

Both are full standalone pages served by ``GET /source/<doc_id>`` (the
dispatcher route in comments_server routes by doc_type and falls back to
the document's source_url). Renderers return None when the document isn't
viewable in that shape — the route turns that into the fallback, never a
crash.

``?fragment=1`` (UX9) returns the same content chrome-less — a ``sv-frag``
div instead of a full document — for the command-center shell's peek
popover, which provides the styles via ``VIEWER_CONTENT_CSS``. Fragment
mode never 302s to an external source_url (the peek fetch couldn't follow
it cross-origin); external-only documents render their metadata card with
the outbound link instead.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import cast

from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import controls_css
from ui.tokens import FAVICON_LINK, palette_css

_TRANSCRIPT_DOC_TYPES = frozenset({"earnings_call_transcript", "ir_transcript"})
_FORM_JSON_DOC_TYPES = frozenset({"fmp_10k_json", "fmp_10q_json"})
# Plain array-of-records FMP statement endpoints (docs/design/
# provenance_clickthrough.md section 2.2) -- previously served by NEITHER
# _FORM_JSON_DOC_TYPES' section reader nor any other viewer; fell through to
# render_fallback_page. This is the highest-volume financial_facts writer
# family, so closing this gap is Phase A's biggest-volume target.
STATEMENT_JSON_DOC_TYPES = frozenset(
    {
        "fmp_income_statement",
        "fmp_balance_sheet",
        "fmp_cashflow",
        "fmp_as_reported_income",
        "fmp_as_reported_balance",
        "fmp_as_reported_cashflow",
        "fmp_as_reported_financial",
    }
)

# Lines like "Operator:" / "Brian Chesky:" / "Analyst, Morgan Stanley:" get
# their speaker prefix bolded. Conservative: short prefix, single colon.
_SPEAKER_RE = re.compile(r"^([A-Z][\w.\- ',()]{0,70}?):(\s|$)")

# Content rules shared by the full pages and the ``fragment=1`` payloads.
# The command-center shell appends this block to its own stylesheet so a
# fragment injected into the peek renders identically to the full page.
VIEWER_CONTENT_CSS = ""

_PAGE_CSS = ""


@dataclass(slots=True)
class SourceDocRow:
    id: int
    ticker: str
    doc_type: str
    file_path: str
    fetched_at: str | None
    source_url: str | None
    accession_number: str | None
    filing_date: str | None
    # Content hash off the documents row — the PDF page-image cache key
    # (pipeline.pdf_render). "" on schema-tolerant reads of pre-sha256 DBs.
    sha256: str = ""


def load_document(db_path: Path, doc_id: int) -> SourceDocRow | None:
    """The documents row a viewer needs, schema-tolerant on pre-0075 DBs."""
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        identity = (
            "accession_number, filing_date"
            if "accession_number" in cols
            else "NULL AS accession_number, NULL AS filing_date"
        )
        sha_select = "sha256" if "sha256" in cols else "NULL AS sha256"
        row = conn.execute(
            f"SELECT id, ticker, doc_type, file_path, fetched_at, source_url, {identity}, "
            f"{sha_select} FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return SourceDocRow(
        id=int(row["id"]),
        ticker=str(row["ticker"]),
        doc_type=str(row["doc_type"]),
        file_path=str(row["file_path"]),
        fetched_at=str(row["fetched_at"])[:19] if row["fetched_at"] is not None else None,
        source_url=str(row["source_url"]) if row["source_url"] is not None else None,
        accession_number=(
            str(row["accession_number"]) if row["accession_number"] is not None else None
        ),
        filing_date=str(row["filing_date"]) if row["filing_date"] is not None else None,
        sha256=str(row["sha256"]) if row["sha256"] is not None else "",
    )


def _page(title: str, head_extra: str, body: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en" data-theme="dark"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>{FAVICON_LINK}"
        f"<style>{palette_css('dark')}{controls_css('dark')}"
        f"{RESEARCH_PANEL_STYLE.removeprefix('<style>').removesuffix('</style>')}"
        f'{_PAGE_CSS}</style></head><body class="sv-page">'
        f'<div class="sv-head">{head_extra}</div>'
        f'<div class="sv-body">{body}</div>'
        "</body></html>"
    )


def _fragment(title: str, meta_html: str, body: str) -> str:
    """The chrome-less ``fragment=1`` shape: same content classes, no document
    head or styles — the host surface supplies ``VIEWER_CONTENT_CSS``."""
    return (
        '<div class="sv-frag">'
        f'<div class="sv-frag-head"><span class="sv-title">{escape(title)}</span>'
        f"{meta_html}</div>"
        f'<div class="sv-frag-body">{body}</div></div>'
    )


def render_record_view(title: str, body: str, *, fragment: bool = False) -> str:
    """Use the existing source-viewer chrome for already escaped retained evidence."""
    if fragment:
        return _fragment(title, "", body)
    return _page(title, f'<span class="sv-title">{escape(title)}</span>', body)


def _doc_meta_html(doc: SourceDocRow) -> str:
    bits = [f'<span class="sv-meta">doc #{doc.id} · {escape(doc.doc_type)}</span>']
    if doc.accession_number:
        filed = f" · filed {escape(doc.filing_date)}" if doc.filing_date else ""
        bits.append(f'<span class="sv-meta">{escape(doc.accession_number)}{filed}</span>')
    if doc.fetched_at:
        bits.append(f'<span class="sv-meta">fetched {escape(doc.fetched_at[:10])}</span>')
    if doc.source_url and doc.source_url.strip().lower().startswith(("http://", "https://")):
        bits.append(
            f'<a href="{escape(doc.source_url)}" target="_blank" rel="noopener" '
            'class="sv-original-link">original source ↗</a>'
        )
    return "".join(bits)


# ----------------------------------------------------------------------------
# Transcript reader
# ----------------------------------------------------------------------------


def render_transcript_page(
    repo_root: Path, db_path: Path, doc_id: int, *, fragment: bool = False
) -> str | None:
    """Numbered-line transcript page; None when the doc isn't a readable
    transcript (wrong type, missing file). ``fragment=True`` returns the
    chrome-less peek shape (line ids unchanged, so ``#L<n>`` anchors still
    resolve inside the host's popover)."""
    doc = load_document(db_path, doc_id)
    if doc is None or doc.doc_type not in _TRANSCRIPT_DOC_TYPES:
        return None
    path = repo_root / doc.file_path
    if not path.exists() or path.suffix.lower() != ".txt":
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    items: list[str] = []
    for n, line in enumerate(lines, start=1):
        m = _SPEAKER_RE.match(line)
        if m is not None:
            rest = line[m.end(1) + 1 :]
            text = f'<span class="ln-speaker">{escape(m.group(1))}:</span>{escape(rest)}'
        else:
            text = escape(line) or "&nbsp;"
        items.append(f'<li id="L{n}"><span class="ln-text">{text}</span></li>')

    title = f"{doc.ticker} transcript · {path.stem}"
    body = f'<ol class="sv-lines">{"".join(items)}</ol>'
    if fragment:
        meta = f'{_doc_meta_html(doc)}<span class="sv-meta">{len(lines)} lines</span>'
        return _fragment(title, meta, body)
    head = (
        f'<span class="sv-title">{escape(title)}</span>{_doc_meta_html(doc)}'
        f'<span class="sv-meta">{len(lines)} lines · link any line as #L&lt;n&gt;</span>'
    )
    return _page(title, head, body)


# ----------------------------------------------------------------------------
# 10-K / 10-Q section reader
# ----------------------------------------------------------------------------

_META_KEYS = frozenset({"symbol", "period", "year"})


def render_form10k_page(
    repo_root: Path,
    db_path: Path,
    doc_id: int,
    section: str | None = None,
    *,
    fragment: bool = False,
) -> str | None:
    """Section reader over a parsed FMP form 10-K/10-Q JSON; None when the
    doc isn't one or the file can't be read. ``section`` deep-links the
    section a FactLocator.section names; default = the first section.

    In ``fragment`` mode the section nav uses absolute ``/source/<id>?section=``
    hrefs — a relative ``?section=`` would resolve against the HOST page the
    peek is embedded in, navigating the whole shell instead of the popover.
    """
    doc = load_document(db_path, doc_id)
    if doc is None or doc.doc_type not in _FORM_JSON_DOC_TYPES:
        return None
    path = repo_root / doc.file_path
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = cast("object", json.load(f))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    doc_map = cast("dict[str, object]", payload)
    sections = [k for k in doc_map if k not in _META_KEYS]
    if not sections:
        return None
    active = section if section in doc_map else sections[0]

    nav_parts: list[str] = []
    for k in sections:
        cls = "k-chip k-chip-btn is-on" if k == active else "k-chip k-chip-btn"
        href = (
            f"/source/{doc.id}?section={urllib.parse.quote(k)}"
            if fragment
            else f"?section={escape(k)}"
        )
        nav_parts.append(f'<a class="{cls}" href="{href}">{escape(k)}</a>')
    nav = "".join(nav_parts)
    year = doc_map.get("year")
    period = doc_map.get("period")
    title = f"{doc.ticker} {escape(str(period)) if period else ''} {year if year else ''} filing"
    body = (
        f'<div class="sv-secnav">{nav}</div>'
        f"<h2>{escape(active)}</h2>{_render_section(doc_map.get(active))}"
    )
    if fragment:
        return _fragment(title.strip(), _doc_meta_html(doc), body)
    head = f'<span class="sv-title">{escape(title.strip())}</span>{_doc_meta_html(doc)}'
    return _page(title.strip(), head, body)


def _render_section(raw: object) -> str:
    """Generic renderer for one parsed-filing section.

    FMP's parsed shape is a list of single-key dicts mapping a label to a
    list of values (the Cover sample: {"Document Type": ["10-K"]}); long
    narrative sections carry paragraphs of text in those values. Render
    label/value rows, escape everything, tolerate any shape drift.
    """
    if raw is None:
        return '<p class="sv-sec-val">—</p>'
    if isinstance(raw, str):
        return f'<p class="sv-sec-val">{escape(raw)}</p>'
    if not isinstance(raw, list):
        return f'<p class="sv-sec-val">{escape(json.dumps(raw, default=str))}</p>'
    rows: list[str] = []
    for entry in cast("list[object]", raw):
        if isinstance(entry, dict):
            for key, vals in cast("dict[str, object]", entry).items():
                if isinstance(vals, list):
                    text = "\n".join(str(v) for v in cast("list[object]", vals) if v is not None)
                else:
                    text = str(vals) if vals is not None else ""
                rows.append(
                    '<div class="sv-sec-row">'
                    f'<div class="sv-sec-key">{escape(str(key))}</div>'
                    f'<div class="sv-sec-val">{escape(text) or "—"}</div></div>'
                )
        else:
            rows.append(
                f'<div class="sv-sec-row"><div class="sv-sec-val">{escape(str(entry))}</div></div>'
            )
    return "".join(rows)


# ----------------------------------------------------------------------------
# FMP statement-endpoint reader (fmp_json_table locators, array-of-records
# shape -- docs/design/provenance_clickthrough.md section 2.2)
# ----------------------------------------------------------------------------

_JSON_PATH_RX = re.compile(r"^\[(\d+)\]\.(.+)$")


def _render_statement_table(
    doc: SourceDocRow,
    records: list[dict[str, object]],
    columns: list[str],
    *,
    json_path: str | None,
    row_label: str | None,
    column_header: str | None,
    fragment: bool,
) -> str:
    """Body for render_statement_json_page: locate the cited cell (json_path
    fast-path, else a row_label/column_header field+period match -- the
    pointer may be stale if the source file re-shaped since extraction),
    then render the table with that cell highlighted."""
    hit_idx: int | None = None
    hit_field: str | None = None
    if json_path:
        m = _JSON_PATH_RX.match(json_path)
        if m is not None:
            idx = int(m.group(1))
            if 0 <= idx < len(records):
                hit_idx, hit_field = idx, m.group(2)
    if hit_idx is None and (row_label or column_header):
        for i, rec in enumerate(records):
            period_val = rec.get("date") or rec.get("period")
            if column_header is not None and str(period_val) != column_header:
                continue
            if row_label is not None and row_label in rec:
                hit_idx, hit_field = i, row_label
                break

    def _period_label(rec: dict[str, object]) -> str:
        val = rec.get("date") or rec.get("period")
        return str(val) if val is not None else "—"

    def _cell_html(rec: dict[str, object], col: str, idx: int) -> str:
        val = rec.get(col)
        text = "" if val is None else str(val)
        is_hit = hit_idx == idx and hit_field == col
        cls = "sv-cell-hit" if is_hit else ""
        cell_id = ' id="sv-hit"' if is_hit else ""
        return f'<td class="{cls}"{cell_id}>{escape(text) or "—"}</td>'

    head_row = "<tr><th>Period</th>" + "".join(f"<th>{escape(c)}</th>" for c in columns) + "</tr>"
    body_rows = "".join(
        f"<tr><td>{escape(_period_label(rec))}</td>"
        + "".join(_cell_html(rec, c, i) for c in columns)
        + "</tr>"
        for i, rec in enumerate(records)
    )
    table = (
        '<div class="sv-stmt-wrap"><table class="sv-stmt-table">'
        f"<thead>{head_row}</thead><tbody>{body_rows}</tbody></table></div>"
    )
    script = (
        '<script>(function(){var el=document.getElementById("sv-hit");'
        'if(el&&el.scrollIntoView){el.scrollIntoView({block:"center",inline:"center"});}})();</script>'
        if hit_idx is not None
        else ""
    )
    footer = (
        f'<div class="sv-stmt-foot"><a href="/source/{doc.id}">open full filing &rarr;</a></div>'
    )
    title = f"{doc.ticker} · {doc.doc_type.replace('_', ' ')}"
    body = table + footer + script
    if fragment:
        return _fragment(title, _doc_meta_html(doc), body)
    head = f'<span class="sv-title">{escape(title)}</span>{_doc_meta_html(doc)}'
    return _page(title, head, body)


def render_statement_json_page(
    repo_root: Path,
    db_path: Path,
    doc_id: int,
    *,
    json_path: str | None = None,
    row_label: str | None = None,
    column_header: str | None = None,
    fragment: bool = False,
) -> str | None:
    """Renders a plain FMP statement-endpoint JSON (income/balance/cashflow,
    as-reported) as one table: rows = records (periods), columns = the
    fields present in that record. None when the doc isn't one of this
    family or the file can't be parsed as a JSON array."""
    doc = load_document(db_path, doc_id)
    if doc is None or doc.doc_type not in STATEMENT_JSON_DOC_TYPES:
        return None
    path = repo_root / doc.file_path
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = cast("object", json.load(f))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None

    columns: list[str] = []
    seen_cols: set[str] = set()
    records: list[dict[str, object]] = []
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            continue
        rec = cast("dict[str, object]", entry)
        records.append(rec)
        for key in rec:
            if key not in seen_cols:
                seen_cols.add(key)
                columns.append(key)
    if not records:
        return None
    return _render_statement_table(
        doc,
        records,
        columns,
        json_path=json_path,
        row_label=row_label,
        column_header=column_header,
        fragment=fragment,
    )


# ----------------------------------------------------------------------------
# PDF page-image viewer (pdf_slide locators — provenance click-through
# Phase B, docs/design/provenance_clickthrough.md section 2.3)
# ----------------------------------------------------------------------------


def render_pdf_page_view(
    repo_root: Path,
    db_path: Path,
    doc_id: int,
    page: int | None = None,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    snippet: str | None = None,
    fragment: bool = False,
) -> str | None:
    """One rendered PDF page as an image, with an optional bbox highlight.

    The destination a ``pdf_slide`` locator points at: the page image (via
    the ``/source/<doc_id>/page/<n>.png`` route, rendered+cached lazily by
    ``pipeline.pdf_render``), a highlight rectangle when the locator carries
    a ``pdf_bbox`` (percentage-positioned over the image so it survives
    responsive scaling), and the ``verbatim_snippet`` as a callout below so
    the reader isn't hunting blind on a bbox-less page.

    None when the doc isn't a renderable PDF (wrong suffix, missing file,
    PyMuPDF unavailable) — the dispatcher's fallback page takes over, never
    a dead end. In ``fragment`` mode the pager uses absolute
    ``/source/<id>?page=`` hrefs (same reasoning as render_form10k_page's
    section nav).
    """
    from pipeline.pdf_render import page_count, page_dimensions

    doc = load_document(db_path, doc_id)
    if doc is None or not doc.file_path.lower().endswith(".pdf"):
        return None
    path = Path(doc.file_path)
    if not path.is_absolute():
        path = repo_root / path
    n_pages = page_count(path)
    if n_pages is None or n_pages < 1:
        return None
    active = min(max(page if page is not None else 1, 1), n_pages)

    img_src = f"/source/{doc.id}/page/{active}.png"
    overlay = ""
    if bbox is not None:
        dims = page_dimensions(path, active)
        if dims is not None and dims[0] > 0 and dims[1] > 0:
            w, h = dims
            x0, y0, x1, y1 = bbox
            left = max(0.0, min(100.0, x0 / w * 100))
            top = max(0.0, min(100.0, y0 / h * 100))
            width = max(0.0, min(100.0 - left, (x1 - x0) / w * 100))
            height = max(0.0, min(100.0 - top, (y1 - y0) / h * 100))
            overlay = (
                f'<div class="sv-pdf-hit" style="left:{left:.2f}%;top:{top:.2f}%;'
                f'width:{width:.2f}%;height:{height:.2f}%"></div>'
            )

    def _page_href(n: int) -> str:
        return f"/source/{doc.id}?page={n}" if fragment else f"?page={n}"

    pager_bits: list[str] = []
    if active > 1:
        pager_bits.append(
            f'<a class="k-chip k-chip-btn" href="{_page_href(active - 1)}">&larr; p.{active - 1}</a>'
        )
    pager_bits.append(f'<span class="sv-pdf-page-n">page {active} / {n_pages}</span>')
    if active < n_pages:
        pager_bits.append(
            f'<a class="k-chip k-chip-btn" href="{_page_href(active + 1)}">p.{active + 1} &rarr;</a>'
        )
    pager = f'<div class="sv-pdf-pager">{"".join(pager_bits)}</div>'

    caption = ""
    if snippet:
        note = "" if bbox is not None else "cited value is on this page &mdash; "
        caption = f'<div class="k-well sv-pdf-snippet">{note}&ldquo;{escape(snippet)}&rdquo;</div>'

    stage = (
        f'<div class="sv-pdf-stage"><img src="{img_src}" '
        f'alt="{escape(doc.ticker)} {escape(doc.doc_type)} page {active}">{overlay}</div>'
    )
    body = pager + stage + caption
    title = f"{doc.ticker} · {doc.doc_type.replace('_', ' ')} · p.{active}"
    if fragment:
        return _fragment(title, _doc_meta_html(doc), body)
    head = f'<span class="sv-title">{escape(title)}</span>{_doc_meta_html(doc)}'
    return _page(title, head, body)


# ----------------------------------------------------------------------------
# Fallback page for non-viewable documents
# ----------------------------------------------------------------------------


def render_fallback_page(db_path: Path, doc_id: int, *, fragment: bool = False) -> str:
    """Shown when a document has no in-app viewer: its registry metadata, so
    the link is never a dead end. The full-page route only reaches this when
    there is also no source_url to 302 to; ``fragment`` mode reaches it for
    ANY non-viewable doc (a peek fetch can't follow an external redirect), so
    the metadata card's "original source ↗" link is the way out."""
    doc = load_document(db_path, doc_id)
    if doc is None:
        if fragment:
            return (
                '<div class="sv-frag"><p class="sv-sec-key">'
                f"No documents row with id {doc_id}.</p></div>"
            )
        body = f'<div class="sv-fallback"><h2>Unknown document</h2><p class="sv-sec-key">No documents row with id {doc_id}.</p></div>'
        return _page("Unknown document", '<span class="sv-title">Source</span>', body)
    rows = (
        '<p class="sv-sec-key">No in-app viewer for this document type.</p>'
        f'<div class="sv-sec-row"><div class="sv-sec-key">file</div>'
        f'<div class="sv-sec-val">{escape(doc.file_path)}</div></div>'
        + (
            f'<div class="sv-sec-row"><div class="sv-sec-key">accession</div>'
            f'<div class="sv-sec-val">{escape(doc.accession_number)}'
            + (f" · filed {escape(doc.filing_date)}" if doc.filing_date else "")
            + "</div></div>"
            if doc.accession_number
            else ""
        )
    )
    if fragment:
        return _fragment(f"{doc.ticker} · {doc.doc_type}", _doc_meta_html(doc), rows)
    body = (
        '<div class="sv-fallback">'
        f"<h2>{escape(doc.ticker)} · {escape(doc.doc_type)}</h2>" + rows + "</div>"
    )
    head = f'<span class="sv-title">Source · doc #{doc.id}</span>{_doc_meta_html(doc)}'
    return _page(f"Source doc {doc.id}", head, body)


# ----------------------------------------------------------------------------
# Sealed evidence document-version viewer (M4 F10 — sealed citation destination)
# ----------------------------------------------------------------------------
#
# The sealed Ask lane cites ``/source/version/<document_version_id>?node=<node_id>``
# (``src/ask/sealed_retrieval.py`` ``_narrative_item`` / ``_fact_item``). Before
# this route existed the strongest provenance chain in the repo dead-ended at
# click time: the href had no handler. ``render_document_version_page`` resolves
# that immutable evidence-ledger coordinate to the complete chain — issuer,
# fiscal period, source-document identity, the cited node locator, the
# observation version, and every canonical fact projection entry bound to the
# cited node — and returns ``None`` (an honest 404) when the document version or
# the cited node does not exist. It reads only the append-only evidence tables;
# it never touches the legacy ``documents`` table and never fabricates a chain.
#
# The rendered rows carry the same values in ``data-*`` attributes as in the
# visible text so a hermetic test can reconcile every displayed field against
# the fixture's immutable observation version (VAL-SEARCH-002/003).


@dataclass(slots=True)
class DocumentVersionChunk:
    """One search chunk anchored inside an evidence node's exact text span."""

    chunk_id: str
    text: str
    char_start: int
    char_end: int


@dataclass(slots=True)
class DocumentVersionNode:
    """One immutable evidence node and the chunks that anchor inside it."""

    node_id: str
    node_kind: str
    text: str
    locator_json: str | None
    chunks: tuple[DocumentVersionChunk, ...]


@dataclass(slots=True)
class DocumentVersionFact:
    """One canonical fact projection entry citing this document version/node."""

    metric_name: str
    value: str | None
    unit_key: str | None
    currency: str | None
    period_start: str | None
    period_end: str | None
    node_id: str
    locator_json: str | None


@dataclass(slots=True)
class DocumentVersionView:
    """The complete provenance chain behind one sealed evidence citation."""

    document_version_id: str
    version_sequence: int
    issuer_id: str
    ticker: str | None
    document_type: str
    form_type: str
    accession_number: str | None
    exhibit_id: str | None
    period_start: str | None
    period_end: str | None
    as_of_at: str | None
    language: str
    replaces_document_version_id: str | None
    legacy_document_id: int | None
    recorded_at: str
    blob_sha256: str
    observation_id: str
    source_kind: str
    source_url: str
    observed_at: str
    retrieved_at: str
    nodes: tuple[DocumentVersionNode, ...]
    facts: tuple[DocumentVersionFact, ...]


_DOCUMENT_VERSION_SQL = (
    "SELECT document.document_version_id,document.version_sequence,document.issuer_id,"
    "document.ticker,document.document_type,document.form_type,document.accession_number,"
    "document.exhibit_id,document.period_start,document.period_end,document.as_of_at,"
    "document.language,document.replaces_document_version_id,document.legacy_document_id,"
    "document.recorded_at,document.blob_sha256,observation.observation_id,"
    "observation.source_kind,observation.source_url,observation.observed_at,"
    "observation.retrieved_at "
    "FROM evidence_document_versions document "
    "JOIN evidence_source_observations observation "
    "ON observation.observation_id=document.observation_id "
    "WHERE document.document_version_id=?"
)

_DOCUMENT_VERSION_NODES_SQL = (
    "SELECT node.node_id,node.node_kind,node.text,node.locator_json,"
    "chunk.chunk_id,chunk.text AS chunk_text,chunk.char_start,chunk.char_end "
    "FROM evidence_nodes node "
    "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
    "LEFT JOIN search_chunks chunk ON chunk.evidence_node_id=node.node_id "
    "WHERE run.document_version_id=? "
    "ORDER BY node.node_id,chunk.chunk_id"
)

_DOCUMENT_VERSION_FACTS_SQL = (
    "SELECT canonical_metric_cell_id,canonical_metric_name,canonical_value,unit_key,currency,"
    "period_kind,period_start,period_end,evidence_node_id,evidence_locator_json "
    "FROM canonical_fact_projection_entries "
    "WHERE evidence_document_version_id=? AND change_kind='upsert' "
    "ORDER BY canonical_metric_name,period_end,canonical_metric_cell_id"
)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def load_document_version(db_path: Path, document_version_id: str) -> DocumentVersionView | None:
    """Read one immutable evidence document version, or None when it is absent.

    Schema-tolerant and fail-closed: a missing database, a missing row, or a
    query error all return None so the route answers an honest 404 rather than
    inventing a provenance chain.
    """
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        document = conn.execute(_DOCUMENT_VERSION_SQL, (document_version_id,)).fetchone()
        if document is None:
            return None
        node_rows = conn.execute(_DOCUMENT_VERSION_NODES_SQL, (document_version_id,)).fetchall()
        fact_rows = conn.execute(_DOCUMENT_VERSION_FACTS_SQL, (document_version_id,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    nodes: dict[str, DocumentVersionNode] = {}
    node_order: list[str] = []
    for node_row in node_rows:
        node_id = str(node_row["node_id"])
        if node_id not in nodes:
            nodes[node_id] = DocumentVersionNode(
                node_id=node_id,
                node_kind=str(node_row["node_kind"]),
                text=str(node_row["text"]),
                locator_json=_optional_text(node_row["locator_json"]),
                chunks=(),
            )
            node_order.append(node_id)
        if node_row["chunk_id"] is not None:
            node = nodes[node_id]
            node.chunks = (
                *node.chunks,
                DocumentVersionChunk(
                    chunk_id=str(node_row["chunk_id"]),
                    text=str(node_row["chunk_text"]),
                    char_start=int(node_row["char_start"]),
                    char_end=int(node_row["char_end"]),
                ),
            )

    facts = tuple(
        DocumentVersionFact(
            metric_name=str(fact_row["canonical_metric_name"]),
            value=_optional_text(fact_row["canonical_value"]),
            unit_key=_optional_text(fact_row["unit_key"]),
            currency=_optional_text(fact_row["currency"]),
            period_start=_optional_text(fact_row["period_start"]),
            period_end=_optional_text(fact_row["period_end"]),
            node_id=str(fact_row["evidence_node_id"]),
            locator_json=_optional_text(fact_row["evidence_locator_json"]),
        )
        for fact_row in fact_rows
    )

    return DocumentVersionView(
        document_version_id=str(document["document_version_id"]),
        version_sequence=int(document["version_sequence"]),
        issuer_id=str(document["issuer_id"]),
        ticker=_optional_text(document["ticker"]),
        document_type=str(document["document_type"]),
        form_type=str(document["form_type"]),
        accession_number=_optional_text(document["accession_number"]),
        exhibit_id=_optional_text(document["exhibit_id"]),
        period_start=_optional_text(document["period_start"]),
        period_end=_optional_text(document["period_end"]),
        as_of_at=_optional_text(document["as_of_at"]),
        language=str(document["language"]),
        replaces_document_version_id=_optional_text(document["replaces_document_version_id"]),
        legacy_document_id=(
            None if document["legacy_document_id"] is None else int(document["legacy_document_id"])
        ),
        recorded_at=str(document["recorded_at"]),
        blob_sha256=str(document["blob_sha256"]),
        observation_id=str(document["observation_id"]),
        source_kind=str(document["source_kind"]),
        source_url=str(document["source_url"]),
        observed_at=str(document["observed_at"]),
        retrieved_at=str(document["retrieved_at"]),
        nodes=tuple(nodes[node_id] for node_id in node_order),
        facts=facts,
    )


def _sv_data_attr(name: str, value: object) -> str:
    """Render one optional ``data-*`` attribute (omitted when the value is None)."""
    if value is None:
        return ""
    return f' data-{name}="{escape(str(value), quote=True)}"'


def _sv_row(label: str, value_html: str, attrs: str = "") -> str:
    return (
        f'<div class="sv-sec-row"{attrs}>'
        f'<div class="sv-sec-key">{escape(label)}</div>'
        f'<div class="sv-sec-val">{value_html}</div></div>'
    )


def _sv_locator_text(locator_json: str | None) -> str:
    """A compact, stable rendering of one evidence locator object."""
    if not locator_json:
        return "no structured locator"
    try:
        parsed = json.loads(locator_json)
    except (TypeError, ValueError):
        return locator_json
    if not isinstance(parsed, dict):
        return locator_json
    locator = cast("dict[str, object]", parsed)
    parts = [f"{key}={locator[key]!r}" for key in sorted(locator)]
    return ", ".join(parts) if parts else "empty locator"


def _document_version_body(
    view: DocumentVersionView,
    nodes: tuple[DocumentVersionNode, ...],
    facts: tuple[DocumentVersionFact, ...],
) -> str:
    ticker = view.ticker or "—"
    period = (
        " → ".join(part for part in (view.period_start, view.period_end) if part) or "not stated"
    )
    accession = view.accession_number or "not stated"
    source_url_html = (
        f'<a href="{escape(view.source_url)}" target="_blank" rel="noopener" '
        'class="sv-original-link">original source ↗</a>'
        if view.source_url.strip().lower().startswith(("http://", "https://"))
        else escape(view.source_url)
    )
    rows: list[str] = [
        _sv_row(
            "issuer",
            f"{escape(ticker)} · issuer {escape(view.issuer_id)}",
            _sv_data_attr("issuer-id", view.issuer_id) + _sv_data_attr("ticker", view.ticker),
        ),
        _sv_row(
            "fiscal period",
            escape(period),
            _sv_data_attr("period-start", view.period_start)
            + _sv_data_attr("period-end", view.period_end),
        ),
        _sv_row(
            "source document",
            f"{escape(view.form_type)} · {escape(view.document_type)} · accession "
            f"{escape(accession)}",
            _sv_data_attr("document-version-id", view.document_version_id)
            + _sv_data_attr("form-type", view.form_type)
            + _sv_data_attr("document-type", view.document_type)
            + _sv_data_attr("accession-number", view.accession_number)
            + _sv_data_attr("exhibit-id", view.exhibit_id),
        ),
        _sv_row(
            "source bytes",
            f"sha256 {escape(view.blob_sha256)} · {source_url_html}",
            _sv_data_attr("blob-sha256", view.blob_sha256)
            + _sv_data_attr("source-url", view.source_url)
            + _sv_data_attr("source-kind", view.source_kind),
        ),
        _sv_row(
            "observation version",
            f"document version {escape(view.document_version_id)} "
            f"(v{view.version_sequence}) · recorded {escape(view.recorded_at)}",
            _sv_data_attr("document-version-id", view.document_version_id)
            + _sv_data_attr("version-sequence", view.version_sequence)
            + _sv_data_attr("recorded-at", view.recorded_at),
        ),
        _sv_row(
            "observation",
            f"observation {escape(view.observation_id)} · observed "
            f"{escape(view.observed_at)} · retrieved {escape(view.retrieved_at)}",
            _sv_data_attr("observation-id", view.observation_id)
            + _sv_data_attr("observed-at", view.observed_at)
            + _sv_data_attr("retrieved-at", view.retrieved_at),
        ),
    ]
    if view.replaces_document_version_id is not None:
        rows.append(
            _sv_row(
                "supersedes",
                f"replaces document version {escape(view.replaces_document_version_id)}",
                _sv_data_attr("replaces-document-version-id", view.replaces_document_version_id),
            )
        )
    if view.legacy_document_id is not None:
        rows.append(
            _sv_row(
                "legacy document",
                f'<a href="/source/{view.legacy_document_id}">doc #{view.legacy_document_id}</a>',
                _sv_data_attr("legacy-document-id", view.legacy_document_id),
            )
        )

    for node in nodes:
        rows.append(
            _sv_row(
                "cited node",
                f"{escape(node.node_id)} · {escape(node.node_kind)} · "
                f"{escape(_sv_locator_text(node.locator_json))}",
                _sv_data_attr("node-id", node.node_id)
                + _sv_data_attr("node-kind", node.node_kind)
                + _sv_data_attr("node-locator", node.locator_json),
            )
        )
        if node.chunks:
            for chunk in node.chunks:
                rows.append(
                    _sv_row(
                        "cited chunk span",
                        f"{escape(chunk.chunk_id)} · chars {chunk.char_start}&ndash;"
                        f"{chunk.char_end}",
                        _sv_data_attr("chunk-id", chunk.chunk_id)
                        + _sv_data_attr("char-start", chunk.char_start)
                        + _sv_data_attr("char-end", chunk.char_end),
                    )
                )
            cited_text = node.chunks[0].text
        else:
            cited_text = node.text
        rows.append(_sv_row("cited text", f'<div class="k-well">{escape(cited_text)}</div>'))

    for fact in facts:
        value_text = " ".join(part for part in (fact.value, fact.currency or fact.unit_key) if part)
        fact_period = fact.period_end or fact.period_start or "not stated"
        rows.append(
            _sv_row(
                "reported fact",
                f"{escape(fact.metric_name)}: {escape(value_text)} ({escape(fact_period)})",
                _sv_data_attr("fact-metric", fact.metric_name)
                + _sv_data_attr("fact-value", fact.value)
                + _sv_data_attr("fact-unit", fact.unit_key)
                + _sv_data_attr("fact-currency", fact.currency)
                + _sv_data_attr("fact-period-start", fact.period_start)
                + _sv_data_attr("fact-period-end", fact.period_end)
                + _sv_data_attr("fact-node-id", fact.node_id)
                + _sv_data_attr("fact-locator", fact.locator_json),
            )
        )

    root_attrs = (
        _sv_data_attr("document-version-id", view.document_version_id)
        + _sv_data_attr("version-sequence", view.version_sequence)
        + _sv_data_attr("issuer-id", view.issuer_id)
        + _sv_data_attr("ticker", view.ticker)
        + _sv_data_attr("document-type", view.document_type)
        + _sv_data_attr("form-type", view.form_type)
        + _sv_data_attr("period-start", view.period_start)
        + _sv_data_attr("period-end", view.period_end)
        + _sv_data_attr("observation-id", view.observation_id)
        + _sv_data_attr("blob-sha256", view.blob_sha256)
        + _sv_data_attr("observed-at", view.observed_at)
        + _sv_data_attr("retrieved-at", view.retrieved_at)
        + _sv_data_attr("recorded-at", view.recorded_at)
    )
    return f'<div class="sv-fallback"{root_attrs}>' + "".join(rows) + "</div>"


def render_document_version_page(
    db_path: Path,
    document_version_id: str,
    *,
    node_id: str | None = None,
    fragment: bool = False,
) -> str | None:
    """The sealed citation destination: the full provenance chain, or None.

    ``None`` is the honest-404 signal: an unknown document version, or a
    ``node`` that is not part of that document version, never renders a
    fabricated chain.
    """
    view = load_document_version(db_path, document_version_id)
    if view is None:
        return None
    if node_id is not None:
        nodes = tuple(node for node in view.nodes if node.node_id == node_id)
        if not nodes:
            return None
        facts = tuple(fact for fact in view.facts if fact.node_id == node_id)
    else:
        nodes = view.nodes
        facts = view.facts
    title_bits = (
        view.ticker,
        view.form_type or view.document_type,
        view.period_end,
    )
    title = " · ".join(part for part in title_bits if part) or view.document_version_id
    meta = (
        f'<span class="sv-meta">version {escape(view.document_version_id)}</span>'
        f'<span class="sv-meta">observation {escape(view.observation_id)}</span>'
    )
    body = _document_version_body(view, nodes, facts)
    if fragment:
        return _fragment(title, meta, body)
    head = f'<span class="sv-title">{escape(title)}</span>{meta}'
    return _page(f"Source version {view.document_version_id}", head, body)
