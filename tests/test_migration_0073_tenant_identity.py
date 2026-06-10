"""Round-trip + enforcement tests for ``0073_tenant_identity``.

This migration establishes ONE canonical tenant identity. Before it, the same
single human is represented two incompatible ways:

  * ``tracked_companies.user_id`` is INTEGER, default ``1``.
  * the Personal-CIO substrate tables (``alerts`` / ``user_kpi_registry`` /
    ``position_sizing_intent`` / ``thesis_ledger_entries``) carry
    ``user_id`` TEXT, default ``'bhanu'``.

0073 introduces a ``tenants`` table (TEXT PK), migrates both ``1`` and
``'bhanu'`` onto the canonical id ``'bhanu'``, and makes every ``user_id``
column a FK to ``tenants.id`` — picking TEXT everywhere.

The pre-0073 schema is built with the real chain (``init_db()`` + ``alembic
upgrade 0072``) so the test exercises the actual production tables — the long
``tracked_companies`` column set, its CHECK / partial indexes, and the substrate
CHECKs / partial-unique index — rather than a hand-rolled approximation that
could drift.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRIOR_HEAD = "0072_kpi_reporting_cadence"
NEW_HEAD = "0073_tenant_identity"

# Every table whose user_id is reconciled onto tenants.id.
USER_SCOPED_TABLES: tuple[str, ...] = (
    "tracked_companies",
    "alerts",
    "user_kpi_registry",
    "position_sizing_intent",
    "thesis_ledger_entries",
)


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    assert row is not None, f"{table} missing"
    return str(row[0])


def _user_id_affinity(conn: sqlite3.Connection, table: str) -> str:
    for _cid, name, decl_type, *_ in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if name == "user_id":
            return str(decl_type).upper()
    raise AssertionError(f"{table}.user_id not found")


def _fk_targets(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[2]) for r in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}


def _index_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return None if row is None else (None if row[0] is None else str(row[0]))


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One DB built to the real pre-0073 head (0072) via init_db + alembic,
    shared (read-only) across the module.

    init_db() owns ``tracked_companies`` (it predates alembic); the migration
    chain then grows it to its full column set and creates the substrate
    tables — exactly the production shape 0073 has to migrate. Building the full
    chain once and copying the file per test keeps the suite fast.
    """
    db = tmp_path_factory.mktemp("tenant_identity_tmpl") / "at_0072.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    """A fresh, writable copy of the 0072 template for one test."""
    db = tmp_path / "tenant_identity.db"
    shutil.copy(prior_template, db)
    return db


def _seed_legacy_rows(db_path: Path) -> None:
    """Seed the two legacy namespaces as they exist pre-0073: tracked_companies
    keyed by integer 1, the substrate keyed by 'bhanu'."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES (1, 'NU', 'Nu Holdings', 'portfolio')"
        )
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES (1, 'MELI', 'MercadoLibre', 'watchlist')"
        )
        conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
            "signature_sha) VALUES ('bhanu', 'NU', 'kpi_inflection', '2026-06-09T00:00:00', "
            "'{}', 'sig-legacy')"
        )
        conn.execute(
            "INSERT INTO user_kpi_registry (user_id, ticker, kpi_name, created_at, updated_at) "
            "VALUES ('bhanu', 'NU', 'monthly_active_customers', '2026-06-09', '2026-06-09')"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tenants table + value migration
# ---------------------------------------------------------------------------


def test_upgrade_creates_tenants_with_canonical_bhanu(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        assert "tenants" in {
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        ids = {str(r[0]) for r in conn.execute("SELECT id FROM tenants")}
        assert "bhanu" in ids
    finally:
        conn.close()


def test_tracked_companies_user_id_migrated_to_text_bhanu(db_at_prior: Path) -> None:
    """The integer-1 rows are rewritten onto the canonical TEXT id 'bhanu'."""
    _seed_legacy_rows(db_at_prior)
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        rows = conn.execute(
            "SELECT user_id, typeof(user_id) FROM tracked_companies ORDER BY ticker"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [("bhanu", "text"), ("bhanu", "text")]
        assert _user_id_affinity(conn, "tracked_companies") == "TEXT"
    finally:
        conn.close()


def test_substrate_user_id_unchanged_value_but_now_text(db_at_prior: Path) -> None:
    _seed_legacy_rows(db_at_prior)
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        for table in ("alerts", "user_kpi_registry"):
            assert _user_id_affinity(conn, table) == "TEXT"
            val = conn.execute(f"SELECT user_id FROM {table} LIMIT 1").fetchone()
            assert val is not None and val[0] == "bhanu"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FK enforcement — the unified key space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", USER_SCOPED_TABLES)
def test_every_user_scoped_table_references_tenants(db_at_prior: Path, table: str) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        assert "tenants" in _fk_targets(conn, table), f"{table}.user_id is not a FK to tenants"
    finally:
        conn.close()


def test_tracked_companies_fk_rejects_unknown_tenant(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
                "VALUES ('ghost', 'ZZZ', 'Z', 'none')"
            )
            conn.commit()
    finally:
        conn.close()


def test_alerts_fk_rejects_unknown_tenant(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
                "signature_sha) VALUES ('ghost', 'NU', 'kpi_inflection', '2026-06-09T00:00:00', "
                "'{}', 'sig-x')"
            )
            conn.commit()
    finally:
        conn.close()


def test_canonical_tenant_accepts_writes_in_both_namespaces(db_at_prior: Path) -> None:
    """The point of the reconciliation: the SAME 'bhanu' key now FK-resolves in
    both the holdings namespace and the substrate namespace."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES ('bhanu', 'BN', 'Brookfield', 'portfolio')"
        )
        conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
            "signature_sha) VALUES ('bhanu', 'BN', 'earnings_tone', '2026-06-09T00:00:00', "
            "'{}', 'sig-bn')"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pre-existing constraints must survive the table recreations
# ---------------------------------------------------------------------------


def test_tracked_companies_list_type_check_preserved(db_at_prior: Path) -> None:
    """The recreate must not drop tracked_companies' inline list_type CHECK."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
                "VALUES ('bhanu', 'ZZZ', 'Z', 'bogus_list_type')"
            )
            conn.commit()
    finally:
        conn.close()


def test_tracked_companies_indexes_and_unique_preserved(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        active = _index_sql(conn, "ix_tracked_companies_active")
        assert active is not None and "archived_at IS NULL" in active
        brief = _index_sql(conn, "ix_tracked_companies_brief_dirty")
        assert brief is not None and "brief_dirty = 1" in brief
        assert _index_sql(conn, "ix_tracked_companies_processing_tier") is not None
        assert _index_sql(conn, "idx_tracked_processing_tier") is not None
        # UNIQUE(user_id, ticker) still rejects a duplicate
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES ('bhanu', 'DUP', 'D', 'none')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
                "VALUES ('bhanu', 'DUP', 'D2', 'none')"
            )
            conn.commit()
    finally:
        conn.close()


def test_alerts_checks_and_partial_unique_preserved(db_at_prior: Path) -> None:
    """The batch FK add must preserve alerts' named CHECKs and the partial-unique
    active-signature dedup index."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        # status CHECK
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, status, "
                "evidence_json, signature_sha) VALUES ('bhanu', 'NU', 'kpi_inflection', "
                "'2026-06-09', 'BOGUS', '{}', 's1')"
            )
            conn.commit()
        conn.rollback()
        # partial-unique on (user_id, signature_sha) WHERE status <> 'expired'
        partial = _index_sql(conn, "uq_alerts_active_signature")
        assert partial is not None and "status <> 'expired'" in partial
        conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
            "signature_sha) VALUES ('bhanu', 'NU', 'kpi_inflection', '2026-06-09', '{}', 'dup')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
                "signature_sha) VALUES ('bhanu', 'NU', 'earnings_tone', '2026-06-09', '{}', 'dup')"
            )
            conn.commit()
    finally:
        conn.close()


def test_queued_actions_fk_to_alerts_survives_alerts_recreate(db_at_prior: Path) -> None:
    """Recreating alerts (to add its tenant FK) must not break the
    queued_actions → alerts FK."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    conn = _connect(db_at_prior)
    try:
        cur = conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
            "signature_sha) VALUES ('bhanu', 'NU', 'kpi_inflection', '2026-06-09', '{}', 'qa')"
        )
        alert_id = cur.lastrowid
        conn.execute(
            "INSERT INTO queued_actions (alert_id, action_kind, payload_json, status, created_at) "
            "VALUES (?, 'thesis_update', '{}', 'pending', '2026-06-09')",
            (alert_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO queued_actions (alert_id, action_kind, payload_json, status, "
                "created_at) VALUES (999999, 'thesis_update', '{}', 'pending', '2026-06-09')"
            )
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------


def test_downgrade_restores_integer_namespace(db_at_prior: Path) -> None:
    _seed_legacy_rows(db_at_prior)
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = _connect(db_at_prior)
    try:
        # tenants gone
        assert "tenants" not in {
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # tracked_companies back to INTEGER, 'bhanu' -> 1
        assert _user_id_affinity(conn, "tracked_companies") == "INTEGER"
        rows = conn.execute(
            "SELECT user_id, typeof(user_id) FROM tracked_companies ORDER BY ticker"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(1, "integer"), (1, "integer")]
        # FK to tenants gone from every table
        for table in USER_SCOPED_TABLES:
            assert "tenants" not in _fk_targets(conn, table)
        # substrate value preserved
        assert conn.execute("SELECT user_id FROM alerts LIMIT 1").fetchone()[0] == "bhanu"
    finally:
        conn.close()


def test_downgrade_refuses_when_multiple_tenants_exist(db_at_prior: Path) -> None:
    """A true multi-tenant state cannot be represented by the old single-integer
    default; downgrade must refuse rather than silently collapse tenants."""
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    try:
        conn.execute("INSERT INTO tenants (id, created_at) VALUES ('analyst-2', '2026-06-09')")
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES ('analyst-2', 'AAPL', 'Apple', 'portfolio')"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(Exception, match=r"(?i)tenant"):
        command.downgrade(cfg, PRIOR_HEAD)


def test_round_trip_idempotent(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    first = _schema_snapshot(db_at_prior)
    command.downgrade(cfg, PRIOR_HEAD)
    command.upgrade(cfg, NEW_HEAD)
    second = _schema_snapshot(db_at_prior)
    assert first == second


def _schema_snapshot(db_path: Path) -> dict[str, list[str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        out: dict[str, list[str]] = {}
        for table in (*USER_SCOPED_TABLES, "tenants"):
            cols = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            out[table] = cols
        return out
    finally:
        conn.close()
