# Design language — the canonical UI guidelines

**Status:** canonical (v6, 2026-08-08 Compact Hierarchy Edition). Supersedes the scattered conventions in
individual renderer docstrings. Sources of reusable palette/control behavior in
code are `src/ui/tokens.py` and `src/ui/controls.py`. The four-step visible type
scale below is the approved migration target; legacy semantic token aliases move
to it only when the live front-end implementation is authorized. Every HTML
surface composes its `<style>` as `palette_css(...) + controls_css(...) +
local layout CSS` — local CSS may add **layout** (widths, grids, gaps), never
re-skin type, color, or controls.

The voice of the product is **calm desk, deep drawers**: a quiet, dense,
dark instrument panel for daily work and focused research reading. Briefs change
the arrangement, not the visual system. Premium here means *restraint* — curated color swatches, one radius hierarchy,
one motion, mono only where mono means something.

---

## 1. Type

Application surfaces use **four visible sizes**, matching the simplified
dashboard. Size encodes importance, not surface: the same role renders at the
same size everywhere.

| Token          | px | Use                                                        |
|----------------|----|------------------------------------------------------------|
| `--fs-display` | 20 | The view's one dominant title or hero value                 |
| `--fs-title`   | 15 | Page sections, panels, cards, drawers, document sections    |
| `--fs-body`    | 13 | Prose, tables, controls, tabs, and ordinary labels          |
| `--fs-caption` | 11 | Metadata, table headers, chips, timestamps, and source marks |

Existing semantic names are aliases, not extra sizes: `--fs-stat` resolves to
`--fs-display`; `--fs-header-title` resolves to `--fs-title`; and
`--fs-mono-sm` / `--fs-micro` / `--fs-nano` resolve to `--fs-caption`.
Hierarchy inside a shared size comes from weight, color, and spacing — never a
new intermediate font size. `--sans` (Inter) is the application voice,
including in-app research briefs; `--serif` (Source Serif 4) is reserved for
standalone/exported editorial artifacts; `--mono` (JetBrains Mono) is **only**
tickers, numbers, code, timestamps, and locators. Mono is an annotation voice,
not a theme — a label or button in mono is drift.

In a **data table** this resolves the obvious way: numeric cells are mono (`font-feature-settings: 'tnum', 'zero'`), but the row labels and column headers are sans — never put `font-family: var(--mono)` on the whole table. Every value cell is a `<td>` and every label/header a `<th>`, so the entire rule is `.matrix td { font-family: var(--mono); }` and the `<th>` inherit the UI sans.

---

## 2. Color

Palettes live in `PALETTE_LIGHT` / `PALETTE_DARK` (+ white overrides). Roles, not colors, in all surface CSS — **zero raw hex outside tokens.py**:

| Role                          | Tokens                                  | Rule                                                         |
|-------------------------------|------------------------------------------|--------------------------------------------------------------|
| Canvas / cards / wells        | `--bg`, `--surface`, `--paper`           | Page → panel → inset (`#090a0c` → `#121316` → `#18191d`)     |
| Ink                           | `--fg`, `--fg-soft`, `--muted`           | ONE gray of de-emphasis (`--muted-2` folded in 2026-07-19)   |
| Lines                         | `--border`, `--border-2`, `--hairline`   | hairline = row rules; border = boxes; border-2 = hover/strong |
| Semantics                     | `--ok` / `--warn` / `--bad` (= pos/neg)  | green=good, red=bad, **everywhere**                          |
| Legacy editorial mark         | `--mark`, `--mark-soft`                  | Existing exports only; no new in-app use                     |
| Interactive                   | `--accent`, `--accent-soft`, `--accent-contrast` | Accent is RESERVED for interactive/selected/unread; never decoration |
| Benchmark Series              | `--series-qqq`                           | `#5b8def` index comparator series in relative performance views |
| Tones (report)                | `--tone-*`                               | Quote/sentiment washes in the report only                    |
| Elevation                     | `--shadow-pop`, `--shadow-card`, `--shadow-drawer` | Elevation shadows for cards, popovers, and drawers          |

**Neutral temperature (2026-08-07).** The dark ground palette uses an **Ultra-Deep Warm Obsidian ground (`#090a0c`)** with desaturated, quiet neutral temperature, glassmorphism surface tokens (`--glass-bg`, `--glass-border`), and crisp hairline borders (`rgba(255,255,255,0.06)`). Semantics did not move: `--ok/--warn/--bad` keep their exact values (`#4ade80`, `#f5c66a`, `#f08a8a`) and `--accent` stays blue (`#8aa8ff`), so "green = good" and "blue = interactive" are intact.

Theme-dependent **glyphs** (`--k-chevron`, `--k-check`) are *derived* from the
palette in `controls.py`, never copied. They used to freeze their inks as
literals while claiming to track `--muted` / `--accent-contrast`; the warming
falsified both silently and nothing failed. `test_glyph_ink_tracks_the_palette`
now pins the link.

One scoped exception to "accent = interactive": the workspace REPORT's
category/kind tags (`.qa-tag`, `.ir-type`, `.decision-action`, `.oi-kind`)
keep a single `--accent-soft`/`--accent` treatment as editorial wayfinding
marks in long prose. That is the report's ONE category treatment — status
there still routes through `--ok/--warn/--bad`, and dashboards keep
category-quiet.

**Enforced, opt-out (§7).** "Zero raw hex outside tokens.py" is not a
convention — it is checked on *rendered output* over every CSS-emitting surface
by the executable guard `tests/test_ui_controls.py`. Status pills/wells use the
`.k-pill` / `.k-well` kit (`controls.py`), never a freehand bg/fg hex pair.

## 3. Chrome & 3-Layer Work OS Architecture

These eight destinations are the complete primary IA. Surface-specific views,
filters, drawers, and shortcuts nest beneath them; they do not add competing
top-level navigation. This simplification boundary keeps the left rail stable
while the content canvas changes depth.

- **Cockpit is the only inbox.** Alerts, reviews, and pending work resolve into
  its action stack instead of creating another feed destination.
- **No trade-execution surface.** The product may prepare and audit a decision,
  but remains pull-only and does not place trades.
- **Diet destination and general-purpose feed are retired.** Signals are nested
  in the Company Desk or summarized in Cockpit when they require action.
- **Discovery has no primary navigation.** Search, command palette, and grounded
  Copilot suggestions are doorways into the eight destinations.
- **One responsive product.** Desktop and mobile share the same information
  architecture; mobile collapses the rail to `--sidebar-collapsed-width` rather
  than replacing it with a different top navigation.
- Dismissing transient UI **clears it for the current session**; dismissal alone
  never writes a durable portfolio fact. **Only an explicit decision or threshold change may create durable state.**

- **Harvey/Legora 3-Layer Sidebar Navigation**: Dashboard surfaces adopt the 3-Layer Work OS sidebar structure (`.app-sidebar`, `--sidebar-width: 240px`):
  - **L1 · Portfolio Intelligence**: `Portfolio Cockpit`, `Performance vs Index`, `Risk & Allocations`, Active Holdings shortcuts (`NU`, `BKNG`, `TSM`).
  - **L2 · Research Engine**: `Company Desk`, `Full Research Brief`, `Analytics Playground`.
  - **L3 · Operations & Governance**: `Decision Audit Log`, `Execution Queue & Operations`.
