from __future__ import annotations

import json
from pathlib import Path

import scripts.check_architecture_boundaries as boundaries


def _write_config(
    root: Path, *, helpers: list[str], mutations: list[str], root_modules: list[str]
) -> Path:
    config = root / "config"
    config.mkdir(parents=True)
    path = config / "architecture_boundaries.json"
    path.write_text(
        json.dumps(
            {
                "sanctioned_execution_helpers": helpers,
                "execution_sys_path_mutations": mutations,
                "root_src_modules": root_modules,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_current_repo_matches_the_recorded_baseline() -> None:
    assert boundaries.validate() == []


def test_new_execution_sys_path_mutation_fails(tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "execution").mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "execution" / "_lib.py").write_text("", encoding="utf-8")
    (repo_root / "execution" / "sqlite_bootstrap.py").write_text("", encoding="utf-8")
    (repo_root / "execution" / "existing_cli.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "existing.py").write_text("", encoding="utf-8")
    config_path = _write_config(
        repo_root,
        helpers=["execution/_lib.py", "execution/sqlite_bootstrap.py"],
        mutations=["execution/existing_cli.py"],
        root_modules=["src/existing.py"],
    )

    (repo_root / "execution" / "new_cli.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\n",
        encoding="utf-8",
    )

    failures = boundaries.validate(repo_root, config_path)
    assert any("execution/new_cli.py" in failure for failure in failures)


def test_new_root_src_module_fails(tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "execution").mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "execution" / "_lib.py").write_text("", encoding="utf-8")
    (repo_root / "execution" / "sqlite_bootstrap.py").write_text("", encoding="utf-8")
    (repo_root / "execution" / "existing_cli.py").write_text("", encoding="utf-8")
    (repo_root / "src" / "existing.py").write_text("", encoding="utf-8")
    config_path = _write_config(
        repo_root,
        helpers=["execution/_lib.py", "execution/sqlite_bootstrap.py"],
        mutations=[],
        root_modules=["src/existing.py"],
    )

    (repo_root / "src" / "new_feature.py").write_text("", encoding="utf-8")

    failures = boundaries.validate(repo_root, config_path)
    assert any("src/new_feature.py" in failure for failure in failures)


def test_stale_allowlist_entries_fail(tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "execution").mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "execution" / "_lib.py").write_text("", encoding="utf-8")
    (repo_root / "execution" / "existing_cli.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "existing.py").write_text("", encoding="utf-8")
    config_path = _write_config(
        repo_root,
        helpers=["execution/_lib.py", "execution/sqlite_bootstrap.py"],
        mutations=["execution/existing_cli.py", "execution/removed_cli.py"],
        root_modules=["src/existing.py", "src/removed.py"],
    )

    failures = boundaries.validate(repo_root, config_path)
    assert any("stale sanctioned execution helpers" in failure for failure in failures)
    assert any("stale execution sys.path mutations" in failure for failure in failures)
    assert any("stale loose src root modules" in failure for failure in failures)
