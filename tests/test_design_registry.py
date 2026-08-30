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


def test_registry_is_frozen_typed_and_complete() -> None:
    assert registry.REGISTRY_VERSION == "1.10.2"
    records = (
        registry.CARD_ARCHETYPES[0],
        registry.SHAPE_ARCHETYPES[0],
        registry.SHAPE_ARCHETYPES[0].signatures[0],
        registry.GRID_ARCHETYPES[0],
        registry.GRID_ARCHETYPES[0].signatures[0],
        registry.TITLE_PLACEMENTS[0],
        registry.PERMANENT_EXEMPTIONS[0],
        registry.BESPOKE_BUTTON_APPROVALS[0],
        registry.MONO_TABLE_APPROVALS[0],
        registry.SurfaceSanction("test", "radius", frozenset(), "test", "test"),
        registry.CCACTION_REGRESSION_FLOOR[0],
    )
    assert all(is_dataclass(record) for record in records)
    with pytest.raises(FrozenInstanceError):
        setattr(registry.SHAPE_ARCHETYPES[0], "name", "changed")

    assert isinstance(registry.REGISTERED, frozenset)
    assert isinstance(registry.GOVERNED, frozenset)
    assert len(registry.REGISTERED) == 111
    assert len(registry.VISUAL_EMITTER_MANIFEST) == 155
    assert len(registry.GOVERNED) == 132
    assert (
        frozenset(
            {
                "ui/controls.py",
                "ui/cite_marks.py",
                "ui/source_chip.py",
                "dashboard/_styles.py",
                "execution/build_earnings_calendar.py",
                "pipeline/analysis_styles.py",
                "pipeline/portfolio_styles.py",
                "pipeline/operations_styles.py",
                "pipeline/research_panel_styles.py",
                "report/renderers/workspace_styles.py",
                "pipeline/work_os_styles.py",
                "report/renderers/workspace_charts.py",
                "ui/living_grid.py",
                "viewspec/render.py",
                "design-system/src/styles/controls.css",
                "design-system/src/tokens/tokens.css",
            }
        )
        == registry.MASTER_SOURCES
    )
    assert (
        frozenset(
            {
                "ui/controls.py",
                "design-system/src/styles/controls.css",
                "design-system/src/tokens/tokens.css",
            }
        )
        == registry.GLOBAL_MASTER_SOURCES
    )
    assert (
        registry.FAMILY_MASTER_SOURCES == registry.MASTER_SOURCES - registry.GLOBAL_MASTER_SOURCES
    )
    assert registry.MASTER_SOURCES <= registry.GOVERNED
    assert len(registry.PERMANENT_EXEMPTIONS) == 2
    assert registry.QUARANTINE_ENTRIES == ()
    assert len(registry.BESPOKE_BUTTON_APPROVALS) == 17
    assert len(registry.MONO_TABLE_APPROVALS) == 1
    assert registry.SURFACE_SANCTIONS == ()
    assert len(registry.CCACTION_REGRESSION_FLOOR) == 27
    for name in (
        "CHROME_TOKENS",
        "INDENT_TOKENS",
        "RAIL_TOKENS",
        "PALETTE_DARK",
        "PALETTE_LIGHT",
    ):
        with pytest.raises(TypeError):
            exec(f'registry.{name}["__mutation_probe__"] = "changed"')


def test_visual_emitter_manifest_is_complete_immutable_and_drives_registered() -> None:
    assert all(is_dataclass(entry) for entry in registry.VISUAL_EMITTER_MANIFEST)
    assert all(
        entry.path and entry.owner.strip() and entry.rationale.strip()
        for entry in registry.VISUAL_EMITTER_MANIFEST
    )
    assert all(
        entry.adapter_kinds and entry.evidence_modes for entry in registry.VISUAL_EMITTER_MANIFEST
    )
    assert (
        frozenset(
            entry.path
            for entry in registry.VISUAL_EMITTER_MANIFEST
            if entry.disposition is registry.EmitterDisposition.PRODUCTION
        )
        == registry.REGISTERED
    )
    with pytest.raises(FrozenInstanceError):
        setattr(registry.VISUAL_EMITTER_MANIFEST[0], "path", "changed")


def test_visual_emitter_manifest_rejects_conflicting_records_and_blank_metadata() -> None:
    valid = registry.VisualEmitterEntry(
        "ui/example.py",
        registry.EmitterDisposition.PRODUCTION,
        frozenset({registry.EvidenceAdapter.HTML}),
        frozenset({registry.EvidenceMode.STATIC}),
        "design-system",
        "Example governed emitter.",
    )
    duplicate = registry.VisualEmitterEntry(
        "ui/example.py",
        registry.EmitterDisposition.PRODUCTION,
        frozenset({registry.EvidenceAdapter.HTML}),
        frozenset({registry.EvidenceMode.STATIC}),
        "design-system",
        "Same path with conflicting evidence.",
    )
    with pytest.raises(ValueError, match="duplicate"):
        registry.validate_visual_emitter_manifest((valid, duplicate))
    with pytest.raises(ValueError, match="owner"):
        registry.validate_visual_emitter_manifest(
            (
                registry.VisualEmitterEntry(
                    "ui/example.py",
                    registry.EmitterDisposition.PRODUCTION,
                    frozenset({registry.EvidenceAdapter.HTML}),
                    frozenset({registry.EvidenceMode.STATIC}),
                    " ",
                    "Example governed emitter.",
                ),
            )
        )


