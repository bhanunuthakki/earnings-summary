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

``CITE_MARKS_CSS`` keys off whichever theme the host sets, with fallback
chains across the shell vocabulary (--surface/--fg/--border/--accent) and
the report vocabulary (--panel/--ink/--hairline/--link).
"""

from __future__ import annotations

CITE_MARKS_CSS = """
.cite-wrap { position: relative; display: inline; }
.cite-wrap .cite-mark {
  color: var(--link, var(--accent)); text-decoration: none;
  font-size: 0.8em; vertical-align: super; cursor: pointer; white-space: nowrap;
}
.cite-wrap .cite-pop {
  display: none; position: absolute; z-index: 60;
  bottom: calc(100% + 4px); left: 0;
  min-width: 170px; max-width: 260px; padding: 7px 9px;
  background: var(--surface, var(--bg-elev, var(--panel)));
  border: 1px solid var(--border, var(--hairline));
  border-radius: var(--radius, 6px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  font-size: var(--fs-caption, 12px); font-weight: 400; line-height: 1.45;
  white-space: normal; text-align: left;
}
.cite-wrap:hover .cite-pop, .cite-wrap:focus-within .cite-pop { display: block; }
.cite-pop-label { display: block; font-weight: 600; color: var(--fg, var(--ink)); }
.cite-pop-meta { display: block; color: var(--muted); margin-top: 2px; }
.cite-unverified {
  display: inline-block; font-size: var(--fs-micro, 10px);
  color: var(--warn); border: 1px dashed var(--warn);
  border-radius: var(--radius-full, 999px); padding: 1px 7px; cursor: default;
}
"""

# Raw string: regexes pass through verbatim. Self-guarded global — safe to
# embed in several fragments of the same document.
CITE_MARKS_JS = r"""
(function () {
  if (window.ccCiteMarks) return;
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function popHtml(c) {
    var html = '<span class="cite-pop-label">' + esc(c.label || 'source') + '</span>';
    var meta = [];
    if (c.kind) meta.push(esc(c.kind));
    if (typeof c.confidence === 'number') {
      meta.push('confidence ' + Math.round(c.confidence * 100) + '%');
    }
    if (meta.length) html += '<span class="cite-pop-meta">' + meta.join(' &middot; ') + '</span>';
    return '<span class="cite-pop" role="tooltip">' + html + '</span>';
  }
  function linkify(html, items, opts) {
    var base = (opts && opts.hrefBase) || '';
    var map = {};
    (items || []).forEach(function (c) { if (c && c.n) map[String(c.n)] = c; });
    return String(html).replace(/\[(\d{1,2})\]/g, function (m, n) {
      var c = map[n];
      if (!c) return m;
      var href = c.href || c.source_url || '';
      if (href && !/^https?:/.test(href)) href = base + href;
      var mark = href
        ? '<a class="cite-mark" href="' + esc(href) + '" target="_blank" rel="noopener">[' + n + ']</a>'
        : '<span class="cite-mark">[' + n + ']</span>';
      return '<span class="cite-wrap" tabindex="0">' + mark + popHtml(c) + '</span>';
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

__all__ = ["CITE_MARKS_CSS", "CITE_MARKS_JS", "CITE_MARKS_SNIPPET"]
