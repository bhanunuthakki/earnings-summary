/**
 * `.k-num-pos` / `.k-num-neg` — the one green/red NUMBER-text tone (P&L
 * cells, deltas, alpha). Zero-handling mirrors every `src/pipeline/*.py`
 * call site (e.g. `allocation_decisions_panel.py`, `research_cockpit.py`,
 * `ticker_command_center.py`: uniformly `v >= 0 ? pos : neg`) — zero renders
 * `.k-num-pos`, never a third neutral tone.
 */
import * as React from "react";

export interface NumTextProps {
  value: number;
  /** Defaults to a `toLocaleString()`-based formatter. */
  format?: (n: number) => string;
  className?: string;
}

function defaultFormat(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function NumText({ value, format = defaultFormat, className }: NumTextProps): React.JSX.Element {
  const tone = value >= 0 ? "k-num-pos" : "k-num-neg";
  const classes = [tone];
  if (className) classes.push(className);

  return <span className={classes.join(" ")}>{format(value)}</span>;
}