- **Zero-Layout-Pop Card Dismissal Contract**: Action cards implement the `.card-dismissing` collapse contract (`dismissCard()`, height-locked transition with `border-color: transparent !important`, `max-height: 0`, `opacity: 0`, `transform: scale(0.97) translateY(-2px)`). Dismissing an item prevents vertical layout jumping and persists state to `data/portfolio.db` with a `.toast-notice` toast.
- **One radius hierarchy**: `--radius` (8px) for inputs/buttons; `--radius-card` (10px) for surface cards; `--radius-drawer` (14px) for slide drawers; `--radius-full` (999px) for pills, dots, and chips.
- **One motion**: `var(--transition)` (150ms ease) or `var(--transition-fluid)` (250ms cubic-bezier) with explicit properties — never `transition: all`.
- **Elevation shadows**: `--shadow-card` for cards, `--shadow-card-hover` for interactive cards, `--shadow-pop` for popovers, and `--shadow-drawer` for slide drawers.

### 3.1 Surface dismissal — the `CCOverlay` contract (Law 3)

Every transient surface — drawer, peek, palette, dock, sidebar, popover — is an
instance of ONE primitive, `window.CCOverlay` (`src/pipeline/cc_overlay.py`),
registered into a single in-memory open-surface stack. No surface re-derives its
own dismissal; there is no per-surface `close*` function wired to its own scrim,
no enumerated Escape `switch`, no hardcoded id registry, no cross-document
`window.__close*` handshake. The look + open motion of `.k-scrim` / `.k-overlay`
are the control kit's (§4); CCOverlay owns dismissal + close motion. Guarded by
`tests/test_overlay_dismissal.py`.

```js
var handle = window.CCOverlay.register(el, {
  modal, priority, scrim, scrimOpacity, trapFocus, restoreFocus,
  group, motion, closeId, wireClose, toggleHidden, autofocus, onOpen, onClose,
});
handle.open(); handle.close(); handle.isOpen();
```

- **Modal surfaces get the full triad by construction:** close-control (`×`,
  declared via `closeId`) + Escape + scrim click-out, plus focus-trap and
  focus-restore. A registered modal surface MUST declare a `closeId` whose
  element exists in the markup.
- **Escape resolves by stored modality + PRIORITY, never recency.** The ladder
  is `PALETTE (50) > PEEK (40) > DRAWER (30) > DOCK (10)` — a drawer opened
  *after* the palette never steals Escape from it. Recency breaks ties between
  equal priorities only.
- **One scrim, one Escape, one scrim-click listener per document.** A single
  `.k-scrim` element is shown beneath the topmost `scrim:true` surface, its
  `z-index` set just under that surface; the per-surface scrims are deleted.

---

## 4. Controls (`src/ui/controls.py`)

`controls_css(default)` rides after `palette_css(...)` on every page. It
skins the **elements** (`select`, `input`, `textarea`) directly: dark
`color-scheme`, `appearance: none` + a custom chevron on single selects, one
accent focus ring, `accent-color` checkboxes. Surfaces add layout only
(width, flex).

Buttons — three intents, no other treatments:

| Class            | Look                              | Use                                              |
|------------------|-----------------------------------|--------------------------------------------------|
| `.k-btn-primary` | solid accent, `--accent-contrast` ink | THE one main action of a view/form (Run, Save) |
| `.k-btn-quiet`   | outline, muted → fg on hover      | Everything else                                  |
| `.k-btn-danger`  | red ink, red border on hover      | Destructive only (delete, dismiss-forever)       |

Chips & Suggestion Prompts: `.k-chip` (+ `-ok/-warn/-bad/-accent`, `-mono`, `.is-on` for filter toggles). Pre-populated Copilot suggestion chips submit through the unified Ask controller, inherit the active company and research context, and render governed evidence through the same durable exchange as a typed question. Chips never call a template-local fake response function.

Interactive Handles & Doorways:
- `.drill-handle`: Dotted bottom border (`--bw-thin`) in `--accent`, launching slide drawers (`openDrillDrawer('financials')`).
- `.src-chip`: Mono micro mark (`--fs-nano`, `--radius-md`) launching raw citation peek drawers (`openPeekDrawer('doc:ref')`).

Interactive Modules & Visualizers:
- **Card Grids**: `.card-grid-action`, `.card-grid-stat`, `.card-grid-risk` for auto-fitting card layouts.
- **Stat Numbers**: `.stat-heading`, `.stat-number` (`--fs-stat`, mono tabular), `.stat-subtext`.
- **Benchmark Bar Comparator**: `.perf-bar-track` (background track, `--bar-track-height: 8px`), `.perf-bar-fill` (colored performance fill).
- **Time Horizon Picker**: `.picker-container` (`--paper` background, thin border), `.picker-btn`.
primitive and are legitimately bespoke — "compose the kit, never reinvent" can't
bind where there is nothing to compose. The reviewed keeps: a **segmented
selector** (`.qbtn` quarter picker — a radio-group of buttons sharing one track),
a **toggle track** (`.twk-toggle-btn`), an **icon-only button** (`.ic-btn`), and
**status list-items** (`.flag-list li`, whose status reads via a `border-left`
rail — a list row is not a chip). These stay local CSS by design; they route
color through the status tokens (`--ok/--warn/--bad`) for *status* (§2-correct)
and carry no `color-mix` filled *pill* skin, so the `kit-badge` enforcer (§7.1)
leaves them alone by construction. Add a NEW bespoke control here only when the
kit genuinely lacks the primitive — not to dodge it; a filled status badge is
never one of these (use `.k-pill`).

### 4.1 Doorways — every datum is a depth (Law 2)

Any number, count, KPI, cell, or stream item **with a deeper view** is rendered
as an `<a>`/`<button>` carrying **exactly ONE** shell-handled action attribute.
A `title=` tooltip is a *supplement*, never the only depth — an inert `<span>`
whose payload is buried in a tooltip is forbidden. The three rails (all
delegated once at the document level in `command_center_shell`, so lazily
injected panels are covered):

| Attribute        | Opens                                   | Carrier example |
|------------------|-----------------------------------------|-----------------|
| `data-peek-url`  | the backing collection in the shared peek popover (href stays the real middle-click destination) | a count/pill → its rows (`/api/peek/documents`, `/api/peek/alerts`) |
| `data-ask-q`     | the datum as a chart/series in Ask, via the goAsk stash-and-jump rail | a KPI chip → its time series |
| `data-fact-ref`  | the datum as its **exact PK series** in Ask (degrades to name) | a report KPI cell → that fact's series |

Rules:

- **`data-ask-q` uses relative-window phrasing — period COUNTS, never ISO date
  ranges** (`"Net margin for NU, last 12 quarters"`, not `"…2025-01-01 to
  2026-03-31"`). The ViewSpec compiler (`viewspec/nl_compile`) parses counts; an
  ISO range compiles to an empty chart. Period dates ride in `title=`.
- **`data-ask-q` is scoped to exclude the Ask panel** (`data-panel="explore"`):
  the Ask panel wires its own example chips (`submitAsk`), so a shell-level
  handler over them would double-fire. One owner per region.
- **Precedence — `data-fact-ref` beats `data-ask-q`.** A cell may carry both (an
  exact series *and* a relative-window question). The fact-ref handler claims
  the click; the ask-q handler bails on any element that also carries
  `data-fact-ref`, so the looser relative window never overrides the exact
  series. (Cockpit stats own `data-ask-q`; report KPI cells own `data-fact-ref`
  — directive §6.)
