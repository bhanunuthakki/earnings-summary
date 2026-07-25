/**
 * GENERATED from src/ui/tokens.py — do not hand-edit.
 * Regenerate: python scripts/gen_design_tokens.py
 */

export const PALETTE_LIGHT = {
  "bg": "#fafaf7",
  "surface": "#ffffff",
  "paper": "#f4f3ef",
  "fg": "#0d0d0c",
  "fg-soft": "#2d2a23",
  "muted": "#6f6b60",
  "border": "#e4e3dd",
  "border-2": "#d1cfc7",
  "hairline": "#ecebe5",
  "accent": "#1d4ed8",
  "accent-soft": "#eef2ff",
  "accent-contrast": "#ffffff",
  "ok": "#15803d",
  "warn": "#b97c00",
  "bad": "#b91c1c",
  "mark": "#8a5a28",
  "mark-soft": "#c8ad8a",
  "seg-1": "#0d0d0c",
  "seg-2": "#443f34",
  "seg-3": "#7d7869",
  "seg-4": "#b5b0a4",
  "seg-5": "#dcdcd7",
  "shadow-pop": "0 12px 32px rgba(15, 15, 20, 0.18)",
  "scrim": "rgba(0, 0, 0, 0.5)",
} as const;

export const PALETTE_WHITE_OVERRIDES = {
  "bg": "#ffffff",
  "paper": "#fafaf7",
  "hairline": "#efeeea",
} as const;

export const PALETTE_DARK = {
  "bg": "#0d0d0c",
  "surface": "#141412",
  "paper": "#191815",
  "fg": "#f4f3ef",
  "fg-soft": "#d3cec3",
  "muted": "#8b877d",
  "border": "#272621",
  "border-2": "#38342b",
  "hairline": "#1c1b18",
  "accent": "#8aa8ff",
  "accent-soft": "#1c2138",
  "accent-contrast": "#0d0d0c",
  "ok": "#4ade80",
  "warn": "#f5c66a",
  "bad": "#f08a8a",
  "mark": "#b08d5f",
  "mark-soft": "#705737",
  "seg-1": "#f4f3ef",
  "seg-2": "#b5b0a4",
  "seg-3": "#7d7869",
  "seg-4": "#443f34",
  "seg-5": "#262219",
  "shadow-pop": "0 12px 32px rgba(0, 0, 0, 0.45)",
  "scrim": "rgba(0, 0, 0, 0.5)",
} as const;

export const FONT_TOKENS = {
  "sans": "'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
  "serif": "'Source Serif 4', 'Source Serif Pro', Georgia, serif",
  "mono": "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
} as const;

export const TYPE_SCALE = {
  "fs-display": "22px",
  "fs-title": "16px",
  "fs-body": "13px",
  "fs-caption": "11px",
} as const;

export const SPACING_SCALE = {
  "sp-1": "4px",
  "sp-2": "8px",
  "sp-3": "12px",
  "sp-4": "16px",
  "sp-5": "24px",
  "sp-6": "32px",
} as const;

export const CHROME_TOKENS = {
  "radius": "8px",
  "radius-full": "999px",
  "transition": "150ms ease",
} as const;

export const CHART_SERIES = ["#0173b2", "#de8f05", "#029e73", "#cc78bc", "#ca9161", "#949494"] as const;

export type Theme = "paper" | "white" | "dark";
export type Tone = "ok" | "warn" | "bad" | "accent";
