"""Gate the ratchet enforcement script over small synthetic repositories."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.check_architecture_ratchet as gate
from quality.admission_policy import verify_registry
from quality.architecture import build_architecture_receipt
from quality.duplicates import build_inventory
from quality.git_env import clean_local_git_env


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        env=clean_local_git_env(),
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "alpha.py").write_text(
        "def work(value: int) -> int:\n    return value + 1\n", encoding="utf-8"
    )
    (root / "src" / "beta.py").write_text(
        "def more(value: int) -> int:\n    return value * 2\n", encoding="utf-8"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def _write_baselines(root: Path) -> tuple[Path, Path]:
    docs = root / "docs" / "quality"
    docs.mkdir(parents=True)
    architecture = docs / "architecture-ratchet.json"
    architecture.write_text(
        build_architecture_receipt(root, "WORKTREE").model_dump_json(indent=2), encoding="utf-8"
    )
    duplicates = docs / "duplicates-ratchet.json"
    duplicates.write_text(
        build_inventory(root, "WORKTREE").model_dump_json(indent=2), encoding="utf-8"
    )
    return architecture, duplicates


def test_admission_registry_is_intact() -> None:
    assert verify_registry() is True


def test_clean_tree_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    architecture, duplicates = _write_baselines(root)

    assert gate.validate(root, architecture, duplicates) == ([], False)


def test_unbaselined_module_fails_naming_the_regression(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    architecture, duplicates = _write_baselines(root)

    probe = root / "src" / "pipeline" / "probe.py"
    probe.parent.mkdir()
    probe.write_text("PROBE = 1\n", encoding="utf-8")
    _git(root, "add", "-N", "src/pipeline/probe.py")

    messages, hold = gate.validate(root, architecture, duplicates)
    assert hold is False
    assert any(
        "architecture: executable_modules increased from 2 to 3" in message for message in messages
    )
    assert any(
        "architecture: total_noncomment_loc increased from 4 to 5" in message
        for message in messages
    )

    probe.unlink()
    _git(root, "reset", "-q")

    assert gate.validate(root, architecture, duplicates) == ([], False)


def test_scanner_drift_holds(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    architecture, duplicates = _write_baselines(root)

    payload: dict[str, object] = json.loads(architecture.read_text(encoding="utf-8"))
    payload["scanner_sha256"] = "f" * 64
    architecture.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    messages, hold = gate.validate(root, architecture, duplicates)
    assert hold is True
    assert any(
        "architecture scanner_sha256 drifted from the baseline" in message for message in messages
    )


def test_duplicate_growth_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    architecture, duplicates = _write_baselines(root)

    body = (
        "def bulk(value: int) -> int:\n"
        + "".join(f"    step_{index} = value + {index}\n" for index in range(16))
        + "    return step_15\n"
    )
    for name in ("src/copy_a.py", "src/copy_b.py"):
        (root / name).write_text(body, encoding="utf-8")
        _git(root, "add", "-N", name)

    messages, hold = gate.validate(root, architecture, duplicates)
    assert hold is False
    assert any(
        "duplicates: exact duplicated LOC increased from 0 to" in message for message in messages
    )


def test_admission_registry_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    architecture, duplicates = _write_baselines(root)

    monkeypatch.setattr(gate, "verify_registry", lambda: False)

    messages, hold = gate.validate(root, architecture, duplicates)
    assert hold is False
    assert any("quality admission registry mismatch" in message for message in messages)
