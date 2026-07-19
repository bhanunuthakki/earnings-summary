/**
 * React scaffold over the `palette_css` theme contract (`tokens.generated.ts`
 * `Theme` = `"paper" | "white" | "dark"`, matching `tokens.css`'s `:root` /
 * `[data-theme="white"]` / `[data-theme="dark"]` blocks). Sets `data-theme`
 * either on a wrapper `<div>` (default) or on `document.documentElement`
 * (`target="document"`).
 *
 * Wrapper-div is the default because it lets a single prototyping page mount
 * several independently-themed sections side by side (e.g. a Storybook
 * "kit sheet" showing paper/white/dark simultaneously) — `tokens.css`'s
 * `[data-theme="..."]` selectors are plain attribute selectors, so they work
 * scoped to any ancestor, not just `:root`. `target="document"` is offered
 * for the case a whole page/app wants a single global theme (matching how
 * the Python surfaces set `data-theme` on `<html>`), since CSS custom
 * properties set on `:root` specifically (via the real `:root` selector, not
 * just any high ancestor) are what `tokens.css`'s base `:root {}` block
 * targets for its *default* (paper/light) values — non-default themes only
 * override via the attribute selector either way, so both modes render
 * correctly, but `target="document"` more closely matches production.
 */
import * as React from "react";
import type { Theme } from "../tokens/tokens.generated";

const DEFAULT_THEME: Theme = "paper";

const ThemeContext = React.createContext<Theme>(DEFAULT_THEME);

export interface ThemeProviderProps {
  /** `"paper" | "white" | "dark"` — see `tokens.generated.ts`. */
  theme: Theme;
  /**
   * Where `data-theme` is set. `"wrapper"` (default) scopes the theme to a
   * `<div>` around `children` — composable for multi-theme prototyping pages.
   * `"document"` sets it on `document.documentElement`, matching how the
   * Flask surfaces theme the whole page.
   */
  target?: "wrapper" | "document";
  /** Extra class names for the wrapper `<div>` (`target="wrapper"` only). */
  className?: string;
  children?: React.ReactNode;
}

/**
 * Provides `theme` to descendants via {@link useTheme} and stamps
 * `data-theme` so `tokens.css`'s override blocks apply.
 */
export function ThemeProvider({
  theme,
  target = "wrapper",
  className,
  children,
}: ThemeProviderProps): React.JSX.Element {
  React.useEffect(() => {
    if (target !== "document") return;
    const root = document.documentElement;
    const previous = root.getAttribute("data-theme");
    root.setAttribute("data-theme", theme);
    return () => {
      if (previous === null) root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", previous);
    };
  }, [theme, target]);

  const content = (
    <ThemeContext.Provider value={theme}>{children}</ThemeContext.Provider>
  );

  if (target === "document") {
    return content;
  }

  return (
    <div data-theme={theme} className={className}>
      {content}
    </div>
  );
}

/** Reads the nearest {@link ThemeProvider}'s theme; defaults to `"paper"`. */
export function useTheme(): Theme {
  return React.useContext(ThemeContext);
}
