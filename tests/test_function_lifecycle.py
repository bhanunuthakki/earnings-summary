from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from quality import function_lifecycle
from quality.function_lifecycle import (
    FunctionLifecycleError,
    FunctionLifecycleInventory,
    build_inventory,
    validate_inventory,
)


def _runner(paths: list[str], *, revision: str = "head"):
    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if list(command[:2]) == ["git", "ls-files"]:
            separator = "\0" if "-z" in command else "\n"
            return subprocess.CompletedProcess(command, 0, separator.join(paths) + separator, "")
        if list(command[:3]) == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, revision + "\n", "")
        if list(command[:2]) == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    return run


def _write(root: Path, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(files)


def _one(root: Path, text: str):
    paths = _write(root, {"src/sample.py": text})
    return build_inventory(root, _runner(paths))


def test_aliases_and_nested_functions_have_qualified_evidence(tmp_path: Path) -> None:
    inventory = _one(
        tmp_path,
        """\
def target() -> None:
    pass

def caller() -> None:
    alias = target
    alias()
    def nested() -> None:
        target()
    nested()
""",
    )
    target = next(item for item in inventory.symbols if item.qualified_name.endswith(".target"))
    nested = next(
        item for item in inventory.symbols if item.qualified_name.endswith(".caller.nested")
    )
    assert target.inbound_static_ref_count >= 2
    assert target.classification == "referenced"
    assert nested.visibility == "nested"


def test_reference_from_later_file_is_collected_before_classification(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "src/a.py": "def _target() -> None:\n    pass\n",
            "src/z.py": "from src.a import _target\n_target()\n",
        },
    )
    inventory = build_inventory(tmp_path, _runner(paths))
    target = next(item for item in inventory.symbols if item.qualified_name == "src.a._target")
    assert target.classification == "referenced"
    assert target.inbound_static_refs == ("src/z.py:2",)


def test_decorators_registries_callbacks_and_public_exports_are_protected(tmp_path: Path) -> None:
    inventory = _one(
        tmp_path,
        """\
__all__ = ["exported"]

@app.route("/health")
def route_handler() -> None:
    pass

@registry.register
def registered() -> None:
    pass

def exported() -> None:
    pass
""",
    )
    by_name = {item.qualified_name.rsplit(".", 1)[-1]: item for item in inventory.symbols}
    assert by_name["route_handler"].classification == "protected"
    assert by_name["registered"].classification == "protected"
    assert by_name["exported"].classification in {"protected", "unknown"}


def test_methods_overrides_reflection_and_dynamic_imports_are_not_candidates(
    tmp_path: Path,
) -> None:
    inventory = _one(
        tmp_path,
        """\
class Base:
    def hook(self) -> None:
        pass

class Child(Base):
    @override
    def hook(self) -> None:
        pass

def reflected() -> None:
    getattr(object(), "reflected")
    __import__("plugin")
""",
    )
    for item in inventory.symbols:
        assert item.classification != "unreferenced-static-candidate"
    reflected = next(
        item for item in inventory.symbols if item.qualified_name.endswith(".reflected")
    )
    assert "dynamic:getattr" in reflected.dynamic_hazards
    assert "dynamic:__import__" in reflected.dynamic_hazards


def test_only_private_unreferenced_symbols_are_candidates(tmp_path: Path) -> None:
    inventory = _one(tmp_path, "def _candidate() -> None:\n    return None\n")
    item = inventory.symbols[0]
    assert item.classification == "unreferenced-static-candidate"
    assert inventory.status == "PASS"


def test_nested_function_is_not_mistaken_for_a_public_module_symbol(tmp_path: Path) -> None:
    inventory = _one(
        tmp_path,
        "def outer() -> None:\n    def nested() -> None:\n        pass\n",
    )
    nested = next(item for item in inventory.symbols if item.qualified_name.endswith(".nested"))
    assert nested.visibility == "nested"
    assert "public-module-symbol" not in nested.dynamic_hazards
    assert nested.classification == "unreferenced-static-candidate"


def test_dynamic_calls_use_ast_names_not_substring_matches(tmp_path: Path) -> None:
    inventory = _one(
        tmp_path,
        "def _execute_plan() -> None:\n    evaluation = 'ordinary text'\n    print(evaluation)\n",
    )
    item = inventory.symbols[0]
    assert not any(hazard.startswith("dynamic:") for hazard in item.dynamic_hazards)
    assert item.classification == "unreferenced-static-candidate"


