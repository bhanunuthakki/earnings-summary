# @earnings-summary/design-system

A self-contained npm package used as a **prototyping sandbox** for `claude.ai/design` via the
`/design-sync` skill. It is not a replacement for the Flask surfaces earnings-summary actually
ships. The Python design system under `src/ui/` (one level up, in the main repo) is canonical —
tokens and components are defined there first; changes flow forward into this package (generated
tokens, hand-ported controls), not the other way. Full plan:
`../docs/design_system_react_port_plan.md`.

## What's in it

- **Tokens** (`src/tokens/`) — generated from `src/ui/tokens.py` by `scripts/gen_design_tokens.py`.
  A four-step type scale (`--fs-display` 22 · `--fs-title` 16 · `--fs-body` 13 · `--fs-caption` 11),
  one de-emphasis gray (`--muted`), one status family (`--ok`/`--warn`/`--bad`), and `@kind`
  annotations on the font + motion tokens. **Do not hand-edit** the two files under `src/tokens/`.
- **Controls** (`src/styles/controls.css`) — a port of `src/ui/controls.py`'s `controls_css("paper")`:
  the form-control baseline, a native-chrome kill list (themed scrollbars, kit-drawn checkbox/radio,
  hidden spinners/date-indicator, the one `--k-chevron` on `<select>` and `<summary>`), and the
  `.k-*` component classes. One deliberate divergence from canonical: the native `select[multiple]`
  rules are dropped in favor of the `<MultiSelect>` composite.
- **Components** (`src/components/`) — `Button`, `Pill`, `Chip`, `Well`, `Dot`, `NumText`,
  `TickerLabel`, `Label`, `Menu`, `Toolbar`, `Input`, `Textarea`, `Select`, `MultiSelect`,
  `DateField`, plus `ThemeProvider`. `Select`/`MultiSelect` are popover composites (a `.k-trigger`
  opening the shared `.k-menu`, full keyboard nav); `Select` takes a `native` escape hatch.
- **`ThemeProvider`** — stamps `data-theme`. Use `target="document"` for the dark instrument-panel
  theme; the default wrapper mode does not activate `:root[data-theme=…]` overrides.

## Build

```sh
npm install
npm run build   # -> dist/index.js, dist/index.d.ts, dist/index.css
npm run check   # tsc --noEmit + the token/off-system lint (scripts/check-tokens.mjs)
```
