/**
 * GENERATED from src/ui/tokens.py — do not hand-edit.
 * Regenerate: python scripts/gen_design_tokens.py
 */

export const PALETTE_LIGHT = {
  "bg": "#fafaf7",
  "surface": "#ffffff",
  "paper": "#f4f3ef",
  "fg": "#0c0d10",
  "fg-soft": "#2a2c33",
  "muted": "#6c6f78",
  "muted-2": "#70737a",
  "border": "#e4e3dd",
  "border-2": "#d1cfc7",
  "hairline": "#ecebe5",
  "accent": "#1d4ed8",
  "accent-soft": "#eef2ff",
  "accent-contrast": "#ffffff",
  "ok": "#15803d",
  "warn": "#b97c00",
  "bad": "#b91c1c",
  "pos": "#15803d",
  "neg": "#b91c1c",
  "neu": "#6c6f78",
  "seg-1": "#0c0d10",
  "seg-2": "#43464e",
  "seg-3": "#7a7d86",
  "seg-4": "#b6b8be",
  "seg-5": "#dcdcd7",
  "tone-pos": "#eef2ff",
  "tone-neu": "#f4f3ef",
  "tone-opt": "#fff8e6",
  "tone-neg": "#fdf2f2",
  "shadow-pop": "0 12px 32px rgba(15, 15, 20, 0.18)",
  "scrim": "rgba(0, 0, 0, 0.5)",
} as const;

export const PALETTE_WHITE_OVERRIDES = {
  "bg": "#ffffff",
  "paper": "#fafaf7",
  "hairline": "#efeeea",
} as const;

export const PALETTE_DARK = {
  "bg": "#0c0d10",
  "surface": "#14161b",
  "paper": "#1a1d23",
  "fg": "#f4f3ef",
  "fg-soft": "#d5d6d2",
  "muted": "#888b94",
  "muted-2": "#7d8089",
  "border": "#2a2d35",
  "border-2": "#383b44",
  "hairline": "#1f2127",
  "accent": "#8aa8ff",
  "accent-soft": "#1c2138",
  "accent-contrast": "#0c0d10",
  "ok": "#4ade80",
  "warn": "#f5c66a",
  "bad": "#f08a8a",
  "pos": "#4ade80",
  "neg": "#f08a8a",
  "neu": "#888b94",
  "seg-1": "#f4f3ef",
  "seg-2": "#b6b8be",
  "seg-3": "#7a7d86",
  "seg-4": "#43464e",
  "seg-5": "#25282f",
  "tone-pos": "#1a2238",
  "tone-neu": "#1a1d23",
  "tone-opt": "#2b2418",
  "tone-neg": "#2b1a1a",
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
  "fs-section": "14px",
  "fs-body": "13px",
  "fs-caption": "11.5px",
  "fs-micro": "10px",
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
