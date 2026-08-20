/**
 * `.k-btn` button hierarchy — one primary per view, `-quiet` / `-danger` for
 * the rest, `-sm` for the compact row size (design_language.md: type/weight
 * encodes hierarchy, never an extra color). Mirrors the class combinations
 * emitted throughout `src/ui/*.py` (e.g. `class="k-btn k-btn-primary k-btn-sm"`).
 *
 * `variant` is optional: the Python kit does occasionally emit a bare
 * `.k-btn` with no variant (e.g. `position_lifecycle_panel.py`'s "Save
 * grading" button) — that renders as the transparent/neutral base look, so
 * this component leaves `variant` unset by default rather than defaulting to
 * `"quiet"` or `"primary"`, to preserve that bare-base option for callers.
 */
import * as React from "react";

export interface ButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "style"> {
  /** `k-btn-primary` / `k-btn-quiet` / `k-btn-danger`. Omit for the bare `.k-btn` base look. */
  variant?: "primary" | "quiet" | "danger";
  /** `k-btn-sm` — the compact row size. */
  size?: "sm";
}

export function Button({
  variant,
  size,
  className,
  disabled,
  children,
  ...rest
}: ButtonProps): React.JSX.Element {
  const classes = ["k-btn"];
  if (variant) classes.push(`k-btn-${variant}`);
  if (size === "sm") classes.push("k-btn-sm");
  if (className) classes.push(className);

  return (
    <button type="button" className={classes.join(" ")} disabled={disabled} {...rest}>
      {children}
    </button>
  );
}
