"""Living grid: a no-build, progressive-enhancement layer that turns a
server-rendered ``<table>`` into a client-sortable / -filterable grid via Alpine.

Rows arrive fully rendered (and pre-sorted) from the server; the client only
re-orders the existing ``<tr>`` nodes and toggles a hide class — so the table
works with JS off and the server HTML stays the single source of truth (no row
HTML is rebuilt in JS).

Load model: :func:`head_assets` inlines Alpine + the ``livingGrid`` factory +
the grid CSS ONCE per document (the command-center shell head; reused by the
offline report head). It is self-contained — no asset fetches — so it survives
``file://``. Panels emit only markup: wrap a table in :func:`grid_open` /
:func:`grid_close` with a :func:`filter_bar` above it, build sortable headers
with :func:`th`, and tag each row with :func:`data_text` / :func:`data_text_key`
/ :func:`data_num`. Token-only CSS keeps it design-guard clean.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

# Vendored once (src/ui/vendor/); inlined raw — verified free of "</script>" so
# no escaping is needed. ~44KB, negligible for a single-user localhost shell.
_ALPINE_JS = (Path(__file__).resolve().parent / "vendor" / "alpinejs-3.14.1.min.js").read_text(
    encoding="utf-8"
)

# Generic, token-only. Keyed off the ``.lg-*`` hooks (not a specific table
# class) so every converted table styles identically. NO x-cloak on the grid
# root: the table must render WITHOUT JS (progressive enhancement), so it is
# never hidden pending Alpine.
LIVING_GRID_CSS = """<style>
.lg { min-width: 0; max-width: 100%; overflow-x: auto; }
.lg-bar { display: flex; align-items: center; gap: 10px; margin: 0 0 10px; flex-wrap: wrap; }
.lg-filter { padding: 5px 9px; font-size: var(--fs-caption); font-family: var(--sans);
  background: var(--paper); color: var(--fg); border: 1px solid var(--border);
  border-radius: var(--radius); min-width: 210px; }
