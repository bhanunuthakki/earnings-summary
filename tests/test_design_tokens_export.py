"""Drift guard for the React design-system's generated token layer.

`scripts/gen_design_tokens.py` mirrors `src/ui/tokens.py` (the single source
of truth) into `design-system/src/tokens/tokens.css` and
`tokens.generated.ts`. Those two files are committed, not built on the fly,
so nothing guarantees they stay in sync with `tokens.py` except this test:
it runs the generator's `--check` mode, which regenerates both outputs in
memory and diffs them against what's on disk. If `tokens.py` changes without
re-running `python scripts/gen_design_tokens.py`, this test fails the same
pytest run that already exercises `tests/test_ui_controls.py` — see
docs/design_system_react_port_plan.md §3.
"""

from __future__ import annotations

from pathlib import Path

from scripts import gen_design_tokens

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_generated_token_files_match_ui_tokens() -> None:
    """`gen_design_tokens.check()` must report clean: regenerating
    `tokens.css` / `tokens.generated.ts` in memory from `ui.tokens` produces
    byte-identical output to what's committed under `design-system/src/tokens/`.
    """
    assert gen_design_tokens.check() is True, (
        "design-system/src/tokens/{tokens.css,tokens.generated.ts} are stale "
        "relative to src/ui/tokens.py — regenerate with "
        "`python scripts/gen_design_tokens.py` and commit the result."
    )


def test_generated_css_is_nonempty_and_has_all_three_theme_blocks() -> None:
    """Sanity check the generator actually produced the paper/white/dark
    contract `palette_css("paper")` promises, not just an empty diff."""
    css = gen_design_tokens.render_css()
    assert ":root {" in css
    assert ':root[data-theme="white"]' in css
    assert ':root[data-theme="dark"]' in css


def test_generated_ts_has_no_broken_string_literals() -> None:
    """Regression guard for a real bug caught during Phase 1 authoring: a
    naive `repr(value).replace("'", '"')` serializer corrupted FONT_TOKENS
    values (which contain embedded single-quoted font names like
    `'Inter'`) into invalid JS/TS (`""Inter", ...`). The generator now uses
    `json.dumps` for value/key serialization; assert the known offender
    round-trips as a single well-formed double-quoted string.
    """
    ts = gen_design_tokens.render_ts()
    assert '"sans": "\'Inter\', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",' in ts
    assert '""Inter"' not in ts


def test_generated_outputs_and_token_barrel_expose_indent_and_rail_families() -> None:
    """Generated mirrors and the stable React import surface expose both
    layout families independently rather than folding them into another scale."""
    css = gen_design_tokens.render_css()
    ts = gen_design_tokens.render_ts()
    barrel = (PROJECT_ROOT / "design-system" / "src" / "tokens" / "index.ts").read_text(
        encoding="utf-8"
    )

    for name, value in {
        "indent-0": "0",
        "indent-1": "4px",
        "indent-4": "16px",
        "rail-sm": "360px",
        "rail-lg": "400px",
    }.items():
        assert f"--{name}: {value};" in css

    assert "export const INDENT_TOKENS" in ts
    assert "export const RAIL_TOKENS" in ts
    assert '"indent-0": "0",' in ts
    assert '"indent-4": "16px",' in ts
    assert '"rail-sm": "360px",' in ts
    assert '"rail-lg": "400px",' in ts
    assert "  INDENT_TOKENS," in barrel
    assert "  RAIL_TOKENS," in barrel
