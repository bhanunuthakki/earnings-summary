"""Shared control kit — the one way to draw inputs, buttons, chips, tickers.

Why this exists (UI polish v3, 2026-06-11): the surfaces grew ~10 independent
``select``/``input`` stylings (every one keeping the native OS dropdown look),
six button treatments (solid accent / accent-soft / outline / ghost / link /
unstyled), a chip zoo spanning five corner radii and four font sizes, and
"ticker + company name" rendered as one undifferentiated string in pickers,
headers, and palette rows. This module is the component layer over
:mod:`ui.tokens`: tokens own the values, this owns the controls.

Composition contract — ``controls_css(default)`` rides immediately after
:func:`ui.tokens.palette_css` in every page-level ``<style>``:

* element BASELINE for form controls (select/input/textarea): dark-menu
  ``color-scheme``, ``appearance: none`` + a custom chevron on single
  ``<select>`` (the native arrow is gone everywhere), one focus ring.
  Low-specificity on purpose: any surface rule still wins — but surfaces
  should only add layout (widths, flex), never re-skin.
  CAUTION: a surface that sets ``background: <anything>`` on a ``<select>``
  wipes the chevron (shorthand resets ``background-image``) and gets an
  arrowless box — set ``background-color`` instead, or nothing at all.
* ``.k-btn`` + ``.k-btn-primary`` / ``.k-btn-quiet`` / ``.k-btn-danger`` —
  the WHOLE button hierarchy. One solid-accent primary per view, quiet for
  everything else, danger only for destructive actions.
* ``.k-chip`` (+ tone modifiers, + ``.k-chip-btn`` for clickable filters) —
  the one badge/chip shape: radius-full, micro type, uppercase.
* ``.k-tick`` — the canonical ticker label: mono ticker + regular-weight
  muted company name, ellipsis-truncated with the full name in ``title``.
  Use :func:`ticker_label`; never concatenate ``f"{ticker} · {name}"`` again.
* ``.k-menu`` — the shared popover-list surface (combobox results, palette
  rows): one elevation (``--shadow-pop``), one hover treatment.
* ``.k-label`` — the uppercase field/section caption.
* ``.k-pill`` (+ ``-ok/-warn/-bad/-accent``) — the one FILLED status/score
  badge: a soft ``color-mix`` status fill + token ink. THE replacement for the
  per-panel raw-hex pill systems (``.ev-score-*``, ``.badge.b-*``, the
  calib/cockpit/memo tone pairs); never freehand a bg/fg hex pair again.
  (``.k-chip`` is the *outline* tag/filter chip; ``.k-pill`` is the *filled*
  status pill; ``.p-pill`` stays the neutral pipeline-table pill.)
* ``.k-well`` (+ ``-ok/-warn/-bad/-accent``) — the soft-filled BLOCK sibling of
  ``.k-pill`` for KPI cards / callouts / tone rows: same ``color-mix`` family,
  box radius.
* ``.k-scrim`` + ``.k-overlay`` — the one transient-surface primitive (Law 3):
  a neutral scrim + an elevated, radiused, motion-on-open panel. ``CCOverlay``
  (S4) wires dismissal (close + Esc + scrim click-out + focus trap) on top; kit
  owns the look + open motion.
* ``.k-toolbar`` + :func:`panel_toolbar` — the ONE operating band a panel gets
  (design_language §6.1): title left, filters + actions on the same flex row.
  :func:`panel_section_title` emits (or, when the nav owns the title, suppresses)
  the panel's ``<h2>`` so an in-shell single-sub-tab panel never re-prints its
  section name.

Like ``palette_css``, the output contains literal CSS braces: surfaces that
splice it into ``str.format`` templates must brace-double it the same way
they already do for the palette block.
"""

from __future__ import annotations

# Chevron for single-selects, URL-encoded (no literal braces/quotes/#: the
# string survives both raw f-string assembly and brace-doubled .format
# templates). Stroke matches each theme's --muted.
_CHEVRON_DARK = (
    "url('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 "
    "viewBox=%220 0 16 16%22%3E%3Cpath d=%22M4 6l4 4 4-4%22 stroke=%22%23888b94%22 "
    "stroke-width=%221.6%22 fill=%22none%22 stroke-linecap=%22round%22 "
    "stroke-linejoin=%22round%22/%3E%3C/svg%3E')"
)
_CHEVRON_LIGHT = _CHEVRON_DARK.replace("%23888b94", "%236c6f78")

