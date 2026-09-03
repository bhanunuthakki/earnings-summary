"""Ownership regressions for the Portfolio visual family."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
OWNED = (
    "portfolio_panel.py",
    "portfolio_console_panel.py",
    "positioning_panel.py",
    "position_lifecycle_panel.py",
    "allocation_recommendation_panel.py",
    "allocation_decisions_panel.py",
    "advisor_memos_panel.py",
)


def test_owned_consumers_import_the_family_master_and_define_no_css_literals() -> None:
    for name in OWNED:
        source = (ROOT / "src" / "pipeline" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        css_assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and "CSS" in target.id for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and "{" in node.value.value
        ]
        assert not css_assignments, f"{name} contains a CSS literal"
        assert "portfolio_styles" in source


@pytest.mark.parametrize(
    "name",
    ["portfolio_styles.py", *OWNED],
)
def test_portfolio_family_modules_compile(name: str) -> None:
    source = (ROOT / "src" / "pipeline" / name).read_text(encoding="utf-8")
    compile(source, name, "exec")


def test_family_master_exposes_each_composition() -> None:
    source = (ROOT / "src" / "pipeline" / "portfolio_styles.py").read_text(encoding="utf-8")
    for name in (
        "portfolio_css",
        "console_css",
        "positioning_css",
        "lifecycle_css",
        "allocation_css",
        "decisions_css",
        "memos_css",
        "page_css",
    ):
        assert f"def {name}()" in source


def test_performance_methodology_popover_is_viewport_bounded_on_narrow_screens() -> None:
    source = (ROOT / "src" / "pipeline" / "portfolio_styles.py").read_text(encoding="utf-8")
    assert "@media (max-width:700px) { .pf-info-pop" in source
    assert "position:fixed" in source
    assert "right:var(--sp-3); left:var(--sp-3); width:auto" in source