def test_shipped_visual_emitters_cannot_receive_blanket_exemption() -> None:
    manifest_paths = {entry.path for entry in registry.VISUAL_EMITTER_MANIFEST}
    assert manifest_paths >= registry.EXEMPT
    assert {"ui/tokens.py", "ui/conformance_scan.py"} == registry.EXEMPT
    charts = next(
        entry
        for entry in registry.VISUAL_EMITTER_MANIFEST
        if entry.path == "report/renderers/charts_v2.py"
    )
    assert registry.EvidenceAdapter.SVG in charts.adapter_kinds
    assert registry.EvidenceMode.SCOPED in charts.evidence_modes


def test_local_property_contracts_are_scoped_and_immutable() -> None:
    assert len(registry.LOCAL_PROPERTY_CONTRACTS) == 10
    assert all(
        contract.name.startswith("--")
        and contract.surfaces
        and contract.owner.strip()
        and contract.rationale.strip()
        and contract.value_grammar
        for contract in registry.LOCAL_PROPERTY_CONTRACTS
    )
    with pytest.raises(FrozenInstanceError):
        setattr(registry.LOCAL_PROPERTY_CONTRACTS[0], "name", "--changed")


def test_every_master_has_one_pinned_geometry_recipe() -> None:
    contracts = registry.MASTER_GEOMETRY_CONTRACTS
    assert {contract.surface for contract in contracts} == registry.MASTER_SOURCES
    assert len(contracts) == len(registry.MASTER_SOURCES)
    assert all(re.fullmatch(r"[0-9a-f]{64}", contract.digest) for contract in contracts)
    assert all(contract.owner and contract.rationale for contract in contracts)


def test_dynamic_visual_recipes_are_pinned_and_typed() -> None:
    contracts = registry.DYNAMIC_VISUAL_CONTRACTS
    surfaces = [contract.surface for contract in contracts]
    assert len(surfaces) == len(set(surfaces))
    assert all(re.fullmatch(r"[0-9a-f]{64}", contract.digest) for contract in contracts)
    assert all(contract.owner and contract.rationale for contract in contracts)


def test_metadata_is_nonblank_and_quarantine_is_empty() -> None:
    governed = (
        *registry.PERMANENT_EXEMPTIONS,
        *registry.QUARANTINE_ENTRIES,
        *registry.BESPOKE_BUTTON_APPROVALS,
        *registry.MONO_TABLE_APPROVALS,
        *registry.SURFACE_SANCTIONS,
        *registry.CCACTION_REGRESSION_FLOOR,
    )
    assert all(entry.owner.strip() and entry.rationale.strip() for entry in governed)
    assert registry.QUARANTINE_ENTRIES == ()


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


def test_card_archetypes_are_closed_unique_and_backed_by_kit_css() -> None:
    css = controls_css("dark")
    assert [item.name for item in registry.CARD_ARCHETYPES] == [
        "section",
        "stat",
        "action",
        "navigation",
    ]
    assert len({item.selector for item in registry.CARD_ARCHETYPES}) == len(
        registry.CARD_ARCHETYPES
    )
    for item in registry.CARD_ARCHETYPES:
        body = _rule_body(css, item.selector)
        assert body
        if item.title_selector is not None:
            assert _rule_bodies(css, item.title_selector)


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


def test_agent_facing_design_contract_is_compact_and_points_to_executable_authority() -> None:
    directive = (PROJECT_ROOT / "directives" / "design_language.md").read_text(encoding="utf-8")
    agent_contract = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    # Near-current ratchets: a real rule may be added only by consolidating the
    # contract, not by restarting the accumulation this migration removes.
    assert len(directive.splitlines()) <= 190
    assert len(directive.encode("utf-8")) <= 9_000
    assert "<!-- design-registry:" not in directive
    assert "Historical note" not in directive
    for executable_owner in (
        "src/ui/tokens.py",
        "src/ui/controls.py",
        "src/ui/design_registry.py",
        "src/ui/conformance_scan.py",
        "execution/verify_design_conformance.py",
        "scripts/check_design_sync.py",
    ):
        assert executable_owner in directive

    ui_section = agent_contract.split("## UI / Front-end", 1)[1].split("\n## ", 1)[0]
    assert len(ui_section.splitlines()) <= 10
    assert "The guard is partial" not in ui_section
    assert "scripts/check_design_sync.py" in ui_section


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
