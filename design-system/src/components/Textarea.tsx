/**
 * Typed wrapper over the bare `<textarea>` baseline in `styles/controls.css`
 * (styled by the same `select, textarea, input[...]` rule as `Input`/
 * `Select` — no class required). Forwards a ref to the underlying
 * `<textarea>` element; all native props pass through unchanged.
 */
import * as React from "react";

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  props,
  ref,
) {
  return <textarea ref={ref} {...props} />;
});
