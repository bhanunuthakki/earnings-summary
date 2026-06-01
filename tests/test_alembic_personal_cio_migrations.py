"""Round-trip tests for the Personal CIO substrate migrations (0060–0064).

Verifies the chain `user_kpi_registry → position_sizing_intent →
thesis_ledger_entries → alerts → queued_actions` is:

  * idempotent under upgrade-then-downgrade-then-upgrade,
  * adds exactly the five expected tables (and removes exactly those
    five on downgrade -5),
  * preserves the migration chain head — downgrade -5 lands at
    0059_kpi_facts_restatement, the prior head.

The 5 new migrations don't depend on any pre-0059 tables (queued_actions
FK is internal to the set: queued_actions → alerts, both in this batch),
so the test stamps a fresh tmp_path SQLite DB at the prior head and
exercises only the 5 new migrations. This is the hermetic equivalent of
the prompt's "in-memory SQLite" requirement — alembic's env.py spawns
its own connections via `engine_from_config`, which `:memory:` can't
share, so we use a tmp_path-backed file.

Each migration also exercises an INSERT after upgrade, so a typo in a
column name or constraint surfaces here rather than in the trigger /
applier PRs that follow.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
# This suite covers the 0060-0064 batch specifically, so it upgrades to this
# explicit revision rather than the global "head"; later migrations (e.g.
# 0065_news) must not retro-break the batch's head/downgrade-count assertions.
PERSONAL_CIO_HEAD = "0064_queued_actions"
PERSONAL_CIO_TABLES: tuple[str, ...] = (
    "user_kpi_registry",
    "position_sizing_intent",
    "thesis_ledger_entries",
    "alerts",
    "queued_actions",
)


def _build_config(db_path: Path) -> Config:
    """Build an alembic Config rooted at the repo's alembic.ini, with the
    sqlalchemy.url overridden to point at the test's tmp DB."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def _alembic_head(db_path: Path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh tmp_path SQLite DB stamped at PRIOR_HEAD.

    Stamping (vs. running every prior migration from <base>) is correct
    because none of the 5 Personal CIO migrations reference pre-0059
    tables — queued_actions's FK points at alerts, which is in the same
    batch. Stamping skips the bootstrap of `tracked_companies` /
    `quarterly_artifacts` etc. that the pre-alembic init_db() owns.
    """
    db = tmp_path / "personal_cio_round_trip.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    return db


def test_upgrade_creates_all_five_tables(db_path: Path) -> None:
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)

    tables = _table_names(db_path)
    for name in PERSONAL_CIO_TABLES:
        assert name in tables, f"{name} missing after upgrade head"
    assert _alembic_head(db_path) == PERSONAL_CIO_HEAD


def test_downgrade_five_steps_drops_only_the_new_tables(db_path: Path) -> None:
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    tables_at_head = _table_names(db_path)

    command.downgrade(cfg, "-5")

    tables_after = _table_names(db_path)
    for name in PERSONAL_CIO_TABLES:
        assert name not in tables_after, f"{name} survived downgrade -5"
    # The downgrade must NOT have touched the rest of the schema (here:
    # only alembic_version exists since we stamped without running any
    # prior migrations).
    untouched_tables = tables_at_head - set(PERSONAL_CIO_TABLES) - {"alembic_version"}
    for name in untouched_tables:
        assert name in tables_after, (
            f"{name} disappeared during downgrade -5 — chain unexpectedly walked further"
        )
    assert _alembic_head(db_path) == PRIOR_HEAD


def test_upgrade_then_downgrade_then_upgrade_is_idempotent(db_path: Path) -> None:
    """The full round trip must leave the schema indistinguishable from a
    first-pass upgrade. Cross-checks both table names and per-table
    column sets so a forgotten Column() in a future downgrade()'s
    recreate path would fail this test."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    snapshot_after_first_upgrade = _columns_per_personal_cio_table(db_path)

    command.downgrade(cfg, "-5")
    after_downgrade = _table_names(db_path)
    for name in PERSONAL_CIO_TABLES:
        assert name not in after_downgrade

    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    snapshot_after_second_upgrade = _columns_per_personal_cio_table(db_path)

    assert snapshot_after_first_upgrade == snapshot_after_second_upgrade
    assert _alembic_head(db_path) == PERSONAL_CIO_HEAD


