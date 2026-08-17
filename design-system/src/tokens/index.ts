/**
 * Hand-written re-exports for the token layer. The values themselves are
 * generated (see `tokens.generated.ts` — do not hand-edit that file);
 * this module is the stable import surface the rest of the kit and any
 * consuming app should use (`import { PALETTE_DARK, type Theme } from
 * "@earnings-summary/design-system/tokens"`-style access via the barrel).
 *
 * Canonical source: `src/ui/tokens.py`. Regenerate with
 * `python scripts/gen_design_tokens.py` after any change there — see
 * docs/design_system_react_port_plan.md §3 and the root README's
 * "Python side is canonical" note.
 */
export {
  CHART_SERIES,
  CHROME_TOKENS,
  FONT_TOKENS,
  INDENT_TOKENS,
  PALETTE_DARK,
  PALETTE_LIGHT,
  PALETTE_WHITE_OVERRIDES,
  RAIL_TOKENS,
  SPACING_SCALE,
  TYPE_SCALE,
  type Theme,
  type Tone,
} from "./tokens.generated.js";

// The CSS side-effect import lives at the package's top-level `index.ts`
// (per Phase 0's `index.css` wiring), not here — this module is types +
// values only, so it can be imported from non-DOM contexts (e.g. a Node
// script or a test) without pulling in a stylesheet.
