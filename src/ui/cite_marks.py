"""Shared inline citation marks for grounded Ask answers (S8).

One anatomy for every chat surface (ask dock, Explore panel, report chat
drawer): each ``[n]`` marker in a finished narrative answer becomes a
superscript cite chip whose hover/focus popover shows the evidence item —
label, kind, the S2 scored-confidence % when the backing fact carries one —
and whose click opens the ``/source`` viewer. A trailing
"⚠ N unverified claim(s)" chip surfaces claims the grounding audit
(``ask.claims``) could not back with evidence.

Streaming contract: deltas render as plain text (the raw ``[n]`` markers
are just characters, so they survive SSE chunking); when the trailing
``citations`` event lands, the surface re-renders the finished answer
through ``window.ccCiteMarks.linkify`` with the event's items. Progressive
text first, chips at stream close — no marker is ever lost mid-stream.

The popover is phrasing content (nested ``<span>``s, CSS-only reveal on
:hover/:focus-within) — NOT a ``<details>`` like ``ui.source_chip``'s
table-cell popover, because ``<details>`` is flow content and would split
the surrounding ``<p>`` mid-sentence.

``CITE_MARKS_JS`` registers ``window.ccCiteMarks`` once (self-guarded — the
dock and the Explore panel share the shell document, and each embeds the
snippet so neither depends on the other having loaded):

    linkify(html, items, opts?)      — replace [n] markers with cite chips;
                                       opts.hrefBase prefixes relative hrefs
                                       (the file:// report drawer points at
                                       the research server)
    unverifiedChipHtml(claims)       — '' or the warn chip for claims with
                                       supported === false

``CITE_MARKS_CSS`` keys off whichever theme the host sets and consumes only
the canonical tokens (--surface/--fg/--border/--accent/--hairline/…,
src/ui/tokens.py) — no legacy report-vocabulary aliases (2026-07-18 sweep).
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import cast

CITE_MARKS_CSS = """
.cite-wrap { position: relative; display: inline; }
.cite-wrap .cite-mark {
  color: var(--accent); text-decoration: none;
  font-size: 0.8em; vertical-align: super; cursor: pointer; white-space: nowrap;
}
/* When a [n] marker sits right after a financial value, the value itself gets
   the accent-tinted "this is cited" wash and the marker becomes a small round
   badge. Accent (interactive), NOT green — green = a positive number here. */
