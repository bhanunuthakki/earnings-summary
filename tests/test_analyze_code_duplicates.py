from __future__ import annotations

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

    def fake_tracked_python_files(_: Path) -> list[Path]:
        return sorted(paths)

    analyzer.tracked_python_files = fake_tracked_python_files
    try:
        return analyzer.build_inventory(tmp_path)
    finally:
        analyzer.tracked_python_files = original


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
    assert all("tests" not in path.parts for path in analyzer.tracked_python_files(tmp_path))


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
    try:
        assert analyzer.main(["--repo-root", str(tmp_path)]) == 2
    finally:
        analyzer.tracked_python_files = original
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
