"""The tenant identifier must have ONE type and ONE key space across every store.

This is the guard for the identity reconciliation (alembic 0073). Before it, the
operator was an INTEGER ``1`` in ``tracked_companies`` and a TEXT ``'bhanu'`` in
the Personal-CIO substrate — two incompatible namespaces. After it there is a
single canonical tenant id, TEXT, FK-anchored on ``tenants``.

Two layers are asserted:

  * **Python type space** — every store's ``user_id`` (the four substrate row
    dataclasses, the ``Company`` model, and the ``db`` / ``queries`` function
    defaults) is ``str`` and defaults to ``identity.DEFAULT_USER_ID``. A
    regression that re-types any one of them back to ``int`` fails here.

  * **DB key space** — on a fully-migrated schema every ``user_id`` column is
    TEXT and a FK to ``tenants``, and the SAME canonical key resolves in both
    the holdings table and a substrate table (the join that was impossible
    before).
"""

from __future__ import annotations

import inspect
import shutil
import sqlite3
import sys
import typing
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db as dbmod  # noqa: E402
import identity  # noqa: E402
from alerts.store import AlertRow  # noqa: E402
from models.companies import Company, ListType  # noqa: E402
from pipeline import queries  # noqa: E402
from user_state.ledger import ThesisLedgerEntryRow  # noqa: E402
from user_state.registry import UserKpiRegistryRow  # noqa: E402
from user_state.sizing import PositionSizingIntentRow  # noqa: E402

# Every row-type that carries the tenant identifier.
_SUBSTRATE_ROW_TYPES = (
    AlertRow,
    UserKpiRegistryRow,
    PositionSizingIntentRow,
    ThesisLedgerEntryRow,
)

# Every db/queries function whose user_id default must be the canonical id.
_USER_SCOPED_FUNCS = (
    dbmod.track_company,
    dbmod.remove_company,
    dbmod.archive_company,
    dbmod.reactivate_company,
    dbmod.get_tracked_companies,
    dbmod.refresh_all_fmp_dates,
    queries.tracked_companies_for_user,
)

USER_SCOPED_TABLES: tuple[str, ...] = (
    "tracked_companies",
    "alerts",
    "user_kpi_registry",
    "position_sizing_intent",
    "thesis_ledger_entries",
)


# ---------------------------------------------------------------------------
# Python type space
# ---------------------------------------------------------------------------


def test_default_user_id_is_a_string() -> None:
    assert isinstance(identity.DEFAULT_USER_ID, str)


def test_every_substrate_row_types_user_id_is_str() -> None:
    for row_type in _SUBSTRATE_ROW_TYPES:
        hints = typing.get_type_hints(row_type)
        assert hints["user_id"] is str, f"{row_type.__name__}.user_id is {hints['user_id']!r}"


def test_company_model_user_id_is_str() -> None:
    """The Pydantic model must match the (now TEXT) column — and Pydantic v2
    rejects an int for a str field, so a stale int annotation would silently
    break reads of migrated rows."""
    assert Company.model_fields["user_id"].annotation is str


def test_company_constructs_from_string_user_id() -> None:
    company = Company(id=1, user_id="bhanu", ticker="NU", name="Nu", list_type=ListType.PORTFOLIO)
    assert company.user_id == "bhanu"


def test_user_scoped_function_defaults_are_canonical_str() -> None:
    """Every user-scoped entry point defaults user_id to the canonical str, so a
    caller that omits it lands in the single tenant namespace — never int 1."""
    for func in _USER_SCOPED_FUNCS:
        hints = typing.get_type_hints(func)
        assert hints["user_id"] is str, f"{func.__name__} user_id annotated {hints['user_id']!r}"
        default = inspect.signature(func).parameters["user_id"].default
        assert default == identity.DEFAULT_USER_ID, (
            f"{func.__name__} user_id default {default!r} != {identity.DEFAULT_USER_ID!r}"
        )
        assert isinstance(default, str)


# ---------------------------------------------------------------------------
# DB key space (fully-migrated schema)
# ---------------------------------------------------------------------------


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One fully-migrated DB (init_db + alembic head), shared across the module."""
    db = tmp_path_factory.mktemp("tenant_consistency_tmpl") / "head.db"
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    return db


@pytest.fixture
def head_db(head_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "head.db"
    shutil.copy(head_template, db)
    return db


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _user_id_affinity(conn: sqlite3.Connection, table: str) -> str:
    for _cid, name, decl_type, *_ in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if name == "user_id":
            return str(decl_type).upper()
    raise AssertionError(f"{table}.user_id not found")


def test_all_user_id_columns_are_text(head_db: Path) -> None:
    conn = _connect(head_db)
    try:
        for table in USER_SCOPED_TABLES:
            assert _user_id_affinity(conn, table) == "TEXT", f"{table}.user_id not TEXT"
    finally:
        conn.close()


def test_all_user_id_columns_reference_tenants(head_db: Path) -> None:
    conn = _connect(head_db)
    try:
        for table in USER_SCOPED_TABLES:
            targets = {str(r[2]) for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
            assert "tenants" in targets, f"{table}.user_id is not a FK to tenants"
    finally:
        conn.close()


def test_same_canonical_key_joins_holdings_and_substrate(head_db: Path) -> None:
    """The reconciliation's whole point: one key resolves in both namespaces, so
    'this user's alerts for this user's holdings' is finally a real join."""
    canonical = identity.DEFAULT_USER_ID
    conn = _connect(head_db)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) VALUES (?, ?, ?, ?)",
            (canonical, "NU", "Nu Holdings", "portfolio"),
        )
        conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
            "signature_sha) VALUES (?, 'NU', 'kpi_inflection', '2026-06-09', '{}', 'sig')",
            (canonical,),
        )
        conn.commit()
        joined = conn.execute(
            "SELECT a.ticker FROM alerts a "
            "JOIN tracked_companies t ON t.user_id = a.user_id AND t.ticker = a.ticker "
            "JOIN tenants te ON te.id = a.user_id "
            "WHERE a.user_id = ?",
            (canonical,),
        ).fetchall()
        assert [r[0] for r in joined] == ["NU"]
    finally:
        conn.close()


def test_unknown_tenant_rejected_in_both_namespaces(head_db: Path) -> None:
    conn = _connect(head_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
                "VALUES ('ghost', 'ZZZ', 'Z', 'none')"
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, evidence_json, "
                "signature_sha) VALUES ('ghost', 'NU', 'kpi_inflection', '2026-06-09', '{}', 's')"
            )
            conn.commit()
    finally:
        conn.close()
