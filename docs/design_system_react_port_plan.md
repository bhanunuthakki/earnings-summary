# React Design-System Port — Execution Plan

**Target:** a standalone, esbuild-buildable React/TypeScript package mirroring the `src/ui/` design
system, with a `dist/` consumable by the `/design-sync` skill (claude.ai/design), used as a
prototyping sandbox — NOT a replacement for the Flask surfaces the product actually ships.

Scoped by a Fable planning pass, 2026-07-18. Companion to the direct-on-stack UX audit fixes
(inbox chips, contrast, a11y, legacy-alias unfork — PR #928) which improve the shipped app
directly and do not depend on this project.

---

## 0. Ground truth (read before any phase)

- Tokens: `src/ui/tokens.py` — `PALETTE_LIGHT`, `PALETTE_DARK`, `PALETTE_WHITE_OVERRIDES`,
  `FONT_TOKENS`, `TYPE_SCALE` (6 steps), `SPACING_SCALE` (sp-1..6), `CHROME_TOKENS` (radius 8px /
  radius-full 999px / transition 150ms), `CHART_SERIES` (Okabe-Ito), `_FAVICON_SVG`. Emission:
  `palette_css("paper"|"dark")` — paper mode emits `:root` light + `[data-theme="white"]` +
  `[data-theme="dark"]` override blocks.
- Components: `src/ui/controls.py` — `_CONTROLS_BODY` is the exact CSS to replicate; the docstring
  enumerates the kit.
- Spec: `directives/design_language.md` — type-encodes-importance, zero-raw-hex, one radius,
  `color-mix` 16%/45% status fills, accent-is-interactive-only, "calm desk, deep drawers."
- Enforcement precedent: `tests/test_ui_controls.py` — the drift-guard pattern the React side
  mirrors.

---

## 1. Package shape & location — decision

**A new subdirectory in this repo — `design-system/` — as a self-contained npm package. No
monorepo tooling (no pnpm workspaces, no turborepo, no nx).**

Rationale / tradeoff:
- The token generator (Phase 1) must import `src/ui/tokens.py`. Same-repo keeps generation +
  drift-check a single CI job with zero cross-repo versioning. A separate repo would need a
  published token artifact or a submodule — pure overhead for a solo, localhost project.
- Cost of same-repo: JS toolchain files appear in a Python repo (`node_modules/`, lockfile).
  Mitigate by fully containing everything under `design-system/` — its own `.gitignore` entries
  (`design-system/node_modules/`, `design-system/dist/`), its own `package.json`. The Python side
  never imports from it; the only coupling is the one-way token generator.
- No workspace tooling because there is exactly one JS package. If a second ever appears, revisit
  then.

Layout:

```
design-system/
  package.json
  tsconfig.json
  build.mjs                  # esbuild script
  .storybook/                # Phase 4 (optional but recommended)
  scripts/
    check-tokens.mjs         # drift check: generated file matches committed file
  src/
    tokens/
      tokens.generated.ts    # GENERATED from src/ui/tokens.py — do not hand-edit
      tokens.css             # GENERATED — palette_css("paper") output, verbatim
      index.ts               # hand-written re-exports / helpers (theme types, tone type)
    styles/
      controls.css           # ported _CONTROLS_BODY (kept close to verbatim)
    components/
      Button.tsx  Pill.tsx  Chip.tsx  Well.tsx  Dot.tsx  TickerLabel.tsx
      Label.tsx  Menu.tsx  Toolbar.tsx  Prose.tsx  NumText.tsx
      Input.tsx  Select.tsx  Overlay.tsx  ProvRow.tsx ...
    index.ts                 # public barrel export
  stories/                   # *.stories.tsx mirroring real surface usage
  dist/                      # build output (gitignored)
scripts/
  gen_design_tokens.py       # Python-side generator (lives with the Python it reads)
```

---

## 2. Build tooling — decision

- **Package manager: npm** (ships with Node, zero extra install, solo project — pnpm's wins don't
  matter at one package).
- **Build: a small `build.mjs` esbuild script**, not tsup/vite. `/design-sync` needs an
  esbuild-bundlable package; the simplest guarantee is building with esbuild itself:
  - Entry `src/index.ts` → `dist/index.js` (ESM, `external: ["react", "react-dom"]`), plus
    `dist/index.css` (tokens.css + controls.css concatenated via esbuild CSS entry or a copy step).
  - Types: `tsc --emitDeclarationOnly --outDir dist` (esbuild doesn't emit `.d.ts`).
  - `package.json`: `"type": "module"`, `main`/`module`/`types`/`exports` pointing at `dist/`,
    `sideEffects: ["*.css"]`, `peerDependencies: react >=18`.
- **TypeScript:** strict, `jsx: "react-jsx"`, `target: "es2022"`, `moduleResolution: "bundler"`. No
  path aliases (keeps esbuild config trivial).
- **CSS strategy — plain CSS with custom properties, NOT CSS-in-JS and NOT CSS modules.** The
  Python system is class-based CSS keyed off custom properties; porting it verbatim (`.k-btn`,
  `.k-pill-ok`, …) maximizes fidelity, makes diffing against the Python CSS mechanical, and keeps
  the drift-guard idea (regex over CSS text) portable. Components are thin typed wrappers that
  compose the same class names.
- **Storybook: yes, lazily** — Storybook 8 + `@storybook/react-vite`, added in Phase 4. It is the
  fidelity-review surface and the local dev harness; it is not needed for the first `/design-sync`.
  Keep it a devDependency; it must not affect `dist/`.
- Scripts: `npm run build` (esbuild + tsc types), `npm run check` (tsc --noEmit + token drift
  check), `npm run storybook`.

---

## 3. Token translation — decision

**A Python generation script, CI-checked — not a manually-synced fork.**

`scripts/gen_design_tokens.py` (stdlib only, importable with `src/` on path):

1. Imports `PALETTE_LIGHT`, `PALETTE_DARK`, `PALETTE_WHITE_OVERRIDES`, `FONT_TOKENS`, `TYPE_SCALE`,
   `SPACING_SCALE`, `CHROME_TOKENS`, `CHART_SERIES` from `ui.tokens`.
2. Emits `design-system/src/tokens/tokens.css` = literally `palette_css("paper") + the
   color-scheme/chevron head from controls_css("paper")` — the same function output the Flask
   pages inline, so the CSS custom-property layer is *identical by construction*, including the
   `[data-theme="white"]`/`[data-theme="dark"]` blocks.
3. Emits `design-system/src/tokens/tokens.ts` — typed constants (`export const PALETTE_LIGHT =
   {...} as const`, `TYPE_SCALE`, `CHART_SERIES`, plus derived `type Theme = "paper" | "white" |
   "dark"`, `type Tone = "ok" | "warn" | "bad" | "accent"`), each file headed with `/* GENERATED
   from src/ui/tokens.py — do not edit. Regenerate: python scripts/gen_design_tokens.py */`.
4. `--check` mode: regenerates to memory and diffs against the committed files; nonzero exit on
   mismatch.

Drift enforcement: a new Python test `tests/test_design_tokens_export.py` that runs the generator
in `--check` mode — so the *existing* pytest CI (the same run that executes
`test_ui_controls.py`) fails the moment `tokens.py` changes without regeneration. Generated files
ARE committed (the JS build never needs Python).

Why not a manual fork + drift test: the drift test would itself need to read `tokens.py` and
compare — i.e., 90% of the generator — while still requiring hand-editing on every change.
Generation is the same effort with zero hand-sync.

Scope note: only tokens are generated. `controls.css` is a **one-time hand port** of
`_CONTROLS_BODY` with a lighter guard (Phase 5) — its CSS is stable and mixed with HTML-emitting
Python, so full generation isn't worth it.

---

## 4. Component inventory & priority

### (a) Must-port — first useful sync ("kit core")

| React component | Source (`controls.py`) | Notes |
|---|---|---|
| `Button` | `.k-btn` + `-primary/-quiet/-danger/-sm`, `[disabled]` | `variant`, `size`, `disabled` props |
| `Pill` | `.k-pill` + `-ok/-warn/-bad/-accent` | `tone?: Tone` (absent = neutral), mirrors `pill_tone_class` |
| `Chip` | `.k-chip` + tones, `-mono`, `.k-chip-btn`/`.is-on` | `tone`, `mono`, `interactive`, `on` props |
| `Well` | `.k-well` + tones | block sibling of Pill |
| `Dot` | `.k-dot` + `-ok/-warn/-bad/-muted` | `size` prop → `--k-dot-size` |
| `TickerLabel` | `.k-tick` + `ticker_label()` | `ticker`, `name?`, `href?`, `nameMax?`; symbol-only link, full name in `title` |
| `Label` | `.k-label` | uppercase field caption |
| `NumText` | `.k-num-pos/-neg` | or a `numTone()` classname helper |
| `Input`, `Select`, `Textarea` | the form-control baseline | thin styled wrappers; Select re-implements the chevron data-URI (`--k-chevron`) |
| `Toolbar` | `.k-toolbar` + `panel_toolbar()`/`panel_section_title()` | `title`, `suppressTitle`, `filters`, `actions` slots |
| `Menu` | `.k-menu` | presentational list; selection state via prop |
| Theme scaffold | `palette_css` contract | `ThemeProvider`/`data-theme` setter + the tokens.css import |

Also port the tone helpers as pure TS: `thesisStatusTone()`, `pillToneClass()`, `chipToneClass()`
(from `controls.py`), so prototypes speak the same status vocabulary.

### (b) Second wave — port after core syncs

- `Overlay`/`Scrim` (`.k-scrim`/`.k-overlay` + a React re-imagining of the CCOverlay dismissal
  contract: Esc, click-out, focus trap — React idioms replace the JS stack; keep the priority-ladder
  semantics only if prototypes need stacked surfaces).
- `Prose` (`.prose` container styles + a TS port of `render_prose`'s markdown subset, or
  `dangerouslySetInnerHTML` over pre-rendered HTML for fixtures).
- Provenance kit: `ProvRow`, `ProvTick`, `ProvAction`, `ProvDrawer`, `ProvCase` (`.k-prov*`) —
  severity tone map ported as a TS literal (`halt→bad, warn→warn`, unknown→muted).
- `Table` (`.p-table`) + `PPill` (`.p-pill`).
- `DataGrid` — React-native replacement for `living_grid.py` (sort/filter in React state; do NOT
  port Alpine). Only the CSS hooks (`.lg-*` look) carry over.
- `SourceChip` (`src/ui/source_chip.py` anatomy: 8.5px chip + details-popover) and `CiteMark`
  (`src/ui/cite_marks.py` hover popover) — worth porting because they're distinctive product
  primitives, but they need data mocking.
- Workspace/editorial register: a `workspace.css` slice (reading ramp, display ramp, tone washes,
  dark-fixed) as an opt-in stylesheet + a few components (section title, identity header) — only if
  the user starts prototyping report surfaces.

### (c) Not worth porting as reusable components

- `htmx_runtime.py`, Alpine vendoring, `CCOverlay`'s cross-document registration machinery
  (server-runtime concerns).
- Surface layouts: `command_center_shell.py`, `inbox.py`, `research_cockpit.py`,
  `analytical_dashboard_html.py` bodies — these are apps, not kit. (Their *patterns* become
  Storybook stories instead.)
- `fact_anchor_attrs` / doorway rails (`data-peek-url`/`data-ask-q`/`data-fact-ref`) — meaningless
  without the shell's delegated handlers; a prototype uses plain onClick.
- Server helpers: `ui/time.py` stamp rendering (port as a tiny `relTime()` util only if a story
  needs it), favicon/page-title branding, `prov_action`'s `data-prov-post` wire format.
- `charts_v2` SVG internals — out of scope; expose `CHART_SERIES` tokens only.

---

## 5. Fidelity verification

Three layers, cheapest first:

1. **CSS text diff (mechanical, CI):** a script (`design-system/scripts/check-css-parity.mjs` or a
   Python test) extracts each `.k-*` rule block from `controls.py::_CONTROLS_BODY` and from
   `design-system/src/styles/controls.css` and diffs normalized declarations. Because the port
   strategy is verbatim class-based CSS, this catches ~all value drift for free — it is the
   React-side analog of `test_ui_controls.py`. Allowlist deliberate divergences in the script with
   a comment per entry.
2. **Storybook stories mirroring real surfaces (human review):** each component gets stories
   reproducing actual usage found in the consuming surfaces (e.g., the cockpit's pill row, inbox
   chips, a `panel_toolbar` with filters+actions, ticker labels in a picker), each in all three
   themes via a theme toolbar. Reviewer opens the Flask page and the story side-by-side once per
   component.
3. **Screenshot diff (optional, deferred):** a fixture HTML page generated from the Python kit (a
   "kit sheet" route or static file rendering every `.k-*` variant) vs. the same grid rendered in
   Storybook, compared with Playwright + pixelmatch. Do this only if manual review proves
   error-prone — it's the highest-maintenance layer and both DOMs must be pixel-aligned to compare,
   which costs real effort. Not a Phase-1..3 requirement.

Also carry over the *spirit* of the Python guard: a small JS-side lint (`scripts/check-tokens.mjs`
extension) that scans `design-system/src/**` for raw hex outside `tokens.generated.*`, off-scale
`font-size` px, off-scale `border-radius` px, `transition: all`, and `font-weight: 700+` — the same
denied dimensions as `tests/test_ui_controls.py` §7.1, run in `npm run check`.

---

## 6. Phased rollout (subagent task lists)

### Phase 0 — Scaffold (1 agent, blocking prerequisite for all else)
- T0.1 Create `design-system/` with `package.json` (npm, ESM, react peer dep), `tsconfig.json`,
  `build.mjs` (esbuild bundle w/ react external + CSS output + `tsc --emitDeclarationOnly`), root
  `.gitignore` additions.
- T0.2 Stub `src/index.ts`, verify `npm run build` produces `dist/index.js` + `dist/index.d.ts` +
  `dist/index.css` from a hello-world component.
- T0.3 Add a `design-system/README.md` stating: prototyping sandbox, generated-token contract,
  "Python side is canonical."

### Phase 1 — Tokens (1 agent; only depends on Phase 0 for file locations)
- T1.1 Write `scripts/gen_design_tokens.py` (emit tokens.css verbatim from `palette_css("paper")` +
  chevron head; emit tokens.ts typed constants; `--check` mode).
- T1.2 Commit generated `tokens.css` / `tokens.generated.ts`; hand-write `src/tokens/index.ts`
  (Theme/Tone types, re-exports).
- T1.3 Add `tests/test_design_tokens_export.py` invoking `--check` (registers the JS package in the
  existing pytest CI).
- T1.4 `scripts/check-tokens.mjs`: raw-hex / off-scale / transition-all scan of
  `design-system/src/**`; wire into `npm run check`.

### Phase 2 — Kit core components (parallelizable across 3–4 agents once Phase 1 lands)
Shared first task: T2.0 port `_CONTROLS_BODY` → `src/styles/controls.css` (one agent, verbatim,
keep section comments; strip Python-only bits like the mobile 16px rule if desired — decide and
note in the file header). Then in parallel:
- Agent A: `Button`, `Pill`, `Chip`, `Well`, `Dot`, `NumText`, tone helpers (`thesisStatusTone`
  etc.).
- Agent B: `TickerLabel`, `Label`, `Menu`, `Toolbar` (+ section-title suppression semantics).
- Agent C: `Input`/`Select`/`Textarea` wrappers + chevron handling + theme scaffold (`data-theme`
  provider, tokens.css/controls.css import wiring in `index.ts`).
- Each agent: typed props mirroring the Python helper signatures, JSDoc quoting the relevant
  `design_language.md` rule, export from the barrel.
- T2.9 (any agent, last): `check-css-parity` script comparing controls.css to `_CONTROLS_BODY`.

### Phase 3 — First design-sync (1 agent, after Phase 2)
- T3.1 `npm run build`; verify `dist/` matches what the `/design-sync` skill expects (compiled ESM
  + css + types); run the sync; fix packaging issues (`exports` map, css side-effects) until
  components appear in claude.ai/design.
- T3.2 Document the sync procedure in `design-system/README.md`.

### Phase 4 — Storybook + fidelity review (1–2 agents, parallel with Phase 3)
- T4.1 Storybook 8 (react-vite builder), theme toolbar switching `data-theme` on the preview html,
  tokens/controls css loaded globally.
- T4.2 Stories per component mirroring real surface usage (source the exact markup patterns from
  `command_center_shell.py` / `inbox.py` / `research_cockpit.py` grep hits); a "kit sheet" story
  showing every variant × theme.
- T4.3 Manual side-by-side pass vs. the running Flask app; log divergences as issues; fix.

### Phase 5 — Second wave (parallelizable, on demand)
- T5.1 `Overlay`/`Scrim` with React dismissal (Esc, click-out, focus trap, reduced-motion).
- T5.2 Provenance kit (`ProvRow`/`ProvTick`/`ProvAction`/`ProvDrawer`/`ProvCase`) + severity tone
  map.
- T5.3 `Prose` + TS `renderProse` inline/block subset.
- T5.4 `Table`/`PPill`, `DataGrid` (React state, `.lg-*` look).
- T5.5 `SourceChip` + `CiteMark` with mocked payload types.
- T5.6 (optional) workspace editorial slice; (optional) Playwright screenshot-diff harness.

---

## 7. Ongoing drift risk — the containment story

1. **Tokens can't drift**: generated + `--check` in the existing pytest CI (Phase 1). This is the
   load-bearing guarantee.
2. **Component CSS drift is detected, not prevented**: `check-css-parity` diffs `.k-*` blocks
   against `_CONTROLS_BODY` per declaration. When the Python side changes a control (as the
   just-shipped UX pass did), CI on the Python side won't know about the JS side — so run the
   parity check in the same pytest CI too (a thin Python test shelling `node
   scripts/check-css-parity.mjs`, skipped gracefully if node is absent locally but required in CI).
   Divergence then requires an explicit allowlist entry — mirroring the Python guard's "quarantine
   shrinks only" ethos.
3. **The JS side gets its own §7.1-style token guard** (`check-tokens.mjs`): no raw hex, off-scale
   sizes, `transition: all`, heavy font weights — so prototypes built in the sandbox can't quietly
   grow off-system values that later get "ported back."
4. **Directionality is policy, written down**: `design-system/README.md` states the Python system
   is canonical; changes land in `tokens.py`/`controls.py` first and flow forward via the
   generator/parity check. A prototype that discovers a *better* design comes back as a Python
   change, never as a React-only fork.
5. **Deliberate non-parity is documented per item**: the sanctioned divergences (no Alpine grid,
   React-native overlay dismissal, no doorway rails, workspace type ramp) live in a "Divergences"
   section of the README so future agents don't "fix" them toward false parity or mistake them for
   drift.
6. **Version stamp**: the generator embeds the source `tokens.py` git hash in the generated file
   headers, so a stale `dist/` synced to claude.ai/design is diagnosable at a glance.

---

### Critical files referenced

- `src/ui/tokens.py` — the generator's input; every palette/scale dict and `palette_css()`.
- `src/ui/controls.py` — `_CONTROLS_BODY` (the CSS to port verbatim) + helper signatures the React
  props mirror.
- `directives/design_language.md` — the rules each component's JSDoc cites; source of the
  denied-dimension list for the JS guard.
- `tests/test_ui_controls.py` — the enforcement pattern to mirror in `check-tokens.mjs` / the
  parity test.
- `src/report/renderers/workspace_styles.py` — the editorial register (Phase 5.6 scope; documents
  the sanctioned type-ramp divergence).
