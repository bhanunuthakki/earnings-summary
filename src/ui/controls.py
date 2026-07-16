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
* ``.prose`` (+ its ``h3``-``h6`` / ``p`` / ``ul`` / ``li`` / ``strong`` /
  ``em`` / ``code`` / ``hr`` / ``table`` descendants) — the shared container for
  :func:`ui.prose.render_prose` output (design_language §9): ONE token-only
  treatment for every stored analyst/LLM body / note / memo. Headings start at
  ``<h3>`` (the panel owns the ``<h2>`` above); a surface adds only the
  wrapper's padding/width and never re-skins the rendered children.
* ``.k-prov*`` + :func:`prov_row` / :func:`prov_drawer` / :func:`prov_case` /
  :func:`prov_severity_tick` / :func:`prov_action` — the ONE render boundary for
  any data-quality row (design_language §10; Law "provenance is actionable"):
  a severity tick + relative stamp + drill-down + ≥1 inline action. Severity
  tone resolves through the LIVE :class:`models.validation.Severity` enum
  (:func:`prov_severity_tone`), so the report Sources tab, the System Validation
  panel and the freshness peek can't drift into the shipped bug where a ``halt``
  rendered muted grey. ``test_provenance_severity_contract`` guards the map.

Like ``palette_css``, the output contains literal CSS braces: surfaces that
splice it into ``str.format`` templates must brace-double it the same way
they already do for the palette block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from models.validation import Severity

