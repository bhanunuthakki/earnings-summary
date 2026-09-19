"""Managed and direct SQLite connections preserve the established wire format."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """
import warnings
warnings.simplefilter("error", DeprecationWarning)
{startup}
import sqlite3
from datetime import date, datetime, timedelta, timezone
values = [date(2026, 9, 19), datetime(2026, 9, 19, 1, 2, 3),
          datetime(2026, 9, 19, 1, 2, 3, 123456),
          datetime(2026, 9, 19, 1, 2, 3, 123456, timezone(timedelta(hours=-7))),
          datetime(2026, 9, 19, 1, 2, 3, tzinfo=timezone.utc)]
with {connection} as conn:
    conn.execute("CREATE TABLE timestamps (value TEXT)")
    conn.executemany("INSERT INTO timestamps VALUES (?)", [(value,) for value in values])
    import json
    print(json.dumps([row[0] for row in conn.execute("SELECT value FROM timestamps ORDER BY rowid")]))
"""


@pytest.mark.parametrize("managed", [False, True])
def test_sqlite_dates_preserve_bytes_without_deprecated_defaults(managed: bool) -> None:
    startup = (
        "from execution.sqlite_bootstrap import preload_sqlite; preload_sqlite()"
        if managed
        else "from sqlite_runtime import connect_sqlite, SQLiteConnectionRole"
    )
    connection = (
        "sqlite3.connect(':memory:')"
        if managed
        else "connect_sqlite(':memory:', role=SQLiteConnectionRole.SNAPSHOT_DESTINATION)"
    )
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT.format(startup=startup, connection=connection)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "2026-09-19",
        "2026-09-19 01:02:03",
        "2026-09-19 01:02:03.123456",
        "2026-09-19 01:02:03.123456-07:00",
        "2026-09-19 01:02:03+00:00",
    ]
