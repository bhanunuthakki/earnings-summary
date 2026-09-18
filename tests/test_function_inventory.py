from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quality.function_inventory import build_inventory, main
from quality.git_env import clean_local_git_env


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=clean_local_git_env())
    _write(
        tmp_path,
        "src/app.py",
        "def used():\n    return 1\n\ndef unused():\n    return 2\n\nused()\n",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, env=clean_local_git_env())
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
        env=clean_local_git_env(),
    )
    return tmp_path


def test_classifies_referenced_and_candidate_functions(tmp_path: Path) -> None:
    inventory = build_inventory(_repo(tmp_path))
    dispositions = {entry.qualified_name: entry.disposition for entry in inventory.entries}
    assert inventory.status == "PASS"
    assert inventory.deletion_authority is False
    assert "deletion is not authorized" in inventory.definitions["candidate"]
    assert dispositions == {"unused": "candidate", "used": "referenced"}


def test_protects_framework_and_dynamic_hazards(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "src/hazards.py",
        """__all__ = ["exported"]
def exported(): pass
def callback(): pass
def reflected(): pass
HANDLER_REGISTRY = {"x": callback}
getattr(object(), "reflected", None)
class Base: pass
class Child(Base):
    def method(self): pass
""",
    )
    _write(
        root,
        "tests/test_hazards.py",
        "import pytest\n@pytest.fixture\ndef sample(): return 1\ndef test_case(): pass\n",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    inventory = build_inventory(root)
    entries = {
        entry.qualified_name: entry
        for entry in inventory.entries
        if entry.path.endswith("hazards.py")
    }
    assert entries["exported"].reasons == ("export",)
    assert "registry" in entries["callback"].reasons
    assert "reflection" in entries["reflected"].reasons
    assert "override" in entries["Child.method"].reasons
    assert "fixture" in entries["sample"].reasons
    assert "entrypoint" in entries["test_case"].reasons
    assert all(entry.disposition == "protected" for entry in entries.values())


def test_unresolved_dynamic_lookup_preserves_candidate_as_unknown(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "src/dynamic.py", "def hidden(): pass\ngetattr(object(), name)\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entry = next(item for item in build_inventory(root).entries if item.qualified_name == "hidden")
    assert entry.disposition == "unknown"
    assert entry.reasons == ("unresolved-dynamic-reflection",)


def test_cross_module_reflection_protects_or_holds_target(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "src/handlers.py", "def literal_target(): pass\ndef dynamic_target(): pass\n")
    _write(
        root,
        "src/runner.py",
        "import handlers\ngetattr(handlers, 'literal_target')()\ngetattr(handlers, name)()\n",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entries = {
        entry.qualified_name: entry
        for entry in build_inventory(root).entries
        if entry.path == "src/handlers.py"
    }
    assert entries["literal_target"].disposition == "protected"
    assert entries["literal_target"].reasons == ("reflection",)
    assert entries["dynamic_target"].disposition == "unknown"
    assert entries["dynamic_target"].reasons == ("unresolved-dynamic-reflection",)


@pytest.mark.parametrize(
    "import_line,receiver",
    [
        ("import pkg.handlers", "pkg.handlers"),
        ("from pkg import handlers", "handlers"),
        ("from . import handlers", "handlers"),
        ("import pkg.handlers as handlers\nother = handlers", "other"),
    ],
)
def test_qualified_and_aliased_module_reflection_holds_target(
    tmp_path: Path, import_line: str, receiver: str
) -> None:
    root = _repo(tmp_path)
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/handlers.py", "def hidden(): pass\n")
    _write(
        root,
        "src/pkg/runner.py",
        f"{import_line}\ngetattr({receiver}, name)()\n",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entry = next(
        item
        for item in build_inventory(root).entries
        if item.path == "src/pkg/handlers.py" and item.qualified_name == "hidden"
    )
    assert entry.disposition == "unknown"
    assert entry.reasons == ("unresolved-dynamic-reflection",)


def test_getattr_default_string_does_not_resolve_dynamic_name(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "src/dynamic.py", "def hidden(): pass\ngetattr(object(), name, 'fallback')\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entry = next(item for item in build_inventory(root).entries if item.qualified_name == "hidden")
    assert entry.disposition == "unknown"
    assert entry.reasons == ("unresolved-dynamic-reflection",)


def test_other_dynamic_apis_keep_separate_string_semantics(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "src/other_dynamic.py",
        """import importlib
def import_hidden(): pass
def eval_hidden(): pass
def exec_hidden(): pass
importlib.import_module("pkg.mod")
eval("eval_hidden")
exec("exec_hidden()")
""",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entries = {
        entry.qualified_name: entry
        for entry in build_inventory(root).entries
        if entry.path == "src/other_dynamic.py"
    }
    assert entries["eval_hidden"].disposition == "protected"
    assert entries["eval_hidden"].reasons == ("reflection",)
    assert entries["import_hidden"].disposition == "unknown"
    assert entries["exec_hidden"].disposition == "unknown"


@pytest.mark.parametrize("dynamic_call", ["eval", "exec"])
def test_cross_module_eval_and_exec_hold_targets(tmp_path: Path, dynamic_call: str) -> None:
    root = _repo(tmp_path)
    _write(root, "src/handlers.py", "def hidden(): pass\n")
    _write(
        root,
        "src/runner.py",
        f'import handlers\n{dynamic_call}("handlers.hidden()")\n',
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entry = next(
        item
        for item in build_inventory(root).entries
        if item.path == "src/handlers.py" and item.qualified_name == "hidden"
    )
    assert entry.disposition == "unknown"
    assert entry.reasons == ("unresolved-dynamic-reflection",)


def test_cross_module_namespace_access_holds_targets(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "src/handlers.py", "def hidden(): pass\n")
    _write(root, "src/runner.py", "import handlers\nvars(handlers)['hidden']()\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    entry = next(
        item
        for item in build_inventory(root).entries
        if item.path == "src/handlers.py" and item.qualified_name == "hidden"
    )
    assert entry.disposition == "unknown"
    assert entry.reasons == ("unresolved-dynamic-reflection",)


def test_parse_error_holds_and_excluded_roots_stay_out(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "src/broken.py", "def broken(:\n")
    _write(root, "scratch/ignored.py", "def ignored(): pass\n")
    _write(root, "alembic/versions/0001_old.py", "def upgrade(): pass\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    inventory = build_inventory(root)
    assert inventory.status == "HOLD"
    assert inventory.parse_errors == ("src/broken.py:1: parse failed",)
    assert all(entry.qualified_name not in {"ignored", "upgrade"} for entry in inventory.entries)


def test_inventory_is_deterministic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert build_inventory(root).model_dump() == build_inventory(root).model_dump()


def test_cli_writes_receipt(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    output = root / ".tmp/quality/inventory.json"
    assert main(["--repo-root", str(root), "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "function-candidate-inventory/v1"
    assert payload["deletion_authority"] is False


def test_cli_rejects_tracked_source_output_without_modifying_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    source = root / "src/app.py"
    before = source.read_bytes()
    assert main(["--repo-root", str(root), "--output", str(source)]) == 1
    assert source.read_bytes() == before
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "FunctionInventoryError"
    assert "repository .tmp" in error["message"]


def test_cli_rejects_symlink_escape_from_tmp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    link = root / ".tmp/escape"
    link.parent.mkdir(parents=True)
    link.symlink_to(root / "src", target_is_directory=True)
    source = root / "src/app.py"
    before = source.read_bytes()
    assert main(["--repo-root", str(root), "--output", str(link / "app.py")]) == 1
    assert source.read_bytes() == before
    assert json.loads(capsys.readouterr().err)["error"] == "FunctionInventoryError"


def test_cli_rejects_symlinked_tmp_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    (root / ".tmp").symlink_to(root / "src", target_is_directory=True)
    source = root / "src/app.py"
    before = source.read_bytes()
    assert main(["--repo-root", str(root), "--output", ".tmp/app.py"]) == 1
    assert source.read_bytes() == before
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "FunctionInventoryError"
    assert "cannot be a symlink" in error["message"]
