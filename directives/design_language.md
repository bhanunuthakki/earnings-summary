# Design language — the canonical UI guidelines

**Status:** canonical (v3, 2026-06-11). Supersedes the scattered conventions in
individual renderer docstrings. Sources of truth in code:
`src/ui/tokens.py` (values) and `src/ui/controls.py` (components). Every HTML
surface composes its `<style>` as `palette_css(...) + controls_css(...) +
local layout CSS` — local CSS may add **layout** (widths, grids, gaps), never
re-skin type, color, or controls.

The voice of the product is **calm desk, deep drawers**: a quiet, dense,
dark instrument panel for daily work; an editorial paper reading surface for
briefs. Premium here means *restraint* — one accent, one radius, one shadow,
mono only where mono means something.

---

## 1. Type

Six semantic steps (`src/ui/tokens.py::TYPE_SCALE`). Size encodes
**importance, not surface**: the same kind of element renders the same size on
every screen.

| Token          | px   | Use for                                                            |
|----------------|------|--------------------------------------------------------------------|
| `--fs-display` | 22   | The page's ONE dominant element: page title, hero stat            |
| `--fs-title`   | 16   | Panel / drawer / card titles (the h2 tier)                        |
| `--fs-section` | 14   | Sub-section headings; serif reading prose; prominent inputs        |
| `--fs-body`    | 13   | Default UI text: tables, inputs, buttons, tabs                    |
| `--fs-caption` | 11.5 | Secondary metadata: table headers, stamps, hints, sublabels       |
| `--fs-micro`   | 10   | Smallest annotations: chips, badges, kind tags, axis marks        |

Font roles (`FONT_TOKENS`): `--sans` (Inter) is the UI; `--serif` (Source
Serif 4) is reading prose in reports; `--mono` (JetBrains Mono) is **only**
tickers, numbers, code, timestamps, and locators. Mono is an annotation voice,
not a theme — a label or button in mono is drift.

Sanctioned escapes — everything else snaps to the scale:

1. The workspace report's **reading ramp** (12.5–14px prose/cells) and
   **display ramp** (15px lede → 28px section title → 60px identity ticker →
   100px hero mark). The report is an editorial surface; its larger type is
   deliberately surface-specific (see `workspace_styles.py` docstring).
2. `font-size: 0.93em` for inline mono inside running text (optical
   correction, not an importance level).
3. The 8.5px `.src-chip` per-number provenance mark.
4. SVG chart internals (`charts_v2.py`) — axis/label sizes are tuned to the
   plot geometry, not the UI scale.

## 2. Color

Palettes live in `PALETTE_LIGHT` / `PALETTE_DARK` (+ white overrides). Roles,
not colors, in all surface CSS — **zero raw hex outside tokens.py**:

| Role                          | Tokens                                  | Rule                                                         |
|-------------------------------|------------------------------------------|--------------------------------------------------------------|
| Canvas / cards / wells        | `--bg`, `--surface`, `--paper`           | Page → panel → inset, in that order                          |
| Ink                           | `--fg`, `--fg-soft`, `--muted`, `--muted-2` | Two grays of de-emphasis, no more                         |
| Lines                         | `--border`, `--border-2`, `--hairline`   | hairline = row rules; border = boxes; border-2 = hover/strong |
| Semantics                     | `--ok` / `--warn` / `--bad` (= pos/neg)  | green=good, red=bad, **everywhere**                          |
| Interactive                   | `--accent`, `--accent-soft`, `--accent-contrast` | Accent is RESERVED for interactive/selected/unread; never decoration. `--accent-contrast` is the only ink allowed on accent fill |
| Tones (report)                | `--tone-*`                               | Quote/sentiment washes in the report only                    |
| Charts                        | `CHART_SERIES` (Okabe-Ito)               | Categorical series only                                      |
| Elevation                     | `--shadow-pop`                           | The one popover/menu shadow                                  |

Status pills derive from semantic tokens (`color` + `color-mix(...45%,
transparent)` border) — never freehand a new background/foreground pair per
status (the old `#14361f/#6ee7a0` family is exactly the drift this kills).

## 3. Chrome

- **One radius**: `--radius` (8px) for every rectangular box — cards, inputs,
  popovers, drawers. `--radius-full` only for deliberately round things
  (pills, dots, toggle tracks). 3/4/5/6px corners are drift.
- **One motion**: `var(--transition)` (150ms ease) with explicit properties —
  never `transition: all`.
