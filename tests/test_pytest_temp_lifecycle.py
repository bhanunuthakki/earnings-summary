"""Exercise the actual pytest producer hook in isolated child sessions."""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from src.operations import temp_run_retention
from src.operations.artifact_retention import run_retention
from src.operations.storage_reservation import release_peak
from src.operations.temp_run_retention import discover_temp_runs

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("passed", [True, False])
def test_session_hook_registers_success_and_preserves_failure(
    tmp_path: Path, passed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic-project"
    tests = source / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(PROJECT_ROOT / "tests" / "conftest.py", tests / "conftest.py")
    locator = source / "fixture-location.txt"
    (tests / "test_one.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "def test_one(tmp_path):\n"
        "    (tmp_path / 'fixture.db').write_bytes(b'synthetic fixture')\n"
        "    Path(os.environ['FIXTURE_LOCATION']).write_text(str(tmp_path))\n"
        f"    assert {passed!r}\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=os.pathsep.join(
            (str(PROJECT_ROOT), str(PROJECT_ROOT / "src"), str(Path(pytest.__file__).parent.parent))
        ),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        FIXTURE_LOCATION=str(locator),
        EARNINGS_SUMMARY_ENV_FILE=str(source / "no-external-env"),
        EARNINGS_SUMMARY_DB_PATH=str(source / "synthetic-only.db"),
        EARNINGS_SUMMARY_MANAGED_TEST_TEMPS="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "execution/sqlite_bootstrap.py"),
            "-m",
            "pytest",
            str(tests),
            "-q",
            "-o",
            "addopts=",
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == (0 if passed else 1), result.stdout + result.stderr
    fixture_dir = Path(locator.read_text())
    run_root = next(
        parent for parent in fixture_dir.parents if (parent / ".earnings-temp-run.json").is_file()
    )
    manifest: object = json.loads((run_root / ".earnings-temp-run.json").read_bytes())
    assert isinstance(manifest, dict)
    assert manifest["status"] == ("completed" if passed else "failed")
    fixture = fixture_dir / "fixture.db"
    if passed:
        assert not fixture.exists(), "successful payload retires at child process exit"
        assert list((run_root / ".earnings-temp-retirement").glob("*.jsonl"))
    else:
        assert fixture.read_bytes() == b"synthetic fixture"

    def isolated_roots(repo_root: Path, code_root: Path | None) -> list[tuple[Path, bool]]:
        return [(run_root, False)]

    monkeypatch.setattr(temp_run_retention, "_search_roots", isolated_roots)
    now = datetime.now(UTC) + timedelta(days=8)
    discovery = discover_temp_runs(source, code_root=source, now=now)
    catalog = discovery.catalog.model_copy(
        update={
            "artifacts": [
                item for item in discovery.catalog.artifacts if item.allowed_root == run_root
            ]
        }
    )
    retirement = run_retention(source, now=now, apply=True, catalog=catalog)
    assert retirement.deleted == 0
    assert fixture.exists() is not passed
    assert (run_root / ".earnings-temp-run.json").is_file()
    # Remove only the exact child test directory that this test created. Failed
    # session protection is already proved; this test does not leave test debris.
    if not passed:
        sealed = temp_run_retention.TempRunManifest.model_validate(manifest)
        if sealed.reservation_token is not None:
            # This failure was intentional and verified. Release its exact lease
            # before the test removes the owned reproduction it no longer needs.
            release_peak(run_root, sealed.reservation_token)
    shutil.rmtree(run_root)


def test_actions_floor_keeps_fixture_parent_inside_admitted_system_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = ast.parse((PROJECT_ROOT / "tests/conftest.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_collection_temp_policy"
    )
    namespace: dict[str, object] = {"Path": Path, "os": os, "stat": stat}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "fixture_policy", "exec"), namespace)
    policy = cast(Callable[[], tuple[Path | None, int]], namespace["_collection_temp_policy"])
    runner = tmp_path / "runner-separate-from-system-temp"
    runner.mkdir()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(runner))
    assert policy() == (None, 4 * 1024**3)