def test_computed_reflection_blocks_private_candidates_in_its_module(tmp_path: Path) -> None:
    inventory = _one(
        tmp_path,
        "def _target() -> None:\n    pass\n\ndef lookup(obj: object, name: str):\n    return getattr(obj, name)\n",
    )
    target = next(item for item in inventory.symbols if item.qualified_name.endswith("._target"))
    assert target.classification == "unknown"
    assert "dynamic:unbounded-reflection-in-module" in target.dynamic_hazards


def test_vars_reflection_blocks_private_candidates_in_its_module(tmp_path: Path) -> None:
    inventory = _one(
        tmp_path,
        "def _target() -> None:\n"
        "    pass\n\n"
        "def lookup(obj: object, name: str):\n"
        "    return vars(obj)[name]\n",
    )
    target = next(item for item in inventory.symbols if item.qualified_name.endswith("._target"))
    lookup = next(item for item in inventory.symbols if item.qualified_name.endswith(".lookup"))
    assert target.classification == "unknown"
    assert "dynamic:unbounded-reflection-in-module" in target.dynamic_hazards
    assert "dynamic:vars" in lookup.dynamic_hazards


def test_explicitly_documented_compatibility_seams_are_protected(
    tmp_path: Path,
) -> None:
    inventory = _one(
        tmp_path,
        "# private compatibility seam for older callers\n"
        "def _comment_seam() -> None:\n"
        "    pass\n\n"
        "def _docstring_seam() -> None:\n"
        '    """Backward-compatible helper for legacy callers."""\n'
        "    pass\n\n"
        "def _ordinary_wording() -> None:\n"
        '    """Compatibility is important, but this is not a seam."""\n'
        "    pass\n",
    )
    by_name = {item.qualified_name.rsplit(".", 1)[-1]: item for item in inventory.symbols}
    for name in ("_comment_seam", "_docstring_seam"):
        assert by_name[name].classification == "protected"
        assert "documented-compatibility-seam" in by_name[name].dynamic_hazards
    assert by_name["_ordinary_wording"].classification == "unreferenced-static-candidate"


def test_malformed_ast_holds(tmp_path: Path) -> None:
    inventory = _one(tmp_path, "def broken(:\n")
    assert inventory.status == "HOLD"
    assert inventory.files_failed == ("src/sample.py",)
    assert any("malformed AST" in value for value in inventory.violations)


def test_stale_validation_binds_tree_without_cyclic_commit_identity(tmp_path: Path) -> None:
    paths = _write(tmp_path, {"src/sample.py": "def _candidate() -> None:\n    return None\n"})
    persisted = build_inventory(tmp_path, _runner(paths, revision="old"))
    assert not validate_inventory(tmp_path, persisted, _runner(paths, revision="new"))
    (tmp_path / "src/sample.py").write_text(
        "def _candidate() -> None:\n    return 1\n", encoding="utf-8"
    )
    assert "tracked tree hash changed" in validate_inventory(
        tmp_path, persisted, _runner(paths, revision="new")
    )


def test_receipt_round_trip_is_strict(tmp_path: Path) -> None:
    inventory = _one(tmp_path, "def _candidate() -> None:\n    return None\n")
    parsed = FunctionLifecycleInventory.model_validate_json(inventory.model_dump_json())
    assert parsed.tracked_tree_hash == inventory.tracked_tree_hash
    with pytest.raises(Exception):
        FunctionLifecycleInventory.model_validate({**inventory.model_dump(), "unexpected": True})
    with pytest.raises(Exception, match="candidate inventory"):
        FunctionLifecycleInventory.model_validate(
            {**inventory.model_dump(), "candidate_symbols": []}
        )
    forged = inventory.model_copy(
        update={
            "candidate_symbols": tuple(
                item.model_copy(update={"qualified_name": "src.sample._forged"})
                for item in inventory.candidate_symbols
            )
        }
    )
    assert "candidate inventory changed" in validate_inventory(
        tmp_path, forged, _runner(["src/sample.py"])
    )


def test_missing_tracked_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FunctionLifecycleError, match="missing"):
        build_inventory(tmp_path, _runner(["src/missing.py"]))


