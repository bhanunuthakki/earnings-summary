/**
 * `.k-pill` — the ONE filled status/score badge (design_language.md §3): a
 * soft status fill + token ink, never a per-panel raw-hex pill. `tone`
 * absent renders the neutral `--paper`/`--fg-soft` base; mirrors
 * `pill_tone_class()` in `src/ui/controls.py`.
 */
import * as React from "react";
import type { Tone } from "../tokens/tokens.generated";
import { pillToneClass } from "../lib/tone";

export interface PillProps {
  /** `ok` / `warn` / `bad` / `accent`. Omit for the neutral base fill. */
  tone?: Tone;
  className?: string;
  children?: React.ReactNode;
}

export function Pill({ tone, className, children }: PillProps): React.JSX.Element {
  const classes = ["k-pill" + pillToneClass(tone)];
  if (className) classes.push(className);

  return <span className={classes.join(" ")}>{children}</span>;
}
