from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from quality import static_quality
from quality.static_quality import InventoryFailure, inventory


def test_inventory_partitions_tracked_python_and_counts_diagnostics(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    paths = [
        "src/app.py",
        "alembic/versions/0001_initial_schema.py",
        "alembic/versions_archived/0000_baseline.py",
        "execution/build_redesigned_dcf.py",
        "scratch/retire_me.py",
    ]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# type: ignore\n", encoding="utf-8")

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "ls-files", "--"]:
            return subprocess.CompletedProcess(command, 0, "\n".join(paths) + "\n", "")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, f"{command[0]} 1.0\n", "")
        if str(command[0]).endswith("ruff") and "format" not in command:
            return subprocess.CompletedProcess(
                command, 1, '[{"filename":"src/app.py","code":"F401"}]', ""
            )
        if str(command[0]).endswith("ruff"):
            return subprocess.CompletedProcess(command, 1, "Would reformat: src/app.py\n", "")
        return subprocess.CompletedProcess(
            command,
            1,
            '{"generalDiagnostics":[{"file":"src/app.py","rule":"reportUnusedImport"}]}',
            "",
        )

    result = inventory(tmp_path, run, exception_paths=["execution/build_redesigned_dcf.py"])
    assert result.active == ["src/app.py"]
    assert result.immutable_historical_migration == [
        "alembic/versions/0001_initial_schema.py",
        "alembic/versions_archived/0000_baseline.py",
    ]
    assert result.generated_declarative_exception == ["execution/build_redesigned_dcf.py"]
    assert result.retirement_candidates == ["scratch/retire_me.py"]
    assert result.diagnostics[0].count == 1
    assert result.diagnostics[1].count == 1
    assert result.diagnostics[2].count == 1
    assert result.diagnostics[-1].diagnostics_by_rule["# type: ignore"] == 5
    assert result.diagnostics[0].receipt_path.startswith(".tmp/static_quality/")
    assert result.diagnostics[0].diagnostics_by_directory == {"src": 1}
    pyright = next(item for item in result.diagnostics if item.tool == "pyright")
    assert pyright.command[-3:] == ["--pythonpath", static_quality.sys.executable, "--outputjson"]


def test_absolute_diagnostic_paths_are_checkout_portable(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "ls-files", "--"]:
            return subprocess.CompletedProcess(command, 0, "src/app.py\n", "")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        if str(command[0]).endswith("ruff") and "format" not in command:
            payload = json.dumps(
                [
                    {"filename": str(root / "src/app.py"), "code": "F401"},
                    {"filename": "C:\\checkout\\src\\app.py", "code": "F401"},
                    {"filename": "../outside.py", "code": "F401"},
                    {"filename": "", "code": "F401"},
                ]
            )
            return subprocess.CompletedProcess(command, 1, payload, "")
        if str(command[0]).endswith("ruff"):
            return subprocess.CompletedProcess(command, 0, "", "")
        payload = json.dumps({"generalDiagnostics": [{"file": str(root / "src/app.py")}]})
        return subprocess.CompletedProcess(command, 1, payload, "")

    result = inventory(tmp_path, run)
    assert result.diagnostics[0].diagnostics_by_directory == {
        ".": 1,
        "<external>": 2,
        "src": 1,
    }
    assert result.diagnostics[2].diagnostics_by_directory == {"src": 1}
    assert all(not item.receipt_path.startswith(str(tmp_path)) for item in result.diagnostics)


def test_inventory_fails_when_tracked_file_is_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "missing.py\n", "")

    with pytest.raises(InventoryFailure, match="missing"):
        inventory(tmp_path, run)


def test_exception_configuration_is_capped_and_requires_tracked_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "ls-files", "--"]:
            return subprocess.CompletedProcess(command, 0, "a.py\n", "")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        if str(command[0]).endswith("pyright"):
            return subprocess.CompletedProcess(command, 0, '{"generalDiagnostics":[]}', "")
        return subprocess.CompletedProcess(command, 0, "[]" if "format" not in command else "", "")

    result = inventory(tmp_path, run, exception_paths=["a.py", "a.py", "x.py", "y.py"])
    assert result.status == "HOLD"
    assert any("hard cap" in item for item in result.violations)
    assert any("not tracked" in item for item in result.violations)


def test_active_file_outside_pyright_include_is_hold_and_identity_is_stable(
    tmp_path: Path,
) -> None:
    paths = ["src/app.py", "scripts/tool.py"]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# pyright: ignore[reportUnusedImport]\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = ["src"]\nexclude = []\n', encoding="utf-8"
    )

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "ls-files", "--"]:
            return subprocess.CompletedProcess(command, 0, "\n".join(paths) + "\n", "")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        if str(command[0]).endswith("pyright"):
            return subprocess.CompletedProcess(command, 0, '{"generalDiagnostics":[]}', "")
        return subprocess.CompletedProcess(command, 0, "[]" if "format" not in command else "", "")

    first = inventory(tmp_path, run)
    second = inventory(tmp_path, run)
    assert first.status == "HOLD"
    assert first.receipt_identity == second.receipt_identity
    assert any("outside Pyright include roots" in item for item in first.violations)
    assert first.suppressions_by_file["scripts/tool.py"]["# pyright: ignore"] == 1


def test_unavailable_tool_and_malformed_machine_output_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def unavailable(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "ls-files", "--"]:
            return subprocess.CompletedProcess(command, 0, "src/app.py\n", "")
        return subprocess.CompletedProcess(command, 127, "", "missing")

    with pytest.raises(InventoryFailure, match="required tool unavailable"):
        inventory(tmp_path, unavailable)

    def malformed(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "ls-files", "--"]:
            return subprocess.CompletedProcess(command, 0, "src/app.py\n", "")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        return subprocess.CompletedProcess(command, 1, "not-json", "")

    with pytest.raises(InventoryFailure, match="malformed ruff"):
        inventory(tmp_path, malformed)


def test_cli_returns_hold_for_hidden_active_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = static_quality.StaticQualityInventory(
        repo_root=str(tmp_path),
        tracked_python_files=1,
        active=[".hidden/tool.py"],
        immutable_historical_migration=[],
        generated_declarative_exception=[],
        diagnostics=[],
        current_exclusions={},
        status="HOLD",
        violations=["hidden active directory is not allowed"],
        scoped_commit="head",
        source_hash="a",
        config_hash="b",
        receipt_identity="c",
    )

    def fake_inventory(
        repo_root: Path, *, exception_paths: Sequence[str] = ()
    ) -> static_quality.StaticQualityInventory:
        del repo_root, exception_paths
        return result

    monkeypatch.setattr(static_quality, "inventory", fake_inventory)
    assert static_quality.main(["--repo-root", str(tmp_path)]) == 2
