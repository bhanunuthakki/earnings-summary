/**
 * `<Select>` — the kit's single-choice dropdown. By default it "owns every
 * pixel": a `.k-trigger` button (reads like the `<select>` baseline — paper
 * field + right chevron) opens a `.k-menu` popover of options with full
 * keyboard navigation (↑/↓, Home/End, Enter/Space, Escape, outside-click), so
 * the OS never draws its own option list. Pass `native` to fall back to a plain
 * `<select>` (the escape hatch — e.g. where native a11y/mobile pickers are
 * preferred); that path keeps the chevron-footgun guard the old wrapper had.
 *
 * Rebuilt from the bare-`<select>` wrapper in the design-sync 2026-07-19 pass
 * (the Python kit still server-renders a native `<select>`; this popover
 * composite is React-only). One popover surface, one focus ring, one chevron —
 * shared with `<MultiSelect>`.
 */
import * as React from "react";

export interface SelectOption {
  value: string;
  /** Display label; defaults to `value`. */
  label?: React.ReactNode;
  disabled?: boolean;
}

export interface SelectProps {
  options: SelectOption[];
  /** Controlled selected value. */
  value?: string;
  /** Uncontrolled initial value. */
  defaultValue?: string;
  onChange?: (value: string) => void;
  /** Shown in the trigger when nothing is selected. */
  placeholder?: string;
  /** Escape hatch: render a native `<select>` instead of the popover composite. */
  native?: boolean;
  id?: string;
  name?: string;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

function labelOf(options: SelectOption[], value: string | undefined): React.ReactNode | undefined {
  const opt = options.find((o) => o.value === value);
  return opt ? (opt.label ?? opt.value) : undefined;
}

export function Select({
  options,
  value,
  defaultValue,
  onChange,
  placeholder = "Select…",
  native,
  id,
  name,
  disabled,
  className,
  "aria-label": ariaLabel,
}: SelectProps): React.JSX.Element {
  const isControlled = value !== undefined;
  const [internal, setInternal] = React.useState<string | undefined>(defaultValue);
  const current = isControlled ? value : internal;

  const commit = React.useCallback(
    (next: string) => {
      if (!isControlled) setInternal(next);
      onChange?.(next);
    },
    [isControlled, onChange],
  );

  // ---- native escape hatch -------------------------------------------------
  if (native) {
    return (
      <select
        id={id}
        name={name}
        disabled={disabled}
        className={className}
        aria-label={ariaLabel}
        value={current ?? ""}
        onChange={(e) => commit(e.target.value)}
      >
        {current === undefined ? (
          <option value="" disabled hidden>
            {placeholder}
          </option>
        ) : null}
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {typeof o.label === "string" ? o.label : o.value}
          </option>
        ))}
      </select>
    );
  }

  // ---- popover composite ---------------------------------------------------
  const [open, setOpen] = React.useState(false);
  const [active, setActive] = React.useState<number>(-1);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);

  const enabledIndexes = options.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0);

  const openMenu = React.useCallback(() => {
    if (disabled) return;
    const sel = options.findIndex((o) => o.value === current);
    setActive(sel >= 0 && !options[sel]?.disabled ? sel : (enabledIndexes[0] ?? -1));
    setOpen(true);
  }, [disabled, options, current, enabledIndexes]);

  const closeMenu = React.useCallback((refocus = true) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const step = (dir: 1 | -1) => {
    setActive((cur) => {
      const pos = enabledIndexes.indexOf(cur);
      const nextPos = pos < 0 ? (dir === 1 ? 0 : enabledIndexes.length - 1) : pos + dir;
      const clamped = Math.max(0, Math.min(enabledIndexes.length - 1, nextPos));
      return enabledIndexes[clamped] ?? cur;
    });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openMenu();
      }
      return;
    }
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        step(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        step(-1);
        break;
      case "Home":
        e.preventDefault();
        setActive(enabledIndexes[0] ?? -1);
        break;
      case "End":
        e.preventDefault();
        setActive(enabledIndexes[enabledIndexes.length - 1] ?? -1);
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        if (active >= 0 && !options[active]?.disabled) {
          commit(options[active].value);
          closeMenu();
        }
        break;
      case "Escape":
        e.preventDefault();
        closeMenu();
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  };

  const display = labelOf(options, current);
  const base = id ?? "sel";

  return (
    <div ref={rootRef} className={className ? `k-pop ${className}` : "k-pop"}>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        className="k-trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onKeyDown}
      >
        <span
          className={
            display === undefined ? "k-trigger-placeholder k-trigger-value" : "k-trigger-value"
          }
        >
          {display === undefined ? placeholder : display}
        </span>
        <span className="k-trigger-chevron" aria-hidden="true" />
      </button>
      {name ? <input type="hidden" name={name} value={current ?? ""} /> : null}
      {open ? (
        <ul
          className="k-menu"
          role="listbox"
          aria-activedescendant={active >= 0 ? `${base}-opt-${active}` : undefined}
        >
          {options.map((o, i) => (
            <li
              key={o.value}
              id={`${base}-opt-${i}`}
              role="option"
              aria-selected={o.value === current}
              aria-disabled={o.disabled || undefined}
              className={i === active ? "sel" : undefined}
              onMouseEnter={() => !o.disabled && setActive(i)}
              onClick={() => {
                if (o.disabled) return;
                commit(o.value);
                closeMenu();
              }}
            >
              {o.label ?? o.value}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
