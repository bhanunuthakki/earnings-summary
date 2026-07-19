/**
 * Typed wrapper over the bare `<select>` baseline in `styles/controls.css`
 * (`select:not([multiple])` gets the custom `--k-chevron` chevron via
 * `background-image`; `select[multiple]` gets its own padding/option rules —
 * both need no class, and this wrapper supports the native `multiple` prop).
 *
 * CHEVRON FOOTGUN: `src/ui/controls.py`'s module docstring documents that a
 * `background: <anything>` shorthand on a `<select>` wipes `background-image`
 * (the chevron) and produces an arrowless box — the fix is `background-color`
 * instead. Since React's `style` prop takes the same CSS shorthand, this
 * wrapper reproduces the trap 1:1 unless guarded. We guard by TRANSLATING:
 * if `style.background` is set, it is moved to `style.backgroundColor`
 * (only filling it in if `backgroundColor` wasn't already set explicitly)
 * and a `console.warn` fires so the caller notices and fixes the call site.
 * Translating (rather than just warning) was chosen over silently dropping
 * the shorthand or leaving it in place: dropping would surprise a caller who
 * genuinely wanted a background color and got none; leaving it in place
 * reproduces the exact bug this wrapper exists to prevent. The warning still
 * fires unconditionally (no `NODE_ENV` gate) because this bundle is built
 * with `platform: "browser"` and no `process.env` define (see
 * `design-system/build.mjs`) — referencing `process.env.NODE_ENV` at runtime
 * would throw `ReferenceError: process is not defined` in the browser.
 */
import * as React from "react";

function sanitizeSelectStyle(style: React.CSSProperties | undefined): React.CSSProperties | undefined {
  if (!style || !("background" in style) || style.background === undefined) {
    return style;
  }
  console.warn(
    "[design-system] <Select style={{ background: ... }}> wipes the `--k-chevron` " +
      "background-image and renders an arrowless <select> (src/ui/controls.py's " +
      "Composition contract). Translating to `backgroundColor` — pass `backgroundColor` " +
      "directly to silence this warning.",
  );
  const { background, backgroundColor, ...rest } = style;
  return {
    ...rest,
    backgroundColor: backgroundColor ?? (background as React.CSSProperties["backgroundColor"]),
  };
}

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { style, ...rest },
  ref,
) {
  return <select ref={ref} style={sanitizeSelectStyle(style)} {...rest} />;
});
