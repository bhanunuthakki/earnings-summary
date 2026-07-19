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

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gen_design_tokens  # noqa: E402


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
