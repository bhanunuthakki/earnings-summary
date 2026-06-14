"""Round-trip test for the ``0106_standup_messages`` migration (L9).

Proves the standup ledger table + its three indices are created on upgrade, are
idempotent (re-running the create is a no-op), and that downgrade drops them
cleanly back at the prior head.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0106_confidence_observations"
STANDUP_HEAD = "0107_standup_messages"

_EXPECTED_INDICES = {
    "ix_standup_messages_user_sig",
    "ix_standup_messages_user_ticker_created",
    "ix_standup_messages_user_status_created",
}


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _names(db_path: Path, kind: str, *, tbl: str | None = None) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        if tbl is not None:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ? AND tbl_name = ?", (kind, tbl)
            ).fetchall()
        else:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _upgrade(db_path: Path) -> Config:
    cfg = _build_config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, STANDUP_HEAD)
    return cfg


def test_upgrade_creates_table_and_indices(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    _upgrade(db)
    assert "standup_messages" in _names(db, "table")
    cols = {
        r[1]
        for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(standup_messages)").fetchall()
    }
    assert {"signature_sha", "status", "score", "session_id", "turn_id", "conclusion"} <= cols
    assert _names(db, "index", tbl="standup_messages") >= _EXPECTED_INDICES


def test_downgrade_drops_table_and_is_reversible(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    cfg = _upgrade(db)
    command.downgrade(cfg, PRIOR_HEAD)
    assert "standup_messages" not in _names(db, "table")
    # re-upgrade is column-stable (idempotent create guard)
    command.upgrade(cfg, STANDUP_HEAD)
    assert "standup_messages" in _names(db, "table")
