/**
 * `.k-menu` — shared popover-list surface (combobox results, palette rows).
 * controls.css styles a `<ul class="k-menu">` of `<li>` rows directly
 * (`.k-menu { list-style: none; ... } .k-menu li { padding: ...; cursor:
 * pointer; } .k-menu li.sel, .k-menu li:hover { background: var(--paper); }`)
 * — so this component renders that exact `ul > li` shape rather than divs,
 * to pick up the CSS unmodified.
 */
import * as React from "react";

export interface MenuItem {
  /** Unique React key / row identity. */
  key: string;
  label: React.ReactNode;
  onSelect?: () => void;
  /** Marks the row `.sel` (matches controls.css's `.k-menu li.sel` hover-equivalent state). */
  selected?: boolean;
}

export interface MenuProps {
  /** Declarative rows. Each renders as `<li>` with `onSelect` wired to `onClick`. */
  items?: MenuItem[];
  /** Escape hatch: raw `<li>` (or other) markup rendered inside `.k-menu` when
   * `items` isn't flexible enough. Ignored if `items` is provided. */
  children?: React.ReactNode;
  /** Additional class names appended alongside `k-menu`. */
  className?: string;
}

/** The shared popover-list surface. See `.k-menu` in
 * `design-system/src/styles/controls.css` (ported verbatim from
 * `_CONTROLS_BODY` in `src/ui/controls.py`) for the styled contract. */
export function Menu({ items, children, className }: MenuProps): React.JSX.Element {
  const cls = className ? `k-menu ${className}` : "k-menu";
  return (
    <ul className={cls}>
      {items
        ? items.map((item) => (
            <li key={item.key} className={item.selected ? "sel" : undefined} onClick={item.onSelect}>
              {item.label}
            </li>
          ))
        : children}
    </ul>
  );
}