- A **primary picker** is in-section furniture, not a floating overlay (§6.1) —
  it is not a doorway; it *is* the section's own control.

Enforced by the `no-inert-stat` guard (`tests/test_no_inert_stat.py`): it
renders a populated cockpit and asserts each paradigm doorway stat is an
`<a>`/`<button>` with exactly one action attr, never an inert `<span>`, and that
the KPI chip's `data-ask-q` carries no ISO date.

### 4.2 Doorway handles — the `fact_ref` grammar (Law 2)

The `data-fact-ref` rail (§4.1) carries a **stable handle**, not the display
name: the label is what the reader sees; the handle is what the click resolves.
A human label drifts (KPIs get renamed); a handle does not. Emit the handle +
its name-keyed comment anchor together with one helper —
`ui.controls.fact_anchor_attrs(fact_ref, label)` — never hand-write the
attributes. It always emits `data-anchor-key="{label}"` (the comment anchor +
degrade path) and adds `data-fact-ref="{handle}"` only when the datum resolves
to a stable identity (else the cell degrades to the name-keyed anchor). It also
accepts an `ask_q=` and orders `data-fact-ref` before `data-ask-q`, baking the
§4.1 precedence.

**Grammar** (`fact_ref` is `prefix:ticker:…`):

| Form | Resolves to | Emitted by |
|---|---|---|
| `kpi:{ticker}:{def_id}` | `kpi_facts` by `kpi_definition_id` PK | KPI ledger rows (`thesis_risk`) |
| `fin:{ticker}:{line_item}:{fiscal_period_type}` | `financial_facts` by `(line_item, fpt)` | financial line-item cells |

`fpt` is a single cadence (`Q1`…`Q4`, `FY`, `TTM`, …) or any other token to mean
the quarterly series.

**PK fast-path vs string degrade.** A cell gets a `fact_ref` only when it maps
to a queryable series by primary key. Cells whose anchor is inherently
text-keyed — segment names, say/do rows, failure-mode hypotheses, news
headlines — stay name-keyed (`data-anchor-key` only): there is no PK to point
at, so they keep the free-text anchor regime. `ask.grounding` reads any
`fact_ref` token in a question and resolves the **exact** series by PK; its NL
name-match is the FALLBACK. A note (and the report comment it mirrors) persists
the handle in `analyst_notes.fact_ref` (0101) so it re-binds across a rename.

### 4.3 Contextual card actions — capability before chrome

Research cards may expose compact **Chat** and **Edit** actions when the action
has a named product capability. Place them in the card header or relevant row;
reveal them on card hover **and** `:focus-within`, and keep them visible on
non-hover devices. Hover-only access is a failure. Use the kit button classes
and preserve the dense one-band panel contract — contextual actions must not
create a new toolbar band.

Every action emits a stable `data-capability` identifier and is registered in
the surface's interaction catalog with one of three states: **Ready now**,
**Adapter needed**, or **New governed capability**. The catalog names the
owning module or route, required identity/context fields, write boundary, and
remaining backend work. Visible controls must never imply that an unbuilt
backend mutation already exists.

- **Discussion is read-only.** Reuse the company-scoped Ask seam, prefill the
  card prompt, and pass durable item/fact identifiers through `context_spec`.
  Ask may create governed thesis and KPI proposal cards, but a proposal is not
  a mutation: render the exact current-to-proposed diff and require explicit Owner
  approval before the owning domain module applies it.
- **Edit is a proposed mutation.** Load the current revision, preview a diff,
  validate through the module that owns the domain object, require Owner
  approval, and persist with an idempotency key plus audit event. Never turn an
  LLM response directly into an approved write.
- A card without a meaningful edit contract gets Chat only. Do not add
  symmetrical controls merely for visual balance.

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

## 6. Spacing, Density & 100% Token Purity Rule

Gaps/paddings snap to `--sp-1..6` (4/8/12/16/24/32) or `--sp-half` (2px).
The default application rhythm is **16 / 12 / 8**: `--sp-4` at the view edge,
`--sp-3` between modules and inside cards, and `--sp-2` between rows or tightly
related elements. Density is deliberate, not accidental:

- **View padding**: `--sp-4`; `--sp-5` is reserved for a major page boundary, never repeated through nested containers.
- **Cards use `--sp-3`** for padding and sibling gaps. A card with a separate header uses `--sp-2` vertically / `--sp-3` horizontally in the header and `--sp-3` in the body.
- **Dense rows**: `--sp-2` vertical rhythm; do not make a list pay card padding again on every row.
- **Table rhythm**: th/td `--sp-2` / `--sp-3`; headers are `--fs-caption` uppercase muted.
- **Strict 100% Token Purity Rule (Zero Raw Pixel Escapes)**: Every single dimension (`width`, `height`, `max-width`, `min-width`), padding, margin, font-size, corner radius, backdrop blur filter, box shadow, transform lift, and border width outside of `:root` **MUST** bind to CSS custom variables (`var(--sp-*)`, `var(--fs-*)`, `var(--radius-*)`, `var(--blur-*)`, `var(--shadow-*)`, `var(--bw-*)`, `var(--lift-*)`). Hardcoded pixel escapes (`px`) in CSS rules are forbidden and fail CI!

---

## 7. Enforcement & Verification

- **Executable Guard (`tests/test_ui_controls.py`)**: Automatically scans all registered surface Python files in `src/` to enforce 100% token purity, verifying zero un-tokenized hex literals, zero off-scale font sizes, zero illegal corner radii, and zero un-tokenized pixel escapes.
- **Token Contract (`tests/test_ui_tokens.py`)**: Pins the palette swatches, type scale steps, spacing ladder, chrome corner radii, and contrast ratios.
- **Fast Feedback**: Run `python -m pytest tests/test_ui_tokens.py tests/test_ui_controls.py -q` before pushing any front-end changes.

### 6.1 One operating band per panel

A panel sitting under an already-labeled tab is **one labeled instrument**
(Instrument Paradigm, Law 3). It gets **at most ONE chrome band** before its
content — never a title band stacked over a filter band:

- Use `controls.py::panel_toolbar(title, filters=…, actions=…)`: the title sits
  on the left, filters **and** actions share the SAME flex row to its right
  (`.k-toolbar`). One band, one row.
- The **nav owns the title.** A panel under a single-sub-tab section (Home, Ask)
  must not re-print its own section name — the tab label already says it. Pass
  `panel_section_title(title, suppressed=True)` (or `panel_toolbar(...,
  suppress_title=True)`) and the heading collapses; the controls left-align into
  the freed row.
- Primary pickers are in-section furniture, not floating overlays: a section's
  own search/filter renders in the toolbar band, not a `.k-menu` popover.

### 6.2 Metadata embeds — it never gets its own band

A view's self-description — series count, transform, cadence, coverage / "N of
M", n/m tallies — is metadata **about** the content, not content. It rides a
frame the layout already pays for (a chart's title strip, a card's actions row,
a table caption), never a row reserved above the data. A caption band that
exists only to restate `transform · cadence` is the drift this kills — and the
line is stated **once**, not per layer. (The Ask result card carried it three
times — an assistant-message band, a fragment caption, and the chart title —
over two contradictory counts. It now shows one reconciled line on the actions
row, and when the request outran the data that line says so, `4 quarters · ⚠ 8
missing`, instead of disagreeing with itself.)

