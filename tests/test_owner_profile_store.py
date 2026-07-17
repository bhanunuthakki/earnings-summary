"""owner_profile_facts store (alembic 0159 + src/owner_profile/).

- Value-shape validators: closed tax-bucket vocabulary, bounds.
- Store: append -> supersede chain (is_latest flips, superseded_at stamped,
  superseded_by_id back-links), idempotent re-append (unchanged value is a
  no-op; changed value supersedes regardless of prior status), affirm/reject
  gating (only a 'proposed' latest row can be affirmed/rejected; re-tap is a
  no-op), list_facts filters, get_current_profile groups affirmed-only by
  category, undecodable-row loud skip, missing-table degrade.
- Migration 0159 round-trip incl. the one-latest-per-(user,category,key)
  partial index and the three CHECK constraints.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from owner_profile.models import (  # noqa: E402
    CashBufferMonths,
    HumanCapitalBucket,
    TaxBucketBalances,
)
from owner_profile.store import (  # noqa: E402
    affirm_fact,
    append_fact,
    get_current_profile,
    get_fact,
    list_facts,
    reject_fact,
)

_DDL = """
CREATE TABLE owner_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    category TEXT NOT NULL CHECK (category IN ('capacity','appetite','behavioral')),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (
        provenance IN ('wealthplan_import','cio_context_import','owner','derived')
    ),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','affirmed','rejected')),
    affirmed_at TEXT,
    review_horizon_days INTEGER,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);
CREATE UNIQUE INDEX ux_owner_profile_facts_latest
    ON owner_profile_facts(user_id, category, key) WHERE is_latest = 1;
