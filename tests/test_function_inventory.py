from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def test_cli_writes_receipt(tmp_path: Path, capsys: object) -> None:
    root = _repo(tmp_path)
    output = root / "inventory.json"
    assert main(["--repo-root", str(root), "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "function-candidate-inventory/v1"
    assert payload["deletion_authority"] is False