### 6.3 The research document — a reading surface, not a panel grid

Some surfaces are **documents**: a thesis review, a monthly brief, a memo. They
carry an argument end to end and use a different arrangement from a console,
but an in-app document inherits the same Warm Obsidian palette, Inter type
hierarchy, neutral rules, and chrome as the rest of the workspace. It does not
introduce a paper palette, ochre furniture, a second type system, or a nested
application shell. The primitives remain in `controls.py`, so a document
composes the kit like every other surface.

**Rules instead of boxes.** A document separates sections with hairlines and
whitespace, never cards with fills. This is the load-bearing rule: cards,
filled pills and gradients are what make a research page read as a *dashboard*,
and a research page that reads as a dashboard has lost the seriousness it needs
to be trusted. Status is a **word in plain type** (`holds`, `near`), not a
`.k-pill` — pills are chrome, and in a document state belongs to the sentence.

**One application type hierarchy.** The document masthead gets the page's one
`--fs-display`; major sections and subsections share `--fs-title`, differentiated
by weight and spacing; prose uses `--fs-body`; labels and metadata use
`--fs-caption`. In-app brief prose is Inter. Mono remains limited to numbers,
tickers, dates, and locators. A brief does not add intermediate sizes just
because it is a reading surface.

**Compact vertical rhythm.** Mastheads and major sections use `--sp-4` vertical
padding with at most `--sp-5` horizontal padding. Title-to-deck and paragraph
gaps use `--sp-1`/`--sp-2`; subsection separation uses `--sp-3`. Do not use
`--sp-6` or larger inside a document to manufacture importance. Tables retain
the dashboard's `--sp-2`/`--sp-3` row rhythm.

**Neutral edges.** Section rules, the contents rail, margin notes, and source
quotes use `--border` or `--hairline`. They never introduce a colored left
border. Selected navigation may use the normal interactive background/text
treatment, but the rule itself remains neutral and matches the workspace.

**A margin note attaches to one section, never to a standing gutter.** Use
`.k-doc-row` for the section that has a note; leave every other section
full-bleed. A reserved note column runs the height of the page and sits empty
everywhere a note isn't, which on a long document is a dead strip down the
whole surface. `.k-doc` deliberately has no `grid-template-columns`, and
`test_doc_row_carries_the_note_column_not_the_document` keeps it that way.

**The kit** (all in `controls.py`): `.k-doc` (the measure) · `.k-doc-row`
(content + its note; tune with `--k-note-w`) · `.k-doc-mast` (neutral identity
band) · `.k-note` / `.k-note-title` (the margin note, separated by a neutral
rule) · `.k-fn` (footnote marker, sized in `em` so it rides its prose) ·
`.k-band` / `.k-band-v` / `.k-band-x` (a full-bleed strip of read-only figures;
compose `.k-label` for each caption, tune with `--k-band-cols`) · `.k-qa` /
`.k-qa-q` / `.k-qa-a` / `.k-qa-who` (a recorded exchange). Section labels use
the normal muted label treatment.

**Density does not drop.** A document is not a spacious version of a panel — it
carries *more*, because it has room for the qualitative half a panel grid has
nowhere to put: what is priced in, what management was asked and how they
answered, what would falsify the thesis, what was decided and why. Tables keep
their row rhythm and mono numerals unchanged. Readability comes from a
controlled measure, the shared hierarchy, and disciplined spacing — not from
changing the palette, typeface, or furniture.

**Where interaction lives.** A document is a reading surface; it does not grow
its own chat. The conversational surface is the existing Ask dock
(`pipeline/ask_dock.py`) in its `split` mode, scoped to the ticker. A document
may offer a launcher, never a second input.

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

### 7.1 Enforcement — the executable guard

Conformance is a property of **rendered output**, enforced **opt-out** by
`tests/test_ui_controls.py` (the source of truth; this prose only summarizes
it). It is not a curated allowlist — that was the old opt-in guard's blind spot,
where the shell itself shipped a legacy-alias `:root` and passed green.

- **Auto-discovery.** Every `.py` under `src/` whose source contains `var(--`
  is a CSS-emitting surface (~41). The filesystem, not an import list, is the
  set; a **new unregistered surface fails CI** until it is registered and
  classified (clean / exempt / quarantined).
- **Denied dimensions** (per surface, in CSS rules AND inline `style="…"`):
  raw color — `#hex` (incl. `var(--x, #hex)` fallbacks and hex inside
  `linear-gradient(...)`) **and raw `rgb()/rgba()/hsl()/hsla()`** (the gap that
  hid every freehand shadow / status wash / white-hover — compose `--scrim` /
  `--shadow-pop` / `color-mix(var(--token) …)` instead); off-scale `font-size` px
  (not a `TYPE_SCALE` step); off-scale `border-radius` px (not
  `--radius`/`--radius-full`); **font-family literals** — only the three font
  tokens (`var(--sans|serif|mono)`) + generic keywords are legal, any fourth
  family is the "too many fonts" drift; **`font-weight` 700/800/900/bold** (the
  kit tops out at 600); **`transition: all`** (explicit properties only, §3); and
  legacy alias var-names (`--panel`/`--ink`/`--link`/`--font-mono`/…).
