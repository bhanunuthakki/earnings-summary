"""Generate the React design-system's token layer from ``src/ui/tokens.py``.

``src/ui/tokens.py`` is the single source of truth for every color, type,
spacing, and chrome value in the app (see its module docstring). This script
mirrors those Python dicts into the ``design-system/`` npm package as two
generated files:

- ``design-system/src/tokens/tokens.css`` — the literal output of
  ``ui.tokens.palette_css("paper")``, byte-for-byte identical to what the
  Flask surfaces inline, so the CSS custom-property layer can never drift by
  hand-editing.
- ``design-system/src/tokens/tokens.generated.ts`` — typed TS mirrors of the
  same Python dicts (``PALETTE_LIGHT``, ``PALETTE_DARK``,
  ``PALETTE_WHITE_OVERRIDES``, ``FONT_TOKENS``, ``TYPE_SCALE``,
  ``SPACING_SCALE``, ``CHROME_TOKENS``, ``CHART_SERIES``) plus derived
  ``Theme`` and ``Tone`` types.

Both files are committed (the JS build never needs Python at build time).
Usage::

    python scripts/gen_design_tokens.py            # write/overwrite both files
    python scripts/gen_design_tokens.py --check     # diff only, nonzero on drift

Stdlib only — no new Python dependencies (per docs/design_system_react_port_plan.md §3).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui.tokens import (  # noqa: E402
    CHART_SERIES,
    CHROME_TOKENS,
    FONT_TOKENS,
    PALETTE_DARK,
    PALETTE_LIGHT,
    PALETTE_WHITE_OVERRIDES,
    SPACING_SCALE,
    TYPE_SCALE,
    palette_css,
)

TOKENS_DIR = PROJECT_ROOT / "design-system" / "src" / "tokens"
CSS_OUTPUT = TOKENS_DIR / "tokens.css"
TS_OUTPUT = TOKENS_DIR / "tokens.generated.ts"

_GENERATED_WARNING = (
    "GENERATED from src/ui/tokens.py — do not hand-edit.\n"
    " * Regenerate: python scripts/gen_design_tokens.py"
)


def render_css() -> str:
    """The exact ``tokens.css`` file contents: a generated-file header
    followed verbatim by ``palette_css("paper")`` — the same function output
    the Flask pages inline (plan §3.2).

    Note: no git-hash version stamp is embedded here (plan §7.6 floats this
    as a future nicety) — baking the generation-time HEAD hash into a file
    that is itself committed in the same commit is self-contradictory (the
    hash goes stale the instant the commit lands), and would make the
    ``--check`` drift guard flap on every commit that touches these files
    without ``tokens.py`` itself changing. If a version stamp is wanted
    later, it should be resolved as a stable content hash of ``tokens.py``,
    not the repo HEAD.
    """
    banner = (
        "/*\n"
        f" * {_GENERATED_WARNING}\n"
        " *\n"
        ' * palette_css("paper"): light `:root` + `[data-theme=\"white\"]` +\n'
        ' * `[data-theme=\"dark\"]` override blocks. Identical by construction to\n'
        " * what the Python Flask surfaces inline via ui.tokens.palette_css.\n"
        " */\n\n"
    )
    return banner + palette_css("paper")


def _ts_object_literal(name: str, data: dict[str, str], indent: str = "  ") -> str:
    # json.dumps gives correct JS/TS string-literal escaping (quotes,
    # backslashes, etc.) — the Python dicts include values with embedded
    # single quotes (FONT_TOKENS' `'Inter'` family stacks), so a naive
    # repr()-based quote swap corrupts those into invalid syntax.
    lines = [f"export const {name} = {{"]
    for key, value in data.items():
        lines.append(f"{indent}{json.dumps(key)}: {json.dumps(value)},")
    lines.append("} as const;")
    return "\n".join(lines)


def _ts_tuple_literal(name: str, data: tuple[str, ...]) -> str:
    items = ", ".join(json.dumps(v) for v in data)
    return f"export const {name} = [{items}] as const;"


def render_ts() -> str:
    """Typed TS mirrors of the Python token dicts, plus derived ``Theme`` and
    ``Tone`` types (plan §3.3). Every value is emitted verbatim from the
    Python dicts — nothing here is invented."""
    banner = (
        "/**\n"
        f" * {_GENERATED_WARNING}\n"
        " */\n\n"
    )

    blocks = [
        _ts_object_literal("PALETTE_LIGHT", PALETTE_LIGHT),
        _ts_object_literal("PALETTE_WHITE_OVERRIDES", PALETTE_WHITE_OVERRIDES),
        _ts_object_literal("PALETTE_DARK", PALETTE_DARK),
        _ts_object_literal("FONT_TOKENS", FONT_TOKENS),
        _ts_object_literal("TYPE_SCALE", TYPE_SCALE),
        _ts_object_literal("SPACING_SCALE", SPACING_SCALE),
        _ts_object_literal("CHROME_TOKENS", CHROME_TOKENS),
        _ts_tuple_literal("CHART_SERIES", CHART_SERIES),
    ]

    # Derived types. Theme mirrors palette_css's supported `default` args plus
    # the "white" override surface it emits as a `[data-theme="white"]` block
    # (ui.tokens.PALETTE_WHITE_OVERRIDES). Tone mirrors the semantic status
    # keys present in every palette (ok/warn/bad) plus the interactive accent
    # tone the kit's `.k-pill`/`.k-chip`/`.k-well` variants use.
    types = (
        'export type Theme = "paper" | "white" | "dark";\n'
        'export type Tone = "ok" | "warn" | "bad" | "accent";\n'
    )

    return banner + "\n\n".join(blocks) + "\n\n" + types


def generate() -> dict[Path, str]:
    """Return {output path: file contents} for both generated files."""
    return {
        CSS_OUTPUT: render_css(),
        TS_OUTPUT: render_ts(),
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
            print(f"DRIFT: {rel} does not exist — run `python scripts/gen_design_tokens.py`")
            ok = False
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            print(
                f"DRIFT: {rel} is out of date relative to src/ui/tokens.py.\n"
                "  Regenerate with: python scripts/gen_design_tokens.py"
            )
            ok = False
    if ok:
        print("design tokens: up to date")
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
