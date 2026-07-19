# earnings-summary kit — build conventions

Voice: **calm desk, deep drawers** — a quiet, dense instrument panel. Premium = restraint: one accent, one radius, one shadow, mono only where mono means something.

## Setup

Import the stylesheet once (`@earnings-summary/design-system/index.css`, shipped here as `styles.css`) — components need no provider. The default palette is the light "paper" theme on `:root`. For the product's real daily face (the dark instrument panel), wrap the app once in `<ThemeProvider theme="dark" target="document">…</ThemeProvider>`. **`target="document"` is required for theming**: token overrides are scoped `:root[data-theme=…]`, so the default wrapper-div mode does NOT recolor descendants.

## Styling idiom: kit components + CSS custom properties, zero raw hex

Use the React components for anything they cover — `Button`, `Pill`, `Chip`, `Well`, `Dot`, `NumText`, `TickerLabel`, `Label`, `Menu`, `Toolbar`, `Input`, `Textarea`, `Select`, `MultiSelect`, `DateField`, `ThemeProvider`. Your own layout glue (grids, flex rows, panels) styles ONLY with these tokens — never a hex/rgb literal, never an off-scale px font size:

- Surfaces: `var(--bg)` page → `var(--surface)` panel → `var(--paper)` inset, in that order. Lines: `var(--hairline)` row rules, `var(--border)` boxes, `var(--border-2)` hover/strong.
- Ink: `var(--fg)`, `var(--fg-soft)`, `var(--muted)` — one gray of de-emphasis (the old `--muted-2` was folded into `--muted`).
- Status: `var(--ok)` / `var(--warn)` / `var(--bad)` — green=good, red=bad, everywhere (the old `--pos/--neg/--neu` aliases were folded into these). Soft fills via `color-mix(in srgb, var(--ok) 16%, transparent)`; the old `--tone-*` fills use this idiom now.
- `var(--accent)` is RESERVED for interactive/selected/unread — never decoration. Ink on accent fill: `var(--accent-contrast)`.
- Type — FOUR steps (size = importance, not surface): `var(--fs-display)` 22px one dominant element · `--fs-title` 16 panel titles + real headings · `--fs-body` 13 default UI + reading prose · `--fs-caption` 11 everything smaller (metadata, chips, badges, labels). Fonts: `var(--sans)` UI, `var(--serif)` reading prose, `var(--mono)` ONLY tickers/numbers/timestamps/code. (Uppercase+letterspacing is reserved for Label / Chip / Pill.)
- Chrome: `var(--radius)` (8px) every box; `var(--radius-full)` pills/dots only; spacing `var(--sp-1)`…`var(--sp-6)`; motion `var(--transition)` with explicit properties (never `transition: all`); one popover shadow `var(--shadow-pop)`. The kit owns every native widget — themed scrollbars, kit-drawn checkbox/radio, one chevron on `<select>`/`<summary>`, hidden number spinners + date indicator — so a design never inherits an OS accent.

Component quick-map: filled status badge → `<Pill tone="ok|warn|bad|accent">`; outline tag/filter → `<Chip tone=… mono interactive on>`; callout block → `<Well tone=…>`; status dot → `<Dot tone=… size=…>`; signed number → `<NumText value=…>` (green/red by sign); ticker+company → `<TickerLabel ticker name href>` (never concatenate `"NU · Nu Holdings"` by hand); single-choice dropdown → `<Select options=… value onChange>` (a `.k-trigger` + `.k-menu` popover; pass `native` for a plain `<select>`); multi-choice → `<MultiSelect options=… value onChange>` (chips summary + kit-checkbox popover — never `<select multiple>`); date → `<DateField>`; popover list on its own → `<Menu items=…>`; panel header band → `<Toolbar title filters={<Chip…>} actions={<Button…>}>` — title left, controls same row, never stacked. One `<Button variant="primary">` per view; everything else `quiet`; `danger` only destructive; `size="sm"` in table rows.

## Where the truth lives

Read `styles.css` and its import `_ds_bundle.css` (all tokens under `:root` / `:root[data-theme="dark"]` / `:root[data-theme="white"]`, plus every `.k-*` class). Per-component API: `<Name>.d.ts`; usage: `<Name>.prompt.md`.

## Idiomatic example

```tsx
<div style={{ background: "var(--surface)", borderRadius: "var(--radius)", padding: "var(--sp-4)" }}>
  <Toolbar
    title="Portfolio (11)"
    filters={<><Chip interactive on>All 14</Chip><Chip interactive>Earnings 3</Chip></>}
    actions={<Button variant="quiet" size="sm">full feed</Button>}
  />
  <div style={{ display: "flex", alignItems: "baseline", gap: "var(--sp-3)" }}>
    <TickerLabel ticker="NU" name="Nu Holdings" />
    <Pill tone="warn">warn</Pill>
    <NumText value={18.4} format={(n) => `+${n}%`} />
  </div>
</div>
```
