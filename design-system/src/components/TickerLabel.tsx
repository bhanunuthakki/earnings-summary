/**
 * `.k-tick` / `.k-tick-sym` / `.k-tick-name` — ports `ticker_label()` from
 * `src/ui/controls.py`. "Mono ticker symbol, regular-weight muted company
 * name beside it, ellipsis-truncated at `--k-tick-max` (default 20ch,
 * override per call via `name_max`); the FULL name always rides in `title`.
 * `href` links the symbol only — the name stays plain text so long names
 * never become long links. Replaces every `f"{ticker} · {name}"` /
 * `f"{ticker} — {name}"` concatenation."
 */
import * as React from "react";

export interface TickerLabelProps {
  /** Ticker symbol; rendered upper-cased, mono, in `.k-tick-sym`. */
  ticker: string;
  /** Company name. Rendered in `.k-tick-name` (truncated) and mirrored into
   * the wrapper's `title` attribute for hover. Omitted entirely — no
   * `.k-tick-name` node, no `title` — when absent, matching the Python
   * function's `name_max` guard (`if name else ""`). */
  name?: string;
  /** When set, only the ticker symbol becomes a link (`<a class="k-tick-sym">`);
   * the name never becomes part of the link, mirroring the Python function. */
  href?: string;
  /** Overrides the `--k-tick-max` CSS var (default in controls.css: `20ch`)
   * that bounds `.k-tick-name`'s ellipsis truncation width. */
  nameMax?: number | string;
  /** Additional class names appended alongside `k-tick`. */
  className?: string;
}

/** The canonical ticker + company-name label. See `ticker_label()` in
 * `src/ui/controls.py` for the HTML-string original this mirrors. */
export function TickerLabel({
  ticker,
  name,
  href,
  nameMax,
  className,
}: TickerLabelProps): React.JSX.Element {
  const cls = className ? `k-tick ${className}` : "k-tick";
  const style: React.CSSProperties | undefined = nameMax
    ? ({ "--k-tick-max": typeof nameMax === "number" ? `${nameMax}ch` : nameMax } as React.CSSProperties)
    : undefined;
  const symbol = ticker.toUpperCase();

  return (
    <span className={cls} title={name || undefined} style={style}>
      {href ? (
        <a className="k-tick-sym" href={href}>
          {symbol}
        </a>
      ) : (
        <span className="k-tick-sym">{symbol}</span>
      )}
      {name ? <span className="k-tick-name">{name}</span> : null}
    </span>
  );
}
