"""Generate the React control kit from the canonical Python control kit.

``src/ui/controls.py`` owns all shared visual primitives.  The React package
may append the small set of composite-only rules below, but it must never copy
or alter the canonical layer by hand.

Usage::

    python scripts/gen_design_controls.py            # write controls.css
    python scripts/gen_design_controls.py --check    # fail on drift
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC))

from ui.controls import controls_css  # noqa: E402

CSS_OUTPUT = PROJECT_ROOT / "design-system" / "src" / "styles" / "controls.css"
CANONICAL_START = "/* BEGIN GENERATED PYTHON CONTROL KIT */"
CANONICAL_END = "/* END GENERATED PYTHON CONTROL KIT */"
REACT_EXTENSIONS_START = "/* BEGIN REACT-ONLY CONTROL EXTENSIONS */"
REACT_EXTENSIONS_END = "/* END REACT-ONLY CONTROL EXTENSIONS */"

_BANNER = """/*
 * GENERATED from src/ui/controls.py — do not hand-edit the canonical layer.
 * Regenerate: python scripts/gen_design_controls.py
 *
 * React-only composites are intentionally isolated below the generated layer.
 */

"""

# The Flask UI renders native selects; these rules support React's accessible
# Select, MultiSelect, and DateField composites.  They are the sole allowed
# React-side additions to the canonical control kit.
REACT_EXTENSIONS = """.k-trigger {
  display: inline-flex; align-items: center; justify-content: space-between;
  gap: var(--sp-2); width: 100%; font: inherit; font-size: var(--fs-body);
  color: var(--fg); background-color: var(--paper); border: var(--bw-thin) solid var(--border);
  border-radius: var(--radius); padding: var(--sp-1) var(--sp-2); cursor: pointer; text-align: left;
  transition: border-color var(--transition), box-shadow var(--transition);
}
.k-trigger:focus-visible { outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 var(--bw-thick) var(--accent-soft); }
.k-trigger[aria-expanded="true"] { border-color: var(--accent); }
.k-trigger[disabled] { opacity: 0.5; cursor: default; pointer-events: none; }
.k-trigger-value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.k-trigger-placeholder { color: var(--muted); }
.k-trigger-chevron { flex: none; width: var(--icon-size); height: var(--icon-size);
  background-image: var(--k-chevron); background-repeat: no-repeat;
  background-position: center; background-size: var(--icon-size);
  transition: transform var(--transition); }
.k-trigger[aria-expanded="true"] .k-trigger-chevron { transform: rotate(180deg); }

.k-pop { position: relative; }
.k-pop > .k-menu { position: absolute; top: calc(100% + var(--sp-1)); left: 0;
  right: 0; z-index: var(--z-modal); max-height: var(--grid-card-sm); overflow-y: auto;
  animation: k-overlay-rise var(--transition); }
.k-menu li[aria-disabled="true"] { color: var(--muted); cursor: default; }

.k-ms-summary { display: inline-flex; flex-wrap: wrap; align-items: center;
  gap: var(--sp-1); min-width: 0; }
.k-ms-row { display: flex; align-items: center; gap: var(--sp-2); }

.k-date { position: relative; display: inline-flex; align-items: center; width: 100%; }
.k-date input[type="date"] { width: 100%; padding-right: var(--icon-button-size); }
.k-date input[type="date"]::-webkit-calendar-picker-indicator {
  position: absolute; right: 0; width: var(--icon-button-size); height: 100%; margin: 0;
  opacity: 0; cursor: pointer; }
.k-date-icon { position: absolute; right: var(--sp-2); pointer-events: none; flex: none;
  width: var(--icon-size); height: var(--icon-size); color: var(--muted); }
"""


def canonical_css() -> str:
    """Return the exact shared kit CSS used by server-rendered surfaces."""
    return controls_css("paper").rstrip() + "\n"


def canonical_region(css: str) -> str:
    """Extract the generated region, rejecting missing or reordered markers."""
    start = css.find(CANONICAL_START)
    end = css.find(CANONICAL_END)
    if start < 0 or end < 0 or end < start:
        return ""
    region_start = start + len(CANONICAL_START)
    return css[region_start:end].strip() + "\n"


def render_css() -> str:
    """Render the committed React stylesheet deterministically."""
    return (
        _BANNER
        + CANONICAL_START
        + "\n"
        + canonical_css()
        + CANONICAL_END
        + "\n\n"
        + REACT_EXTENSIONS_START
        + "\n"
        + REACT_EXTENSIONS
        + REACT_EXTENSIONS_END
        + "\n"
    )


def write() -> None:
    CSS_OUTPUT.write_text(render_css(), encoding="utf-8", newline="\n")
    print(f"wrote {CSS_OUTPUT.relative_to(PROJECT_ROOT)}")


def check() -> bool:
    """Return whether the committed stylesheet is the deterministic output."""
    expected = render_css()
    if not CSS_OUTPUT.exists():
        print(f"DRIFT: {CSS_OUTPUT.relative_to(PROJECT_ROOT)} does not exist")
        return False
    actual = CSS_OUTPUT.read_text(encoding="utf-8")
    if actual == expected:
        print("design controls: up to date")
        return True
    print("DRIFT: design-system/src/styles/controls.css differs from generated controls")
    diff = difflib.unified_diff(
        expected.splitlines(), actual.splitlines(), fromfile="expected", tofile="actual", n=2
    )
    print("\n".join(diff))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check without writing")
    args = parser.parse_args()
    if args.check:
        return 0 if check() else 1
    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
