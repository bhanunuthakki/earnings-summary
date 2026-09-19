"""Explicit SQLite date bindings compatible with existing stored text.

Keep the historical space separator, precision, and UTC offsets. These are
write adapters only: callers still choose their existing read/conversion policy.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime


def adapt_datetime(value: datetime) -> str:
    return value.isoformat(" ")


def register_datetime_adapters() -> None:
    """Replace deprecated stdlib defaults without changing serialized values."""
    sqlite3.register_adapter(date, date.isoformat)
    sqlite3.register_adapter(datetime, adapt_datetime)
