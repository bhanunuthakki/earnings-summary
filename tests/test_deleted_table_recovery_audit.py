from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from execution.audit_deleted_table_recovery import audit


def _catalog(
    path: Path,
    targets: list[str],
    *,
    exemptions: dict[str, str] | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "id": "retired-plane",
                        "schema_targets": targets,
                        "data_restore_exemptions": exemptions or {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _backup(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alembic_version(version_num TEXT NOT NULL)")
    conn.execute("INSERT INTO alembic_version VALUES ('0272_archive_generation_catalog')")
    conn.execute("CREATE TABLE retired_rows(id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO retired_rows(value) VALUES ('recoverable')")
    conn.commit()
    conn.close()
    return path


def test_audit_proves_cataloged_rows_are_readable(tmp_path: Path) -> None:
    receipt = audit(
        _backup(tmp_path / "backup.db"),
        _catalog(tmp_path / "catalog.json", ["retired_rows"]),
    )

    assert receipt.verified
    assert receipt.source_revision == "0272_archive_generation_catalog"
    assert receipt.targets[0].row_count == 1
    assert len(receipt.source_sha256) == 64


def test_audit_fails_closed_when_cataloged_table_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing cataloged schema targets"):
        audit(
            _backup(tmp_path / "backup.db"),
            _catalog(tmp_path / "catalog.json", ["missing_rows"]),
        )


def test_audit_accepts_only_explicitly_evidenced_absent_target(tmp_path: Path) -> None:
    receipt = audit(
        _backup(tmp_path / "backup.db"),
        _catalog(
            tmp_path / "catalog.json",
            ["migration_scratch"],
            exemptions={"migration_scratch": "temporary rebuild name; never persisted"},
        ),
    )

    assert receipt.verified
    assert not receipt.targets[0].present
    assert receipt.targets[0].row_count == 0
    assert receipt.targets[0].exemption_reason
