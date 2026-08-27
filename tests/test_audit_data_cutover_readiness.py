"""Operational adapter for the exact 13-gate population cutover audit."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.audit_data_cutover_readiness import main
from provenance.population_completeness import REQUIRED_CUTOVER_AUDIT_GATES

STAMP = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def test_adapter_emits_one_closed_json_receipt_and_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "candidate.db"
    sqlite3.connect(database).close()

    assert (
        main(
            [
                "--db-path",
                str(database),
                "--knowledge-cutoff",
                STAMP.isoformat(),
                "--observed-through",
                STAMP.isoformat(),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["schema_version"] == "data-cutover-readiness-audit/v1"
    assert {row["gate"] for row in payload["coverage"]} == set(REQUIRED_CUTOVER_AUDIT_GATES)
    assert payload["has_blockers"] is True
    assert '"event": "data_cutover_readiness_audit_finished"' in captured.err


def test_adapter_is_directly_executable_from_repo_root() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "execution" / "audit_data_cutover_readiness.py"),
            "--help",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--knowledge-cutoff" in result.stdout
    assert "--observed-through" in result.stdout