def test_git_runner_discards_inherited_repository_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, str]] = []

    def capture(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        seen.append(cast("dict[str, str]", environment))
        command = args[0]
        output = "head\n" if "rev-parse" in str(command) else ""
        return subprocess.CompletedProcess(["git"], 0, output, "")

    monkeypatch.setenv("GIT_DIR", "/unrelated/repository")
    monkeypatch.setattr(function_lifecycle.subprocess, "run", capture)
    build_inventory(tmp_path)
    assert seen and "GIT_DIR" not in seen[0]


def test_tracked_path_cannot_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def _private(): pass\n", encoding="utf-8")
    with pytest.raises(FunctionLifecycleError, match="escapes"):
        build_inventory(tmp_path, _runner(["../outside.py"]))


def test_tracked_link_cannot_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def _private(): pass\n", encoding="utf-8")
    (tmp_path / "sample.py").symlink_to(outside)
    with pytest.raises(FunctionLifecycleError, match="escapes"):
        build_inventory(tmp_path, _runner(["sample.py"]))


def test_invalid_utf8_records_incomplete_scan(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_bytes(b"\xff")
    inventory = build_inventory(tmp_path, _runner(["sample.py"]))
    assert inventory.status == "HOLD"
    assert inventory.files_failed == ("sample.py",)


def test_cli_cannot_overwrite_tracked_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src/sample.py"
    inventory = _one(tmp_path, "def _private(): pass\n")
    original = source.read_bytes()

    def current(root: Path) -> FunctionLifecycleInventory:
        return inventory

    monkeypatch.setattr(function_lifecycle, "build_inventory", current)
    assert function_lifecycle.main(["--root", str(tmp_path), "--output", str(source)]) == 2
    assert source.read_bytes() == original


def test_current_migrations_remain_in_active_source_inventory(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "alembic/versions/current.py": "def upgrade(): pass\n",
            "alembic/versions_archived/historical.py": "def upgrade(): pass\n",
        },
    )
    inventory = build_inventory(tmp_path, _runner(paths))
    assert inventory.files_scanned == 1
    assert inventory.symbols[0].path == "alembic/versions/current.py"


def test_cli_does_not_echo_invalid_receipt_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory = _one(tmp_path, "def _private(): pass\n")
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"unexpected": "PRIVATE_RECEIPT_SENTINEL"}', encoding="utf-8")

    def current(root: Path) -> FunctionLifecycleInventory:
        return inventory

    monkeypatch.setattr(function_lifecycle, "build_inventory", current)
    assert function_lifecycle.main(["--root", str(tmp_path), "--validate", str(receipt)]) == 2
    assert "PRIVATE_RECEIPT_SENTINEL" not in capsys.readouterr().err


@pytest.mark.parametrize("alias_source", [False, True])
def test_cli_allows_only_output_that_does_not_alias_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_source: bool,
) -> None:
    inventory = _one(tmp_path, "def _private(): pass\n")
    source = tmp_path / "src/sample.py"
    original = source.read_bytes()
    output = tmp_path / ".tmp/receipt.json"
    output.parent.mkdir()
    if alias_source:
        os.link(source, output)

    def current(root: Path) -> FunctionLifecycleInventory:
        return inventory

    def tracked(root: Path) -> list[str]:
        return ["src/sample.py"]

    monkeypatch.setattr(function_lifecycle, "build_inventory", current)
    monkeypatch.setattr(function_lifecycle, "_tracked", tracked)
    result = function_lifecycle.main(["--root", str(tmp_path), "--output", str(output)])
    assert result == (2 if alias_source else 0)
    assert source.read_bytes() == original
    if not alias_source:
        persisted = function_lifecycle.load_inventory(output)
        assert persisted.inventory_hash == inventory.inventory_hash


def test_unresolved_decorator_is_unknown_without_source_text(tmp_path: Path) -> None:
    inventory = _one(tmp_path, "@(lambda function: function)\ndef _candidate(): pass\n")
    symbol = inventory.symbols[0]
    assert symbol.classification == "unknown"
    assert symbol.decorators == ("<dynamic>",)


def test_source_collection_does_not_execute_or_delete_source(tmp_path: Path) -> None:
    text = "raise RuntimeError('scanned source must never execute')\ndef _candidate(): pass\n"
    inventory = _one(tmp_path, text)
    assert inventory.counts["unreferenced-static-candidate"] == 1
    assert (tmp_path / "src/sample.py").read_text(encoding="utf-8") == text