- **One popover elevation**: `box-shadow: var(--shadow-pop)`.
- **Close glyphs** (the `×` buttons on drawers/popovers/peeks): `20px`,
  muted → fg on hover. A glyph size, not a type-scale step.
- **Soft status fills**: `color-mix(in srgb, var(--ok|warn|bad|accent) ~16%,
  transparent)` + token ink — never a freehand dark-well/pastel hex pair.

## 4. Controls (`src/ui/controls.py`)

`controls_css(default)` rides after `palette_css(...)` on every page. It
skins the **elements** (`select`, `input`, `textarea`) directly: dark
`color-scheme`, `appearance: none` + a custom chevron on single selects, one
accent focus ring, `accent-color` checkboxes. Surfaces add layout only
(width, flex). **Never set `background:` (shorthand) on a `<select>`** — it
wipes the chevron `background-image`; use `background-color` if a different
well is truly needed.

Buttons — three intents, no other treatments:

| Class            | Look                              | Use                                              |
|------------------|-----------------------------------|--------------------------------------------------|
| `.k-btn-primary` | solid accent, `--accent-contrast` ink | THE one main action of a view/form (Run, Save) |
| `.k-btn-quiet`   | outline, muted → fg on hover      | Everything else                                  |
| `.k-btn-danger`  | red ink, red border on hover      | Destructive only (delete, dismiss-forever)       |