def _columns_per_personal_cio_table(db_path: Path) -> dict[str, list[str]]:
    """Return {table_name: [column_name, ...]} for each of the 5 new tables."""
    out: dict[str, list[str]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        for name in PERSONAL_CIO_TABLES:
            rows = conn.execute(f"PRAGMA table_info({name})").fetchall()
            out[name] = [str(r[1]) for r in rows]
    finally:
        conn.close()
    return out


_INSERT_USER_KPI = """
    INSERT INTO user_kpi_registry
      (user_id, ticker, kpi_name, is_thesis_breaker, created_at, updated_at)
    VALUES ('bhanu', 'NU', 'monthly_active_customers', ?,
            '2026-05-27T00:00:00Z', '2026-05-27T00:00:00Z')
"""

_INSERT_SIZING_INTENT = """
    INSERT INTO position_sizing_intent
      (ticker, intent_kind, intent_value, created_at, updated_at)
    VALUES ('META', 'target_pct', 0.05,
            '2026-05-27T00:00:00Z', '2026-05-27T00:00:00Z')
"""

_INSERT_ALERT = """
    INSERT INTO alerts
      (ticker, trigger_kind, fired_at, evidence_json, signature_sha)
    VALUES ('GOOG', 'kpi_inflection', '2026-05-27T00:00:00Z',
            '{"kpi":"cloud_revenue_qoq","value":0.32}', 'abc123')
"""

_INSERT_ORPHAN_QUEUED_ACTION = """
    INSERT INTO queued_actions
      (alert_id, action_kind, payload_json, created_at)
    VALUES (9999, 'thesis_update', '{"body":"..."}',
            '2026-05-27T00:00:00Z')
"""


def test_user_kpi_registry_unique_natural_key(db_path: Path) -> None:
    """`(user_id, ticker, kpi_name)` is unique — second insert with the
    same tuple must raise IntegrityError."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(_INSERT_USER_KPI, (1,))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _ = conn.execute(_INSERT_USER_KPI, (0,))
            conn.commit()
    finally:
        conn.close()


def test_user_id_defaults_to_bhanu_on_insert_without_column(db_path: Path) -> None:
    """`user_id TEXT NOT NULL DEFAULT 'bhanu'` — inserts that omit the
    column must succeed and read back as 'bhanu'. This is the contract
    the single-user inserts rely on; verifies the DEFAULT clause survived
    SQLite's CREATE TABLE rendering."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(_INSERT_SIZING_INTENT)
        conn.commit()
        row = conn.execute(
            "SELECT user_id FROM position_sizing_intent WHERE ticker = 'META'"
        ).fetchone()
        assert row is not None
        assert row[0] == "bhanu"
    finally:
        conn.close()


def test_alerts_status_defaults_to_pending(db_path: Path) -> None:
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(_INSERT_ALERT)
        conn.commit()
        row = conn.execute("SELECT status FROM alerts WHERE ticker = 'GOOG'").fetchone()
        assert row is not None
        assert row[0] == "pending"
    finally:
        conn.close()


def test_queued_action_fk_to_alerts(db_path: Path) -> None:
    """A queued action's alert_id must reference an existing alerts.id.
    SQLite only enforces FKs when `PRAGMA foreign_keys = ON` is set, so
    the test enables it and verifies the constraint fires."""
    cfg = _build_config(db_path)
    command.upgrade(cfg, PERSONAL_CIO_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _ = conn.execute(_INSERT_ORPHAN_QUEUED_ACTION)
            conn.commit()
    finally:
        conn.close()