if TYPE_CHECKING:
    from datetime import datetime

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
.k-label { font-size: var(--fs-micro); font-weight: 600; color: var(--muted);
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
.k-scrim { position: fixed; inset: 0; background: var(--scrim);
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

/* ---- rendered prose: the shared container for render_prose() output
   (design_language §9; src/ui/prose.py). ONE token-only treatment for every
   stored analyst/LLM body / note / memo / narrative. render_prose starts
   headings at <h3> (a panel owns the <h2> above its prose), so h3 is the top
   content tier and h4-h6 step down. A surface adds ONLY the wrapper's
   padding/width — it never re-skins the rendered children. ---- */
.prose { font-size: var(--fs-body); line-height: 1.6; color: var(--fg-soft); }
.prose > :first-child { margin-top: 0; }
.prose > :last-child { margin-bottom: 0; }
.prose p { margin: 0 0 var(--sp-3); }
.prose h3 { font-size: var(--fs-section); font-weight: 600; color: var(--fg);
  margin: var(--sp-4) 0 var(--sp-2); }
.prose h4, .prose h5, .prose h6 { font-size: var(--fs-body); font-weight: 600;
  color: var(--fg); margin: var(--sp-3) 0 var(--sp-1); }
.prose ul, .prose ol { margin: 0 0 var(--sp-3); padding-left: var(--sp-5); }
.prose li { margin-bottom: var(--sp-1); }
.prose strong { font-weight: 600; color: var(--fg); }
.prose em { font-style: italic; }
.prose code { font-family: var(--mono); font-size: 0.93em;
  background: var(--paper); border-radius: var(--radius); padding: 0.05em 0.35em; }
.prose hr { border: none; border-top: 1px solid var(--border); margin: var(--sp-4) 0; }
.prose table { width: 100%; border-collapse: collapse; font-size: var(--fs-body);
  margin: 0 0 var(--sp-3); }
.prose th, .prose td { padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
.prose th { font-size: var(--fs-caption); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.04em; }

/* ---- provenance / data-quality rows (design_language §10; Law: provenance is
   actionable). prov_row()/prov_drawer()/prov_case() are the ONE render boundary
   for any data-quality row — the report Sources tab, the System Validation
   panel, the freshness peek, the evals failed-case drawer. The severity tick is
   the single home of the halt/warn/grey decision: tone resolves through the live
   models.validation.Severity enum (prov_severity_tone), so a writer↔reader drift
   can't mute a halt. ---- */
.k-prov { display: flex; flex-direction: column; gap: var(--sp-1); }
.k-prov-row { display: flex; align-items: baseline; gap: var(--sp-3);
  padding: var(--sp-2) 0; border-bottom: 1px solid var(--hairline); }
.k-prov-row:last-child { border-bottom: none; }
.k-prov-label { flex: 1 1 auto; min-width: 0; font-size: var(--fs-body);
  color: var(--fg); overflow: hidden; text-overflow: ellipsis; }
.k-prov-stamp { flex: none; font-size: var(--fs-caption); color: var(--muted); }
.k-prov-note { flex: none; font-size: var(--fs-caption); color: var(--muted); }
.k-prov-actions { flex: none; display: inline-flex; align-items: center;
  gap: var(--sp-2); }

/* severity tick: a tone dot + uppercase label. THE one place halt/warn/grey is
   decided (prov_severity_tone) — an unknown severity degrades to a muted tick,
   never the wrong color. */
.k-prov-tick { display: inline-flex; align-items: center; gap: var(--sp-1);
  font-size: var(--fs-micro); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; white-space: nowrap; color: var(--muted); }
.k-prov-tick::before { content: ""; width: 7px; height: 7px; flex: none;
  border-radius: var(--radius-full); background: currentColor; }
.k-prov-tick-bad   { color: var(--bad); }
.k-prov-tick-warn  { color: var(--warn); }
.k-prov-tick-ok    { color: var(--ok); }
.k-prov-tick-muted { color: var(--muted); }

/* inline action: a small quiet control (resolve/refresh/diagnose) or a /source
   deep-link, radius-full like a chip. */
.k-prov-act { font: inherit; font-size: var(--fs-caption); font-weight: 600;
  color: var(--accent); background: transparent; border: 1px solid var(--border);
  border-radius: var(--radius-full); padding: 1px 10px; cursor: pointer;
  text-decoration: none; white-space: nowrap;
  transition: border-color var(--transition), color var(--transition); }
.k-prov-act:hover { border-color: var(--accent); }
.k-prov-act[disabled], .k-prov-act[aria-disabled="true"] { color: var(--muted);
  border-color: var(--border); cursor: progress; }

/* drill-down drawer (generalizes the evals failed-case drawer): a <details>
   whose summary toggles the body; per-case rows nest inside. */
.k-prov-drawer { margin: var(--sp-1) 0; }
.k-prov-drawer > summary, .k-prov-case > summary { cursor: pointer;
  list-style: none; font-size: var(--fs-caption); color: var(--muted);
  padding: var(--sp-1) 0; }
.k-prov-drawer > summary:hover, .k-prov-case > summary:hover { color: var(--fg); }
.k-prov-case { margin: var(--sp-1) 0; padding-left: var(--sp-3);
  border-left: 2px solid var(--border); }
.k-prov-case > summary { display: flex; align-items: baseline; gap: var(--sp-2); }
.k-prov-rationale { font-size: var(--fs-caption); color: var(--fg-soft);
  margin: var(--sp-1) 0; }
.k-prov-evidence { display: grid; grid-template-columns: 1fr 1fr;
  gap: var(--sp-2); margin: var(--sp-1) 0; }
.k-prov-evidence-label { font-size: var(--fs-micro); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.04em; }
.k-prov-evidence pre { font-family: var(--mono); font-size: var(--fs-micro);
  color: var(--fg-soft); background: var(--paper); border: 1px solid var(--border);
  border-radius: var(--radius); padding: var(--sp-2); margin: var(--sp-1) 0 0;
  white-space: pre-wrap; word-break: break-word; overflow-x: auto; }
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


# ---------------------------------------------------------------------------
# Provenance / data-quality primitives (design_language §10; Instrument
# Paradigm — "provenance is actionable"). prov_row/prov_drawer are the ONE
# render boundary for any data-quality row; every severity color decision flows
# through prov_severity_tone so writer↔reader drift can't mute a halt.
# ---------------------------------------------------------------------------

# Severity → kit tone, keyed by the LIVE enum (NOT hand-typed severity strings).
# The writer emits ``halt``/``warn`` (record_validation_issue); the report
# Sources renderer used to match ``error``/``warning``, so a HALT issue resolved
# to neither and rendered MUTED GREY — the worst issue, hidden. Keying on the
# enum means a renamed/added severity that nobody maps degrades to a neutral
# tick, and ``test_provenance_severity_contract`` fails until it is mapped here.
_SEVERITY_TONE: dict[Severity, str] = {
    Severity.HALT: "bad",
    Severity.WARN: "warn",
}


def prov_severity_tone(severity: str | None) -> str:
    """Kit tone token (``'bad'`` / ``'warn'`` / ``'ok'`` / ``'muted'``) for a
    stored validation-severity string. Resolved through the live
    :class:`models.validation.Severity` enum; an unknown or blank value degrades
    to ``'muted'`` rather than mis-coloring. THE single source of the
    halt↔warn↔grey decision (S10) — the report Sources tab, the System
    Validation panel, and the freshness peek all route severity through here."""
    if not severity:
        return "muted"
    try:
        sev = Severity(severity.strip().lower())
    except ValueError:
        return "muted"
    return _SEVERITY_TONE.get(sev, "muted")


# ---------------------------------------------------------------------------
# Thesis / break-rule status → tone: ONE vocabulary for every surface that
# colors a thesis verdict or rule status (the cockpit, peeks, the ticker
# command center, allocation decisions, portfolio health, the workspace break
# rules). The per-surface copies of this map drifted: `warn` rendered as a
# NEUTRAL pill on the ticker command center (its local dict only knew `watch`),
# `broken` fell to muted on the allocation panel, and the workspace break-rules
# table colored a healthy rule ACCENT BLUE (tokens.py reserves accent for
# interactive). Route every thesis-status color through here instead.
# ---------------------------------------------------------------------------

_THESIS_STATUS_TONE: dict[str, str] = {
    "breach": "bad",
    "broken": "bad",
    "warn": "warn",
    "watch": "warn",
    "ok": "ok",
    "intact": "ok",
}

_TONED = ("ok", "warn", "bad", "accent")


def thesis_status_tone(status: str | None) -> str:
    """Kit tone token (``'ok'`` / ``'warn'`` / ``'bad'``) for a thesis-verdict /
    break-rule status word; ``''`` (neutral) for anything else — `unresolved`
    and unknown vocabulary degrade to the un-toned base control, never to a
    wrong color."""
    return _THESIS_STATUS_TONE.get((status or "").strip().lower(), "")


def pill_tone_class(tone: str) -> str:
    """``' k-pill-<tone>'`` (leading space, appendable to a class string) for a
    real tone, ``''`` for neutral/unknown — the muted-fallback guard every
    surface used to re-implement inline."""
    return f" k-pill-{tone}" if tone in _TONED else ""


def chip_tone_class(tone: str) -> str:
    """``' k-chip-<tone>'`` counterpart of :func:`pill_tone_class`."""
    return f" k-chip-{tone}" if tone in _TONED else ""


def prov_severity_tick(severity: str | None, *, label: str | None = None) -> str:
    """The canonical data-quality severity indicator: a tone dot + short label
    (HALT / WARN). Tone via :func:`prov_severity_tone`; ``label`` overrides the
    text (default: the severity itself, upper-cased; blank → an em-dash)."""
    from html import escape

    tone = prov_severity_tone(severity)
    text = (label if label is not None else (severity or "")).strip().upper() or "—"
    return f'<span class="k-prov-tick k-prov-tick-{tone}">{escape(text)}</span>'


def prov_action(
    label: str,
    *,
    href: str | None = None,
    post_url: str | None = None,
    post_body: dict[str, object] | None = None,
    title: str = "",
    issue_id: int | None = None,
) -> str:
    """One inline data-quality action — a ``/source``-style deep LINK (``href``)
    or a resolve/refresh/diagnose BUTTON (``post_url`` + optional ``post_body``).

    The button carries its endpoint + JSON body as ``data-prov-post`` /
    ``data-prov-body`` attributes; the consuming surface's small delegated
    listener POSTs it (the freshness peek's streaming contract, or the resolve
    fetch). ``issue_id`` rides as ``data-issue-id`` so a resolve handler can
    target the row. Returns escaped HTML."""
    import json
    from html import escape

    attrs = f' title="{escape(title, quote=True)}"' if title else ""
    if issue_id is not None:
        attrs += f' data-issue-id="{int(issue_id)}"'
    if href is not None:
        return f'<a class="k-prov-act" href="{escape(href, quote=True)}"{attrs}>{escape(label)}</a>'
    if post_url is not None:
        body = escape(json.dumps(post_body or {}), quote=True)
        return (
            '<button type="button" class="k-prov-act" '
            f'data-prov-post="{escape(post_url, quote=True)}" '
            f'data-prov-body="{body}"{attrs}>{escape(label)}</button>'
        )
    # No destination — a defensive inert marker (shouldn't happen in practice).
    return f'<span class="k-prov-act" aria-disabled="true"{attrs}>{escape(label)}</span>'


def prov_row(
    label: str,
    *,
    severity: str | None = None,
    stamp: datetime | str | None = None,
    stamp_prefix: str = "",
    note: str | None = None,
    actions: str = "",
    drawer: str = "",
) -> str:
    """One actionable data-quality row (Law: provenance is actionable).

    A severity tick (when ``severity`` is given) + ``label`` + a relative
    ``stamp`` + a plain-text ``note`` aside + inline ``actions`` (pre-rendered
    :func:`prov_action` HTML), with an optional ``drawer`` (:func:`prov_drawer`)
    drilling down beneath the row. ``stamp`` is an ISO string / ``datetime``
    rendered relative via :func:`ui.time.stamp_html`. Returns escaped HTML."""
    from html import escape

    from ui.time import stamp_html

    tick = f"{prov_severity_tick(severity)} " if severity is not None else ""
    stamp_span = (
        stamp_html(stamp, css="k-prov-stamp", prefix=stamp_prefix) if stamp is not None else ""
    )
    note_span = f'<span class="k-prov-note">{escape(note)}</span>' if note else ""
    actions_span = f'<span class="k-prov-actions">{actions}</span>' if actions else ""
    row = (
        '<div class="k-prov-row">'
        f'<span class="k-prov-label">{tick}{escape(label)}</span>'
        f"{stamp_span}{note_span}{actions_span}"
        "</div>"
    )
    return f'<div class="k-prov">{row}{drawer}</div>'


def prov_drawer(summary: str, items: str, *, is_open: bool = False) -> str:
    """The collapsible drill-down: a ``<details>`` labeled ``summary`` wrapping
    the pre-rendered ``items`` (e.g. :func:`prov_case` rows). Lifts the proven
    evals failed-case drawer into the kit so any data-quality surface drops a
    "N failed cases — expected vs actual" expander in one call."""
    from html import escape

    open_attr = " open" if is_open else ""
    return (
        f'<details class="k-prov-drawer"{open_attr}>'
        f"<summary>{escape(summary)}</summary>{items}</details>"
    )


def prov_case(
    title: str,
    *,
    score: float | None = None,
    score_tone: str = "bad",
    meta: str = "",
    rationale: str = "",
    expected: str = "",
    actual: str = "",
) -> str:
    """One drill-down case (a failed eval / a disputed fact): a ``<details>``
    whose summary shows an optional ``score`` pill (``.k-pill``, ``score_tone``)
    + ``title`` + a muted ``meta`` aside, and whose body shows the ``rationale``
    and an expected/actual evidence split. ``expected`` / ``actual`` are shown
    verbatim in ``<pre>`` blocks (machine text — escaped, NOT markdown)."""
    from html import escape

    pill = (
        f'<span class="k-pill k-pill-{score_tone}">{score:.2f}</span> ' if score is not None else ""
    )
    meta_span = f'<span class="k-prov-note">{escape(meta)}</span>' if meta else ""
    rationale_p = f'<p class="k-prov-rationale">{escape(rationale)}</p>' if rationale else ""
    evidence = ""
    if expected or actual:
        evidence = (
            '<div class="k-prov-evidence">'
            '<div><span class="k-prov-evidence-label">expected</span>'
            f"<pre>{escape(expected or '—')}</pre></div>"
            '<div><span class="k-prov-evidence-label">actual</span>'
            f"<pre>{escape(actual or '—')}</pre></div>"
            "</div>"
        )
    return (
        f'<details class="k-prov-case"><summary>{pill}{escape(title)}{meta_span}</summary>'
        f"{rationale_p}{evidence}</details>"
    )


def fact_anchor_attrs(
    fact_ref: str | None,
    label: str,
    *,
    ask_q: str | None = None,
) -> str:
    """Doorway anchor attributes for a clickable datum (Instrument Paradigm
    Law 2 — "every datum is a doorway").

    ``label`` is the human display that doubles as the comment ``data-anchor-key``
    (ALWAYS emitted, so a comment still anchors even with no deeper view).
    ``fact_ref`` is the metric's STABLE handle —
    ``kpi:{ticker}:{def_id}`` / ``fin:{ticker}:{line_item}:{fiscal_period_type}`` —
    emitted as ``data-fact-ref`` so a click resolves the EXACT series by PK
    (``ask.grounding`` fast-path), never by re-phrase-matching the label. When
    ``fact_ref`` is absent the cell degrades to the name-keyed anchor.

    ``ask_q`` is the relative-window question phrasing cockpit stats use (S9).
    **Precedence contract:** a cell may carry both; the click delegate resolves
    ``data-fact-ref`` (exact) BEFORE ``data-ask-q`` (phrasing) — the exact
    handle always wins. The label is a *display*, never a *handle*.

    Returns escaped HTML attributes, space-joined, no leading/trailing space.
    """
    from html import escape

    attrs = [f'data-anchor-key="{escape(label, quote=True)}"']
    if fact_ref:
        attrs.append(f'data-fact-ref="{escape(fact_ref, quote=True)}"')
    if ask_q:
        attrs.append(f'data-ask-q="{escape(ask_q, quote=True)}"')
    return " ".join(attrs)
