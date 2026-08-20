/**
 * `.k-dot` — the ONE filled circular status tick (design_language.md §4):
 * fill is `currentColor`, so a tone modifier only sets `color`. Diameter is
 * owned by the control-kit token; callers cannot freehand inline geometry.
 */
import * as React from "react";

export interface DotProps {
  tone?: "ok" | "warn" | "bad" | "muted";
  className?: string;
}

export function Dot({ tone, className }: DotProps): React.JSX.Element {
  const classes = ["k-dot"];
  if (tone) classes.push(`k-dot-${tone}`);
  if (className) classes.push(className);

  return <span className={classes.join(" ")} />;
}
