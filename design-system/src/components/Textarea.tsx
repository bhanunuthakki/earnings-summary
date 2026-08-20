/**
 * Typed wrapper over the bare `<textarea>` baseline in `styles/controls.css`
 * (styled by the same `select, textarea, input[...]` rule as `Input`/
 * `Select` — no class required). Forwards a ref to the underlying
 * `<textarea>` element; native behavior props pass through while visual style
 * remains owned by the master.
 */
import * as React from "react";

export type TextareaProps = Omit<
  React.TextareaHTMLAttributes<HTMLTextAreaElement>,
  "style"
>;

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  props,
  ref,
) {
  return <textarea ref={ref} {...props} />;
});