"""


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(str(tmp_path / "p.db"))
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Value-shape validators
# ---------------------------------------------------------------------------


def test_tax_bucket_balances_closed_vocab() -> None:
    with pytest.raises(ValidationError):
        TaxBucketBalances(balances={"crypto": 100.0}, as_of="2026-06-21")  # type: ignore[arg-type]
    ok = TaxBucketBalances(balances={"pretax": 100.0, "cash": 10.0}, as_of="2026-06-21")  # type: ignore[arg-type]
    assert ok.balances["pretax"] == 100.0


def test_tax_bucket_balances_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        TaxBucketBalances(balances={"cash": -5.0}, as_of="2026-06-21")  # type: ignore[arg-type]


def test_cash_buffer_months_bounds() -> None:
    with pytest.raises(ValidationError):
        CashBufferMonths(months=-1.0)
    assert CashBufferMonths(months=6.0).months == 6.0


def test_human_capital_bucket_uppercases_members() -> None:
    b = HumanCapitalBucket(cap_pct=15.0, members=["meta", " googl "])
    assert b.members == ["META", "GOOGL"]


# ---------------------------------------------------------------------------
# Store: append / supersede
# ---------------------------------------------------------------------------


def test_append_supersede_chain(conn: sqlite3.Connection) -> None:
    id1 = append_fact(
        conn,
        category="capacity",
        key="cash_buffer_months",
        value={"months": 6.0},
        narrative="6 months of spend in cash",
        provenance="wealthplan_import",
    )
    id2 = append_fact(
        conn,
        category="capacity",
        key="cash_buffer_months",
        value={"months": 9.0},
        narrative="9 months of spend in cash",
        provenance="wealthplan_import",
    )
    assert id2 != id1
    facts = list_facts(conn, category="capacity")
    assert len(facts) == 1
    assert facts[0].id == id2
    assert facts[0].value == {"months": 9.0}

    old = get_fact(conn, id1)
    assert old is not None
    assert old.is_latest is False
    assert old.superseded_at is not None
    assert old.superseded_by_id == id2

    n = conn.execute("SELECT COUNT(*) FROM owner_profile_facts WHERE is_latest = 1").fetchone()[0]
    assert n == 1


def test_append_idempotent_on_unchanged_value(conn: sqlite3.Connection) -> None:
    id1 = append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "San Francisco"},
        narrative="Lives in San Francisco",
        provenance="wealthplan_import",
    )
    id2 = append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "San Francisco"},
        narrative="Lives in San Francisco",
        provenance="wealthplan_import",
    )
    assert id1 == id2
    n = conn.execute("SELECT COUNT(*) FROM owner_profile_facts").fetchone()[0]
    assert n == 1


def test_append_changed_value_supersedes_even_when_rejected(conn: sqlite3.Connection) -> None:
    id1 = append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "Seattle"},
        narrative="Lives in Seattle",
        provenance="wealthplan_import",
    )
    assert reject_fact(conn, id1) is True
    # Re-import of the SAME (rejected) value must stay quiet — no new row.
    same = append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "Seattle"},
        narrative="Lives in Seattle",
        provenance="wealthplan_import",
    )
    assert same == id1
    fact1 = get_fact(conn, id1)
    assert fact1 is not None and fact1.status == "rejected"

    # A CHANGED value resurfaces as a fresh 'proposed' row regardless.
    id2 = append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "San Francisco"},
        narrative="Moved to San Francisco",
        provenance="wealthplan_import",
    )
    assert id2 != id1
    fact2 = get_fact(conn, id2)
    assert fact2 is not None
    assert fact2.status == "proposed"
    assert fact2.is_latest is True


def test_append_rejects_bad_category_provenance_status(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="category"):
        append_fact(
            conn,
            category="bogus",
            key="x",
            value={},
            narrative="x",
            provenance="owner",
        )
    with pytest.raises(ValueError, match="provenance"):
        append_fact(
            conn,
            category="capacity",
            key="x",
            value={},
            narrative="x",
            provenance="bogus",
        )
    with pytest.raises(ValueError, match="status"):
        append_fact(
            conn,
            category="capacity",
            key="x",
            value={},
            narrative="x",
            provenance="owner",
            status="bogus",
        )


# ---------------------------------------------------------------------------
# Affirm / reject gating
# ---------------------------------------------------------------------------


def test_affirm_then_reaffirm_is_noop(conn: sqlite3.Connection) -> None:
    fid = append_fact(
        conn,
        category="capacity",
        key="equity_fraction",
        value={"equity_fraction": 0.9},
        narrative="90% equity",
        provenance="wealthplan_import",
    )
    row = affirm_fact(conn, fid)
    assert row is not None and row.status == "affirmed" and row.affirmed_at is not None
    # Re-affirming an already-affirmed row is a no-op (not 'proposed' anymore).
    assert affirm_fact(conn, fid) is None


def test_reject_then_reaffirm_is_noop(conn: sqlite3.Connection) -> None:
    fid = append_fact(
        conn,
        category="capacity",
        key="equity_fraction",
        value={"equity_fraction": 0.9},
        narrative="90% equity",
        provenance="wealthplan_import",
    )
    assert reject_fact(conn, fid) is True
    assert reject_fact(conn, fid) is False  # already rejected
    assert affirm_fact(conn, fid) is None  # can't affirm a rejected row


def test_owner_provenance_can_land_affirmed_directly(conn: sqlite3.Connection) -> None:
    fid = append_fact(
        conn,
        category="appetite",
        key="cash_floor_rule",
        value={"floor_pct": 5.0},
        narrative="Keep at least 5% in cash",
        provenance="owner",
        status="affirmed",
    )
    row = get_fact(conn, fid)
    assert row is not None
    assert row.status == "affirmed"
    assert row.affirmed_at is not None


# ---------------------------------------------------------------------------
# Reads: list_facts / get_current_profile / degrade
# ---------------------------------------------------------------------------


def test_get_current_profile_groups_affirmed_only(conn: sqlite3.Connection) -> None:
    a = append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "SF"},
        narrative="n",
        provenance="wealthplan_import",
    )
    affirm_fact(conn, a)
    append_fact(
        conn,
        category="capacity",
        key="cash_buffer_months",
        value={"months": 6.0},
        narrative="n",
        provenance="wealthplan_import",
    )  # left 'proposed' — must NOT appear
    b = append_fact(
        conn,
        category="appetite",
        key="cash_floor_rule",
        value={"floor_pct": 5.0},
        narrative="n",
        provenance="owner",
    )
    affirm_fact(conn, b)

    profile = get_current_profile(conn)
    assert {f.key for f in profile["capacity"]} == {"home_city"}
    assert {f.key for f in profile["appetite"]} == {"cash_floor_rule"}
    assert profile["behavioral"] == []


def test_undecodable_row_is_loud_skip(conn: sqlite3.Connection) -> None:
    append_fact(
        conn,
        category="capacity",
        key="home_city",
        value={"city": "SF"},
        narrative="n",
        provenance="wealthplan_import",
    )
    conn.execute("UPDATE owner_profile_facts SET value_json = 'not json'")
    assert list_facts(conn) == []


def test_missing_table_degrades(tmp_path: Path) -> None:
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        assert list_facts(bare) == []
    finally:
        bare.close()


# ---------------------------------------------------------------------------
# Migration 0159 round-trip
# ---------------------------------------------------------------------------


def test_migration_0159_roundtrip(tmp_path: Path) -> None:
    from alembic.config import Config

    from alembic import command

    db = tmp_path / "m.db"
    sqlite3.connect(str(db)).close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0152_v_thesis_status_stub_substring")
    command.upgrade(cfg, "0159_owner_profile_facts")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        id1 = append_fact(
            conn,
            category="capacity",
            key="home_city",
            value={"city": "SF"},
            narrative="n",
            provenance="wealthplan_import",
        )
        id2 = append_fact(
            conn,
            category="capacity",
            key="home_city",
            value={"city": "Seattle"},
            narrative="n2",
            provenance="wealthplan_import",
        )
        assert id2 > id1
        conn.commit()
        # CHECK constraints enforced by the migrated table.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO owner_profile_facts "
                "(user_id, category, key, value_json, narrative, provenance, created_at) "
                "VALUES ('bhanu', 'bogus', 'k', '{}', 'n', 'owner', '2026-07-17T00:00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO owner_profile_facts "
                "(user_id, category, key, value_json, narrative, provenance, created_at) "
                "VALUES ('bhanu', 'capacity', 'k', '{}', 'n', 'bogus', '2026-07-17T00:00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO owner_profile_facts "
                "(user_id, category, key, value_json, narrative, provenance, status, created_at) "
                "VALUES ('bhanu', 'capacity', 'k', '{}', 'n', 'owner', 'bogus', '2026-07-17T00:00:00')"
            )
        # Partial unique index: a second is_latest=1 row for the same
        # (user, category, key) is rejected at the SQL layer.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO owner_profile_facts "
                "(user_id, category, key, value_json, narrative, provenance, created_at, is_latest) "
                "VALUES ('bhanu', 'capacity', 'home_city', '{}', 'n', 'owner', "
                "'2026-07-17T00:00:00', 1)"
            )
    finally:
        conn.close()

    command.downgrade(cfg, "0152_v_thesis_status_stub_substring")
    conn = sqlite3.connect(str(db))
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "owner_profile_facts" not in tables
    finally:
        conn.close()
