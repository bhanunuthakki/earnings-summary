"""S-1 registration hashes retained bytes, independently of parsing normalization."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.s1_financials import S1Datum
from execution import extract_s1_financials as cli
from models.facts import Currency, FiscalPeriodType, Unit


def test_s1_registration_hashes_exact_crlf_file_bytes(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "data/sec_text/FRVO_s1_2026.txt"
    source.parent.mkdir(parents=True)
    raw = b"Revenue\r\n2025\r\n100\r\n"
    source.write_bytes(raw)
    database = migrated_db(tmp_path / "runtime.db")

    def parse(text: str) -> list[S1Datum]:
        assert text == "Revenue\n2025\n100\n"
        return [
            S1Datum(
                "revenue",
                datetime(2025, 12, 31),
                FiscalPeriodType.FY,
                Decimal("100"),
                Unit.MILLIONS,
                Currency.USD,
            )
        ]

    def connect(_path: str) -> sqlite3.Connection:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        return connection

    def insert(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(cli, "parse_s1_text", parse)
    monkeypatch.setattr(cli, "open_db", connect)
    monkeypatch.setattr(cli, "insert_financial_facts", insert)
    monkeypatch.setattr(
        sys, "argv", ["extract_s1_financials.py", "--ticker", "FRVO", "--repo-root", str(tmp_path)]
    )
    assert cli.main() == 0
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT sha256,raw_bytes_size FROM documents").fetchone()
        assert row == (hashlib.sha256(raw).hexdigest(), len(raw))
    assert source.read_bytes() == raw
