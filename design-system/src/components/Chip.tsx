/**
 * `.k-chip` — the ONE badge shape (radius-full · micro · uppercase) for
 * outline kind/filter tags (design_language.md §2: accent is reserved for
 * interactive/selected/unread, never decoration — `tone="accent"` should
 * only be used for an interactive/selected/unread chip). `interactive`
 * renders a `<button class="k-chip k-chip-btn">` (clickable filter/toggle
 * chip, `on` → `.is-on` for the selected state); otherwise a plain
 * `<span class="k-chip">` (static outline tag). Mirrors `chip_tone_class()`
 * in `src/ui/controls.py`.
 */
import * as React from "react";
import type { Tone } from "../tokens/tokens.generated";
import { chipToneClass } from "../lib/tone";

interface ChipBaseProps {
  /** `ok` / `warn` / `bad` / `accent`. Omit for the neutral outline. */
  tone?: Tone;
  /** `k-chip-mono` — monospace, no uppercase transform (e.g. tickers/codes). */
  mono?: boolean;
  /** Renders as `<button class="k-chip-btn">` instead of `<span>`. */
  interactive?: boolean;
  /** Adds `.is-on` — only meaningful when `interactive`. */
  on?: boolean;
  className?: string;
  children?: React.ReactNode;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
}

export type ChipProps = ChipBaseProps &
  Omit<React.HTMLAttributes<HTMLSpanElement | HTMLButtonElement>, keyof ChipBaseProps>;

export function Chip({
  tone,
  mono,
  interactive,
  on,
  className,
  children,
  onClick,
  ...rest
}: ChipProps): React.JSX.Element {
  const classes = ["k-chip" + chipToneClass(tone)];
  if (mono) classes.push("k-chip-mono");
  if (interactive) classes.push("k-chip-btn");
  if (interactive && on) classes.push("is-on");
  if (className) classes.push(className);
  const classNameJoined = classes.join(" ");

  if (interactive) {
    return (
      <button
        type="button"
        className={classNameJoined}
        onClick={onClick}
        {...(rest as React.ButtonHTMLAttributes<HTMLButtonElement>)}
      >
        {children}
      </button>
    );
  }

  return (
    <span className={classNameJoined} {...(rest as React.HTMLAttributes<HTMLSpanElement>)}>
      {children}
    </span>
  );
}
