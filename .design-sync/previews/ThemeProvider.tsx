/**
 * ThemeProvider preview — document-target theming. The generated tokens.css
 * scopes theme overrides to `:root[data-theme=...]`, so only
 * `target="document"` recolors anything (the default wrapper-div mode does
 * not — see .design-sync/NOTES.md). DarkDocument is the primary story: the
 * whole card renders in the dark palette, the product's daily face.
 */
import * as React from "react";
import {
  Button,
  Pill,
  ThemeProvider,
  TickerLabel,
  Toolbar,
} from "@earnings-summary/design-system";

/**
 * The capture shell hardcodes `body{background:#fff}` (an inline style, so
 * `:root` token flips can't repaint it). The story therefore paints its own
 * surface with the theme tokens — `var(--bg)` / `var(--fg)` resolve to the
 * dark palette because `target="document"` stamps `data-theme` on `:root`.
 */
function PanelSample() {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        maxWidth: 420,
        padding: 16,
        background: "var(--bg)",
        color: "var(--fg)",
        border: "1px solid var(--border)",
        borderRadius: 8,
      }}
    >
      <Toolbar
        title="Portfolio pulse"
        actions={
          <Button variant="quiet" size="sm">
            full feed
          </Button>
        }
      />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <TickerLabel ticker="NU" name="Nu Holdings" />
        <Pill tone="ok">On thesis</Pill>
        <Pill tone="warn">KPI drift</Pill>
        <Pill tone="bad">Break rule</Pill>
      </div>
    </div>
  );
}

export function DarkDocument() {
  return (
    <ThemeProvider theme="dark" target="document">
      <PanelSample />
    </ThemeProvider>
  );
}

export function WhiteDocument() {
  return (
    <ThemeProvider theme="white" target="document">
      <PanelSample />
    </ThemeProvider>
  );
}
