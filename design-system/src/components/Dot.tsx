/**
 * `.k-dot` — the ONE filled circular status tick (design_language.md §4):
 * fill is `currentColor`, so a tone modifier only sets `color`. `size` sets
 * `--k-dot-size` inline (default 8px, matching the CSS fallback) — layout
 * only, never a fill override.
 */
import * as React from "react";

export interface DotProps {
  tone?: "ok" | "warn" | "bad" | "muted";
  /** Diameter in px. Defaults to 8 (the CSS's own `--k-dot-size` fallback). */
  size?: number;
  className?: string;
}

export function Dot({ tone, size = 8, className }: DotProps): React.JSX.Element {
  const classes = ["k-dot"];
  if (tone) classes.push(`k-dot-${tone}`);
  if (className) classes.push(className);

  return (
    <span
      className={classes.join(" ")}
      style={{ "--k-dot-size": `${size}px` } as React.CSSProperties}
    />
  );
}
