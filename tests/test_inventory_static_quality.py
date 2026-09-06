from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from quality import static_quality
from quality.git_env import clean_local_git_env, is_git_executable
from quality.static_quality import InventoryFailure, inventory


def _is_tracked_files_command(command: Sequence[str]) -> bool:
    return list(command[:3]) == ["git", "ls-files", "-z"]


def _tracked_files_payload(paths: Sequence[str]) -> str:
    return "\0".join(paths) + "\0"


def _clean_git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=clean_local_git_env(),
    ).stdout.strip()


@pytest.mark.parametrize(
    "command",
    ["git", "/usr/bin/git", "GIT.EXE", r"C:\\Program Files\\Git\\bin\\git.cmd"],
)
def test_git_executable_detection_is_portable(command: str) -> None:
    assert is_git_executable(command)


def test_local_git_environment_drops_only_git_variables() -> None:
    cleaned = clean_local_git_env(
        {"PATH": "/tools", "GIT_DIR": "/outer.git", "git_work_tree": "/outer"}
    )

    assert cleaned == {"PATH": "/tools"}
    assert is_git_executable("ruff") is False


def _successful_runner(
    paths: Sequence[str], *, tool_version: str = "1.0"
) -> static_quality.CommandRunner:
    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(command, 0, _tracked_files_payload(paths), "")
        if list(command[:3]) == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            tool = Path(command[0]).name
            return subprocess.CompletedProcess(command, 0, f"{tool} {tool_version}\n", "")
        if str(command[0]).endswith("pyright"):
            return subprocess.CompletedProcess(command, 0, '{"generalDiagnostics":[]}', "")
        return subprocess.CompletedProcess(command, 0, "[]" if "format" not in command else "", "")

    return run


def test_scanner_git_ignores_outer_repository_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "sentinel"
    requested = tmp_path / "requested"
    for root in (sentinel, requested):
        (root / "src").mkdir(parents=True)
        (root / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _clean_git(root, "init", "-q")
        _clean_git(root, "add", ".")
    sentinel_config = sentinel / ".git/config"
    before = sentinel_config.read_bytes()
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(sentinel))
    monkeypatch.setenv("GIT_INDEX_FILE", str(sentinel / ".git/index"))

    source_hash, config_hash = static_quality.scanner_input_hashes(requested)

    assert source_hash
    assert config_hash
    assert sentinel_config.read_bytes() == before


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
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(command, 0, _tracked_files_payload(paths), "")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, f"{Path(command[0]).name} 1.0\n", "")
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
    assert pyright.command[-3:] == [
        "--pythonpath",
        static_quality.PROJECT_PYTHON_TOKEN,
        "--outputjson",
    ]
    assert result.repo_root == "."
    assert result.diagnostics[0].command[0] == "ruff"