# The element baseline + the component classes. Theme-independent: the two
# theme-dependent declarations (color-scheme, chevron ink) are prepended by
# controls_css().
_CONTROLS_BODY = """
/* ---- form-control baseline (kit): kill the native look everywhere ---- */
select, textarea, input[type="text"], input[type="search"], input[type="number"],
input[type="date"], input[type="email"], input[type="url"], input[type="password"],
input:not([type]) {
  font: inherit; font-size: var(--fs-body); color: var(--fg);
  background-color: var(--paper); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 6px 10px;
  transition: border-color var(--transition), box-shadow var(--transition);
}
select:not([multiple]) {
  appearance: none; -webkit-appearance: none; cursor: pointer;
  background-image: var(--k-chevron); background-repeat: no-repeat;
  background-position: right 9px center; background-size: 14px;
  padding-right: 28px;
}
select[multiple] { padding: 4px; }
select[multiple] option { padding: 3px 8px; border-radius: var(--radius); }
select:focus-visible, textarea:focus-visible, input:focus-visible {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
input[type="checkbox"], input[type="radio"] { accent-color: var(--accent); }
::placeholder { color: var(--muted-2); opacity: 1; }

/* ---- button hierarchy: primary (one per view) / quiet / danger ---- */
.k-btn {
  display: inline-flex; align-items: center; gap: 6px;
  font: inherit; font-size: var(--fs-body); font-weight: 600; line-height: 1.3;
  border-radius: var(--radius); padding: 6px 14px; cursor: pointer;
  border: 1px solid transparent; background: transparent; color: var(--fg);
  white-space: nowrap;
  transition: color var(--transition), border-color var(--transition),
    background var(--transition), filter var(--transition);
}
.k-btn-primary { background: var(--accent); color: var(--accent-contrast); }
.k-btn-primary:hover { filter: brightness(1.08); }
.k-btn-quiet { border-color: var(--border); color: var(--muted); }
.k-btn-quiet:hover { color: var(--fg); border-color: var(--border-2); }
.k-btn-danger { border-color: transparent; color: var(--bad); }
.k-btn-danger:hover { border-color: var(--bad); }
.k-btn[disabled] { opacity: 0.5; cursor: default; pointer-events: none; }
.k-btn-sm { font-size: var(--fs-caption); padding: 3px 9px; }

/* ---- chips: ONE badge shape (radius-full · micro · uppercase) ---- */
.k-chip {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: var(--fs-micro); font-weight: 600; line-height: 1.5;
  text-transform: uppercase; letter-spacing: 0.05em;
  border-radius: var(--radius-full); padding: 1px 8px;
  border: 1px solid var(--border); color: var(--muted); background: transparent;
  white-space: nowrap;
}
.k-chip-ok   { color: var(--ok);   border-color: color-mix(in srgb, var(--ok) 45%, transparent); }
.k-chip-warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, transparent); }
.k-chip-bad  { color: var(--bad);  border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
.k-chip-accent { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.k-chip-mono { font-family: var(--mono); text-transform: none; letter-spacing: 0.02em; }
button.k-chip, .k-chip-btn { cursor: pointer; font: inherit; font-size: var(--fs-micro);
  font-weight: 600; transition: color var(--transition), border-color var(--transition); }
button.k-chip:hover, .k-chip-btn:hover { color: var(--fg); border-color: var(--border-2); }
button.k-chip.is-on, .k-chip-btn.is-on { color: var(--accent); border-color: var(--accent); }

/* ---- the canonical ticker label: mono symbol + muted truncated name ---- */
.k-tick { display: inline-flex; align-items: baseline; gap: 7px; min-width: 0;
  max-width: 100%; }
.k-tick-sym { font-family: var(--mono); font-weight: 600; letter-spacing: 0.02em;
  color: var(--fg); text-decoration: none; }
a.k-tick-sym:hover { color: var(--accent); }
.k-tick-name { font-family: var(--sans); font-weight: 400; font-size: var(--fs-caption);
  color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: var(--k-tick-max, 20ch); }

/* ---- shared popover list (combobox results, palette rows) ---- */
.k-menu { margin: 0; padding: 4px 0; list-style: none;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-pop); }
.k-menu li { padding: 6px 12px; cursor: pointer; font-size: var(--fs-body); }
.k-menu li.sel, .k-menu li:hover { background: var(--paper); }

/* ---- field/section caption ---- */
.k-label { font-size: var(--fs-caption); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em; }

/* ---- mobile: 16px floor prevents iOS from zooming on input focus ---- */
@media (max-width: 768px) {
  input, select, textarea { font-size: 16px; }
}

/* ---- pipeline panel table (canonical layout for command-center tabs) ---- */
.p-table { width: 100%; border-collapse: collapse; font-size: var(--fs-body); }
.p-table th, .p-table td {
  padding: 6px 10px; border-bottom: 1px solid var(--border); text-align: left;
  vertical-align: top;
}
.p-table td.num, .p-table th.num {
  text-align: right; font-variant-numeric: tabular-nums;
}

/* ---- pipeline panel pill (inline semantic badge; no color — variants add it) ---- */
.p-pill {
  display: inline-block; padding: 1px 8px; border-radius: var(--radius-full);
  font-size: var(--fs-caption); font-weight: 600; white-space: nowrap;
}

/* ---- status/score pills: ONE filled badge — a soft status fill + token ink
   (design_language §3). This is the canonical replacement for the per-panel
   raw-hex pill systems (.ev-score-*, .badge.b-*, the calib/cockpit/memo tone
   pairs): a status pill never freehands a background/foreground hex pair. ---- */
.k-pill {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 1px 9px; border-radius: var(--radius-full);
  font-size: var(--fs-caption); font-weight: 600; line-height: 1.5;
  white-space: nowrap; font-variant-numeric: tabular-nums;
  background: var(--paper); color: var(--fg-soft);
}
.k-pill-ok     { background: color-mix(in srgb, var(--ok) 16%, transparent);     color: var(--ok); }
.k-pill-warn   { background: color-mix(in srgb, var(--warn) 16%, transparent);   color: var(--warn); }
.k-pill-bad    { background: color-mix(in srgb, var(--bad) 16%, transparent);    color: var(--bad); }
.k-pill-accent { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }

/* ---- status wells: the soft-filled BLOCK sibling of .k-pill (KPI cards,
   callouts, tone rows) — same color-mix family, box radius instead of full.
   Ink stays --fg; a status word inside can take its own .k-pill. ---- */
.k-well { background: var(--paper); border-radius: var(--radius); padding: var(--sp-3) var(--sp-4); }
.k-well-ok     { background: color-mix(in srgb, var(--ok) 16%, transparent); }
.k-well-warn   { background: color-mix(in srgb, var(--warn) 16%, transparent); }
.k-well-bad    { background: color-mix(in srgb, var(--bad) 16%, transparent); }
.k-well-accent { background: color-mix(in srgb, var(--accent) 16%, transparent); }

/* ---- overlay primitive (Law 3): one scrim + one elevated panel. S4's
   CCOverlay JS registers/dismisses these (close + Esc + scrim click-out + focus
   trap/restore); the kit owns their look + open motion. Exit is instant — the
   [hidden] toggle can't animate display:none. The scrim is a neutral wash, not
   a palette color. ---- */
.k-scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5);
  z-index: 40; animation: k-overlay-fade var(--transition); }
.k-overlay { position: fixed; z-index: 41; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow-pop); animation: k-overlay-rise var(--transition); }
.k-scrim[hidden], .k-overlay[hidden] { display: none; }
@keyframes k-overlay-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes k-overlay-rise { from { transform: translateY(6px); opacity: 0; }
  to { transform: none; opacity: 1; } }
@media (prefers-reduced-motion: reduce) {
  .k-scrim, .k-overlay { animation-duration: 0.01ms; }
}

/* ---- panel toolbar: the ONE operating band (design_language §6.1). Title on
   the left, filters + actions on the SAME flex row to the right — never a
   title band stacked over a filter band. Emitted by panel_toolbar(). ---- */
.k-toolbar { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap;
  margin-bottom: var(--sp-3); }
.k-toolbar-title { font-size: var(--fs-title); font-weight: 600; margin: 0;
  margin-right: auto; }
.k-toolbar-controls { display: flex; align-items: center; gap: var(--sp-2);
  flex-wrap: wrap; margin-left: auto; }
"""