- **Component dimension `kit-badge` (the §4 enforcer).** The five dimensions
  above are TOKEN-level — they catch raw literals but are *blind to a component
  reinvented from valid tokens*: a hand-rolled status pill built with `var(--bad)`
  + `var(--radius-full)` passes every one of them. `kit-badge` closes that hole. A
  CSS rule whose selector the author **named** a `pill`/`badge`/`chip`/`tag`
  (anything but the kit's `.k-pill`/`.k-chip`/`.k-well`/`.p-pill`) and which sets a
  `color-mix(in srgb, var(--ok|warn|bad) …)` **background** is a reinvented FILLED
  STATUS PILL → use `.k-pill` (+ `.k-pill-ok/-warn/-bad`). `ok/warn/bad` are
  status, never category/decoration/unread, so the check is precise: it does NOT
  fire on tone *washes* (named otherwise — `.chat-role`, `.cmt-pin`), accent
  unread/count marks (`.ix-badge`), or the §2 report category tags. Two component
  checks now auto-enforce: `kit-badge` (reinvented filled status pill) and
  **`test_buttons_compose_the_kit`** — a POSITIVE markup scan asserting every
  emitted `<button>` carries a kit class (`.k-btn`/`.k-chip`/`.k-prov-act`) or an
  allowlisted bespoke control (close glyphs, tabs, the icon toggle, the §4.1
  doorway), so a net-new freehand button fails CI. The remaining §4 drift that
  resists a precise regex (outline-tag reinvention, accent/mono used *as
  decoration*, hierarchy inversions) is caught by the **monthly semantic audit**
  (`directives/design_conformance_audit.md`, the advisory third layer) plus
  reviewer discipline. Composing the kit is the whole point.
- **Registry composition dimensions.** The same scanner also checks interior
  card/well/drawer title placement, indentation-named selectors against the
  `--indent-*` ladder, complete card-like shape signatures against their
  applicable archetype, and card-grid/split-rail columns against their own grid
  archetype. Exact selector parsing preserves `:not(...)`, attributes, native
  CSS nesting, cascade importance, and case-sensitive custom-property identity.
  Dynamic class/tag structure or incomplete headings are reported as typed
  **unverifiable markup** and fail closed; they are never silently counted clean.
- **Dimension-scoped.** Layout px (width/margin/padding/gap/top/left/height) is
  NEVER touched — only color/font/radius. The px checks anchor on `font-size:` /
  `border-radius:`.
- **Sanctioned escapes (§1) survive by construction:** the workspace
  reading/display ramp (incl. 60/100px), the `.src-chip` 8.5px+3px mark, `0.93em`
  inline mono, the `%23`-encoded chevron data-URIs, and `charts_v2.py` SVG
  internals. `tokens.py` is the one place raw hex (the palette) lives.
- **Red-state policy — allowlist-with-expiry, not block-on-merge.** Pre-existing
  drift is **quarantined per (surface, dimension)** with an owner; the quarantine
  can only **shrink** (a dimension that becomes clean must be graduated, or the
  ratchet fails). New drift in any clean surface/dimension fails immediately.
  `test_full_conformance_is_red` is an `xfail` asserting the quarantine is empty:
  it "lands red" today and flips to a hard failure once the last surface
  graduates. There is **no `--ignore` bypass** — the only way to green is to fix
  the CSS. Run `python -m pytest tests/test_ui_controls.py -q` to see the state.

## 8. Growing the language

The design vocabulary grows through one deliberate barrier: build the behavior
in `src/ui/controls.py` or the token family in `src/ui/tokens.py`; register the
typed vocabulary and any governed approval in `src/ui/design_registry.py` with
its owner and rationale; prove the contract with a self-test that turns red on
a representative violation; then regenerate the React mirror and reconcile
these bounded tables. Application surfaces compose the registered kit. They do
not create a second local vocabulary.

The registry preserves four different governance directions. The filesystem
census is exact and carries no approval metadata. Permanent exemptions and
allowlists require an owner and rationale. Quarantine is per surface and
dimension, expires, and can only shrink. The CCAction adopter floor can grow as
new callers adopt it but never forces adoption; it fails only when a pinned
caller regresses. Scanner regexes and structural definitions stay with the
scanner because they are detection logic, not approvals.

The canonical registry tables below are bounded data projections. Their row
keys are checked bidirectionally against the typed registry. The 69-path CSS
surface census is deliberately absent: `discover_surfaces()` is its live
oracle, and duplicating it in prose would recreate the drift this registry
removes.

### Shape archetypes

<!-- design-registry:shapes:start -->
| Key | Kit primitive | Sanctioned geometry |
|---|---|---|
| `macro-container` | `.k-card` | `radius-card` · `bw-thin` border · `shadow-card` |
| `micro-inset` | `.k-well` | `radius` · no border · no elevation |
| `slide-drawer` | `.k-overlay.k-drawer` | `radius-drawer` · `bw-thin` border · `shadow-drawer` |
| `control-button` | `.k-btn` | `radius` · `bw-thin` transparent border · no elevation |
| `pill-chip` | `.k-chip` / `.k-pill` | `radius-full` · outline/filled variants · no elevation |
| `micro-mark` | `.k-dot` | `radius-full` · no border · no elevation |
<!-- design-registry:shapes:end -->

### Horizontal grid archetypes

<!-- design-registry:grids:start -->
| Key | Kit primitive | Column contract |
|---|---|---|
| `shell-ground` | `.k-grid-shell` | `minmax(0, 1fr)` |
| `single-column` | `.k-grid-single` | `minmax(0, 1fr)` |
| `two-column-split-rail` | `.k-grid-split-rail` / `.k-grid-split-rail-lg` | `minmax(0, 1fr) var(--rail-sm)` / `var(--rail-lg)` |
| `three-column-matrix` | `.k-grid-matrix` | `repeat(3, minmax(0, 1fr))` |
| `auto-fit-card-grid` | `.card-grid-stat` / `.card-grid-risk` / `.card-grid-action` | `repeat(auto-fit, minmax(...))` with `var(--grid-card-sm)`, `var(--grid-card-md)`, or `var(--grid-card-lg)` |
<!-- design-registry:grids:end -->

### Indentation ladder

<!-- design-registry:indents:start -->
| Key | Value source |
|---|---|
| `indent-0` | unitless reset `0` |
| `indent-1` | `SPACING_SCALE[sp-1]` |
| `indent-2` | `SPACING_SCALE[sp-2]` |
| `indent-3` | `SPACING_SCALE[sp-3]` |
| `indent-4` | `SPACING_SCALE[sp-4]` |
<!-- design-registry:indents:end -->

### Title placement

<!-- design-registry:titles:start -->
| Key | Selector | Placement |
|---|---|---|
| `card-title` | `.k-card-title` | Interior to a card |
| `well-title` | `.k-well-title` | Interior to a well |
| `drawer-head` | `.cc-drawer-head` | Interior to a drawer |
| `peek-head` | `.cc-peek-head` | Interior to a peek |
| `section-heading` | semantic `<h2>` | Exterior section landmark |
<!-- design-registry:titles:end -->

### Governed approvals and ratchets

Permanent source exemptions remain scanner escapes, not application
precedents.

<!-- design-registry:exemptions:start -->
| Key | Owner | Rationale |
|---|---|---|
| `ui/tokens.py` | design-system | Canonical palette and scale literals |
| `report/renderers/charts_v2.py` | research-ui | Plot-geometry SVG labels and fills |
| `ui/conformance_scan.py` | design-system | Scanner diagnostics contain the discovery signal but render no surface |
<!-- design-registry:exemptions:end -->

<!-- design-registry:quarantine:start -->
| Key | Owner | Expiry / rationale |
|---|---|---|
| `pipeline/data_policy_settings_panel.py:floating-card-title` | BHA-92 | 2026-10-01 · toolbar title inside an Operations card |
| `pipeline/operations_panel.py:floating-card-title` | BHA-92 | 2026-10-01 · toolbar title inside the Operations card |
| `pipeline/ticker_command_center.py:floating-card-title` | BHA-92 | 2026-10-01 · local research drawer/card title composition |
| `report/renderers/workspace_charts.py:radius` | BHA-92 | 2026-10-01 · editorial chart micro radius |
| `report/renderers/workspace_comments.py:radius` | BHA-92 | 2026-10-01 · serialized report migration |
| `report/renderers/workspace_styles.py:radius` | BHA-92 | 2026-10-01 · editorial micro marks |
| `ui/cite_marks.py:color` | BHA-92 | 2026-10-01 · minimal-host fallback color |
| `ui/cite_marks.py:radius` | BHA-92 | 2026-10-01 · minimal-host fallback radius |
<!-- design-registry:quarantine:end -->

<!-- design-registry:bespoke-buttons:start -->
| Key | Owner | Rationale |
|---|---|---|
| `cc-drawer-close` | work-os | Named drawer close glyph |
| `tcc-drawer-close` | work-os | Named ticker drawer close glyph |
| `cc-peek-close` | work-os | Named peek close glyph |
| `cc-palette-close` | work-os | Named palette close glyph |
| `tri-d-close` | research-ui | Named triage drawer close glyph |
| `chat-close` | research-ui | Named chat close glyph |
| `cmt-close` | research-ui | Named comment close glyph |
| `ask-pop-close` | research-ui | Named Ask popover close glyph |
| `cc-palette-btn` | work-os | Icon-only command launcher |
| `cc-theme-toggle` | work-os | Icon-only theme control |
| `cc-tab` | work-os | Specialized tab control |
| `fact-doorway` | research-ui | Datum doorway into Ask |
| `prep-ask` | research-ui | Earnings-prep doorway into Ask |
| `ask-dock-ctl` | research-ui | Specialized Ask-dock control cluster |
| `up-watch-item` | research-ui | Grandfathered interactive watch row |
| `tri-text` | research-ui | Grandfathered triage text control |
| `dq-peek` | research-ui | Grandfathered data-quality peek control |
<!-- design-registry:bespoke-buttons:end -->

<!-- design-registry:mono-tables:start -->
| Key | Owner | Rationale |
|---|---|---|
| `.pfc-table` | portfolio | Headers are ticker identifiers, not prose labels |
<!-- design-registry:mono-tables:end -->

Per-surface font-size and radius sanctions are currently empty; a future entry
must include owner and rationale.

<!-- design-registry:sanctions:start -->
| Key | Owner | Rationale |
|---|---|---|
<!-- design-registry:sanctions:end -->

The CCAction floor pins current adopters without requiring adoption elsewhere.

<!-- design-registry:ccaction-floor:start -->
| Key | Owner |
|---|---|
| `dashboard/inbox.py` | work-os |
| `pipeline/advisor_memos_panel.py` | work-os |
| `pipeline/allocation_decisions_panel.py` | work-os |
| `pipeline/allocation_recommendation_panel.py` | work-os |
| `pipeline/cc_action.py` | work-os |
| `pipeline/dcf_globals_panel.py` | research-ui |
| `pipeline/decision_journal_panel.py` | research-ui |
| `pipeline/diet_panel.py` | research-ui |
| `pipeline/discovery_panel.py` | research-ui |
| `pipeline/evals_panel.py` | research-ui |
| `pipeline/explore_panel.py` | research-ui |
| `pipeline/journal_panel.py` | research-ui |
| `pipeline/ledger_panel.py` | research-ui |
| `pipeline/mobile_inbox_panel.py` | work-os |
| `pipeline/peeks.py` | work-os |
| `pipeline/portfolio_panel.py` | portfolio |
| `pipeline/position_lifecycle_panel.py` | portfolio |
| `pipeline/positioning_panel.py` | portfolio |
| `pipeline/ticker_command_center.py` | work-os |
| `pipeline/ticker_settings_panel.py` | work-os |
| `pipeline/triage_panel.py` | research-ui |
| `pipeline/validation_issues_panel.py` | research-ui |
| `pipeline/worldview_panel.py` | research-ui |
| `redteam/brief.py` | research-ui |
| `report/renderers/workspace_comments.py` | research-ui |
| `report/renderers/workspace_dcf.py` | research-ui |
| `report/renderers/workspace_decision_card.py` | research-ui |
| `ui/controls.py` | design-system |
<!-- design-registry:ccaction-floor:end -->

## 9. Rendered prose (`src/ui/prose.py`)

(Section 11 — "Streams & identity" — is owned by the S3 inbox-identity session
per §6 of the interaction-paradigm directive; this section is numbered 9 to sit
beside it.)

**One render boundary per content-kind.** Any stored analyst/LLM
**body / narrative / memo / note** that becomes HTML passes through the single
boundary `ui.prose.render_prose(md, *, inline=False)`. It renders the markdown
subset the product actually stores — headings, paragraphs, bullet lists, bold,
italic, inline code, horizontal rules, pipe tables — and **always escapes its
input first**, so it never emits unsanitized HTML (do not wrap it in a second
sanitizer). Heading levels start at `<h3>` (`#`→h3, `##`→h4, …): a panel already
owns the `<h2>` title above its prose, so content headings sit beneath it.

- **`inline=True`** runs only the span pass (bold/italic/code, no block tags)
  for `<td>`/`<p>` containers that must stay valid inline HTML; block markers
  (`##`, `-`) survive as literal text rather than break a cell.
- **Bare `escape()` of a prose field is forbidden** — it leaks raw `**bold**` /
  `##` into the page. That is the exact "markdown leaking into rendered prose"
  miss this boundary kills.
- **Do not define a second markdown renderer.** There used to be three divergent
  ones; `render_prose` is now their superset and the former server renderers
  (`workspace _render_markdown`, dashboard `light_markdown_to_html`) are thin
  re-exports. The ask-dock and iframe-chat JS `md()` functions are the ONLY
  sanctioned client renderers — **pinned inline-subset mirrors** that can't be
  server-rendered because they stream tokens and thread cite-marks client-side.
  Keep them in rough inline parity; the server side is canonical.
- **Excluded — deterministic non-markdown fields stay `escape()`d.** The
  attribution narrative and the evals judge rationale are machine-authored plain
  text with literal `*`/`#` that are NOT markdown; `render_prose` would corrupt
  them, so they remain bare `escape()`.

Enforced by `tests/test_ui_prose_boundary.py`: an opt-out scan denies the
markdown-renderer signature outside a documented four-file allowlist (the
boundary + the two JS mirrors + one markdown→plaintext stripper), and asserts
each enumerated surface routes through `render_prose` while the two excluded
fields keep `escape()`. (The shared `.prose` container styling lands in
`controls.py` with the rest of the control kit — appended on the S1 token-kit
merge per §6 of the interaction-paradigm directive; until then each surface's
own container CSS styles the rendered children.)

## 10. Provenance & data quality (`src/ui/controls.py`)

**Provenance is actionable, and drift-proof by construction** (Instrument
Paradigm; the "one render boundary per content-kind" corollary applied to
data-quality rows). Any data-quality surface — the report Sources tab, the
System Validation panel, the freshness peek, the evals failed-case drawer —
renders through ONE kit, not a bespoke per-surface builder:

- **`prov_row(label, *, severity=, stamp=, note=, actions=, drawer=)`** — one
  actionable row: a severity tick (when `severity` is given) + label + a
  *relative* `stamp` (via `ui.time.stamp_html`) + a plain `note` aside + ≥1
  inline `actions` + an optional drill-down `drawer`. A data-quality row that
  shows a problem but offers no way to act on it (resolve / refresh / diagnose /
  open `/source`) is the inert-table miss this kills.
- **`prov_drawer(summary, items)`** + **`prov_case(title, *, score=,
  rationale=, expected=, actual=)`** — the collapsible drill-down (the proven
  evals failed-case drawer, lifted): a `<details>` of per-case rows showing
  judge rationale + an expected/actual split. `expected`/`actual` are machine
  text — `escape()`d, never `render_prose`'d (§9 exclusion).
- **`prov_action(label, *, href= | post_url=, post_body=, issue_id=)`** — one
  inline action: a `/source`-style deep LINK, or a resolve/refresh BUTTON
  carrying its endpoint + JSON body as `data-prov-*` attributes for the
  surface's delegated listener (the freshness-peek streaming contract, or the
  resolve fetch).

