from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quality import duplicates as analyzer


def _function(name: str, *, extra: bool = False) -> str:
    lines = [f"def {name}(value):", "    total = 0"]
    for index in range(18):
        lines.append(f"    total += value + {index}  # comment {index}")
    if extra:
        lines.append("    total += 999")
    lines.append("    return total")
    return "\n".join(lines) + "\n"


def _inventory(tmp_path: Path, files: dict[str, str]) -> analyzer.DuplicateInventory:
    paths: list[Path] = []
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    original = analyzer.tracked_python_files
    original_commit = analyzer.resolve_git_commit

    def fake_tracked_python_files(_: Path) -> list[Path]:
        return sorted(paths)

    analyzer.tracked_python_files = fake_tracked_python_files

    def fake_git_commit(_: Path, __: str) -> str:
        return "a" * 40

    analyzer.resolve_git_commit = fake_git_commit
    try:
        return analyzer.build_inventory(tmp_path)
    finally:
        analyzer.tracked_python_files = original
        analyzer.resolve_git_commit = original_commit


def test_deterministic_exact_groups_and_identifier_comment_normalization(tmp_path: Path) -> None:
    source = _function("first")
    renamed = (
        _function("renamed")
        .replace("total", "renamed_total")
        .replace("# comment", "# renamed comment")
    )
    inventory_a = _inventory(tmp_path / "a", {"one.py": source, "two.py": renamed})
    inventory_b = _inventory(tmp_path / "a", {"one.py": source, "two.py": renamed})
    assert inventory_a.model_dump() == inventory_b.model_dump()
    assert len(inventory_a.exact_groups) == 1
    assert inventory_a.exact_totals.participating_functions == 2
    assert inventory_a.exact_groups[0].functions[0].body_lines >= 15


def test_threshold_exclusions_and_repository_path_exclusion(tmp_path: Path) -> None:
    inventory = _inventory(
        tmp_path,
        {
            "one.py": _function("one"),
            "two.py": "def tiny(x):\n    return x\n",
            "tests/hidden.py": "def hidden(x):\n    return x\n",
        },
    )
    assert inventory.files_scanned == 3  # path filtering is separately tested below
    assert not inventory.exact_groups


def test_git_file_listing_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(*_: object, **__: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(analyzer.subprocess, "run", fail)
    with pytest.raises(ValueError, match="cannot enumerate tracked Python files"):
        analyzer.tracked_python_files(tmp_path)


def test_missing_tracked_file_fails_closed_through_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stdout = b"src/missing.py\0"

    def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(analyzer.subprocess, "run", fake_run)

    assert analyzer.main(["--repo-root", str(tmp_path)]) == 2
    assert "tracked Python files are missing" in capsys.readouterr().err


def test_git_commit_failure_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(*_: object, **__: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(analyzer.subprocess, "run", fail)
    with pytest.raises(ValueError, match="cannot resolve git commit"):
        analyzer.resolve_git_commit(tmp_path, "WORKTREE")


def test_successful_empty_inventory_remains_distinguishable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_tracked_files(_: Path) -> list[Path]:
        return []

    def fake_git_commit(_: Path, __: str) -> str:
        return "a" * 40

    monkeypatch.setattr(analyzer, "tracked_python_files", no_tracked_files)
    monkeypatch.setattr(analyzer, "resolve_git_commit", fake_git_commit)
    inventory = analyzer.build_inventory(tmp_path)
    assert inventory.files_scanned == 0
    assert inventory.functions_scanned == 0
    assert not inventory.parse_errors


def test_near_miss_group_is_distinct_from_exact(tmp_path: Path) -> None:
    inventory = _inventory(
        tmp_path, {"one.py": _function("one"), "two.py": _function("two", extra=True)}
    )
    assert not inventory.exact_groups
    assert len(inventory.near_miss_groups) == 1
    assert inventory.near_miss_groups[0].similarity >= analyzer.NEAR_MISS_RATIO


def test_literal_and_call_target_changes_are_near_misses(tmp_path: Path) -> None:
    base = _function("one")
    changed = base.replace("value + 0", "value + 777", 1).replace(
        "return total", "return int(total)"
    )
    inventory = _inventory(tmp_path, {"one.py": base, "two.py": changed})
    assert not inventory.exact_groups
    assert len(inventory.near_miss_groups) == 1


def test_early_edit_remains_a_near_miss(tmp_path: Path) -> None:
    base = _function("one")
    changed = base.replace("total = 0", "total = 1", 1)
    inventory = _inventory(tmp_path, {"one.py": base, "two.py": changed})
    assert len(inventory.near_miss_groups) == 1


def test_parse_error_is_recorded_and_cli_fails_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "src" / "broken.py"
    broken.parent.mkdir(parents=True)
    broken.write_text("def broken(:\n", encoding="utf-8")
    original = analyzer.tracked_python_files

    def fake_tracked_python_files(_: Path) -> list[Path]:
        return [broken]

    analyzer.tracked_python_files = fake_tracked_python_files
    original_commit = analyzer.resolve_git_commit

    def fake_git_commit(_: Path, __: str) -> str:
        return "a" * 40

    analyzer.resolve_git_commit = fake_git_commit
    try:
        assert analyzer.main(["--repo-root", str(tmp_path)]) == 2
    finally:
        analyzer.tracked_python_files = original
        analyzer.resolve_git_commit = original_commit
    assert "parse_errors" in capsys.readouterr().err


def test_ratchet_rejects_increased_duplicate_totals(tmp_path: Path) -> None:
    baseline = _inventory(tmp_path / "base", {"one.py": _function("one")})
    current = _inventory(
        tmp_path / "current", {"one.py": _function("one"), "two.py": _function("two")}
    )
    current = current.model_copy(update={"scanner_hash": baseline.scanner_hash})
    ratchet = analyzer.compare_inventory(current, baseline)
    assert ratchet.status == "FAIL"
    assert any(item.startswith("exact groups increased") for item in ratchet.regressions)


def test_ratchet_holds_when_baseline_has_parse_errors(tmp_path: Path) -> None:
    baseline = _inventory(tmp_path / "baseline", {"one.py": _function("one")}).model_copy(
        update={"parse_errors": ["src/broken.py: invalid syntax"]}
    )
    current = _inventory(tmp_path / "current", {"one.py": _function("one")}).model_copy(
        update={"scanner_hash": baseline.scanner_hash}
    )

    assert analyzer.compare_inventory(current, baseline).status == "HOLD"


def test_cli_holds_when_baseline_has_parse_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = _inventory(tmp_path / "baseline", {"one.py": _function("one")}).model_copy(
        update={"parse_errors": ["src/broken.py: invalid syntax"]}
    )
    current = _inventory(tmp_path / "current", {"one.py": _function("one")}).model_copy(
        update={"scanner_hash": baseline.scanner_hash}
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json(), encoding="utf-8")

    def fake_build_inventory(_: Path, __: str) -> analyzer.DuplicateInventory:
        return current

    monkeypatch.setattr(analyzer, "build_inventory", fake_build_inventory)

    assert analyzer.main(["--repo-root", str(tmp_path), "--baseline", str(baseline_path)]) == 2