def controls_css(default: str = "paper") -> str:
    """The control kit as a CSS block; compose right after ``palette_css``.

    Mirrors ``palette_css``'s two modes: ``"dark"`` pins a dark
    ``color-scheme`` (native select menus, date pickers, and scrollbars render
    dark) and the dark chevron; ``"paper"`` emits light-scheme defaults plus
    the ``[data-theme="dark"]`` overrides for theme-switching surfaces.
    """
    if default == "dark":
        head = ":root { color-scheme: dark; --k-chevron: " + _CHEVRON_DARK + "; }\n"
    elif default == "paper":
        head = (
            ":root { color-scheme: light; --k-chevron: " + _CHEVRON_LIGHT + "; }\n"
            ':root[data-theme="dark"] { color-scheme: dark; --k-chevron: ' + _CHEVRON_DARK + "; }\n"
        )
    else:
        raise ValueError(f"default must be 'paper' or 'dark', got {default!r}")
    return head + _CONTROLS_BODY


def ticker_label(
    ticker: str,
    name: str | None = None,
    *,
    href: str | None = None,
    name_max: str | None = None,
    classes: str = "",
) -> str:
    """The canonical ticker + company-name label (escaped HTML).

    Mono ticker symbol, regular-weight muted company name beside it,
    ellipsis-truncated at ``--k-tick-max`` (default 20ch, override per call
    via ``name_max``); the FULL name always rides in ``title``. ``href``
    links the symbol only — the name stays plain text so long names never
    become long links. Replaces every ``f"{ticker} · {name}"`` /
    ``f"{ticker} — {name}"`` concatenation.
    """
    from html import escape

    t = escape(ticker.upper())
    cls = f"k-tick {classes}".strip()
    title = f' title="{escape(name, quote=True)}"' if name else ""
    style = f' style="--k-tick-max:{escape(name_max, quote=True)}"' if name_max else ""
    sym = (
        f'<a class="k-tick-sym" href="{escape(href, quote=True)}">{t}</a>'
        if href
        else f'<span class="k-tick-sym">{t}</span>'
    )
    nm = f'<span class="k-tick-name">{escape(name)}</span>' if name else ""
    return f'<span class="{cls}"{title}{style}>{sym}{nm}</span>'


