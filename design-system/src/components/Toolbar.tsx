/**
 * `.k-toolbar` / `.k-toolbar-title` / `.k-toolbar-controls` — ports
 * `panel_toolbar()` + `panel_section_title()` from `src/ui/controls.py`.
 * "The one operating band a panel gets before its content (design_language
 * §6.1): the title on the left, `filters` + `actions` on the SAME flex row
 * to the right. Never stack a title band over a filter band." Renders
 * nothing when there is no title and no filters/actions, matching the
 * Python function's `return ""` short-circuit.
 */
import * as React from "react";

export interface ToolbarProps {
  /** Panel title, rendered as `.k-toolbar-title` (an `<h2>`), unless suppressed. */
  title?: string;
  /** Drops the heading when the surrounding nav/shell already shows the
   * title — "the shell passes `suppress_title=True` for a single-sub-tab
   * section (where the tab label IS the title) and the heading collapses."
   * Ported 1:1 from `panel_section_title(title, suppressed=...)`. */
  suppressTitle?: boolean;
  /** Filter controls slot (e.g. `.k-chip` filters), rendered inside
   * `.k-toolbar-controls` before `actions`. */
  filters?: React.ReactNode;
  /** Action controls slot (e.g. `.k-btn` actions), rendered inside
   * `.k-toolbar-controls` after `filters`. */
  actions?: React.ReactNode;
}

/** The one operating band a panel gets before its content. See
 * `panel_toolbar()` / `panel_section_title()` in `src/ui/controls.py`. */
export function Toolbar({
  title = "",
  suppressTitle = false,
  filters,
  actions,
}: ToolbarProps): React.JSX.Element | null {
  const showTitle = !suppressTitle && title.trim().length > 0;
  const hasControls = Boolean(filters || actions);

  if (!showTitle && !hasControls) {
    return null;
  }

  return (
    <div className="k-toolbar">
      {showTitle ? <h2 className="k-toolbar-title">{title}</h2> : null}
      {hasControls ? (
        <div className="k-toolbar-controls">
          {filters}
          {actions}
        </div>
      ) : null}
    </div>
  );
}
