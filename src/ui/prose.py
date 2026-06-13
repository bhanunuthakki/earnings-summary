"""The one render boundary for stored analyst / LLM prose.

The Instrument Paradigm names exactly **one renderer per content-kind**: any
stored body / narrative / memo / note rendered to HTML passes through
:func:`render_prose`. Use ``inline=True`` for ``<td>``/``<p>`` containers that
must stay valid inline HTML (it runs only the span pass — bold / italic / inline
code — and emits no block tags).

Bare ``html.escape()`` of a prose field, and any locally-defined markdown
renderer outside this module, are forbidden by ``tests/test_ui_prose_boundary``
— EXCEPT deterministic non-markdown fields (the attribution narrative and the
evals judge rationale), which are plain machine-authored text that ``escape``
renders correctly and ``render_prose`` would corrupt.

History — why this module exists. There used to be **three divergent server
renderers** (the workspace ``_render_markdown``, the dashboard
``light_markdown_to_html``, plus the ask-dock JS ``md()``), so the same markdown
rendered differently per surface and ``**bold**`` / ``##`` leaked wherever the
local renderer was thinner or absent. This lifts the most complete of them — the
workspace ``_render_markdown`` (headings, bold, italic, inline code, bullets,
pipe tables) — and folds in the ``<hr>`` rule the dashboard renderer carried, so
:func:`render_prose` is a strict **superset** of every server renderer it
replaces; ``light_markdown_to_html`` and the workspace ``_render_markdown`` are
now thin re-exports of it.

The ask-dock JS ``md()`` (``src/pipeline/ask_dock.py``) is a deliberate,
documented **INLINE-SUBSET MIRROR**: it cannot be server-rendered because Ask
streams tokens token-by-token and threads cite-marks through the same string
client-side. The server side here is canonical; keep the JS mirror in rough
inline parity (bold / code / bullets / paragraphs), and never grow a fourth
server renderer.
"""

from __future__ import annotations

import html
import re

__all__ = ["render_prose"]


_BOLD_RX = re.compile(r"\*\*([^*]+)\*\*")
_ITAL_RX = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_INLINE_CODE_RX = re.compile(r"`([^`]+)`")
_HEADING_RX = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RX = re.compile(r"^\s*[-*]\s+")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(text: str) -> str:
    """The span pass: escape, then bold / italic / inline code. No block tags."""
    text = _esc(text)
    text = _BOLD_RX.sub(r"<strong>\1</strong>", text)
    text = _ITAL_RX.sub(r"<em>\1</em>", text)
    return _INLINE_CODE_RX.sub(r"<code>\1</code>", text)


def render_prose(md: str, *, inline: bool = False) -> str:
    """Render stored markdown prose to HTML — THE one prose render boundary.

    Handles headings, paragraphs, bullet lists, bold, italic, inline code,
    horizontal rules, and pipe tables — enough for the LLM/analyst content this
    product stores. Anything fancier is escaped and shown verbatim. The input is
    always escaped first, so this never emits unsanitized HTML (do NOT wrap it in
    a separate sanitizer).

    ``inline=True`` runs ONLY the inline span pass (bold/italic/code, no block
    tags) for ``<td>``/``<p>`` containers that must stay valid inline HTML;
    block markers (``##``, ``-``) are left as literal text rather than break the
    cell's structure.
    """
    if not md:
        return ""
    if inline:
        return _inline(md)

    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_ul = False
    in_table = False
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal in_table
        if not table_rows:
            return
        out.append('<div class="table-scroll"><table class="tbl"><thead><tr>')
        for c in table_rows[0]:
            out.append(f"<th>{_inline(c)}</th>")
        out.append("</tr></thead><tbody>")
        for row in table_rows[2:]:  # skip the separator row at index 1
            out.append("<tr>")
            for c in row:
                out.append(f"<td>{_inline(c)}</td>")
            out.append("</tr>")
        out.append("</tbody></table></div>")
        table_rows.clear()
        in_table = False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("|") and line.endswith("|"):
            table_rows.append([c.strip() for c in line.strip("|").split("|")])
            in_table = True
            continue
        if in_table:
            flush_table()

        if not line.strip():
            close_ul()
            continue

        if line.strip() in {"---", "***"}:
            close_ul()
            out.append("<hr>")
            continue

        m_h = _HEADING_RX.match(line)
        if m_h:
            close_ul()
            level = min(len(m_h.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline(m_h.group(2))}</h{level}>")
            continue

        if _BULLET_RX.match(line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(_BULLET_RX.sub('', line))}</li>")
            continue

        close_ul()
        out.append(f"<p>{_inline(line)}</p>")

    close_ul()
    if in_table:
        flush_table()
    return "".join(out)
