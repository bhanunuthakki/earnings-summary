/**
 * Typed wrapper over the bare `<input>` baseline in `styles/controls.css`
 * (`select, textarea, input[type="text"], input[type="search"], ...,
 * input:not([type])` — the form-control baseline needs no class). `type` is
 * restricted to the variants that selector list actually styles; anything
 * else (checkbox, radio, file, range, ...) falls outside the kit baseline
 * and should not use this wrapper. Forwards a ref to the underlying
 * `<input>` element.
 */
import * as React from "react";

/** The `input[type="..."]` variants styled by the controls.css baseline. */
export type KitInputType = "text" | "search" | "number" | "date" | "email" | "url" | "password";

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** Omit for the untyped `input:not([type])` baseline (renders as text). */
  type?: KitInputType;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input(
  { type, ...rest },
  ref,
) {
  return <input ref={ref} type={type} {...rest} />;
});
