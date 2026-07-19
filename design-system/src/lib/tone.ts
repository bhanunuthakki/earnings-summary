/**
 * Pure TS port of the tone helpers in `src/ui/controls.py` — kept in lockstep
 * with `pill_tone_class()`, `chip_tone_class()`, and `thesis_status_tone()`
 * there. Python is canonical; if those functions change, update this file to
 * match (docs/design_system_react_port_plan.md §4(a)).
 */
import type { Tone } from "../tokens/tokens.generated";

const TONED = new Set<string>(["ok", "warn", "bad", "accent"]);

/**
 * `" k-pill-<tone>"` (leading space, appendable to a class string) for a real
 * tone, `""` for neutral/undefined — mirrors `pill_tone_class()` in
 * `src/ui/controls.py`.
 */
export function pillToneClass(tone?: Tone | string | null): string {
  return tone && TONED.has(tone) ? ` k-pill-${tone}` : "";
}

/**
 * `" k-chip-<tone>"` counterpart of {@link pillToneClass} — mirrors
 * `chip_tone_class()` in `src/ui/controls.py`.
 */
export function chipToneClass(tone?: Tone | string | null): string {
  return tone && TONED.has(tone) ? ` k-chip-${tone}` : "";
}

/**
 * Thesis-verdict / break-rule status word → kit tone. Ported verbatim from
 * `_THESIS_STATUS_TONE` in `src/ui/controls.py`: unknown vocabulary (e.g.
 * "unresolved") degrades to `undefined` (neutral, un-toned base control),
 * never to a wrong color.
 */
const THESIS_STATUS_TONE: Record<string, Tone> = {
  breach: "bad",
  broken: "bad",
  warn: "warn",
  watch: "warn",
  ok: "ok",
  intact: "ok",
};

export function thesisStatusTone(status?: string | null): Tone | undefined {
  const key = (status ?? "").trim().toLowerCase();
  return THESIS_STATUS_TONE[key];
}
