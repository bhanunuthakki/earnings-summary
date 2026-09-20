"""Generate the Streamlit sandbox theme from ``src/ui/tokens.py``.

The ``explore-sandbox/`` Streamlit surface must carry TOKEN PARITY with the
Flask cockpit (the settled sandbox scope): the same palette, fonts, and type
scale, derived from the single source — never hand-copied. This script
follows the ``gen_design_tokens.py`` pattern: render from ``ui.tokens``,
commit the artifacts, and fail ``python scripts/check_design_sync.py`` on
drift.

Two generated files under ``explore-sandbox/``:

- ``explore-sandbox/.streamlit/config.toml`` — Streamlit's native
  ``[theme]`` block: ``base = "dark"`` plus the dashboard palette's four
  color mapping points. Streamlit's theme keys are coarse (one primary, one
  page background, one widget/sidebar ground, one text color); the mapping
  is documented below and everything else rides the CSS layer.
- ``explore-sandbox/theme/tokens.css`` — ``palette_css("dark")``
  byte-for-byte (the dashboard surfaces' dark-only ``:root``), which the
  sandbox app injects so the full custom-property layer (fonts, type scale,
  spacing, chrome, series colors) reaches sandbox DOM.

Stdlib only — no streamlit import, no new Python dependencies: the sandbox
is an optional-dependency surface, and this generator must run in every
checkout (the design-sync gate calls it).

Usage::

    python scripts/gen_streamlit_theme.py            # write/overwrite both files
    python scripts/gen_streamlit_theme.py --check    # diff only, nonzero on drift
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.tokens import PALETTE_DARK, palette_css  # noqa: E402

SANDBOX_DIR = PROJECT_ROOT / "explore-sandbox"
CONFIG_OUTPUT = SANDBOX_DIR / ".streamlit" / "config.toml"
CSS_OUTPUT = SANDBOX_DIR / "theme" / "tokens.css"

_GENERATED_WARNING = (
    "GENERATED from src/ui/tokens.py — do not hand-edit.\n"
    "Regenerate: python scripts/gen_streamlit_theme.py"
)

# The config.toml mapping, stated once. Streamlit's [theme] surface:
#   base                   — "dark": the dashboard surfaces' only theme
#   primaryColor           — --accent: interactive elements only (links,
#                            active tabs, selection), never a status color
#   backgroundColor        — --bg: the page ground
#   secondaryBackgroundColor — --paper: Streamlit's widget/sidebar/card
#                            ground; the cockpit's raised-panel dark
#   textColor              — --fg
# Type parity is NOT expressible here (config.toml's font key cannot carry
# the Inter stack) — the CSS layer owns fonts and the type scale.
_THEME_KEYS: tuple[tuple[str, str], ...] = (
    ("primaryColor", "accent"),
    ("backgroundColor", "bg"),
    ("secondaryBackgroundColor", "paper"),
    ("textColor", "fg"),
)


def render_config() -> str:
    """The exact ``config.toml`` contents: generated-file header plus the
    ``[theme]`` block, every value emitted verbatim from ``PALETTE_DARK``."""
    lines = [
        f"# {_GENERATED_WARNING}",
        "",
        "[theme]",
        'base = "dark"',
    ]
    for theme_key, palette_key in _THEME_KEYS:
        lines.append(f'{theme_key} = "{PALETTE_DARK[palette_key]}"')
    return "\n".join(lines) + "\n"


def render_css() -> str:
    """The exact ``tokens.css`` contents: a generated-file header followed
    verbatim by ``palette_css("dark")`` — the same function output the
    dashboard Flask surfaces inline, so the sandbox's custom-property layer
    can never drift by hand-editing."""
    banner = (
        "/*\n"
        f" * {_GENERATED_WARNING}\n"
        " *\n"
        ' * palette_css("dark"): the dashboard surfaces\' dark-only `:root`.\n'
        " * Identical by construction to what the Flask surfaces inline via\n"
        " * ui.tokens.palette_css; the sandbox injects this file as its\n"
        " * custom-property layer.\n"
        " */\n\n"
    )
    return banner + palette_css("dark")


def generate() -> dict[Path, str]:
    """Return {output path: file contents} for both generated files."""
    return {
        CONFIG_OUTPUT: render_config(),
        CSS_OUTPUT: render_css(),
    }


def write() -> None:
    for path, contents in generate().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(PROJECT_ROOT)}")


def check() -> bool:
    """Regenerate in-memory and diff against the committed files. Returns
    True if clean (no drift), False otherwise. Never writes files."""
    ok = True
    for path, expected in generate().items():
        rel = path.relative_to(PROJECT_ROOT)
        if not path.exists():
            print(f"DRIFT: {rel} does not exist — run `python scripts/gen_streamlit_theme.py`")
            ok = False
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            print(
                f"DRIFT: {rel} is out of date relative to src/ui/tokens.py.\n"
                "  Regenerate with: python scripts/gen_streamlit_theme.py"
            )
            ok = False
    if ok:
        print("streamlit sandbox theme: up to date")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff generated output against committed files; exit nonzero on drift. Writes nothing.",
    )
    args = parser.parse_args()

    if args.check:
        return 0 if check() else 1

    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
