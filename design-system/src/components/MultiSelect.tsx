/**
 * `<MultiSelect>` — the kit's multi-choice picker, the replacement for the
 * native `<select multiple>` (dropped from the kit in the design-sync
 * 2026-07-19 pass). A `.k-trigger` shows the current selection as `<Chip>`s (or
 * a placeholder), and opens the shared `.k-menu` popover whose rows are kit
 * checkboxes + labels; toggling a row keeps the menu open. Keyboard: ↑/↓ move,
 * Space/Enter toggle, Escape closes, outside-click closes.
 *
 * Shares the one trigger look / popover surface / chevron / focus ring with
 * `<Select>`. React-only (no Python counterpart).
 */
import * as React from "react";
import { Chip } from "./Chip";

export interface MultiSelectOption {
  value: string;
  label?: React.ReactNode;
  disabled?: boolean;
}

export interface MultiSelectProps {
  options: MultiSelectOption[];
  /** Controlled selected values. */
  value?: string[];
  /** Uncontrolled initial values. */
  defaultValue?: string[];
  onChange?: (values: string[]) => void;
  placeholder?: string;
  /** Max chips shown in the trigger before collapsing to "+N" (default 3). */
  maxChips?: number;
  id?: string;
  name?: string;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

export function MultiSelect({
  options,
  value,
  defaultValue,
  onChange,
  placeholder = "Select…",
  maxChips = 3,
  id,
  name,
  disabled,
  className,
  "aria-label": ariaLabel,
}: MultiSelectProps): React.JSX.Element {
  const isControlled = value !== undefined;
  const [internal, setInternal] = React.useState<string[]>(defaultValue ?? []);
  const selected = isControlled ? value : internal;
  const selectedSet = new Set(selected);

  const commit = React.useCallback(
    (next: string[]) => {
      if (!isControlled) setInternal(next);
      onChange?.(next);
    },
    [isControlled, onChange],
  );

  const toggle = (val: string) => {
    commit(selectedSet.has(val) ? selected.filter((v) => v !== val) : [...selected, val]);
  };

  const [open, setOpen] = React.useState(false);
  const [active, setActive] = React.useState<number>(-1);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);

  const enabledIndexes = options.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0);

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
        setActive(enabledIndexes[0] ?? -1);
        setOpen(true);
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
      case "Enter":
      case " ":
        e.preventDefault();
        if (active >= 0 && !options[active]?.disabled) toggle(options[active].value);
        break;
      case "Escape":
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  };

  const chosen = options.filter((o) => selectedSet.has(o.value));
  const base = id ?? "ms";

  const summary =
    chosen.length === 0 ? (
      <span className="k-trigger-placeholder">{placeholder}</span>
    ) : (
      <span className="k-ms-summary">
        {chosen.slice(0, maxChips).map((o) => (
          <Chip key={o.value}>{typeof o.label === "string" ? o.label : o.value}</Chip>
        ))}
        {chosen.length > maxChips ? <Chip mono>{`+${chosen.length - maxChips}`}</Chip> : null}
      </span>
    );

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
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onKeyDown}
      >
        {summary}
        <span className="k-trigger-chevron" aria-hidden="true" />
      </button>
      {name
        ? selected.map((v) => <input key={v} type="hidden" name={name} value={v} />)
        : null}
      {open ? (
        <ul className="k-menu" role="listbox" aria-multiselectable="true">
          {options.map((o, i) => {
            const on = selectedSet.has(o.value);
            return (
              <li
                key={o.value}
                id={`${base}-opt-${i}`}
                role="option"
                aria-selected={on}
                aria-disabled={o.disabled || undefined}
                className={i === active ? "sel" : undefined}
                onMouseEnter={() => !o.disabled && setActive(i)}
                onClick={() => {
                  if (o.disabled) return;
                  toggle(o.value);
                }}
              >
                <span className="k-ms-row">
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={o.disabled}
                    tabIndex={-1}
                    readOnly
                    aria-hidden="true"
                  />
                  <span>{o.label ?? o.value}</span>
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