(`.k-btn-sm` for dense rows. Legacy accent-soft "secondary" buttons migrate to
quiet or primary — two CTAs side by side means one of them isn't primary.)

Chips: `.k-chip` (+ `-ok/-warn/-bad/-accent`, `-mono`, `.is-on` for filter
toggles). Radius-full, micro, uppercase. One shape for every badge/status/
filter chip in the app.

Menus/popovers: `.k-menu` (+ `li.sel`) for any floating list (combobox
results, palettes).

Field captions: `.k-label` (caption size, 600, uppercase, 0.06em).

## 5. The ticker label

**Never render `f"{ticker} · {name}"` (or ` — `) as one string.** Use
`ui.controls.ticker_label(ticker, name, href=..., name_max=...)`:

```
NU  Nu Holdings Ltd.        ← mono 600 symbol · sans muted caption name
```

- name ellipsis-truncates at `--k-tick-max` (default 20ch); FULL name always
  in `title`
- `href` links the symbol only — long names never become long links
- tables may keep symbol-only cells with the name in `title` where width is
  precious (cockpit), but pickers, headers, cards, and palette rows show the
  two-part label.

## 6. Spacing & density

Gaps/paddings snap to `--sp-1..6` (4/8/12/16/24/32). Density is deliberate,
not accidental:

- **Panel padding**: 16–18px (`--sp-4`); dense list cards 8–12px.
- **Table rhythm**: th/td `6px 10px` on dashboards (cockpit-thin `4px 10px`);
  headers are `--fs-caption` uppercase muted.
- The report keeps its own density tokens (`--pad-*`, two densities) — they
  are layout, owned by the surface.

## 7. Do / don't

- **Do** compose: `palette_css(mode) + controls_css(mode) + layout-only CSS`.
- **Do** pick type sizes from the scale by importance, then stop.
- **Do** route every status color through `--ok/--warn/--bad`.
- **Do** use `ticker_label()` wherever a ticker meets a company name.
- **Don't** write raw hex in surface CSS — including `var(--x, #hex)`
  fallbacks: they silently resurrect the pre-token palette (`#7aa2f7`,
  `#2a2d31`, …) on any page that misses a token. Tokens ride on every page;
  fallbacks are dead weight that drifts.
- **Don't** re-skin form elements per panel. Layout only.
- **Don't** use accent for non-interactive decoration, mono for labels,
  `transition: all`, or a new radius/shadow/letter-spacing.
- **Don't** invent a new chip/badge/button variant — extend the kit in
  `controls.py` if a real gap exists, and document it here.

---

## Appendix A — fresh-eyes audit (2026-06-11), the drift this version kills

Catalogued against `origin/main` @ `5d37519` (file:line). Headline counts:
**241** hardcoded `font-size: *px` declarations across 24 files, **~240** raw
hex colors outside tokens.py, **45** native `<select>/<input>` emissions
across 13 files with ~10 distinct ad-hoc skins and zero custom chevrons, ≥6
button treatments, ≥9 chip variants across 5 radii (2/3/4/5/6/8/10px).

**Native/inconsistent controls (the "I don't like the dropdowns" complaint):**
`explore_panel.py:53–68,128` (builder inputs + 3 native multi-selects + 2
selects; `var(--font-mono, monospace)` fallback at 120–125 breaks standalone),
`journal_panel.py:32–37,55–62` (raw hex `#1a1d23/#2a2d31/#8aa8ff`, radii
6/5px, 12.5px), `ticker_command_center.py:420–433` (quicknote: radius 6px,
12.5/12px, old-palette fallbacks `#7aa2f7/#0d1117`), `:894–916` (combobox:
ad-hoc menu shadow `rgba(0,0,0,0.5)`), `advisor_memos_panel.py:344–355`
(radius 4px, mono select), `allocation_decisions_panel.py:647–652` (radius
4px + `#0d1117` on accent), `discovery_panel.py:38–40`,
`dashboard_html.py:55–64` (alien `ui-monospace` stack, radius 4px, `#fff` on
`--link`), `workspace_comments.py:903–920` (radius 4px, `#0d1117` ink on
accent — near-invisible in the report's light theme), `portfolio_panel.py:238`
(date input, only surface with `color-scheme: dark`), shell
`command_center_shell.py:617` (budget selects). None killed the native arrow.

**Off-scale type (worst):** `journal_panel.py:42` (9.5px kind chip!), `:46,50,53`
(10.5/11px), `section_coverage_panel.py:247` (18px KPI), `:233,242` (10px),
`thesis_ledger_panel.py:85` (15px), `advisor_memos_panel.py:310–358`
(10.5–14px spread + 565–571: 20/17px), `allocation_decisions_panel.py:627–661`
(11/12/12.5px), `dashboard_html.py:52–72` (11–13px),
`source_viewers.py:53–79` (11–16px), `ir_coverage_panel.py:29–35`,
`restatements_panel.py:29–31`, `source_calls_panel.py:36`,
`validation_issues_panel.py:30–32`, `ticker_settings_panel.py:86–89`,
`viewspec/render.py:42–45`, `ask_dock.py:54` (10.5px),
`report/renderers/html.py` (35 sizes 10.5–28px + own font stacks at
147/223/414/455 — predates tokens entirely), `workspace_chat.py` (15),
`workspace_comments.py` (18). Workspace reading/display ramps in
`workspace_styles.py` are sanctioned (§1) — its *chrome* tiers were already
tokenized.

**Raw hex hot-spots:** `advisor_memos_panel.py:313–362` (a parallel pill tone
system: `#14361f/#6ee7a0/#1f2b3a/#8fb6e6/#2b2440/#c4b5fd/#103039/#7dd3fc/
#3b2f14/#422006/#3a1f1f/#f0a0a0/#2a2c30/#f5f5f0`),
`allocation_decisions_panel.py:630–660` (same family + `b-ok/b-warn/b-bad`
badge wells `#14532d/#422006/#450a0a`, also `ticker_command_center.py:1412–1414`),
`journal_panel.py:33–58`, `section_coverage_panel.py:239–249`
(`#4ade80/#5b5e66/#3a3d44/#f5c66a/#1a1d23/#1f2125`),
`dashboard_html.py:65–70` (`#b88a1f/#3a8a3a/#b04040/#1c1c1c/#e6e6e6`),
`inbox.py:592–657` + `_styles.py` (OLD-palette `var(--x,#hex)` fallbacks:
`#7aa2f7` accent, `#fbbf24`, `#f87171`, `#16171a`, `#2a2c30`, `#888`;
`ix-badge` ink `#101114`), `charts_v2.py:786–798` (`#67737d/#1a1f2e` + literal
font stacks), `workspace_chat.py`, `peeks.py`.

**Ticker+name as one string:** `ticker_command_center.py:773` (combobox input
value `"NU · Nu Holdings Ltd."`), `:692` (standalone h1),
`command_center_shell.py:1288` (palette row label), `html.py:760`
(`<strong>T</strong> — name`), `markdown.py:224`. Done right (two-part) only
in the combobox dropdown rows (`cc-combo-tk/-nm`) and the peek mini-card
(`cc-mini-ticker/-name`) — now canonized as `.k-tick`.

**Density drift:** card paddings 8/9/10/12/14/16/18px mixed per surface;
`.actions-section` radius 4px vs panels 8px; menu shadows 0.35/0.45/0.5/0.55
alpha at 4 geometries.
