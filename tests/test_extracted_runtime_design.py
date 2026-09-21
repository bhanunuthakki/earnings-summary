"""Extracted browser assets remain visible to the design census and contracts."""

import ast
from pathlib import Path

import pytest

from ui import conformance_scan
from ui.conformance_scan import discover_emitters, dynamic_visual_digest, scan_surface_evidence
from ui.design_registry import DYNAMIC_VISUAL_CONTRACTS, VISUAL_EMITTER_MANIFEST

ROOT = Path(__file__).resolve().parents[1]


def test_census_skips_python_without_visual_lexical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "src" / "plain_data.py"
    module.parent.mkdir(parents=True)
    module.write_text("ANSWER = 42\n", encoding="utf-8")
    original_parse = conformance_scan.ast.parse

    def guarded_parse(
        source: str,
        filename: str = "<unknown>",
        mode: str = "exec",
    ) -> ast.AST:
        if source == "ANSWER = 42\n":
            raise AssertionError("nonvisual source reached the AST parser")
        return original_parse(source, filename=filename, mode=mode)

    monkeypatch.setattr(conformance_scan.ast, "parse", guarded_parse)
    assert discover_emitters(tmp_path) == ()


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ('BODY = ".card { color: var(--fg); }"\n', "python-css"),
        ('def render(tag):\n    return f"<{tag}>ready</{tag}>"\n', "html"),
        ('PATTERN = r"(?P<path>[^/]+)"\n', "svg"),
        ("CSS = build_styles(tokens)\n", "python-css"),
    ],
)
def test_census_prefilter_preserves_visual_syntax_families(
    tmp_path: Path, source: str, expected_kind: str
) -> None:
    module = tmp_path / "src" / "surface.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    discovered = discover_emitters(tmp_path)

    assert len(discovered) == 1
    assert discovered[0].path == "surface.py"
    assert expected_kind in discovered[0].adapter_kinds


@pytest.mark.parametrize("directory", ["src/pipeline", "execution"])
@pytest.mark.parametrize("suffix", ["js", "mjs"])
def test_source_browser_asset_is_discovered_without_registry_membership(
    tmp_path: Path, directory: str, suffix: str
) -> None:
    asset = tmp_path / directory / f"new_runtime.{suffix}"
    asset.parent.mkdir(parents=True)
    asset.write_text("element.innerHTML = '<div>Ready</div>';", encoding="utf-8")

    discovered = discover_emitters(tmp_path)

    assert len(discovered) == 1
    expected_path = f"{directory}/new_runtime.{suffix}".removeprefix("src/")
    assert discovered[0].path == expected_path
    assert discovered[0].adapter_kinds == {"html", "runtime-js"}


@pytest.mark.parametrize("filename", ["work_os_shell.py", "work_os_runtime.js"])
def test_work_os_split_preserves_explicit_visual_evidence_contracts(filename: str) -> None:
    relative = "pipeline/" + filename
    entry = next(item for item in VISUAL_EMITTER_MANIFEST if item.path == relative)
    assert entry.adapter_kinds == {"html", "python-css", "runtime-js"}
    contracts = [item for item in DYNAMIC_VISUAL_CONTRACTS if item.surface == relative]
    assert len(contracts) == 1
    source = (ROOT / "src" / relative).read_text(encoding="utf-8")
    assert dynamic_visual_digest(source) == contracts[0].digest


def test_registered_action_title_is_allowed_but_arbitrary_title_is_rejected() -> None:
    registered = (
        '<article class="k-card k-card-action"><h3 class="k-card-row-title">Task</h3></article>'
    )
    unregistered = (
        '<article class="k-card k-card-action"><h3 class="arbitrary-title">Task</h3></article>'
    )
    accepted = scan_surface_evidence("pipeline/work_os_runtime.js", registered)
    rejected = scan_surface_evidence("pipeline/work_os_runtime.js", unregistered)
    assert not accepted.findings
    assert ("floating-card-title", ("h3.arbitrary-title",)) in rejected.findings