.cite-wrap .cite-val {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  border-radius: var(--radius, 6px); padding: 0 3px;
}
.cite-wrap .cite-badge {
  font-size: var(--fs-caption); line-height: 1; vertical-align: middle;
  color: var(--accent-contrast);
  background: var(--accent); border-radius: var(--radius-full, 999px);
  padding: 1px 5px; margin-left: 3px; white-space: nowrap;
}
.cite-wrap .cite-pop {
  display: none; position: absolute; z-index: 60;
  bottom: calc(100% + 4px); left: 0;
  min-width: 170px; max-width: 260px; padding: 7px 9px;
  background: var(--surface);
  border: 1px solid var(--border, var(--hairline));
  border-radius: var(--radius, 6px);
  box-shadow: var(--shadow-pop, 0 8px 24px rgba(0, 0, 0, 0.45));
  font-size: var(--fs-caption, 12px); font-weight: 400; line-height: 1.45;
  white-space: normal; text-align: left;
}
.cite-wrap:hover .cite-pop, .cite-wrap:focus-within .cite-pop { display: block; }
.cite-pop-label { display: block; font-weight: 600; color: var(--fg); }
.cite-pop-meta { display: block; color: var(--muted); margin-top: 2px; }
.cite-pop-head {
  display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-bottom: 4px;
}
.cite-pop-tick { font-weight: 600; color: var(--fg); }
.cite-pop-kind {
  font-size: var(--fs-caption); color: var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius, 6px); padding: 1px 6px; text-transform: uppercase;
  letter-spacing: 0.03em;
}
.cite-pop-per { font-size: var(--fs-caption, 12px); color: var(--muted); }
.cite-pop-value {
  display: block; font-size: var(--fs-body, 13px); font-weight: 600;
  color: var(--fg); margin-top: 3px;
}
.cite-pop-value-cap {
  display: block; font-size: var(--fs-caption); color: var(--muted); margin-top: 1px;
}
.cite-unverified {
  display: inline-block; font-size: var(--fs-caption);
  color: var(--warn); border: 1px dashed var(--warn);
  border-radius: var(--radius-full, 999px); padding: 1px 7px; cursor: default;
}
"""

# Raw string: regexes pass through verbatim. Self-guarded global — safe to
# embed in several fragments of the same document.
CITE_MARKS_JS = r"""
(function () {
  if (window.ccCiteMarks) return;
  // A [n] marker, optionally preceded by a financial value (currency-led, or a
  // bare number that carries a %/magnitude suffix — so plain years/counts are
  // NOT highlighted), where the value may be wrapped in one inline tag. The
  // Python mirror (ui.cite_marks._CITE_RX) matches the same shape verbatim.
  var CITE_RX = /(?:((?:<(?:strong|em|code)>)?(?:[$€£]\s?\d[\d,]*(?:\.\d+)?\s?(?:%|bps|pp|x|[BMK]|bn|mn|tn|billion|million|trillion|thousand)?|\d[\d,]*(?:\.\d+)?\s?(?:%|bps|pp|x|[BMK]|bn|mn|tn|billion|million|trillion|thousand))(?:<\/(?:strong|em|code)>)?)\s*)?\[(\d{1,2})\]/g;
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function popHtml(c) {
    var head = '';
    var label;
    if (c.ticker) {
      head = '<span class="cite-pop-head"><span class="cite-pop-tick">' + esc(c.ticker) + '</span>';
      if (c.doc_type) head += '<span class="cite-pop-kind">' + esc(c.doc_type) + '</span>';
      if (c.period) head += '<span class="cite-pop-per">' + esc(c.period) + '</span>';
      head += '</span>';
      label = String(c.label || '');
      var pfx = c.ticker + ' · ';
      if (label.indexOf(pfx) === 0) label = label.slice(pfx.length);
    } else {
      label = String(c.label || 'source');
    }
    var html = head;
    if (label) html += '<span class="cite-pop-label">' + esc(label) + '</span>';
    if (c.value) {
      html += '<span class="cite-pop-value">' + esc(c.value) + '</span>'
        + '<span class="cite-pop-value-cap">latest reported</span>';
    }
    var meta = [];
    if (c.kind && !c.ticker) meta.push(esc(c.kind));
    if (typeof c.confidence === 'number') {
      meta.push('confidence ' + Math.round(c.confidence * 100) + '%');
    }
    if (meta.length) html += '<span class="cite-pop-meta">' + meta.join(' &middot; ') + '</span>';
    return '<span class="cite-pop" role="tooltip">' + html + '</span>';
  }
  function safeHref(raw, base) {
    var href = String(raw || '').trim();
    if (!href || /[\u0000-\u001f\u007f]/.test(href)) return '';
    if (/^https?:\/\//i.test(href)) {
      try {
        var absolute = new URL(href);
        if ((absolute.protocol === 'http:' || absolute.protocol === 'https:')
            && absolute.hostname && !absolute.username && !absolute.password) return href;
      } catch (_err) {}
      return '';
    }
    if (href.charAt(0) !== '/' || href.charAt(1) === '/' || href.indexOf('\\') !== -1) return '';
    if (!base) return href;
    var safeBase = safeHref(base, '');
    return safeBase ? safeBase.replace(/\/$/, '') + href : '';
  }
  function linkify(html, items, opts) {
    var base = (opts && opts.hrefBase) || '';
    var map = {};
    (items || []).forEach(function (c) { if (c && c.n) map[String(c.n)] = c; });
    return String(html).replace(CITE_RX, function (m, value, n) {
      var c = map[n];
      if (!c) return m;
      var href = safeHref(c.href || c.source_url || '', base);
      var pop = popHtml(c);
      if (value) {
        var badge = href
          ? '<a class="cite-mark cite-badge" href="' + esc(href) + '" target="_blank" rel="noopener">' + n + '</a>'
          : '<span class="cite-mark cite-badge">' + n + '</span>';
        return '<span class="cite-wrap" tabindex="0"><span class="cite-val">' + value + '</span>'
          + badge + pop + '</span>';
      }
      var mark = href
        ? '<a class="cite-mark" href="' + esc(href) + '" target="_blank" rel="noopener">[' + n + ']</a>'
        : '<span class="cite-mark">[' + n + ']</span>';
      return '<span class="cite-wrap" tabindex="0">' + mark + pop + '</span>';
    });
  }
  function unverifiedChipHtml(claims) {
    var bad = (claims || []).filter(function (c) { return c && c.supported === false; });
    if (!bad.length) return '';
    var titles = bad.map(function (c) { return c.text || ''; }).filter(Boolean).join('\n');
    return '<span class="cite-unverified" title="' + esc(titles) + '">&#9888; '
      + bad.length + ' unverified claim' + (bad.length === 1 ? '' : 's') + '</span>';
  }
  window.ccCiteMarks = { linkify: linkify, unverifiedChipHtml: unverifiedChipHtml };
  // Escape-only dismissal (Law 3 / design_language §3.1): a cite popover is
  // phrasing content revealed on :focus-within — NOT a modal, so it must not
  // gain a scrim or focus trap. Register a CCOverlay dismisser that blurs the
  // focused .cite-wrap; the :hover variant just leaves on mouseout. Runs once
  // per document (the ccCiteMarks guard above), and only when CCOverlay is
  // present (e.g. the shell + the report iframe).
  if (window.CCOverlay) {
    window.CCOverlay.addPopoverDismisser(function () {
      var ae = document.activeElement;
      if (ae && ae.closest && ae.closest('.cite-wrap')) { ae.blur(); return true; }
      return false;
    });
  }
})();
"""

# The drop-in fragment for surfaces that assemble HTML strings.
CITE_MARKS_SNIPPET = f"<style>{CITE_MARKS_CSS}</style>\n<script>{CITE_MARKS_JS}</script>"


# ---------------------------------------------------------------------------
# Server-side linkify — the exact mirror of ``window.ccCiteMarks.linkify``.
#
# The Ask surfaces stream tokens and run the JS ``linkify`` client-side at
# stream close (see the streaming contract above). The STATIC LLM prose — a
# thesis memo, a bear case, a synthesis lens — is server-rendered once through
# ``ui.prose.render_prose`` with no stream and no JS pass, so it needs a
# Python ``linkify`` that produces byte-identical chip anatomy
# (``.cite-wrap`` / ``.cite-mark`` / ``.cite-pop``) keyed off the same
# ``CITE_MARKS_CSS``. The popover is CSS-only (``:hover`` / ``:focus-within``),
# so a server-rendered chip needs the stylesheet but NOT ``CITE_MARKS_JS``.
#
# Items are the same chip payloads ``ask.grounding.EvidenceItem.chip_payload``
# emits — ``{n, label, kind, confidence, href, source_url}`` — so a static
# renderer can hand ``render_prose`` exactly what the Ask citations event
# carries. A ``citations`` payload dict (the ``ask.claims.build_citations_payload``
# shape, ``{"items": [...], ...}``) is accepted too: its ``items`` are used.
# ---------------------------------------------------------------------------

# Mirrors the JS ``CITE_RX`` verbatim: a 1-2 digit ``[n]`` marker, optionally
# preceded (capture group 1) by a financial value the marker cites — currency-
# led, or a bare number carrying a %/magnitude suffix, so plain years/counts are
# left alone — optionally wrapped in one inline tag (``<strong>``/``<em>``/
# ``<code>``, what ``ui.prose`` emits). When the value is present it is washed
# with the accent tint and the marker becomes a round badge; otherwise the old
# superscript ``[n]`` chip renders unchanged.
_VALUE_CORE = (
    r"(?:<(?:strong|em|code)>)?"
    r"(?:[$€£]\s?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:%|bps|pp|x|[BMK]|bn|mn|tn|billion|million|trillion|thousand)?"
    r"|\d[\d,]*(?:\.\d+)?\s?(?:%|bps|pp|x|[BMK]|bn|mn|tn|billion|million|trillion|thousand))"
    r"(?:</(?:strong|em|code)>)?"
)
_CITE_RX = re.compile(r"(?:(" + _VALUE_CORE + r")\s*)?\[(\d{1,2})\]")

# A single citation chip payload (EvidenceItem.chip_payload's shape) and the
# two forms ``linkify`` / ``render_prose`` accept: a bare list of items, or the
# full citations-event dict that carries them under ``items``.
CitationItem = Mapping[str, object]
CitationsPayload = Mapping[str, object] | Sequence[CitationItem]


def _esc(value: object) -> str:
    """HTML-escape a chip field (mirror of the JS ``esc``)."""
    return html.escape("" if value is None else str(value), quote=True)


def _safe_href(raw: object, href_base: str) -> str:
    href = str(raw or "").strip()
    if not href or any(ord(char) < 32 or ord(char) == 127 for char in href):
        return ""
    try:
        parsed = urllib.parse.urlsplit(href)
    except ValueError:
        return ""
    if parsed.scheme:
        if (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return href
        return ""
    if parsed.netloc or not href.startswith("/") or href.startswith("//") or "\\" in href:
        return ""
    if not href_base:
        return href
    base = _safe_href(href_base, "")
    return urllib.parse.urljoin(base, href) if base else ""


def citation_items(payload: CitationsPayload | None) -> list[CitationItem]:
    """Normalize either accepted payload form to the bare list of item dicts.

    Accepts the full citations-event dict (``{"items": [...], "claims": ...}``)
    or a bare list of chip payloads; returns ``[]`` for anything unusable so
    callers never have to branch on the shape.
    """
    if payload is None:
        return []
    raw: object = payload.get("items") if isinstance(payload, Mapping) else payload
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return []
    seq = cast("Sequence[object]", raw)
    return [cast("CitationItem", c) for c in seq if isinstance(c, Mapping)]


def _cite_pop_html(item: CitationItem) -> str:
    """The hover/focus popover for one chip — mirror of the JS ``popHtml``.

    When the chip carries the structured ``ticker`` field it renders the
    auditable header (issuer · doc-type pill · period) above the metric label,
    and — for a fact chip — the newest data point's ``value`` flagged "latest
    reported". A chip without ``ticker`` (transcripts, packs, legacy payloads)
    renders the original label-first layout byte-for-byte unchanged."""
    ticker = item.get("ticker")
    head = ""
    if ticker:
        ticker_s = str(ticker)
        head = f'<span class="cite-pop-head"><span class="cite-pop-tick">{_esc(ticker_s)}</span>'
        doc_type = item.get("doc_type")
        if doc_type:
            head += f'<span class="cite-pop-kind">{_esc(doc_type)}</span>'
        period = item.get("period")
        if period:
            head += f'<span class="cite-pop-per">{_esc(period)}</span>'
        head += "</span>"
        label = str(item.get("label") or "")
        pfx = f"{ticker_s} · "
        if label.startswith(pfx):
            label = label[len(pfx) :]
    else:
        label = str(item.get("label") or "source")
    out = head
    if label:
        out += f'<span class="cite-pop-label">{_esc(label)}</span>'
    value = item.get("value")
    if value:
        out += (
            f'<span class="cite-pop-value">{_esc(value)}</span>'
            '<span class="cite-pop-value-cap">latest reported</span>'
        )
    meta: list[str] = []
    kind = item.get("kind")
    if kind and not ticker:
        meta.append(_esc(kind))
    conf = item.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        meta.append(f"confidence {round(conf * 100)}%")
    if meta:
        out += f'<span class="cite-pop-meta">{" &middot; ".join(meta)}</span>'
    return f'<span class="cite-pop" role="tooltip">{out}</span>'


def linkify(text: str, payload: CitationsPayload | None, *, href_base: str = "") -> str:
    """Replace each ``[n]`` marker in already-rendered HTML with a cite chip.

    The byte-for-byte server mirror of ``window.ccCiteMarks.linkify``: a marker
    whose ``n`` matches an item becomes a ``.cite-wrap`` superscript whose
    popover shows the evidence label / kind / confidence; an unmatched marker
    is left as the literal ``[n]`` text. ``href_base`` prefixes a relative href
    (so a file://-opened report's ``/source/<doc_id>`` links resolve against the
    research server, exactly like the JS ``opts.hrefBase``); an absolute
    ``http(s)`` href is used as-is.

    Single left-to-right pass: ``re.sub`` never rescans the chip HTML it just
    emitted, so a ``[n]`` literal inside a generated chip is never re-matched.
    """
    items = citation_items(payload)
    if not items:
        return text
    by_n: dict[str, CitationItem] = {}
    for c in items:
        n = c.get("n")
        if n is not None:
            by_n[str(n)] = c
    if not by_n:
        return text

    def _repl(m: re.Match[str]) -> str:
        value = m.group(1)
        n = m.group(2)
        c = by_n.get(n)
        if c is None:
            return m.group(0)
        href = _safe_href(c.get("href") or c.get("source_url"), href_base)
        pop = _cite_pop_html(c)
        if value:
            # value is already-rendered, already-escaped prose HTML — wrap as-is.
            badge = (
                f'<a class="cite-mark cite-badge" href="{_esc(href)}" '
                f'target="_blank" rel="noopener">{n}</a>'
                if href
                else f'<span class="cite-mark cite-badge">{n}</span>'
            )
            inner = f'<span class="cite-val">{value}</span>{badge}'
        else:
            inner = (
                f'<a class="cite-mark" href="{_esc(href)}" target="_blank" rel="noopener">[{n}]</a>'
                if href
                else f'<span class="cite-mark">[{n}]</span>'
            )
        return f'<span class="cite-wrap" tabindex="0">{inner}{pop}</span>'

    return _CITE_RX.sub(_repl, text)


__all__ = [
    "CITE_MARKS_CSS",
    "CITE_MARKS_JS",
    "CITE_MARKS_SNIPPET",
    "CitationItem",
    "CitationsPayload",
    "citation_items",
    "linkify",
]