def panel_section_title(title: str, *, suppressed: bool = False) -> str:
    """The panel's own ``<h2>`` title — or ``""`` when the nav already owns it.

    A panel sitting under an already-labeled tab must not re-print its section
    name (design_language §6.1). The shell passes ``suppressed=True`` for a
    single-sub-tab section (where the tab label IS the title) and the heading
    collapses. Used standalone or composed by :func:`panel_toolbar`.
    """
    if suppressed or not title.strip():
        return ""
    from html import escape

    return f'<h2 class="k-toolbar-title">{escape(title)}</h2>'


def panel_toolbar(
    title: str = "",
    *,
    filters: str = "",
    actions: str = "",
    suppress_title: bool = False,
) -> str:
    """The one operating band a panel gets before its content (design_language
    §6.1): the title on the left, ``filters`` + ``actions`` on the SAME flex
    row to the right. Never stack a title band over a filter band.

    ``filters`` / ``actions`` are pre-rendered HTML fragments (``.k-chip``
    filters, ``.k-btn`` actions, selects). ``suppress_title`` drops the heading
    when the nav owns it (single-sub-tab in-shell panels); the controls then
    left-align into the freed row. Returns ``""`` when there is nothing to draw.
    """
    head = panel_section_title(title, suppressed=suppress_title)
    controls = (
        f'<div class="k-toolbar-controls">{filters}{actions}</div>' if (filters or actions) else ""
    )
    if not head and not controls:
        return ""
    return f'<div class="k-toolbar">{head}{controls}</div>'
