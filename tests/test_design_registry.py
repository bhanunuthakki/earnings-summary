"""Contract tests for the typed design-language registry.

The registry deliberately holds data only. Scanner behaviour belongs to the
existing conformance test; these tests pin the data vocabulary and the narrow
projections that scanner consumes.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui import design_registry as registry  # noqa: E402
from ui.controls import controls_css  # noqa: E402


def _rule_bodies(css: str, selector: str) -> tuple[str, ...]:
    bodies: list[str] = []
    for rule in css.split("}"):
        selectors, marker, body = rule.partition("{")
        selectors = re.sub(r"/\*.*?\*/", "", selectors, flags=re.DOTALL)
        if marker and selector in {item.strip() for item in selectors.split(",")}:
            bodies.append(body)
    return tuple(bodies)


def _rule_body(css: str, selector: str) -> str:
    bodies = _rule_bodies(css, selector)
    assert len(bodies) == 1, (
        f"registered selector must have exactly one rule body: {selector}; found {len(bodies)}"
    )
    return bodies[0]


def _shape_signature(body: str) -> tuple[str | None, str | None, str | None]:
    def property_value(name: str) -> str | None:
        matches = re.findall(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", body)
        return matches[-1].strip() if matches else None

    return (
        property_value("border-radius"),
        property_value("border"),
        property_value("box-shadow"),
    )


def _expected_shape_signature(
    item: registry.ShapeSignature,
) -> tuple[str | None, str | None, str | None]:
    border = item.border_signature
    if border is not None:
        border = " ".join(
            f"var(--{part})" if part in {"bw-thin", "border"} else part for part in border.split()
        )
    return (
        f"var(--{item.radius_token})" if item.radius_token else None,
        border,
        f"var(--{item.elevation_token})" if item.elevation_token else None,
    )


def _grid_signature(body: str) -> str | None:
    match = re.search(r"grid-template-columns\s*:\s*([^;]+)", body)
    return match.group(1).strip() if match else None


def _shape_signature_for_selector(
    css: str, selector: str
) -> tuple[str | None, str | None, str | None]:
    body = _rule_body(css, selector)
    if selector == ".k-overlay.k-drawer":
        body = _rule_body(css, ".k-overlay") + ";" + body
    return _shape_signature(body)


def _expected_grid_signature(item: registry.GridSignature) -> str:
    return (
        item.column_signature.replace("rail-sm", "var(--rail-sm)")
        .replace("rail-lg", "var(--rail-lg)")
        .replace("grid-card-sm", "var(--grid-card-sm)")
        .replace("grid-card-md", "var(--grid-card-md)")
        .replace("grid-card-lg", "var(--grid-card-lg)")
    )


def _documentation_rows(text: str, kind: str) -> tuple[str, ...]:
    start = f"<!-- design-registry:{kind}:start -->"
    end = f"<!-- design-registry:{kind}:end -->"
    assert text.count(start) == 1 and text.count(end) == 1
    section = text.split(start, 1)[1].split(end, 1)[0]
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|", section, flags=re.MULTILINE)
    return tuple(rows)


def test_registry_is_frozen_typed_and_complete() -> None:
    assert registry.REGISTRY_VERSION == "1.1.0"
    records = (
        registry.SHAPE_ARCHETYPES[0],
        registry.SHAPE_ARCHETYPES[0].signatures[0],
        registry.GRID_ARCHETYPES[0],
        registry.GRID_ARCHETYPES[0].signatures[0],
        registry.TITLE_PLACEMENTS[0],
        registry.PERMANENT_EXEMPTIONS[0],
        registry.QUARANTINE_ENTRIES[0],
        registry.BESPOKE_BUTTON_APPROVALS[0],
        registry.MONO_TABLE_APPROVALS[0],
        registry.SurfaceSanction("test", "radius", frozenset(), "test", "test"),
        registry.CCACTION_REGRESSION_FLOOR[0],
    )
    assert all(is_dataclass(record) for record in records)
    with pytest.raises(FrozenInstanceError):
        setattr(registry.SHAPE_ARCHETYPES[0], "name", "changed")

    assert isinstance(registry.REGISTERED, frozenset)
    assert len(registry.REGISTERED) == 69
    assert len(registry.PERMANENT_EXEMPTIONS) == 3
    assert len(registry.QUARANTINE_ENTRIES) == 10
    assert len({entry.surface for entry in registry.QUARANTINE_ENTRIES}) == 9
    assert len(registry.BESPOKE_BUTTON_APPROVALS) == 17
    assert len(registry.MONO_TABLE_APPROVALS) == 1
    assert registry.SURFACE_SANCTIONS == ()
    assert len(registry.CCACTION_REGRESSION_FLOOR) == 28
    with pytest.raises(TypeError):
        exec('registry.DOCUMENTATION_PROJECTIONS["shapes"] = ()')
    for name in (
        "CHROME_TOKENS",
        "INDENT_TOKENS",
        "RAIL_TOKENS",
        "PALETTE_DARK",
        "PALETTE_LIGHT",
    ):
        with pytest.raises(TypeError):
            exec(f'registry.{name}["__mutation_probe__"] = "changed"')


def test_metadata_is_nonblank_and_quarantine_expiry_is_typed() -> None:
    governed = (
        *registry.PERMANENT_EXEMPTIONS,
        *registry.QUARANTINE_ENTRIES,
        *registry.BESPOKE_BUTTON_APPROVALS,
        *registry.MONO_TABLE_APPROVALS,
        *registry.SURFACE_SANCTIONS,
        *registry.CCACTION_REGRESSION_FLOOR,
    )
    assert all(entry.owner.strip() and entry.rationale.strip() for entry in governed)
    assert {entry.expires_on for entry in registry.QUARANTINE_ENTRIES} == {date(2026, 10, 1)}
    assert all(isinstance(entry.expires_on, date) for entry in registry.QUARANTINE_ENTRIES)
    assert all(entry.expires_on > date.today() for entry in registry.QUARANTINE_ENTRIES)


def test_registry_shape_grid_and_title_signatures_match_the_kit() -> None:
    css = controls_css("dark")
    assert len(registry.SHAPES_BY_SELECTOR) == sum(
        len(archetype.signatures) for archetype in registry.SHAPE_ARCHETYPES
    )
    assert len(registry.GRIDS_BY_SELECTOR) == sum(
        len(archetype.signatures) for archetype in registry.GRID_ARCHETYPES
    )
    for selector, item in registry.SHAPES_BY_SELECTOR.items():
        assert _shape_signature_for_selector(css, selector) == _expected_shape_signature(item)
    for selector, item in registry.GRIDS_BY_SELECTOR.items():
        assert _grid_signature(_rule_body(css, selector)) == _expected_grid_signature(item)
    for placement in registry.TITLE_PLACEMENTS:
        if placement.selector is not None:
            assert _rule_bodies(css, placement.selector)


def test_signature_comparators_turn_red_for_one_changed_value() -> None:
    shape = registry.SHAPES_BY_SELECTOR[".k-card"]
    expected = _expected_shape_signature(shape)
    mutated_shape_bodies = (
        "border-radius: var(--radius); border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-card);",
        "border-radius: var(--radius-card); border: var(--bw-thick) solid var(--border); "
        "box-shadow: var(--shadow-card);",
        "border-radius: var(--radius-card); border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-pop);",
    )
    assert all(_shape_signature(body) != expected for body in mutated_shape_bodies)

    grid = registry.GRIDS_BY_SELECTOR[".k-grid-matrix"]
    mutated_grid = _grid_signature("grid-template-columns: repeat(2, minmax(0, 1fr));")
    assert mutated_grid != _expected_grid_signature(grid)

    duplicate_override = controls_css("dark") + "\n.k-card { border-radius: var(--radius); }"
    with pytest.raises(AssertionError, match="exactly one rule body"):
        _shape_signature_for_selector(duplicate_override, ".k-card")


def test_documentation_projection_is_bidirectional() -> None:
    text = (PROJECT_ROOT / "directives" / "design_language.md").read_text(encoding="utf-8")
    assert tuple(registry.DOCUMENTATION_PROJECTIONS) == (
        "shapes",
        "grids",
        "indents",
        "titles",
        "exemptions",
        "quarantine",
        "bespoke-buttons",
        "mono-tables",
        "sanctions",
        "ccaction-floor",
    )
    for kind, projection in registry.DOCUMENTATION_PROJECTIONS.items():
        assert _documentation_rows(text, kind) == projection


def test_registry_is_not_a_discovered_surface() -> None:
    registry_source = (SRC / "ui" / "design_registry.py").read_text(encoding="utf-8")
    discovery_signal = "var(" + "--"
    assert discovery_signal not in registry_source
    discovered = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if discovery_signal in path.read_text(encoding="utf-8")
    }
    assert "ui/design_registry.py" not in discovered


def test_ui_controls_has_no_registry_owned_declarations_or_direct_token_imports() -> None:
    source_path = PROJECT_ROOT / "tests" / "test_ui_controls.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    prohibited = {
        "REGISTERED",
        "EXEMPT",
        "QUARANTINE",
        "_BESPOKE_BUTTON_OK",
        "_MONO_TABLE_ALLOWLIST",
        "_FONT_SIZE_EXEMPT",
        "_RADIUS_SANCTIONED",
        "_CCACTION_PINNED",
    }
    local_names = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert not (local_names & prohibited)
    token_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ui.tokens"
    ]
    assert not token_imports