**Severity color derives from the live enum — never a hand-typed set.**
`prov_severity_tick()` resolves tone through `prov_severity_tone()`, which keys a
map on `models.validation.Severity` (`HALT`→bad, `WARN`→warn). The writer emits
`halt`/`warn` (`record_validation_issue`); the report renderer used to match a
dead `{error, warning}` vocabulary, so a **HALT issue rendered muted grey** and
the `ORDER BY` severity sort was a silent no-op — the *worst* issues hidden. The
fix is structural: the renderer tone map AND the Sources `ORDER BY` CASE both
build from the enum (`SEVERITY_ORDER`), and an unknown severity degrades to a
neutral tick instead of mis-coloring. `tests/test_provenance_severity_contract.py`
asserts both maps cover every `Severity` member — add a severity and it lands
red until mapped.

**Resolution is a first-class verb.** `validation_issues` carries
`resolved_at` / `resolved_by` / `resolution_note` (alembic 0094); the resolve
writer (`src/validation_issues_store.py::resolve_validation_issue`, idempotent —
scoped to `resolved_at IS NULL`) is wired behind `POST /actions/resolve-issue`
(a synchronous write, not a streamed job). A `prov_row`'s resolve `prov_action`
POSTs it.

---

## 11. Streams & identity

The inbox/feed is **one ranked stream of items with stored identity**, not a
union of source tables wearing a render-time costume (Instrument Paradigm
Law 1 — *identity over source*). Sources of truth in code:
`src/dashboard/inbox.py` (the item model + renderer) and
`src/dashboard/inbox_rank.py` (identity resolvers + the transparent scorer).

