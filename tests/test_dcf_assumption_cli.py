"""Provider-neutral DCF command retains the old executable alias."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", ["refresh_dcf_assumptions.py", "dcf_opus_assumptions.py"])
def test_assumption_cli_skips_incomplete_history_without_paid_work(
    script: str, tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    repository = Path(__file__).resolve().parents[1]
    database = migrated_db(tmp_path / "external.sqlite")
    state_root = tmp_path / "isolated-state"
    state_root.mkdir()
    result = subprocess.run(
        [sys.executable, str(repository / "execution" / script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "DCF_TICKER": "TEST",
            "DCF_REPO_ROOT": str(state_root),
            "EARNINGS_SUMMARY_DB_PATH": str(database),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("SKIP\tTEST\tno complete fiscal year yet")
    assert not (state_root / "data" / "dcf_assumptions").exists()
    assert not (state_root / "data" / "portfolio.db").exists()
