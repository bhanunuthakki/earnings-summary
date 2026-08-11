"""CLI receipt contract for population cutover evaluation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.evaluate_population_cutover import main
from provenance.population_completeness import canonical_json, digest_text

STAMP = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"population-evaluator-cli").hexdigest()


def test_cli_emits_one_hash_bound_failure_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "empty.db"
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
                "--policy-config-sha256",
                SHA,
                "--source-snapshot-sha256",
                SHA,
                "--evaluated-at",
                STAMP.isoformat(),
                "--sealed-at",
                STAMP.isoformat(),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    receipt_sha256 = payload.pop("receipt_sha256")
    assert receipt_sha256 == digest_text(canonical_json(payload))
    assert payload["status"] == "blocked"
