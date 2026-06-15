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

from ui.controls import controls_css
from ui.tokens import FAVICON_LINK, palette_css

_TRANSCRIPT_DOC_TYPES = frozenset({"earnings_call_transcript", "ir_transcript"})
_FORM_JSON_DOC_TYPES = frozenset({"fmp_10k_json", "fmp_10q_json"})

# Lines like "Operator:" / "Brian Chesky:" / "Analyst, Morgan Stanley:" get
# their speaker prefix bolded. Conservative: short prefix, single colon.
_SPEAKER_RE = re.compile(r"^([A-Z][\w.\- ',()]{0,70}?):(\s|$)")

# Content rules shared by the full pages and the ``fragment=1`` payloads.
# The command-center shell appends this block to its own stylesheet so a
# fragment injected into the peek renders identically to the full page.
VIEWER_CONTENT_CSS = """
.sv-title { font-size: var(--fs-title); font-weight: 600; }
.sv-meta { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); }
.sv-lines { list-style: none; margin: 0; padding: 0; counter-reset: ln; }
.sv-lines li { counter-increment: ln; padding: 1px 8px 1px 0; display: flex; gap: 14px; }
.sv-lines li::before { content: counter(ln); color: var(--muted-2); width: 42px;
  flex: none; text-align: right; font-family: var(--mono); font-size: var(--fs-caption);
  padding-top: 2px; user-select: none; }
.sv-lines li:target { background: color-mix(in srgb, var(--warn) 14%, transparent);
  outline: 1px solid var(--warn); border-radius: var(--radius); }
.sv-lines .ln-text { white-space: pre-wrap; word-break: break-word; }
.sv-lines .ln-speaker { font-weight: 600; color: var(--accent); }
.sv-secnav { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 18px; }
.sv-secnav a { font-size: var(--fs-caption); padding: 3px 9px; border: 1px solid var(--border);
  border-radius: var(--radius-full); text-decoration: none; color: var(--muted); }
.sv-secnav a.active { color: var(--accent); border-color: var(--accent); }
.sv-secnav a:hover { color: var(--fg); }
.sv-sec-row { padding: 4px 0; border-bottom: 1px solid var(--border); }
.sv-sec-key { color: var(--muted); font-size: var(--fs-caption); }
.sv-sec-val { white-space: pre-wrap; word-break: break-word; }
.sv-frag-head { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
  margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
"""

_PAGE_CSS = (
    """
body { margin: 0; font-family: var(--sans); background: var(--bg); color: var(--fg);
  font-size: var(--fs-section); line-height: 1.55; }
a { color: var(--accent); }
.sv-head { padding: 14px 22px; border-bottom: 1px solid var(--border);
  display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
  position: sticky; top: 0; background: var(--bg); z-index: 5; }
.sv-body { max-width: 980px; margin: 0 auto; padding: 18px 22px 80px; }
.sv-fallback { max-width: 720px; margin: 60px auto; padding: 22px;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
"""
    + VIEWER_CONTENT_CSS
)


@dataclass(slots=True)
class _DocRow:
    id: int
    ticker: str
    doc_type: str
    file_path: str
    fetched_at: str | None
    source_url: str | None
    accession_number: str | None
    filing_date: str | None


def load_document(db_path: Path, doc_id: int) -> _DocRow | None:
    """The documents row a viewer needs, schema-tolerant on pre-0075 DBs."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
        row = conn.execute(
            f"SELECT id, ticker, doc_type, file_path, fetched_at, source_url, {identity} "
            "FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _DocRow(
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
    )


def _page(title: str, head_extra: str, body: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en" data-theme="dark"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>{FAVICON_LINK}"
        f"<style>{palette_css('dark')}{controls_css('dark')}{_PAGE_CSS}</style></head><body>"
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


def _doc_meta_html(doc: _DocRow) -> str:
    bits = [f'<span class="sv-meta">doc #{doc.id} · {escape(doc.doc_type)}</span>']
    if doc.accession_number:
        filed = f" · filed {escape(doc.filing_date)}" if doc.filing_date else ""
        bits.append(f'<span class="sv-meta">{escape(doc.accession_number)}{filed}</span>')
    if doc.fetched_at:
        bits.append(f'<span class="sv-meta">fetched {escape(doc.fetched_at[:10])}</span>')
    if doc.source_url:
        bits.append(
            f'<a href="{escape(doc.source_url)}" target="_blank" rel="noopener" '
            'style="font-size:var(--fs-caption);">original source ↗</a>'
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
        cls = ' class="active"' if k == active else ""
        href = (
            f"/source/{doc.id}?section={urllib.parse.quote(k)}"
            if fragment
            else f"?section={escape(k)}"
        )
        nav_parts.append(f'<a href="{href}"{cls}>{escape(k)}</a>')
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