**A stream item's category, label, and actions derive from WHAT it is, not
the table it was UNION-ed out of.** Each `InboxItem` carries a `semantic_kind`
discriminator — stamped at *collect* time from the source row's provenance
(`note_semantic_kind()` reads an analyst note's `source` / `source_ref` /
`context`; a `thesis_ledger` echo reads its `entry_kind`) — orthogonal to the
`kind` lane (which card renders / which JS owns it). The advisor's
memory-everywhere write echoes ONE memo through both `analyst_notes` and
`thesis_ledger_entries`; both echoes get `SEMANTIC_ADVISOR_MEMO`, so the memo
ranks and labels identically whichever table surfaced it.

- **One label resolver.** `inbox_rank.inbox_label(item)` is the single
  human-facing kind label; `inbox._title_for` delegates to it FULLY. It maps
  identity → caption: advisor/synthesis memos → "Advisor memo"; analyst-note
  kinds → a human caption (`observation`/`watch`/… never shows raw); a
  reconcile-pending note gets the "Reconcile · " prefix. Never read a raw enum
  or source-table name into a chip.
- **Machine-authored reading ranks at synthesis weight, by identity.** A
  just-generated advisor/synthesis memo is background reading, not an event —
  `_categorize` demotes it to the synthesis category (the floor severity)
  whenever `semantic_kind` says it's a memo, not only when a ledger title
  happens to match. A portfolio memo can never float to the top of the stream.
- **No internal-format string reaches a label or body.** The write site emits
  a clean body (`"{title} — {summary}"`), and the renderer strips the retired
  `[advisor memo #N · kind]` lead tag permanently for legacy rows
  (`_display_body`, scoped to advisor identity). Guard: `test_inbox_rank` +
  `test_dashboard_inbox` assert no `observation` / `[advisor memo …` ever
  renders as a label or body, for any kind.
- **Ranking is a weighted sum of typed, dated signals**, never an equal-weight
  count — severity (category × status) × recency decay × position weight ×
  thesis relevance, each factor named in the card's `score_why` tooltip.
- **A card's affordances come from the shared kit, by identity.** Advisor-memo
  cards carry their actions as `.k-chip` controls (open-memo → the Memos
  surface; dismiss → the existing `/api/notes/<id>/archive` endpoint), not a
  bespoke per-card button system. "Record in journal / update thesis" route to
  the company/Memos surface, never a net-new write path (a portfolio-level
  memo has no company target — disclosed, not silently dropped).

`semantic_kind` is the deliberate **unified-item-model seed**: the information-
diet substrate and the S12 signals spine EXTEND this discriminator vocabulary
(and scope `_categorize`), never re-cut it and never re-sniff the source table
at render time.

---

## 12. Comments — closed under no-fit

The comment classifier is **closed under no-fit, and every commentable surface
is steerable** (Instrument Paradigm §1 — *closed under no-fit, explainable by
construction*). Sources of truth: `src/comments.py` (`IntentType` / `AnchorType`),
`execution/process_report_comments.py` (classifier + routers),
`src/user_state/notes.py` (the notes mirror). The full implementation contract
lives in `directives/report_comments_and_chat.md`.

- **A classifier always has an explicit `needs_triage` terminal.** The intent
  bucketer is forced-choice over a closed vocabulary, but that vocabulary
  ALWAYS includes `needs_triage` — both an answer the model may pick and the
  hard fallback for an unparseable / out-of-vocabulary answer. The old inert
  `return "ask_question"` default is forbidden: a directive that doesn't fit
  is parked for human disposition (the existing `data_fixes.md` backlog), not
  silently mis-routed. It is always better to triage than to mis-bucket.
- **A no-fit comment is never flattened into an inert note.** The notes mirror
  maps `needs_triage` → an open `question` (a reconcilable loop), NOT
  `observation`. The fix this enforces: an unmappable or *conditional*
  directive used to collapse to `observation` and die there.
- **Every commentable surface carries a structured anchor type — including
  computed panels.** A peek-only computed section (peers, charts) is not exempt:
  the peers panel emits `data-anchor-type="peer_comp"` so a comment on the
  comparable set classifies as `curate_peers` and routes to a *structured*
  artifact, not a memo.
