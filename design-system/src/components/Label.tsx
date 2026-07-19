/**
 * `.k-label` — uppercase field/section caption. `text-transform: uppercase`
 * plus letter-spacing already live on the class in controls.css, so this
 * component does no formatting work of its own; it just carries the class.
 */
import * as React from "react";

export interface LabelProps {
  children?: React.ReactNode;
  /** Additional class names appended alongside `k-label`. */
  className?: string;
}

/** Uppercase field/section caption. No Python function counterpart — the
 * Python side spells `.k-label` inline at each call site; this component
 * exists so React call sites don't have to. */
export function Label({ children, className }: LabelProps): React.JSX.Element {
  const cls = className ? `k-label ${className}` : "k-label";
  return <span className={cls}>{children}</span>;
}
