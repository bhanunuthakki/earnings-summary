"""Migration ownership for the cross-process LLM quota breaker."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm.transport import (
    USAGE_LIMIT,
    FailureInfo,
    clear_quota_block,
    quota_block_active,
    record_quota_exhausted,
)


def test_current_head_owns_quota_breaker_and_transport_never_emits_ddl(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "breaker.db", target="head")

    with sqlite3.connect(db_path) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        columns = {
            str(row[1]): (str(row[2]), int(row[3]), int(row[5]))
            for row in conn.execute("PRAGMA table_info(llm_circuit_breakers)")
        }

    assert revision == ("0004_add_llm_circuit_breakers",)
    assert columns == {
        "provider": ("TEXT", 0, 1),
        "blocked_until": ("TEXT", 1, 0),
        "reason": ("TEXT", 0, 0),
        "set_at": ("TEXT", 1, 0),
    }
    transport_source = Path(__file__).resolve().parents[1] / "src" / "llm" / "transport.py"
    assert "CREATE TABLE IF NOT EXISTS llm_circuit_breakers" not in transport_source.read_text(
        encoding="utf-8"
    )


def test_sqlite_quota_breaker_round_trip_uses_migrated_schema(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "breaker-round-trip.db", target="head")
    # FailureInfo retry timestamps are normalized to the transport's documented
    # naive-UTC contract (matching parsed CLI reset epochs).
    reset_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30)

    written = record_quota_exhausted(
        FailureInfo(USAGE_LIMIT, "subscription window exhausted", retry_after=reset_at),
        path=db_path,
    )

    assert written == reset_at
    assert quota_block_active(path=db_path) == reset_at
    clear_quota_block(path=db_path)
    assert quota_block_active(path=db_path) is None
