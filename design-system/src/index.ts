/**
 * @earnings-summary/design-system — public barrel export.
 *
 * Phase 0 stub: proves the build pipeline (esbuild bundle + tsc declaration
 * emit + CSS bundle) end-to-end with one trivial component. Real kit
 * components (Button, Pill, Chip, ...) land in later phases per
 * docs/design_system_react_port_plan.md.
 */
import "./index.css";
import * as React from "react";

export interface PlaceholderProps {
  /** Text rendered inside the placeholder block. Defaults to a stub label. */
  label?: string;
}

/**
 * Trivial placeholder component — exists only to prove the package's build
 * and export pipeline works end-to-end. Not part of the ported kit.
 */
export function Placeholder({ label = "design-system placeholder" }: PlaceholderProps): React.JSX.Element {
  return React.createElement("div", { className: "ds-placeholder" }, label);
}
