"""Round-trip test for ``0110_pass_decision_source`` (Close-the-Loops L11 PR1).

Adds ``source_dismissal_id`` + ``source_prose`` to ``decisions``, widens the
``ck_decisions_source_present`` CHECK to admit a source-less avoid, and adds the
dismissal partial-unique index. Mirrors the repo's alembic-test pattern: hand-
create a faithful pre-0110 ``decisions`` table (with the 0086 CHECK and the
0086 partial-unique on source_memo), stamp the prior head, run the one
migration. Proves the columns + index land, the CHECK widens (avoid passes,
non-avoid still binds), the existing partial-unique survives, and downgrade
reverses cleanly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0109_qualitative_conditions"
HEAD = "0110_pass_decision_source"
_NEW_COLS = {"source_dismissal_id", "source_prose"}

# Faithful pre-0110 decisions table: the 0086 source CHECK + the 0086 partial
# unique on source_memo_id, so we prove the batch recreate preserves them.
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present
        CHECK (source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_decisions_source_memo
    ON decisions(source_memo_id) WHERE source_memo_id IS NOT NULL;
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    finally:
        conn.close()


def _index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA index_list(decisions)").fetchall()}
    finally:
        conn.close()


def _seed(db_path: Path) -> Config:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    return cfg


def test_upgrade_adds_columns_and_index(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = _seed(db)
    assert not (_NEW_COLS & _columns(db))
    command.upgrade(cfg, HEAD)
    assert _columns(db) >= _NEW_COLS
    idx = _index_names(db)
    assert "uq_decisions_source_dismissal" in idx
    assert "uq_decisions_source_memo" in idx  # the 0086 partial-unique survived


def test_upgrade_widens_check(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        # A source-less avoid is now allowed.
        conn.execute(
            "INSERT INTO decisions(ticker, recommendation_kind, made_at, created_at) "
            "VALUES ('NVDA', 'avoid', '2026-06-14', '2026-06-14')"
        )
        # A source-less non-avoid still violates the CHECK.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO decisions(ticker, recommendation_kind, made_at, created_at) "
                "VALUES ('NVDA', 'add', '2026-06-14', '2026-06-14')"
            )
        # The dismissal partial-unique binds: a duplicate candidate id is rejected.
        conn.execute(
            "INSERT INTO decisions(ticker, recommendation_kind, source_dismissal_id, "
            "made_at, created_at) VALUES ('A', 'avoid', 3, '2026-06-14', '2026-06-14')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO decisions(ticker, recommendation_kind, source_dismissal_id, "
                "made_at, created_at) VALUES ('B', 'avoid', 3, '2026-06-14', '2026-06-14')"
            )
        conn.commit()
    finally:
        conn.close()


def test_downgrade_drops_columns(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    assert _columns(db) >= _NEW_COLS
    command.downgrade(cfg, PRIOR_HEAD)
    assert not (_NEW_COLS & _columns(db))
    assert "uq_decisions_source_dismissal" not in _index_names(db)