.lg-filter:focus { outline: none; border-color: var(--accent); }
.lg-count { font-size: var(--fs-caption); }
th.lg-sortable { cursor: pointer; user-select: none; transition: color var(--transition); }
th.lg-sortable:hover, th.lg-sortable:focus { color: var(--fg); outline: none; }
th.lg-sortable.num { text-align: right; }
.lg-ind { font-family: var(--mono); color: var(--accent); }
.lg-hide { display: none; }
</style>"""

# The Alpine component factory (plain JS; braces are literal). Reorders the
# server's <tr> nodes in place and filters by each row's data-text; any <tfoot>
# totals live outside <tbody> so they never move. First click on a numeric
# column sorts descending (largest first); text columns ascending. Defined once
# on window (idempotent via ||) so re-injecting a fragment can't redefine it.
_LIVING_GRID_JS = r"""
window.livingGrid = window.livingGrid || function () {
  return {
    q: '', sortKey: '', sortType: 'num', sortDir: -1, total: 0, shown: 0, _rows: [],
    init() {
      var body = this.$root.querySelector('tbody');
      this._rows = Array.prototype.slice.call(body ? body.querySelectorAll('tr') : []);
      this.total = this._rows.length;
      this.shown = this.total;
    },
    arrow: function (key) {
      if (this.sortKey !== key) return '';
      return this.sortDir < 0 ? ' ▾' : ' ▴';
    },
    ariaSort: function (key) {
      if (this.sortKey !== key) return 'none';
      return this.sortDir < 0 ? 'descending' : 'ascending';
    },
    sortBy: function (key, type) {
      if (this.sortKey === key) {
        this.sortDir = -this.sortDir;
      } else {
        this.sortKey = key;
        this.sortType = type || 'num';
        this.sortDir = (type === 'text') ? 1 : -1;
      }
      this.render();
    },
    render: function () {
      var q = this.q.trim().toLowerCase();
      var shown = 0;
      this._rows.forEach(function (tr) {
        var hit = !q || (tr.getAttribute('data-text') || '').indexOf(q) !== -1;
        tr.classList.toggle('lg-hide', !hit);
        if (hit) shown++;
      });
      this.shown = shown;
      if (!this.sortKey) return;
      var key = this.sortKey, type = this.sortType, dir = this.sortDir;
      var body = this.$root.querySelector('tbody');
      if (!body) return;
      this._rows.slice().sort(function (a, b) {
        var av = a.getAttribute('data-' + key);
        var bv = b.getAttribute('data-' + key);
        if (type === 'text') {
          av = (av || '').toLowerCase(); bv = (bv || '').toLowerCase();
          return av < bv ? -dir : (av > bv ? dir : 0);
        }
        var am = (av === null || av === ''), bm = (bv === null || bv === '');
        if (am && bm) return 0;
        if (am) return 1;
        if (bm) return -1;
        return (parseFloat(av) - parseFloat(bv)) * dir;
      }).forEach(function (tr) { body.appendChild(tr); });
    },
  };
};
"""


def head_assets() -> str:
    """Inline the grid assets once without blocking body parsing.

    The factory is available before Alpine starts. Alpine itself is a module
    script, so its microtask-based boot sees the parsed ``<body>`` in both the
    live shell and an offline ``file://`` report. Everything remains inline:
    the report has no server from which to fetch an asset.
    """
    return (
        f"<script>{_LIVING_GRID_JS}</script>"
        f'<script type="module">{_ALPINE_JS}</script>'
        f"{LIVING_GRID_CSS}"
    )


def grid_open() -> str:
    """Open the Alpine grid wrapper. Place :func:`filter_bar` then the
    ``<table>`` inside, then :func:`grid_close`."""
    return '<div class="lg" x-data="livingGrid()">'


def grid_close() -> str:
    """Close the grid wrapper opened by :func:`grid_open`."""
    return "</div>"


def filter_bar(count: int, *, noun: str = "rows", placeholder: str | None = None) -> str:
    """The filter input + live ``<shown> of <total> <noun>`` readout. The counts
    carry a server-rendered fallback (``count``) so they read correctly before
    Alpine boots and when JS is off."""
    ph = placeholder or f"Filter {noun}…"
    return (
        '<div class="lg-bar">'
        f'<input type="search" class="lg-filter" placeholder="{escape(ph)}" '
        'x-model="q" @input.debounce.150ms="render()" '
        f'aria-label="{escape(ph)}">'
        f'<span class="lg-count muted"><span x-text="shown">{count}</span> of '
        f'<span x-text="total">{count}</span> {escape(noun)}</span>'
        "</div>"
    )


def th(text: str, key: str, sort_type: str, *, num: bool = True) -> str:
    """A sortable column header: clickable + keyboard-operable + aria-sort, with
    a live indicator bound to the grid's sort. ``key`` matches the rows'
    ``data-<key>`` attribute; ``sort_type`` is ``'num'`` or ``'text'``."""
    cls = "lg-sortable" + (" num" if num else "")
    return (
        f'<th class="{cls}" role="button" tabindex="0" aria-sort="none" '
        f"@click=\"sortBy('{key}','{sort_type}')\" "
        f"@keydown.enter.prevent=\"sortBy('{key}','{sort_type}')\" "
        f":aria-sort=\"ariaSort('{key}')\">"
        f'{text}<span class="lg-ind" x-text="arrow(\'{key}\')"></span></th>'
    )


def data_text(s: str) -> str:
    """The ``data-text`` the filter box matches against (lower-cased)."""
    return f' data-text="{escape(s.lower())}"'


def data_text_key(key: str, value: str | None) -> str:
    """A text sort key (e.g. ticker / name), lower-cased for case-insensitive
    ordering."""
    return f' data-{key}="{escape((value or "").lower())}"'


def data_num(key: str, value: float | None) -> str:
    """A numeric sort key; omitted when absent so missing values sort last."""
    return "" if value is None else f' data-{key}="{value:.6f}"'
