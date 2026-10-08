"""Exercise the actual pytest producer hook in isolated child sessions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.operations import temp_run_retention
from src.operations.artifact_retention import run_retention
from src.operations.temp_run_retention import discover_temp_runs

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("passed", [True, False])
def test_session_hook_registers_success_and_preserves_failure(
    tmp_path: Path, passed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_roots: Callable[[Path, Path | None], list[tuple[Path, bool]]] = getattr(
        temp_run_retention, "_search_roots"
    )

    def isolated_roots(repo_root: Path, code_root: Path | None) -> list[tuple[Path, bool]]:
        return [item for item in search_roots(repo_root, code_root) if item[0] != Path("C:/tmp")]

    monkeypatch.setattr(
        temp_run_retention,
        "_search_roots",
        isolated_roots,
    )
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
        PYTHONPATH=os.pathsep.join((str(PROJECT_ROOT), str(PROJECT_ROOT / "src"))),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        FIXTURE_LOCATION=str(locator),
        EARNINGS_SUMMARY_ENV_FILE=str(source / "no-external-env"),
        EARNINGS_SUMMARY_DB_PATH=str(source / "synthetic-only.db"),
        EARNINGS_SUMMARY_MANAGED_TEST_TEMPS="1",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests), "-q", "-o", "addopts="],
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
    fixture = fixture_dir / "fixture.db"
    if passed:
        assert retirement.deleted == 1
        assert not fixture.exists()
    else:
        assert retirement.deleted == 0
        assert fixture.exists()
    assert (run_root / ".earnings-temp-run.json").is_file()
    # Remove only the exact child test directory that this test created. Failed
    # session protection is already proved; this test does not leave test debris.
    shutil.rmtree(run_root)
