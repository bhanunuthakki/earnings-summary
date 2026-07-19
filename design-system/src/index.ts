/**
 * @earnings-summary/design-system — public barrel export.
 *
 * Phase 0 proved the build pipeline (esbuild bundle + tsc declaration emit +
 * CSS bundle) end-to-end with a trivial placeholder. Phase 1 added the
 * generated design tokens; Phase 2 (this export) adds the kit-core React
 * components ported from src/ui/controls.py. See
 * docs/design_system_react_port_plan.md for the full rollout.
 */
import "./index.css";
import * as React from "react";

export * from "./tokens";
export * from "./components";
export { ThemeProvider, useTheme, type ThemeProviderProps } from "./theme/ThemeProvider";

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
