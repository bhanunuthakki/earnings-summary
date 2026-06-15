"""Round-trip test for ``0111_fact_overrides`` (provenance-override P1).

Creates the ``fact_overrides`` table consulted at read/resolve time so a company-
published figure supersedes FMP. Proves the table + both indexes land, the CHECK
constraints bind (fact_kind / action / status), the partial-unique index enforces a
single ACTIVE override per logical key while retired history coexists, and the
downgrade reverses cleanly. Mirrors the repo's alembic-test pattern: stamp the prior
head against an empty DB, run the one migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0110_pass_decision_source"
HEAD = "0111_fact_overrides"

_BASE_COLS = {
    "id",
    "user_id",
    "ticker",
    "period_end",
    "fiscal_period_type",
    "fact_kind",
    "fact_key",
    "action",
    "value",
    "unit",
    "value_json",
    "source_doc_type",
    "source_accession",
    "source_exhibit",
    "source_url",
    "source_excerpt",
    "source_doc_id",
    "status",
    "confidence",
    "rationale",
    "created_by",
    "created_at",
    "retired_at",
}

# A minimal valid active-override row factory (positional values omitted via defaults).
_INSERT = (
    "INSERT INTO fact_overrides "
    "(user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key, action, "
    " source_doc_type, status, created_by, created_at) "
    "VALUES ('bhanu', ?, '2025-12-31', 'Q4', ?, ?, ?, 'sec_8k', ?, 'test', '2026-06-14')"
)


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed(db_path: Path) -> Config:
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    return cfg


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(fact_overrides)").fetchall()}
    finally:
        conn.close()


def _index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA index_list(fact_overrides)").fetchall()}
    finally:
        conn.close()


def test_upgrade_creates_table_and_indexes(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = _seed(db)
    assert not _columns(db)  # table absent pre-migration
    command.upgrade(cfg, HEAD)
    assert _columns(db) >= _BASE_COLS
    idx = _index_names(db)
    assert "uq_fact_overrides_active" in idx
    assert "ix_fact_overrides_lookup" in idx


def test_check_constraints_bind(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        # Valid row passes.
        conn.execute(_INSERT, ("GOOG", "segment", "product", "replace", "active"))
        # Bad fact_kind / action / status each violate a CHECK.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("GOOG", "bogus_kind", "product", "replace", "active"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("META", "segment", "product", "bogus_action", "active"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("AMZN", "segment", "product", "replace", "bogus_status"))
        conn.commit()
    finally:
        conn.close()


def test_partial_unique_single_active(tmp_path: Path) -> None:
    db = tmp_path / "u.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_INSERT, ("GOOG", "segment", "product", "replace", "active"))
        # A second ACTIVE override for the same logical key is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("GOOG", "segment", "product", "replace", "active"))
        # A RETIRED row for the same key coexists (partial index covers active only).
        conn.execute(_INSERT, ("GOOG", "segment", "product", "replace", "retired"))
        conn.execute(_INSERT, ("GOOG", "segment", "product", "replace", "retired"))
        conn.commit()
    finally:
        conn.close()


def test_downgrade_drops_table(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    cfg = _seed(db)
    command.upgrade(cfg, HEAD)
    assert _columns(db) >= _BASE_COLS
    command.downgrade(cfg, PRIOR_HEAD)
    assert not _columns(db)
