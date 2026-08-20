/**
 * `<DateField>` — the kit's date input. Wraps the styled `<input type="date">`
 * baseline, hides the browser's native calendar-picker indicator, and draws a
 * kit calendar glyph in its place (the native picker still opens on click — the
 * indicator is made transparent, not removed). A fully kit-drawn calendar
 * popover can replace the native picker later; the field is functional today.
 *
 * Added in the design-sync 2026-07-19 "own every pixel" pass (React-only — the
 * Python kit uses the bare native date input).
 */
import * as React from "react";

export type DateFieldProps = Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  "type" | "style"
>;

export const DateField = React.forwardRef<HTMLInputElement, DateFieldProps>(function DateField(
  { className, ...rest },
  ref,
) {
  return (
    <span className={className ? `k-date ${className}` : "k-date"}>
      <input ref={ref} type="date" {...rest} />
      <svg
        className="k-date-icon"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="2.5" y="3.5" width="11" height="10" rx="1.5" />
        <path d="M2.5 6.5h11M5.5 2v2M10.5 2v2" />
      </svg>
    </span>
  );
});
