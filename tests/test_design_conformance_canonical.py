"""Canonical contract for the one importable design-conformance scanner."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from ui import design_registry as registry  # noqa: E402
from ui.conformance_scan import (  # noqa: E402
    DIMENSIONS,
    css_text,
    discover_surfaces,
    scan_surface,
    scan_surface_evidence,
)

EXPECTED_OLD_DIMENSIONS = {
    "color",
    "font-size",
    "radius",
    "font-family",
    "alias",
    "kit-badge",
    "font-weight",
    "transition",
}
EXPECTED_NEW_DIMENSIONS = {
    "floating-card-title",
    "off-scale-indent",
    "unsanctioned-shape-geometry",
    "off-scale-grid-column",
}


def test_shared_scanner_topology_and_registry_classification() -> None:
    assert set(DIMENSIONS) == EXPECTED_OLD_DIMENSIONS | EXPECTED_NEW_DIMENSIONS
    assert not (SRC / "ui" / "conformance.py").exists()
    assert "ui/conformance_scan.py" in registry.REGISTERED
    assert "ui/conformance_scan.py" in registry.EXEMPT
    assert "ui/conformance_scan.py" in discover_surfaces(SRC)
    assert "ui/design_registry.py" not in discover_surfaces(SRC)

    guard_tree = ast.parse((PROJECT_ROOT / "tests" / "test_ui_controls.py").read_text("utf-8"))
    imports = {
        alias.name
        for node in ast.walk(guard_tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ui.conformance_scan"
        for alias in node.names
    }
    assert {"DIMENSIONS", "css_text", "discover_surfaces", "scan_surface"} <= imports
    locally_defined = {
        node.name
        for node in guard_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {"scan_surface", "_css_text", "_discovered_surfaces"} & locally_defined


@pytest.mark.parametrize(
    ("dimension", "text"),
    [
        ("color", ".x { color: #abc123; }"),
        ("font-size", ".x { font-size: 19px; }"),
        ("radius", ".x { border-radius: 4px; }"),
        ("font-family", ".x { font-family: 'Roboto', sans-serif; }"),
        ("alias", ".x { color: var(--ink); }"),
        (
            "kit-badge",
            ".x-pill { background: color-mix(in srgb, var(--bad) 16%, transparent); }",
        ),
        ("font-weight", ".x { font-weight: 700; }"),
        ("transition", ".x { transition: all 150ms ease; }"),
    ],
)
def test_existing_dimensions_survive_relocation(dimension: str, text: str) -> None:
    assert dimension in scan_surface("x", text)


def test_css_extraction_and_discovery_are_importable(tmp_path: Path) -> None:
    src = tmp_path / "src"
    path = src / "demo.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '"""var(--doc-only)"""\nCSS = ".x { color: var(--fg); }"\n# var(--comment)\n',
        encoding="utf-8",
    )
    (src / "comment.py").write_text("# var(--comment-only)\n", encoding="utf-8")
    (src / "doc.py").write_text('"""var(--doc-only)"""\n', encoding="utf-8")
    (src / "split.py").write_text('CSS = "var(" "--fg)"\n', encoding="utf-8")
    assert "var(--fg)" in css_text(path)
    assert "var(--doc-only)" not in css_text(path)
    assert discover_surfaces(src) == frozenset({"comment.py", "demo.py", "doc.py"})


def test_floating_title_flags_only_nested_uncomposed_headings() -> None:
    bad = '<section class="k-card"><h3 class="custom-title">Risk</h3></section>'
    assert scan_surface("x", bad)["floating-card-title"] == ["h3.custom-title"]

    for good in (
        '<section class="k-card"><h3 class="k-card-title">Risk</h3></section>',
        '<div class="k-well"><h4 class="k-well-title extra">Context</h4></div>',
        '<aside class="k-overlay k-drawer"><h2 class="cc-drawer-head">Details</h2></aside>',
        '<section><h2>Portfolio Intelligence</h2><div class="k-card"></div></section>',
    ):
        assert "floating-card-title" not in scan_surface("x", good)
    dynamic_content = (
        '<section class="k-card"><div class="dynamic-{kind}"><h3>{title}</h3></div></section>'
    )
    assert scan_surface("x", dynamic_content)["floating-card-title"] == ["h3"]


def test_indent_scope_uses_hyphen_delimited_selector_atoms() -> None:
    assert scan_surface("x", ".allocation-bucket-row { padding-left: 18px; }")[
        "off-scale-indent"
    ] == [".allocation-bucket-row:padding-left=18px"]
    assert "off-scale-indent" not in scan_surface(
        "x", ".allocation-bucket-row { padding-left: var(--indent-2); }"
    )
    for text in (
        ".street-row { padding-left: 18px; }",
        ".treehouse { margin-left: 18px; }",
        ".bucketed { padding-left: 18px; }",
        ".ordinary-layout { padding-left: 18px; }",
    ):
        assert "off-scale-indent" not in scan_surface("x", text)


def test_shape_geometry_flags_only_complete_novel_token_triples() -> None:
    bad = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); box-shadow: var(--shadow-drawer); }"
    )
    assert scan_surface("x", bad)["unsanctioned-shape-geometry"] == [".forecast-card"]

    for good in (
        ".k-card { border-radius: var(--radius-card); border: var(--bw-thin) solid "
        "var(--border); box-shadow: var(--shadow-card); }",
        ".forecast-card { border-radius: var(--radius-card); border: var(--bw-thin) "
        "solid var(--border); box-shadow: var(--shadow-card); }",
        ".forecast-card { background: var(--surface); border: var(--bw-thin) solid "
        "var(--border); border-radius: var(--radius-card); }",
        ".layout-shell { border-radius: var(--radius-card); border: var(--bw-thin) "
        "solid var(--border); box-shadow: var(--shadow-drawer); }",
        ".forecast-card { border-radius: 4px; border: 1px solid #fff; "
        "box-shadow: 0 1px 2px rgba(0,0,0,.2); }",
    ):
        assert "unsanctioned-shape-geometry" not in scan_surface("x", good)


def test_grid_scope_requires_complete_normalized_registry_signature() -> None:
    bad = ".portfolio-card-grid { grid-template-columns: 340px 1fr; }"
    assert scan_surface("x", bad)["off-scale-grid-column"] == [
        ".portfolio-card-grid:grid-template-columns=340px 1fr"
    ]
    mixed = ".portfolio-card-grid { grid-template-columns: var(--grid-card-sm) 340px; }"
    assert "off-scale-grid-column" in scan_surface("x", mixed)
    for good in (
        ".portfolio-card-grid { grid-template-columns: repeat(auto-fit, "
        "minmax(var(--grid-card-sm), 1fr)); }",
        ".research-split-rail { grid-template-columns: minmax(0, 1fr) var(--rail-sm); }",
        ".ordinary-grid { grid-template-columns: 340px 1fr; }",
        ".split-railroad { grid-template-columns: 340px 1fr; }",
    ):
        assert "off-scale-grid-column" not in scan_surface("x", good)


def test_grouped_and_duplicate_rules_cannot_hide_later_violations() -> None:
    grouped = (
        ".alpha, .portfolio-card-grid { grid-template-columns: repeat(auto-fit, "
        "minmax(var(--grid-card-sm), 1fr)); }"
    )
    assert "off-scale-grid-column" not in scan_surface("x", grouped)
    overridden = grouped + "\n.portfolio-card-grid { grid-template-columns: 340px 1fr; }"
    assert "off-scale-grid-column" in scan_surface("x", overridden)


def test_nested_css_adjacent_braces_do_not_hide_scoped_rules() -> None:
    nested = "@media (min-width: 1px) {.portfolio-card-grid { grid-template-columns: 340px 1fr; }}"
    assert "off-scale-grid-column" in scan_surface("x", nested)
    supports = (
        "@supports (display: grid) {"
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }"
        "}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", supports)
    native_nested = (
        ".forecast-card { &.active { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", native_nested)
    selector_wrapped_media = (
        ".forecast-card { @media (min-width: 1px) {"
        "border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", selector_wrapped_media)

    wrapped_grid = (
        "@media (min-width: 1px) {.portfolio-card-grid { grid-template-columns: 340px 1fr; }}"
    )
    for wrapper in (
        wrapped_grid,
        f"'''{wrapped_grid}'''",
        f"f'''{wrapped_grid}'''",
        f"<style>{wrapped_grid}</style>",
        f'<style data-label="> marker">{wrapped_grid}</style>',
    ):
        assert "off-scale-grid-column" in scan_surface("x", wrapper)


def test_first_at_rule_survives_real_python_token_extraction(tmp_path: Path) -> None:
    wrapped_grid = (
        "@media (min-width: 1px) {.portfolio-card-grid { grid-template-columns: 340px 1fr; }}"
    )
    module = tmp_path / "wrapped_css.py"
    module.write_text(f'CSS = r"""{wrapped_grid}"""\n', encoding="utf-8")
    extracted = css_text(module)
    assert "off-scale-grid-column" in scan_surface("wrapped_css.py", extracted)

    attribute_first = (
        '[data-label="@media"] .portfolio-card-grid {grid-template-columns: 340px 1fr; }'
    )
    module.write_text(f'CSS = r"""{attribute_first}"""\n', encoding="utf-8")
    extracted_attribute = css_text(module)
    assert "off-scale-grid-column" in scan_surface("wrapped_css.py", extracted_attribute)

    module.write_text(
        f'A = ".x {{ color: var(--fg); }}"\nB = """{wrapped_grid}"""\n',
        encoding="utf-8",
    )
    extracted_multiple = css_text(module)
    assert "off-scale-grid-column" in scan_surface("wrapped_css.py", extracted_multiple)

    module.write_text(
        f'A = "prefix"\nB = """{wrapped_grid}"""\n',
        encoding="utf-8",
    )
    extracted_after_plain_text = css_text(module)
    assert "off-scale-grid-column" in scan_surface("wrapped_css.py", extracted_after_plain_text)

    module.write_text(
        f'A = "prefix {{"\nB = """{wrapped_grid}"""\n',
        encoding="utf-8",
    )
    extracted_after_unmatched = css_text(module)
    assert "off-scale-grid-column" in scan_surface("wrapped_css.py", extracted_after_unmatched)

    module.write_text(
        'CSS = ".portfolio-card-grid {" "grid-template-columns: 340px 1fr; }"\n',
        encoding="utf-8",
    )
    extracted_adjacent = css_text(module)
    assert "off-scale-grid-column" in scan_surface("wrapped_css.py", extracted_adjacent)

    split_identifiers = (
        (
            'CSS = (".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-" '
            '"drawer); }")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = (".forecast-card { border-radius:var(--radius-" '
            '"drawer); border:var(--bw-thin) solid var(--border); '
            'box-shadow:var(--shadow-drawer); }")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = (".portfolio-card-grid { grid-template-columns:var(--rail-" "sm); }")\n',
            "off-scale-grid-column",
        ),
        (
            'CSS = (".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-" '
            '+ "drawer); }")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = (".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-") '
            '+ ("drawer); }")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = (".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-") '
            '+ "drawer); }"\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-" '
            '+ ("drawer); }")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-{}); "
            '}}".format("drawer")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); "
            'box-shadow:var(--shadow-{shadow}); }}".format(shadow="drawer")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".portfolio-card-grid {{ grid-template-columns:{}; }}".format("340px 1fr")\n',
            "off-scale-grid-column",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-%s); "
            '}" % "drawer"\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-" '
            '+ ("drawer); }" * 1)\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = "".join([".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-", '
            '"drawer); }"])\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-{}); "
            '}}".format("draw" + "er")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-{}); "
            '}}".format(f"{\'drawer\'}")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-%s); "
            '}" % ("draw" + "er")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-%s); "
            '}" % (("draw" + "er"),)\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); "
            'box-shadow:var(--shadow-{shadow}); }}".format_map('
            '{"shadow": "draw" + "er"})\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-%s); "
            '}" % None\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            'border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-" '
            '+ ("drawer); }" * True)\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-{}); "
            '}}".format(*(("draw" + "er",)))\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); "
            'box-shadow:var(--shadow-{shadow}); }}".format('
            '**{"shadow": "draw" + "er"})\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card[data-build=20260817] {{ '
            "border-radius:var(--radius-card); border:var(--bw-thin) solid var(--border); "
            'box-shadow:var(--shadow-{}); }}".format("drawer")\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); "
            'box-shadow:var(--shadow-%(shadow20260817)s); }" '
            '% {"shadow20260817": "drawer"}\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card {{ border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); box-shadow:var(--shadow-{}); "
            '}}".format(20260817)\n',
            "unsanctioned-shape-geometry",
        ),
        (
            'CSS = ".forecast-card { border-radius:var(--radius-card); '
            "border:var(--bw-thin) solid var(--border); "
            'box-shadow:var(--shadow-%s); }" % 20260817\n',
            "unsanctioned-shape-geometry",
        ),
    )
    for source, dimension in split_identifiers:
        module.write_text(source, encoding="utf-8")
        assert dimension in scan_surface("wrapped_css.py", css_text(module))


@pytest.mark.parametrize(
    "modifier",
    ["&", "&:hover", "&[open]", "&:hover, &[open]"],
)
def test_atom_free_parent_reference_modifiers_remain_governed(modifier: str) -> None:
    nested = (
        f".forecast-card {{ {modifier} {{ border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }} }"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", nested)


@pytest.mark.parametrize(
    "nested",
    [
        "&:hover { box-shadow: var(--shadow-drawer); }",
        "&[open] { box-shadow: var(--shadow-drawer); }",
        "&:hover, &[open] { box-shadow: var(--shadow-drawer); }",
        "@media (min-width: 1px) { &:hover { box-shadow: var(--shadow-drawer); }}",
    ],
)
def test_subject_preserving_modifiers_inherit_base_geometry(nested: str) -> None:
    css = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        f"box-shadow: var(--shadow-card); {nested} }}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", css)


def test_modifier_inheritance_respects_important_and_specificity() -> None:
    base = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-card) !important; "
    )
    normal_modifier = base + "&:hover { box-shadow: var(--shadow-drawer); }}"
    important_modifier = base + ("&:hover { box-shadow: var(--shadow-drawer) !important; }}")
    assert "unsanctioned-shape-geometry" not in scan_surface("x", normal_modifier)
    assert "unsanctioned-shape-geometry" in scan_surface("x", important_modifier)

    canonical_base = base.replace(" !important", "")
    zero_specificity = canonical_base + (":where(&) { box-shadow: var(--shadow-drawer); }}")
    equal_specificity = canonical_base + (":where(&).active { box-shadow: var(--shadow-drawer); }}")
    is_specificity = canonical_base + (":is(&) { box-shadow: var(--shadow-drawer); }}")
    zero_important = canonical_base + (
        ":where(&) { box-shadow: var(--shadow-drawer) !important; }}"
    )
    type_specific_base = canonical_base.replace(".forecast-card", "section article.forecast-card")
    lower_type_specificity = type_specific_base + (
        ":where(&).active { box-shadow: var(--shadow-drawer); }}"
    )
    nth_of_equal_specificity = canonical_base.replace(".forecast-card", ".app .forecast-card") + (
        ":where(&):nth-child(2n of .forecast-card) { box-shadow: var(--shadow-drawer); }}"
    )
    nth_last_of_equal_specificity = nth_of_equal_specificity.replace(
        ":nth-child", ":nth-last-child"
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("x", zero_specificity)
    assert "unsanctioned-shape-geometry" in scan_surface("x", equal_specificity)
    assert "unsanctioned-shape-geometry" in scan_surface("x", is_specificity)
    assert "unsanctioned-shape-geometry" in scan_surface("x", zero_important)
    assert "unsanctioned-shape-geometry" not in scan_surface("x", lower_type_specificity)
    assert "unsanctioned-shape-geometry" in scan_surface("x", nth_of_equal_specificity)
    assert "unsanctioned-shape-geometry" in scan_surface("x", nth_last_of_equal_specificity)


def test_recursive_subject_preserving_modifiers_keep_base_lineage() -> None:
    css = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-card); "
        "&:hover { &[open] { box-shadow: var(--shadow-drawer); }}}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", css)


@pytest.mark.parametrize("pseudo", ["::before", ":before", "::after"])
def test_pseudo_element_geometry_is_not_attributed_to_the_card(pseudo: str) -> None:
    declarations = (
        "border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer);"
    )
    direct = f".forecast-card{pseudo} {{ {declarations} }}"
    nested = f".forecast-card {{ &{pseudo} {{ {declarations} }} }}"
    assert "unsanctioned-shape-geometry" not in scan_surface("x", direct)
    assert "unsanctioned-shape-geometry" not in scan_surface("x", nested)


@pytest.mark.parametrize("label", ["::before", ":before", "::after"])
def test_quoted_pseudo_text_does_not_hide_governed_geometry(label: str) -> None:
    css = (
        f'.forecast-card[data-label="{label}"] {{ '
        "border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", css)


def test_shape_cascade_uses_final_declarations_for_each_exact_selector() -> None:
    split_novel = (
        ".forecast-card { border-radius: var(--radius-card); }"
        ".forecast-card { border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }"
    )
    assert scan_surface("x", split_novel)["unsanctioned-shape-geometry"] == [".forecast-card"]

    final_canonical = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); box-shadow: var(--shadow-card); }"
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("x", final_canonical)

    final_novel = final_canonical.replace(
        "box-shadow: var(--shadow-card);", "box-shadow: var(--shadow-drawer);"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", final_novel)

    same_context_split = (
        "@media (min-width: 1px) {"
        ".forecast-card { border-radius: var(--radius-card); }"
        ".forecast-card { border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }"
        "}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", same_context_split)
    incompatible_contexts = (
        "@media (prefers-color-scheme: dark) {"
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); }}"
        "@media (prefers-color-scheme: light) {"
        ".forecast-card { box-shadow: var(--shadow-drawer); }}"
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("x", incompatible_contexts)

    root_plus_override = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-card); }"
        "@media (min-width: 1px) {"
        ".forecast-card { box-shadow: var(--shadow-drawer); }}"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", root_plus_override)
    root_plus_completion = root_plus_override.replace("box-shadow: var(--shadow-card); }", "}")
    assert "unsanctioned-shape-geometry" in scan_surface("x", root_plus_completion)

    declarations_after_nested_selector = (
        ".forecast-card { border-radius: var(--radius-card); "
        "&.active { color: var(--fg); } "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }"
    )
    declarations_after_nested_at_rule = declarations_after_nested_selector.replace(
        "&.active { color: var(--fg); }",
        "@media (min-width: 1px) { color: var(--fg); }",
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", declarations_after_nested_selector)
    assert "unsanctioned-shape-geometry" in scan_surface("x", declarations_after_nested_at_rule)

    important_novel = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer) !important; }"
        ".forecast-card { box-shadow: var(--shadow-card); }"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", important_novel)
    important_canonical = important_novel.replace(
        "var(--shadow-drawer) !important", "var(--shadow-card) !important"
    ).replace("box-shadow: var(--shadow-card); }", "box-shadow: var(--shadow-drawer); }")
    assert "unsanctioned-shape-geometry" not in scan_surface("x", important_canonical)

    root_important = root_plus_override.replace(
        "box-shadow: var(--shadow-card);",
        "box-shadow: var(--shadow-card) !important;",
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("x", root_important)

    nested_then_root = (
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-card); "
        "@media (min-width: 1px) { box-shadow: var(--shadow-drawer); } "
        "box-shadow: var(--shadow-card); }"
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("x", nested_then_root)
    nested_then_root_important = nested_then_root.replace(
        "box-shadow: var(--shadow-drawer);",
        "box-shadow: var(--shadow-drawer) !important;",
    ).replace(
        "box-shadow: var(--shadow-card); }",
        "box-shadow: var(--shadow-card) !important; }",
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("x", nested_then_root_important)
    nested_then_novel = nested_then_root.replace(
        "box-shadow: var(--shadow-card); }",
        "box-shadow: var(--shadow-drawer); }",
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", nested_then_novel)


def test_shape_and_grid_signatures_are_scoped_to_the_applicable_archetype() -> None:
    wrong_card = (
        ".forecast-card { border-radius: var(--radius-drawer); "
        "border: var(--bw-thin) solid var(--border); box-shadow: var(--shadow-drawer); }"
    )
    wrong_drawer = (
        ".forecast-drawer { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); box-shadow: var(--shadow-card); }"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", wrong_card)
    assert "unsanctioned-shape-geometry" in scan_surface("x", wrong_drawer)

    wrong_card_grid = (
        ".portfolio-card-grid { grid-template-columns: minmax(0, 1fr) var(--rail-sm); }"
    )
    wrong_split = (
        ".research-split-rail { grid-template-columns: repeat(auto-fit, "
        "minmax(var(--grid-card-sm), 1fr)); }"
    )
    assert "off-scale-grid-column" in scan_surface("x", wrong_card_grid)
    assert "off-scale-grid-column" in scan_surface("x", wrong_split)

    wrong_case_shape = wrong_card.replace("radius-drawer", "radius-card").replace(
        "shadow-drawer", "SHADOW-CARD"
    )
    wrong_case_grid = (
        ".portfolio-card-grid { grid-template-columns: repeat(auto-fit, "
        "minmax(var(--GRID-CARD-SM), 1fr)); }"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", wrong_case_shape)
    assert "off-scale-grid-column" in scan_surface("x", wrong_case_grid)


@pytest.mark.parametrize(
    "text",
    [
        ".layout:not(.portfolio-card-grid) { grid-template-columns: 340px 1fr; }",
        '[data-example=".portfolio-card-grid"] .layout { grid-template-columns: 340px 1fr; }',
        ".layout:not(.allocation-bucket) { padding-left: 18px; }",
        '[data-example=".forecast-card"] .layout { border-radius: var(--radius-card); '
        "border: var(--bw-thin) solid var(--border); box-shadow: var(--shadow-drawer); }",
    ],
)
def test_selector_examples_and_negative_pseudo_arguments_are_not_targets(text: str) -> None:
    findings = scan_surface("x", text)
    assert "off-scale-grid-column" not in findings
    assert "off-scale-indent" not in findings
    assert "unsanctioned-shape-geometry" not in findings


def test_positive_selector_functions_and_css_escapes_remain_targets() -> None:
    positive = ".layout:is(.portfolio-card-grid) { grid-template-columns: 340px 1fr; }"
    positive_where = ".layout:where(.portfolio-card-grid) { grid-template-columns: 340px 1fr; }"
    escaped = r".portfolio\-card\-grid { grid-template-columns: 340px 1fr; }"
    assert "off-scale-grid-column" in scan_surface("x", positive)
    assert "off-scale-grid-column" in scan_surface("x", positive_where)
    assert "off-scale-grid-column" in scan_surface("x", escaped)
    at_rule_text_in_attribute = (
        '.forecast-card[data-label="@media"] {'
        "border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }"
    )
    assert "unsanctioned-shape-geometry" in scan_surface("x", at_rule_text_in_attribute)
    for quoted_style in (
        '.portfolio-card-grid[data-example="<style>@media"] { grid-template-columns: 340px 1fr; }',
        '[data-example="<style>@media"] .portfolio-card-grid { grid-template-columns: 340px 1fr; }',
    ):
        assert "off-scale-grid-column" in scan_surface("x", quoted_style)


@pytest.mark.parametrize(
    "text",
    [
        ".portfolio-card-grid .child { grid-template-columns: 340px 1fr; }",
        ".allocation-bucket .row { padding-left: 18px; }",
        ".forecast-card .child { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }",
        ".forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        ".child { box-shadow: var(--shadow-drawer); }}",
        ".portfolio-card-grid { .child { grid-template-columns: 340px 1fr; }}",
    ],
)
def test_governed_ancestor_does_not_own_descendant_declarations(text: str) -> None:
    findings = scan_surface("x", text)
    assert "off-scale-grid-column" not in findings
    assert "off-scale-indent" not in findings
    assert "unsanctioned-shape-geometry" not in findings


def test_governed_subject_still_owns_declarations_after_an_ancestor() -> None:
    findings = scan_surface(
        "x",
        ".shell .forecast-card { border-radius: var(--radius-card); "
        "border: var(--bw-thin) solid var(--border); "
        "box-shadow: var(--shadow-drawer); }",
    )
    assert findings["unsanctioned-shape-geometry"] == [".forecast-card"]


def test_unrelated_template_data_does_not_hide_a_static_bad_title() -> None:
    markup = '<section class="k-card" data-id="{id}"><h3>Risk {dynamic_text}</h3></section>'
    assert scan_surface("x", markup)["floating-card-title"] == ["h3"]
    unverifiable_class = '<section class="k-card"><h3 class="{title_class}">Risk</h3></section>'
    assert "floating-card-title" not in scan_surface("x", unverifiable_class)
    evidence = scan_surface_evidence("x", unverifiable_class)
    assert evidence.unverifiable_markup == ("dynamic-heading-class:h3",)

    incomplete = '<section class="k-card"><h3 class="k-card-title">Risk'
    incomplete_evidence = scan_surface_evidence("x", incomplete)
    assert incomplete_evidence.unverifiable_markup == ("unclosed-heading:h3",)

    for dynamic_container in (
        '<section class="{container_class}"><h3>Risk</h3></section>',
        '<section class="k-{kind}"><h3>Risk</h3></section>',
        '<section class="{{container_class}}"><h3>Risk</h3></section>',
        '<section class="{{ container_class }}"><h3>Risk</h3></section>',
        '<section class="k-card{modifier}"><h3>Risk</h3></section>',
        '<section class="{prefix}k-card"><h3>Risk</h3></section>',
    ):
        assert scan_surface_evidence("x", dynamic_container).unverifiable_markup == (
            "dynamic-container-class:h3",
        )
    dynamic_tag = '<{tag} class="k-card"><h3>Risk</h3></{tag}>'
    assert scan_surface_evidence("x", dynamic_tag).unverifiable_markup == ("dynamic-tag",)

    exterior_dynamic_heading = (
        '<section><h2 class="{title_class}">Portfolio Intelligence</h2></section>'
    )
    irrelevant_dynamic_ancestor = (
        '<section class="theme-{tone}"><h2>Portfolio Intelligence</h2></section>'
    )
    assert not scan_surface_evidence("x", exterior_dynamic_heading).unverifiable_markup
    assert not scan_surface_evidence("x", irrelevant_dynamic_ancestor).unverifiable_markup
    incomplete_drawer_compound = (
        '<section class="k-over{suffix}"><h2>Portfolio Intelligence</h2></section>'
    )
    assert not scan_surface_evidence("x", incomplete_drawer_compound).unverifiable_markup


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "HTML = f'<section class=\"{container_class}\"><h3>Risk</h3></section>'\n",
            "dynamic-container-class:h3",
        ),
        (
            "HTML = f'<{tag} class=\"k-card\"><h3>Risk</h3></{tag}>'\n",
            "dynamic-tag",
        ),
        (
            'HTML = f\'<section class="k-card"><h3 class="{title_class}">Risk</h3></section>\'\n',
            "dynamic-heading-class:h3",
        ),
    ],
)
def test_css_text_preserves_fstring_structural_uncertainty(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    module = tmp_path / "dynamic_markup.py"
    module.write_text(source, encoding="utf-8")
    evidence = scan_surface_evidence("dynamic_markup.py", css_text(module))
    assert expected in evidence.unverifiable_markup


def test_css_text_reconstructs_python_escapes_and_nested_fstrings(tmp_path: Path) -> None:
    module = tmp_path / "dynamic_markup.py"
    module.write_text(
        'HTML = "<section class=\\"k-card\\"><h3>Risk</h3></section>"\n',
        encoding="utf-8",
    )
    assert scan_surface("dynamic_markup.py", css_text(module))["floating-card-title"] == ["h3"]

    module.write_text(
        'HTML = f\'<section class="{f"{kind}-card"}"><h3>Risk</h3></section>\'\n',
        encoding="utf-8",
    )
    evidence = scan_surface_evidence("dynamic_markup.py", css_text(module))
    assert "dynamic-container-class:h3" in evidence.unverifiable_markup

    module.write_text(
        'HTML = f"<section class=\\"k-{kind}\\"><h3>Risk</h3></section>"\n',
        encoding="utf-8",
    )
    evidence = scan_surface_evidence("dynamic_markup.py", css_text(module))
    assert "dynamic-container-class:h3" in evidence.unverifiable_markup

    module.write_text(
        'HTML = f\'<{f"{tag}"} class="k-card"><h3>Risk</h3></{f"{tag}"}>\'\n',
        encoding="utf-8",
    )
    evidence = scan_surface_evidence("dynamic_markup.py", css_text(module))
    assert "dynamic-tag" in evidence.unverifiable_markup

    for static_markup in (
        '<div data-label="<{tag}>">safe</div>',
        '<script>const sample = "<{tag}>";</script>',
        "<!-- <{tag}> --><div>safe</div>",
    ):
        evidence = scan_surface_evidence("dynamic_markup.py", static_markup)
        assert "dynamic-tag" not in evidence.unverifiable_markup

    module.write_text(
        'HTML = \'<section class="%s"><h3>Risk</h3></section>\' % "k-card"\n',
        encoding="utf-8",
    )
    assert scan_surface("dynamic_markup.py", css_text(module))["floating-card-title"] == ["h3"]

    module.write_text(
        'HTML = f\'<{"section" if count > 0 else "div"} class="k-card"><h3>Risk</h3></div>\'\n',
        encoding="utf-8",
    )
    evidence = scan_surface_evidence("dynamic_markup.py", css_text(module))
    assert "dynamic-tag" in evidence.unverifiable_markup


def test_semantic_extraction_does_not_change_legacy_dimensions(tmp_path: Path) -> None:
    module = tmp_path / "escaped_css.py"
    module.write_text(
        'CSS = ".x { color: \\x23fff; border-radius: \\x36\\x70\\x78; '
        'background: var(--surface); }"\n',
        encoding="utf-8",
    )
    findings = scan_surface("escaped_css.py", css_text(module))
    assert "color" not in findings
    assert "radius" not in findings

    module.write_text(
        'CSS = ".x { color: #" "fff; font-size: 1" "7px; background: var(--surface); }"\n',
        encoding="utf-8",
    )
    findings = scan_surface("escaped_css.py", css_text(module))
    assert "color" not in findings
    assert "font-size" not in findings


def test_static_composition_limit_precedes_allocation(tmp_path: Path) -> None:
    module = tmp_path / "bounded_css.py"
    module.write_text('CSS = "x" * 1_000_000_000\n', encoding="utf-8")
    assert len(css_text(module)) < 100

    module.write_text(
        'CSS = "{:1000000000}".format("x")\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 100


def test_fstring_format_specs_reconstruct_runtime_values(tmp_path: Path) -> None:
    module = tmp_path / "formatted_css.py"
    module.write_text(
        'CSS = f".forecast-card{{ border-radius:var(--radius-card); '
        "border:var(--bw-thin) solid var(--border); "
        "box-shadow:var(--shadow-{'cardx':.4}); }}\"\n",
        encoding="utf-8",
    )
    assert "unsanctioned-shape-geometry" not in scan_surface("formatted_css.py", css_text(module))

    module.write_text(
        'CSS = f".research-split-rail{{ grid-template-columns:'
        'minmax({0.4:.0f},1fr) var(--rail-sm); }}"\n',
        encoding="utf-8",
    )
    assert "off-scale-grid-column" not in scan_surface("formatted_css.py", css_text(module))


def test_nested_static_format_bounds_precede_allocation(tmp_path: Path) -> None:
    module = tmp_path / "bounded_css.py"
    module.write_text('CSS = "%*s" % (1_000_000_000, "x")\n', encoding="utf-8")
    assert len(css_text(module)) < 100

    module.write_text(
        'CSS = "{0[0]:{0[1]}}".format(("x", 1_000_000_000))\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 100

    module.write_text(
        'CSS = "{:600000}{:600000}".format("x", "y")\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 100

    module.write_text(
        'CSS = "{0:{1}}".format("x", "1000000000")\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 100

    module.write_text(
        'CSS = "{x:{w}}".format_map({"x": "x", "w": "1000000000"})\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 100

    module.write_text(
        'CSS = ("{0}" * 2000).format("x" * 600_000)\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 700_000

    module.write_text(
        'CSS = ("%(x)s" * 2000) % {"x": "x" * 600_000}\n',
        encoding="utf-8",
    )
    assert len(css_text(module)) < 700_000

    module.write_text(
        "CSS = f\"{'x' * 600_000}{'x' * 600_000}\"\n",
        encoding="utf-8",
    )
    evidence = scan_surface_evidence("bounded_css.py", css_text(module))
    assert "static-composition-limit" in evidence.unverifiable_markup

    for source in (
        'CSS = ".portfolio-card-grid{grid-template-columns:" + ("3" * 1_000_001) + ";}"\n',
        'CSS = ".portfolio-card-grid{{grid-template-columns:{:1000001};}}".format("3")\n',
        'CSS = ".portfolio-card-grid{grid-template-columns:%1000001s;}" % "3"\n',
        'CSS = "".join(["x" * 600_000, "x" * 600_000])\n',
        'CSS = "{0}{1}".format(*["x" * 600_000, "x" * 600_000])\n',
        'CSS_A = "x" * 600_000\nCSS_B = "x" * 600_000\n',
    ):
        module.write_text(source, encoding="utf-8")
        evidence = scan_surface_evidence("bounded_css.py", css_text(module))
        assert "static-composition-limit" in evidence.unverifiable_markup


def test_nested_static_brace_specs_reconstruct_before_scanning(tmp_path: Path) -> None:
    module = tmp_path / "nested_spec.py"
    template = (
        'CSS = (".research-split-rail{{grid-template-columns:'
        'minmax({0:.{1}f},1fr) var(--rail-sm);}}".format(0.4, {precision}))\n'
    )
    module.write_text(template.replace("{precision}", "1"), encoding="utf-8")
    assert "off-scale-grid-column" in scan_surface("nested_spec.py", css_text(module))

    module.write_text(template.replace("{precision}", "0"), encoding="utf-8")
    assert "off-scale-grid-column" not in scan_surface("nested_spec.py", css_text(module))

    automatic = (
        'CSS = (".research-split-rail{{grid-template-columns:'
        'minmax({:.{}f},1fr) var(--rail-sm);}}".format(0.4, {precision}))\n'
    )
    module.write_text(automatic.replace("{precision}", "1"), encoding="utf-8")
    assert "off-scale-grid-column" in scan_surface("nested_spec.py", css_text(module))

    module.write_text(automatic.replace("{precision}", "0"), encoding="utf-8")
    assert "off-scale-grid-column" not in scan_surface("nested_spec.py", css_text(module))


def test_registry_contract_is_versioned_for_importable_scanner() -> None:
    assert registry.REGISTRY_VERSION == "1.3.0"
    exemptions = {entry.surface: entry for entry in registry.PERMANENT_EXEMPTIONS}
    scanner = exemptions["ui/conformance_scan.py"]
    assert scanner.owner == "design-system"
    assert scanner.rationale


def _write_complete_fixture_tree(root: Path, *, include_live_drift: bool) -> Path:
    source_root = root / "src"
    clean = ".fixture { color: var(--fg); }"
    for rel in registry.REGISTERED:
        path = source_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"CSS = {clean!r}\n", encoding="utf-8")

    if include_live_drift:
        violations = """
        <section class="k-card"><h3 class="free-title">Risk</h3></section>
        <style>
          .allocation-bucket-row { padding-left: 18px; }
          .forecast-card { border-radius: var(--radius-card); border: var(--bw-thin) solid var(--border); box-shadow: var(--shadow-drawer); }
          .portfolio-card-grid { grid-template-columns: 340px 1fr; }
        </style>
        """
        (source_root / "dashboard" / "_styles.py").write_text(
            f"CSS = {violations!r}\n", encoding="utf-8"
        )
    return source_root


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "execution/verify_design_conformance.py", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _credentialed_test_url(host: str, port: int, path: str) -> str:
    userinfo = "test-user" + chr(58) + "test-password"
    return f"http://{userinfo}@{host}:{port}{path}"


def test_cli_check_uses_shared_scanner_for_all_four_dimensions(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=True)
    result = _run_cli("--check", "--source-root", str(source_root))
    assert result.returncode == 1, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema_version"] == "1.0.0"
    assert receipt["registry_version"] == registry.REGISTRY_VERSION
    assert receipt["canary"]["status"] == "skipped:not-requested"
    assert receipt["stale_quarantine"] == []
    assert receipt["unverifiable_markup"] == []
    assert receipt["verdict"] == "fail"
    live = {
        finding["dimension"]
        for finding in receipt["findings"]
        if finding["surface"] == "dashboard/_styles.py" and finding["disposition"] == "live"
    }
    assert live >= EXPECTED_NEW_DIMENSIONS
    assert receipt["findings"] == sorted(
        receipt["findings"], key=lambda item: (item["surface"], item["dimension"])
    )


def test_cli_clean_fixture_and_emitted_receipt_are_deterministic(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    first = _run_cli("--check", "--source-root", str(source_root))
    second = _run_cli("--check", "--source-root", str(source_root))
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    receipt = json.loads(first.stdout)
    assert receipt["verdict"] == "pass"
    assert receipt["findings"] == []
    assert receipt["stale_quarantine"] == []

    destination = tmp_path / "receipt.json"
    emitted = _run_cli(
        "--emit-receipt",
        str(destination),
        "--source-root",
        str(source_root),
    )
    assert emitted.returncode == 0, emitted.stderr
    assert json.loads(destination.read_text(encoding="utf-8")) == receipt
    summary = json.loads(emitted.stdout)
    assert summary["receipt_path"] == str(destination.resolve())
    assert summary["verdict"] == "pass"


def test_cli_modes_exit_codes_and_unavailable_canary_are_explicit(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    missing_mode = _run_cli("--source-root", str(source_root))
    assert missing_mode.returncode == 2

    unavailable = _run_cli(
        "--check",
        "--source-root",
        str(source_root),
        "--canary-url",
        "http://127.0.0.1:1/design-conformance-canary",
    )
    assert unavailable.returncode == 0, unavailable.stderr
    canary = json.loads(unavailable.stdout)["canary"]
    assert canary["status"] == "skipped:unavailable"
    assert canary["reason"]

    invalid_scheme = _run_cli(
        "--check",
        "--source-root",
        str(source_root),
        "--canary-url",
        "file:///tmp/design-conformance-canary",
    )
    assert invalid_scheme.returncode == 0, invalid_scheme.stderr
    invalid_canary = json.loads(invalid_scheme.stdout)["canary"]
    assert invalid_canary["status"] == "skipped:unavailable"
    assert invalid_canary["reason"] == "ValueError: canary unavailable"

    credentialed = _run_cli(
        "--check",
        "--source-root",
        str(source_root),
        "--canary-url",
        _credentialed_test_url("127.0.0.1", 1, "/design-conformance-canary"),
    )
    assert credentialed.returncode == 0, credentialed.stderr
    credentialed_canary = json.loads(credentialed.stdout)["canary"]
    assert credentialed_canary["status"] == "skipped:unavailable"
    assert credentialed_canary["reason"] == "ValueError: canary unavailable"


def test_cli_canary_does_not_follow_redirects(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)

    class RedirectHandler(BaseHTTPRequestHandler):
        target_hits = 0
        redirect_target = ""

        def do_GET(self) -> None:
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", type(self).redirect_target)
                self.end_headers()
                return
            type(self).target_hits += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<main>unexpected redirect target</main>")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    address = server.server_address
    assert isinstance(address, tuple) and len(address) == 2
    host, port = address
    assert isinstance(host, str) and isinstance(port, int)
    RedirectHandler.redirect_target = _credentialed_test_url(host, port, "/target")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        redirected = _run_cli(
            "--check",
            "--source-root",
            str(source_root),
            "--canary-url",
            f"http://{host}:{port}/start",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)

    assert redirected.returncode == 0, redirected.stderr
    redirected_canary = json.loads(redirected.stdout)["canary"]
    assert redirected_canary["status"] == "skipped:unavailable"
    assert redirected_canary["reason"] == "HTTPError: canary unavailable"
    assert RedirectHandler.target_hits == 0


def test_cli_canary_has_an_absolute_wall_deadline(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)

    class TrickleHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.25)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
    address = server.server_address
    assert isinstance(address, tuple) and len(address) == 2
    host, port = address
    assert isinstance(host, str) and isinstance(port, int)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        trickled = _run_cli(
            "--check",
            "--source-root",
            str(source_root),
            "--canary-url",
            f"http://{host}:{port}/trickle",
        )
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)

    assert elapsed < 5.0
    assert trickled.returncode == 0, trickled.stderr
    trickled_canary = json.loads(trickled.stdout)["canary"]
    assert trickled_canary["status"] == "skipped:unavailable"
    assert trickled_canary["reason"] == "TimeoutError: canary unavailable"


def test_cli_reports_structurally_unverifiable_markup_instead_of_passing(
    tmp_path: Path,
) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    ambiguous_source = (
        'CSS = f""".fixture {{ color: var(--fg); }}'
        '<section class="k-card"><h3 class="{title_class}">Risk</h3></section>'
        '<section class="{container_class}"><h3>Risk</h3></section>"""\n'
    )
    (source_root / "dashboard" / "_styles.py").write_text(ambiguous_source, encoding="utf-8")
    result = _run_cli("--check", "--source-root", str(source_root))
    assert result.returncode == 1, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["verdict"] == "fail"
    assert receipt["unverifiable_markup"] == [
        {
            "surface": "dashboard/_styles.py",
            "values": ["dynamic-container-class:h3", "dynamic-heading-class:h3"],
        }
    ]
