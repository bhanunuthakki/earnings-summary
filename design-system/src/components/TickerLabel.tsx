/**
 * `.k-tick` / `.k-tick-sym` / `.k-tick-name` — ports `ticker_label()` from
 * `src/ui/controls.py`. "Mono ticker symbol, regular-weight muted company
 * name beside it, ellipsis-truncated at the master-owned width token; the
 * FULL name always rides in `title`.
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
  /** Selects the registered wide-name design-system variant. */
  wide?: boolean;
  /** Additional class names appended alongside `k-tick`. */
  className?: string;
}

/** The canonical ticker + company-name label. See `ticker_label()` in
 * `src/ui/controls.py` for the HTML-string original this mirrors. */
export function TickerLabel({
  ticker,
  name,
  href,
  wide,
  className,
}: TickerLabelProps): React.JSX.Element {
  const cls = ["k-tick", wide ? "k-tick-wide" : "", className ?? ""]
    .filter(Boolean)
    .join(" ");
  const symbol = ticker.toUpperCase();

  return (
    <span className={cls} title={name || undefined}>
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