def test_absolute_diagnostic_paths_are_checkout_portable(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(
                command, 0, _tracked_files_payload(["src/app.py"]), ""
            )
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
        return subprocess.CompletedProcess(command, 0, _tracked_files_payload(["missing.py"]), "")

    with pytest.raises(InventoryFailure, match="missing"):
        inventory(tmp_path, run)


def test_undecodable_pyproject_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_bytes(b"[tool.pyright]\ninclude = [\xff]\n")
    runner = _successful_runner(["src/app.py"])

    with pytest.raises(InventoryFailure, match=r"malformed pyproject\.toml"):
        inventory(tmp_path, runner)
    with pytest.raises(InventoryFailure, match=r"unable to read pyproject\.toml"):
        static_quality.scanner_input_hashes(tmp_path, runner)


def test_exception_configuration_is_capped_and_requires_tracked_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(command, 0, _tracked_files_payload(["a.py"]), "")
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
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(command, 0, _tracked_files_payload(paths), "")
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


def test_suppression_inventory_ignores_string_literals(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text(
        'TEXT = "# type: ignore and # pyright: ignore"\n# type: ignore[assignment]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = ["src"]\nexclude = []\n', encoding="utf-8"
    )

    def run(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(
                command, 0, _tracked_files_payload(["src/app.py"]), ""
            )
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        if str(command[0]).endswith("pyright"):
            return subprocess.CompletedProcess(command, 0, '{"generalDiagnostics":[]}', "")
        return subprocess.CompletedProcess(command, 0, "[]" if "format" not in command else "", "")

    result = inventory(tmp_path, run)
    assert result.suppressions_by_file == {
        "src/app.py": {"# type: ignore": 1, "# pyright: ignore": 0}
    }


def test_unavailable_tool_and_malformed_machine_output_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def unavailable(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(
                command, 0, _tracked_files_payload(["src/app.py"]), ""
            )
        return subprocess.CompletedProcess(command, 127, "", "missing")

    with pytest.raises(InventoryFailure, match="required tool unavailable"):
        inventory(tmp_path, unavailable)

    def malformed(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(
                command, 0, _tracked_files_payload(["src/app.py"]), ""
            )
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        return subprocess.CompletedProcess(command, 1, "not-json", "")

    with pytest.raises(InventoryFailure, match="malformed ruff"):
        inventory(tmp_path, malformed)


def test_nul_paths_preserve_whitespace_and_newlines(tmp_path: Path) -> None:
    paths = [" leading.py", "nested/line\nbreak.py", "trailing .py"]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")

    result = inventory(tmp_path, _successful_runner(paths))

    assert result.active == sorted(paths)
    assert result.status == "PASS"


def test_tracked_path_escape_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InventoryFailure, match="escapes"):
        inventory(tmp_path, _successful_runner(["../outside.py"]))


def test_tracked_symlink_escape_fails_closed(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/link.py").symlink_to(outside)

    with pytest.raises(InventoryFailure, match="escapes"):
        inventory(tmp_path, _successful_runner(["src/link.py"]))


def test_tracked_symlink_loop_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/loop.py").symlink_to("loop.py")

    with pytest.raises(InventoryFailure, match="missing or unreadable"):
        inventory(tmp_path, _successful_runner(["src/loop.py"]))


def test_undecodable_source_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_bytes(b"VALUE = '\xff'\n")

    with pytest.raises(InventoryFailure, match="decode"):
        inventory(tmp_path, _successful_runner(["src/app.py"]))


def test_malformed_diagnostic_fields_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def malformed(command: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
        if _is_tracked_files_command(command):
            return subprocess.CompletedProcess(
                command, 0, _tracked_files_payload(["src/app.py"]), ""
            )
        if list(command[:3]) == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "head\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "tool 1\n", "")
        if str(command[0]).endswith("ruff") and "format" not in command:
            return subprocess.CompletedProcess(command, 1, '[{"filename": 42}]', "")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(InventoryFailure, match="diagnostic path"):
        inventory(tmp_path, malformed)


def test_active_ruff_exclusion_is_hold(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/hidden.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nextend-exclude = ["src/hidden.py"]\n'
        '[tool.pyright]\ninclude = ["src"]\nexclude = []\n',
        encoding="utf-8",
    )

    result = inventory(tmp_path, _successful_runner(["src/hidden.py"]))

    assert result.status == "HOLD"
    assert result.violations == ["active files excluded by Ruff: src/hidden.py"]


def test_receipt_is_byte_stable_across_checkout_roots(tmp_path: Path) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        (root / "src").mkdir(parents=True)
        (root / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[tool.pyright]\ninclude = ["src"]\nexclude = []\n', encoding="utf-8"
        )

    first = inventory(roots[0], _successful_runner(["src/app.py"]))
    second = inventory(roots[1], _successful_runner(["src/app.py"]))

    assert first.model_dump_json() == second.model_dump_json()
    assert first.repo_root == "."
    assert all(str(root) not in first.model_dump_json() for root in roots)
    assert static_quality.sys.executable not in first.model_dump_json()


def test_runtime_and_tool_versions_change_receipt_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    base = inventory(tmp_path, _successful_runner(["src/app.py"], tool_version="1.0"))
    tool_changed = inventory(tmp_path, _successful_runner(["src/app.py"], tool_version="2.0"))
    monkeypatch.setattr(
        static_quality,
        "_runtime_identity",
        lambda: static_quality.RuntimeIdentity(
            implementation="CPython",
            python_version="99.0",
            platform="test",
            machine="test",
        ),
    )
    runtime_changed = inventory(tmp_path, _successful_runner(["src/app.py"], tool_version="1.0"))

    assert tool_changed.receipt_identity != base.receipt_identity
    assert runtime_changed.receipt_identity != base.receipt_identity


def test_cli_returns_hold_for_hidden_active_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = static_quality.StaticQualityInventory(
        repo_root=".",
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
        runtime=static_quality.RuntimeIdentity(
            implementation="CPython",
            python_version="3.14.0",
            platform="darwin",
            machine="arm64",
        ),
    )

    def fake_inventory(
        repo_root: Path, *, exception_paths: Sequence[str] = ()
    ) -> static_quality.StaticQualityInventory:
        del repo_root, exception_paths
        return result

    monkeypatch.setattr(static_quality, "inventory", fake_inventory)
    assert static_quality.main(["--repo-root", str(tmp_path)]) == 2