- **A steerable computed section persists a re-evaluable override the routers
  mutate.** A conditional directive ("remove this section UNLESS you show
  better peers / computed multiples") is modelled as a persisted artifact with
  a machine-checkable condition (`peers_section_override` → a `peers_quality`
  flag), NOT verbatim-logged text. The accessor re-evaluates it on every build
  (`p3_data.evaluate_peers_override`), so the section hides while the bar is
  unmet and returns on its own once it is — the system *acts on the condition*.
- **Curation reuses existing structured fields before inventing new ones.**
  Peer pins APPEND to the already-whitelisted `competitive_watchlist` (reusing
  its +3 scorer coupling); only what that field can't express is new
  (`peer_exclude`, `peers_section_override`). A pinned bare ticker absent from
  the upstream pool is injected so an explicit pin always renders.

Guards: `tests/test_comment_taxonomy.py` (no-fit fallback + the notes mirror
never collapsing `needs_triage` to `observation`), `tests/test_peer_curation.py`
(the `curate_peers` routes + the override-scoring contract), and the workspace
golden (`evaluation/pane_company.html` pins the `peer_comp` anchor).

Deferred to S11 (disclosed, not dropped): a dedicated triage panel/route and
the journal-silo redesign — `needs_triage` rides the existing `data_fixes.md`
backlog + journal until then.

---

## 13. Diet-vs-alert (the information-diet substrate)

A thesis-breach ALERTER and an information-diet CURATOR are **inverse products**
and must not share one pipe. The repo's original `news` → decaying inbox scorer
→ materiality veto was built for the alerter; pushed through it, informative-
but-not-breaching signal (a sell-side downgrade, an upcoming investor day) gets
vetoed away as "not material to the thesis." Sources of truth in code:
`alembic 0095` (the `signals` table), `src/signals/store.py` (the taxonomy +
readers), `src/pipeline/diet_panel.py` (the pull surface).

**Two lanes from one typed taxonomy.** `signal_type` is STORED at ingest (not a
render-time headline regex), with `event_date` (a forward-dated item is a
queryable ROW, not LLM prose), `weight` (curation salience, decoupled from
urgency decay), and `cadence` (`quarterly`/`scheduled`/`event`).

- **The ALERT lane is the decaying PUSH lane** — thesis-breach only. It is the
  EXISTING `material_news` → trigger → alert → inbox pipeline, unchanged.
- **The DIET lane is the non-decaying PULL lane** — "what a diligent analyst
  should ingest." The diet panel reads `signals` directly: an ingest stream
  (`consensus_rating` + `general_news`, newest first) and a forward agenda
  (`investor_day`, soonest first, off the dedicated `event_date` index).

**The invariant (guard `tests/test_signals_diet_guard.py`):** a `signals` DIET
row NEVER enters the urgency-decay scorer or the materiality veto. The guarantee
is structural — a `SignalRow` is never converted into an `InboxItem`, and
`collect_inbox` never reads the `signals` table — so the order of a fixed set of
diet rows is independent of the wall clock.

- **News-mirrored vs diet-only.** Two types — `general_news`, `consensus_rating`
  — carry a `news_id` and can ALSO back an alert via the news pipeline; that
  escalation reads the `news` row, not the signal row. `_categorize` consults
  the stored `signal_type` (identity over source) to type such an alert,
  **scoped to these mirrored types** — it EXTENDS the S3 categorizer, never
  re-cuts it. The other three (`investor_day`, `buyside_rating`,
  `estimate_revision`) are diet-only and never alert.
- **Identity over source for ratings.** `yf_grades` (free, already running) is
  routed into the typed `consensus_rating` lane, replacing the render-time
  headline regex sniff with a type stored at ingest.
- **Disclosed, not promised.** `buyside_rating` + `estimate_revision` are
  scaffolded `signal_type`s with no free data path (FMP analyst is Ultimate-
  gated). The panel names them as fast-follows; it never invents the data.
- **Investor days extend the calendar.** The `investor_day` feed reuses the
  `expected_earnings` calendar machinery (`record_investor_day` is the writer
  the IR-events scrape calls), materialized into `signals.event_date` rows —
  not a greenfield event store. Post-event takeaway summarization is a later
  owner; the substrate STORES the event rows it will summarize.

---

## 14. The Discovery rule (weighted candidate ranking)

The discovery queue is a **ranked surface**, so by the Instrument Paradigm it
scores **by a weighted sum of typed, dated signals through a source-weight
registry — never an equal-weight count** — carries its `score_why`, and is
*closed by construction* (a weak name never enters; a strong one is capped to a
ranked top-N, not printed 500-deep). Sources of truth in code:
`src/discovery/scoring.py` (the engine), `src/discovery/sources.py` (the
registry), `src/discovery/store.py` (the typed rows), `src/pipeline/discovery_panel.py`
(the one lens).

**The schema is the contract** (alembic 0096–0098): a `discovery_signals`
typed child (`signal_class`, `source_key`, `weight`, `raw_strength`,
`observed_at`) holds one row per `(user, ticker, class, source)`; the
`discovery_candidates.score` is a weighted sum over those rows and `score_json`
is its breakdown; `discovery_sources` is the **editable weight registry** — one
row per factor screen, adjacency channel, and rostered 13F investor, with a
`base_weight` the owner tunes and quarterly recalibration writes.

**The score** (one place, `scoring.py`): per-signal contribution =
`weight × raw_strength × action_mult × decay`, summed into a *fundamental* term
(screens + adjacency) and an *investor* term (13F). Four shaping rules, each a
guard test, not prose:

- **Action asymmetry.** A 13F NEW initiation ≫ an incremental ADD, and the gap
  is WIDER for low-turnover long-only funds (a new Edgewood/Loomis position is
  loud; their adds are routine) than for higher-frequency hedge funds (whose
  adds are momentum signals). `trim`/`exit` are not discovery signals.
- **Investor clamp.** A name surfaced ONLY by investor research (no
  screen/adjacency corroboration) can *surface* but **cannot top the queue on
  investor weight alone** — its investor term is capped below any fundamentally-
  corroborated name.
- **Corroboration is super-linear.** 2+ distinct funds on the same name multiply
  the investor term by `n_funds ** CORROB_EXP`.
- **Recency decay.** A per-class half-life fades stale signals (a 45-day-lagged
  13F decays within ~2 quarters; screens/adjacency re-derive each run).

**Both gates, owner-decided.** Raise the *entry* threshold so weak singletons
never enter the queue (`ENTRY_THRESHOLD`; an existing candidate is still
refreshed) AND cap the *render* to a ranked top-N. Crowding is fixed at both
the generation and the display boundary.

**One band, one lens.** The panel is an instrument under the Research →
Discovery tab: `panel_toolbar()` (one operating band — `.k-chip` filter toggles
+ actions on the same row, never a title band over a filter band), `.p-table` /
`.p-pill` rows, `ticker_label()` for ticker+name, and the `score_json` evidence
collapsed behind a peek (the one-line `score_evidence_line()` inline, the full
per-signal breakdown on demand). The roster's weights are editable from the
panel's Sources surface (the existing POST route + SSE), and editing a weight
re-ranks — the guard asserts exactly that.

**13F sourcing is top-of-funnel, never a trigger** (the rule the miner
encodes): EDGAR 13F-HR direct (free; FMP `form13F` is Ultimate-gated), with a
45-day lag, longs-only, and non-US/sub-$100M managers invisible. A move on a
TRACKED ticker is a `news` row; a move on an UNTRACKED ticker is a
`discovery_signals` row — this surface never writes the information-diet
`signals` table.

---

## Appendix A — fresh-eyes audit (2026-06-11): historical note

> **Superseded — no longer the source of truth.** This appendix once held a
> file:line catalogue of the primitive-literal drift this design language was
> written to kill — headline counts against `origin/main` @ `5d37519`: ≈241
> hardcoded `font-size: *px` declarations across 24 files, ≈240 raw hex colors
> outside `tokens.py`, 45 native `<select>/<input>` emissions with ~10 ad-hoc
> skins and zero custom chevrons, ≥6 button treatments, and ≥9 chip variants
> across 5 radii. That was a point-in-time snapshot; it went stale the moment
> work landed, so the catalogue has been **removed rather than hand-maintained**
> (its warning predicted exactly this).
>
> The live, authoritative inventory of remaining drift is the **executable
> guard** (§7.1 above — `tests/test_ui_controls.py`): its `QUARANTINE` map is
> the current burn-down list, ratcheted shrink-only, and
> `test_full_conformance_is_red` flips from `xfail` to a hard failure the moment
> it empties. Run `python -m pytest tests/test_ui_controls.py -q` for the exact
> state — never re-derive it here.
>
> Burn-down lineage: **S1** unforked the shell namespace and added the
> `.k-well/.k-pill` status kit; **S7** swept the dashboard / cockpit / pipeline
> long-tail onto tokens and `color-mix` (deleting the reinvented pill systems);
> the provenance / evals / sources console (**S10**) and the report iframe /
> editorial surfaces are the last quarantined surfaces. The **`kit-badge`
> component dimension** (added 2026-06-15) seeded four reinvented filled-pill
> surfaces — command-center `allocation .ad-pill` / `position_lifecycle .plc-pill`
> and report `workspace_comments .cmt-*` / `workspace_styles .decision-badge` —
> all four have since **graduated onto `.k-pill`**, so `kit-badge` now carries no
> quarantine: a new reinvented filled status badge in any surface fails on merge.
