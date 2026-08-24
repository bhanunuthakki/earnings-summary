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
  "series-spy": "#de8f05",
  "series-qqq": "#5b8def",
  "series-policy": "#949494",
  "seg-1": "#0d0d0c",
  "seg-2": "#443f34",
  "seg-3": "#7d7869",
  "seg-4": "#b5b0a4",
  "seg-5": "#dcdcd7",
  "shadow-pop": "0 12px 32px rgba(15, 15, 20, 0.18)",
  "scrim": "rgba(0, 0, 0, 0.5)",
  "glass-bg": "rgba(255, 255, 255, 0.75)",
  "glass-border": "rgba(0, 0, 0, 0.06)",
} as const;

export const PALETTE_WHITE_OVERRIDES = {
  "bg": "#ffffff",
  "paper": "#fafaf7",
  "hairline": "#efeeea",
} as const;

export const PALETTE_DARK = {
  "bg": "#090a0c",
  "surface": "#121316",
  "paper": "#18191d",
  "fg": "#f4f3ef",
  "fg-soft": "#c5c2b8",
  "muted": "#7c7e87",
  "border": "#22242a",
  "border-2": "#31343e",
  "hairline": "#18191d",
  "accent": "#8aa8ff",
  "accent-soft": "#181f38",
  "accent-contrast": "#090a0c",
  "ok": "#4ade80",
  "warn": "#f5c66a",
  "bad": "#f08a8a",
  "mark": "#b08d5f",
  "mark-soft": "#705737",
  "series-spy": "#de8f05",
  "series-qqq": "#5b8def",
  "series-policy": "#949494",
  "seg-1": "#f4f3ef",
  "seg-2": "#b5b0a4",
  "seg-3": "#7d7869",
  "seg-4": "#443f34",
  "seg-5": "#262219",
  "shadow-pop": "0 16px 48px -8px rgba(0, 0, 0, 0.7)",
  "scrim": "rgba(0, 0, 0, 0.65)",
  "glass-bg": "rgba(18, 19, 22, 0.75)",
  "glass-border": "rgba(255, 255, 255, 0.05)",
} as const;

export const FONT_TOKENS = {
  "sans": "'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
  "serif": "'Source Serif 4', 'Source Serif Pro', Georgia, serif",
  "mono": "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
} as const;

export const TYPE_SCALE = {
  "fs-display": "20px",
  "fs-title": "15px",
  "fs-body": "13px",
  "fs-caption": "11px",
  "fs-stat": "var(--fs-display)",
  "fs-header-title": "var(--fs-title)",
  "fs-serif-body": "var(--fs-body)",
  "fs-mono-sm": "var(--fs-caption)",
  "fs-micro": "var(--fs-caption)",
  "fs-nano": "var(--fs-caption)",
} as const;

export const SPACING_SCALE = {
  "sp-half": "2px",
  "sp-1": "4px",
  "sp-2": "8px",
  "sp-3": "12px",
  "sp-4": "16px",
  "sp-5": "24px",
  "sp-6": "32px",
} as const;

export const INDENT_TOKENS = {
  "indent-0": "0",
  "indent-1": "4px",
  "indent-2": "8px",
  "indent-3": "12px",
  "indent-4": "16px",
} as const;

export const RAIL_TOKENS = {
  "rail-sm": "360px",
  "rail-lg": "400px",
} as const;

export const CHROME_TOKENS = {
  "sidebar-width": "240px",
  "sidebar-collapsed-width": "72px",
  "main-max-width": "1240px",
  "drawer-width": "540px",
  "comments-drawer-width": "380px",
  "header-height": "48px",
  "nav-item-height": "32px",
  "icon-size": "16px",
  "icon-button-size": "32px",
  "mobile-control-font-size": "16px",
  "touch-target-size": "44px",
  "toast-offset-bottom": "28px",
  "toast-offset-right": "28px",
  "dot-size": "6px",
  "ticker-width": "34px",
  "bar-track-height": "8px",
  "reading-measure": "66ch",
  "note-rail-width": "13.5rem",
  "ticker-name-width": "20ch",
  "ticker-name-wide-width": "36ch",
  "grid-card-lg": "340px",
  "grid-card-md": "280px",
  "grid-card-sm": "240px",
  "bw-thin": "1px",
  "bw-thick": "2px",
  "lift-sm": "-1px",
  "lift-md": "-2px",
  "toast-hide-y": "100px",
  "z-sidebar": "10",
  "z-header": "20",
  "z-scrim": "80",
  "z-drawer": "90",
  "z-toast": "100",
  "z-modal": "300",
  "main-max-width-wide": "1440px",
  "dismiss-scale": "0.97",
  "dismiss-y": "-2px",
  "toast-y": "20px",
  "blur-sm": "6px",
  "blur-md": "16px",
  "blur-lg": "24px",
  "radius-sm": "2px",
  "radius-md": "3px",
  "radius": "8px",
  "radius-card": "10px",
  "radius-drawer": "14px",
  "radius-full": "999px",
  "transition": "150ms ease",
  "transition-fluid": "250ms cubic-bezier(0.16, 1, 0.3, 1)",
  "shadow-btn-primary": "0 2px 10px rgba(138, 168, 255, 0.22)",
  "shadow-btn-primary-hover": "0 4px 16px rgba(138, 168, 255, 0.38)",
  "shadow-card": "0 4px 20px -2px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.05)",
  "shadow-card-hover": "0 12px 28px -6px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.1)",
  "shadow-drawer": "-16px 0 48px rgba(0, 0, 0, 0.75)",
} as const;

export const CHART_SERIES = ["#0173b2", "#de8f05", "#029e73", "#cc78bc", "#ca9161", "#949494"] as const;

export type Theme = "paper" | "white" | "dark";
export type Tone = "ok" | "warn" | "bad" | "accent";
