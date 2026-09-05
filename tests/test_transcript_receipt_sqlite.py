"""Tests for the dependency-free transcript receipt SQLite boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from transcripts import receipt_sqlite


def test_open_connection_uses_validator_registered_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection opened before acquisition import observes the later authority."""

    monkeypatch.setattr(receipt_sqlite, "_receipt_validator", None)
    connection = sqlite3.connect(":memory:")
    receipt_sqlite.register_transcript_receipt_sqlite_functions(
        connection,
        database_path=tmp_path / "data" / "portfolio.db",
    )
    values = ",".join("NULL" for _ in range(18))
    assert connection.execute(f"SELECT transcript_receipt_valid({values})").fetchone() == (0,)

    def validator(_project_root: Path, _values: tuple[object, ...]) -> int:
        return 1

    receipt_sqlite.register_transcript_receipt_validator(validator)
    assert connection.execute(f"SELECT transcript_receipt_valid({values})").fetchone() == (1,)
    connection.close()


def test_validator_authority_cannot_be_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first validator may own the process-wide SQLite authority."""

    monkeypatch.setattr(receipt_sqlite, "_receipt_validator", None)

    def first(_project_root: Path, _values: tuple[object, ...]) -> int:
        return 1

    def replacement(_project_root: Path, _values: tuple[object, ...]) -> int:
        return 0

    receipt_sqlite.register_transcript_receipt_validator(first)
    receipt_sqlite.register_transcript_receipt_validator(first)
    with pytest.raises(RuntimeError, match="authority is already registered"):
        receipt_sqlite.register_transcript_receipt_validator(replacement)
