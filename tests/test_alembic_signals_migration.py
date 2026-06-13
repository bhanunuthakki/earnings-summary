"""Round-trip + contract tests for the ``0095_signals`` migration.

Proves the table + its four indices are created, the news→signals backfill maps
yf_grades to the typed consensus_rating lane (and is non-destructive to `news`),
the CHECK constraints reject out-of-vocab values, the backfill is guarded when
`news` is absent, and downgrade lands cleanly back at the prior head.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from ._signals_fixtures import (
    PRIOR_HEAD,
    SIGNALS_HEAD,
    build_config,
    make_news_then_signals,
    signals_only,
)

_NEWS = [
    ("NU", "Nu update", "http://x/1", "2026-06-01 12:00:00", "s", "Reuters", "fmp_stock_news", "t"),
    ("META", "MS upgrades META", "http://x/2", "2026-06-11 09:00:00", None, "MS", "yf_grades", "t"),
]


def _index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signals'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_upgrade_creates_table_and_all_four_indices(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    signals_only(db)
    assert "signals" in _table_names(db)
    assert _index_names(db) >= {
        "ix_signals_ticker_type_date",
        "ix_signals_event_date",
        "ux_signals_news_id",
        "ux_signals_event",
    }


def test_backfill_mirrors_news_and_is_non_destructive(tmp_path: Path) -> None:
    db = tmp_path / "b.db"
    make_news_then_signals(db, _NEWS)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT ticker, signal_type, weight, news_id FROM signals ORDER BY news_id"
        ).fetchall()
        news_count = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    finally:
        conn.close()
    assert [(r[0], r[1]) for r in rows] == [
        ("NU", "general_news"),
        ("META", "consensus_rating"),
    ]
    # `news` is untouched — the recency query + NewsRow gate are preserved.
    assert news_count == len(_NEWS)


def test_backfill_guarded_when_news_absent(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    signals_only(db)  # no `news` table existed
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    finally:
        conn.close()


def test_check_constraints_reject_out_of_vocab(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    signals_only(db)
    conn = sqlite3.connect(str(db))
    try:
        with __import__("pytest").raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO signals (ticker, signal_type, title, published_at, weight, "
                "cadence, created_at) VALUES ('X', 'bogus', 't', '2026-01-01 00:00:00', 1.0, "
                "'event', '2026-01-01 00:00:00')"
            )
        with __import__("pytest").raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO signals (ticker, signal_type, title, published_at, weight, "
                "cadence, created_at) VALUES ('X', 'general_news', 't', '2026-01-01 00:00:00', "
                "1.0, 'weekly', '2026-01-01 00:00:00')"
            )
    finally:
        conn.close()


def test_downgrade_drops_signals(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    make_news_then_signals(db, _NEWS)
    assert "signals" in _table_names(db)
    cfg = build_config(db)
    command.downgrade(cfg, PRIOR_HEAD)
    assert "signals" not in _table_names(db)
    # re-upgrade is column-stable (idempotent create guard).
    command.upgrade(cfg, SIGNALS_HEAD)
    assert "signals" in _table_names(db)
