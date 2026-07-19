/**
 * `.k-well` — the soft-filled BLOCK sibling of `.k-pill` (KPI cards,
 * callouts, tone rows): same `color-mix` tone family, box radius instead of
 * full. Ink stays `--fg`; a status word inside can take its own `<Pill>`.
 */
import * as React from "react";
import type { Tone } from "../tokens/tokens.generated";

export interface WellProps {
  /** `ok` / `warn` / `bad` / `accent`. Omit for the neutral `--paper` fill. */
  tone?: Tone;
  className?: string;
  children?: React.ReactNode;
}

const TONED = new Set<string>(["ok", "warn", "bad", "accent"]);

export function Well({ tone, className, children }: WellProps): React.JSX.Element {
  const classes = ["k-well"];
  if (tone && TONED.has(tone)) classes.push(`k-well-${tone}`);
  if (className) classes.push(className);

  return <div className={classes.join(" ")}>{children}</div>;
}
