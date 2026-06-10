"""tenant_identity — one canonical tenant identity across every user-scoped table.

Why this exists
---------------
The same single operator was represented two incompatible ways:

  * ``tracked_companies.user_id`` — INTEGER, default ``1`` (created by
    ``src/db.py:init_db`` and last recreated by 0028).
  * the Personal-CIO substrate (``alerts`` 0063, ``user_kpi_registry`` 0060,
    ``position_sizing_intent`` 0061, ``thesis_ledger_entries`` 0062) —
    ``user_id`` TEXT, default ``'bhanu'``.

There was no table defining a canonical key and no FK joining the two
namespaces, so "this user's alerts for this user's holdings" was impossible
without an ad-hoc int↔str translation.

This migration is the foundational identity step of the multi-tenancy retrofit
(NOT row-level tenant scoping):

  1. Introduces ``tenants`` (TEXT primary key) — the single source of truth.
  2. Migrates both ``1`` and ``'bhanu'`` onto the canonical id ``'bhanu'``
     (which is exactly ``identity.DEFAULT_USER_ID``, already the str the
     substrate stores key on).
  3. Makes every ``user_id`` column a FK to ``tenants.id``, picking TEXT
     everywhere. The columns keep the name ``user_id`` (the per-row owning
     tenant — user≡tenant 1:1 in this single-tenant tool); renaming them is a
     separate, out-of-scope change.

Mechanics
---------
``tracked_companies`` is rebuilt by hand (like 0028): SQLAlchemy reflection does
NOT capture its *inline anonymous* ``list_type`` CHECK or the ``AUTOINCREMENT``
keyword, so a ``batch_alter_table`` recreate would silently drop both. The
manual recreate reproduces the exact 0072-era schema, flips ``user_id`` to TEXT
+ FK, and remaps ``1 → 'bhanu'``. The ``PRAGMA legacy_alter_table`` dance is the
same as 0028 — the 0026 brief_dirty triggers reference ``tracked_companies`` by
name and would trip trigger revalidation during the RENAME otherwise.

The four substrate tables carry only *named* CHECKs and a partial-unique index,
all of which reflection round-trips faithfully, so they take a far smaller
``batch_alter_table`` FK-add.

Every table op is guarded so the migration is a no-op on a DB that predates a
given table (test fixtures stamp partial schemas).

Revision ID: 0073_tenant_identity
Revises: 0072_kpi_reporting_cadence
Create Date: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0073_tenant_identity"
down_revision: str | Sequence[str] | None = "0072_kpi_reporting_cadence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical tenant id — kept in lockstep with identity.DEFAULT_USER_ID's fallback.
CANONICAL_TENANT = "bhanu"

# Substrate tables whose user_id (already TEXT 'bhanu') only needs a FK added.
_SUBSTRATE_TABLES: tuple[str, ...] = (
    "alerts",
    "user_kpi_registry",
    "position_sizing_intent",
    "thesis_ledger_entries",
)

# tracked_companies column set as of 0072 (verified against the live head schema).
# The migration is a point-in-time snapshot: future columns are added by later
# migrations and are irrelevant at this revision boundary.
_TRACKED_COLS: tuple[str, ...] = (
    "id",
    "user_id",
    "ticker",
    "name",
    "list_type",
    "added_at",
    "sec_validated",
    "ir_url",
    "model_url",
    "publishes_release",
    "publishes_slides",
    "publishes_transcript",
    "fmp_data_upto",
    "manual_data_quarters",
    "fmp_data_saved",
    "instrument_type",
    "filing_regime",
    "fiscal_year_end",
    "archived_at",
    "brief_dirty",
    "last_built_at",
    "last_brief_hash",
    "processing_tier",
)


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _tracked_table_body(user_id_def: str) -> str:
    """The full CREATE TABLE body for tracked_companies, parameterized only by the
    ``user_id`` column definition (the one thing that changes across up/down)."""
    return f"""
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        {user_id_def},
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        list_type TEXT NOT NULL CHECK(list_type IN (
            'portfolio','watchlist','evaluation','etf','index_member','none'
        )),
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sec_validated BOOLEAN DEFAULT 0,
        ir_url TEXT DEFAULT NULL,
        model_url TEXT DEFAULT NULL,
        publishes_release BOOLEAN DEFAULT 0,
        publishes_slides BOOLEAN DEFAULT 0,
        publishes_transcript BOOLEAN DEFAULT 0,
        fmp_data_upto TEXT DEFAULT NULL,
        manual_data_quarters TEXT DEFAULT '[]',
        fmp_data_saved BOOLEAN DEFAULT 0,
        instrument_type VARCHAR,
        filing_regime VARCHAR,
        fiscal_year_end VARCHAR(5),
        archived_at TIMESTAMP,
        brief_dirty BOOLEAN DEFAULT 0 NOT NULL,
        last_built_at DATETIME,
        last_brief_hash VARCHAR(64),
        processing_tier VARCHAR(8) DEFAULT 'P3' NOT NULL,
        UNIQUE(user_id, ticker)
    """


def _recreate_tracked(user_id_def: str, user_id_select: str) -> None:
    """Rebuild tracked_companies with a new user_id column definition, remapping
    the value via ``user_id_select``. Mirrors 0028's PRAGMA-guarded rename so the
    0026 brief_dirty triggers survive, and recreates all four 0072-era indexes."""
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA legacy_alter_table = ON"))
    try:
        conn.execute(sa.text(f"CREATE TABLE tracked_companies_new ({_tracked_table_body(user_id_def)})"))
        insert_cols = ", ".join(_TRACKED_COLS)
        select_cols = ", ".join(
            user_id_select if c == "user_id" else c for c in _TRACKED_COLS
        )
        conn.execute(
            sa.text(
                f"INSERT INTO tracked_companies_new ({insert_cols}) "
                f"SELECT {select_cols} FROM tracked_companies"
            )
        )
        conn.execute(sa.text("DROP TABLE tracked_companies"))
        conn.execute(sa.text("ALTER TABLE tracked_companies_new RENAME TO tracked_companies"))
    finally:
        conn.execute(sa.text("PRAGMA legacy_alter_table = OFF"))
    op.create_index(
        "ix_tracked_companies_active",
        "tracked_companies",
        ["user_id", "list_type"],
        sqlite_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        "ix_tracked_companies_brief_dirty",
        "tracked_companies",
        ["brief_dirty"],
        sqlite_where=sa.text("brief_dirty = 1"),
    )
    op.create_index(
        "ix_tracked_companies_processing_tier",
        "tracked_companies",
        ["processing_tier"],
        sqlite_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        "idx_tracked_processing_tier",
        "tracked_companies",
        ["processing_tier", "last_built_at"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat()

    # 1. The canonical-key table.
    if not _has_table(insp, "tenants"):
        op.create_table(
            "tenants",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )

    # 2. Seed the canonical tenant + (defensively) every distinct legacy id that
    #    already exists, so no row is orphaned when the FKs go on. Substrate ids
    #    copy as-is; tracked_companies' integer 1 maps onto the canonical id.
    op.execute(
        f"INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('{CANONICAL_TENANT}', '{now}')"
    )
    for table in _SUBSTRATE_TABLES:
        if _has_table(insp, table):
            op.execute(
                f"INSERT OR IGNORE INTO tenants (id, created_at) "
                f"SELECT DISTINCT user_id, '{now}' FROM {table}"
            )
    if _has_table(insp, "tracked_companies"):
        op.execute(
            f"INSERT OR IGNORE INTO tenants (id, created_at) "
            f"SELECT DISTINCT CASE WHEN user_id = 1 THEN '{CANONICAL_TENANT}' "
            f"ELSE CAST(user_id AS TEXT) END, '{now}' FROM tracked_companies"
        )

    # 3. tracked_companies: INTEGER → TEXT + FK, remapping 1 → 'bhanu'.
    if _has_table(insp, "tracked_companies"):
        _recreate_tracked(
            user_id_def=f"user_id TEXT DEFAULT '{CANONICAL_TENANT}' REFERENCES tenants(id)",
            user_id_select=(
                f"CASE WHEN user_id = 1 THEN '{CANONICAL_TENANT}' "
                "ELSE CAST(user_id AS TEXT) END"
            ),
        )

    # 4. Substrate tables: add the FK (named CHECKs + partial-unique index
    #    round-trip faithfully through batch reflection).
    for table in _SUBSTRATE_TABLES:
        if _has_table(insp, table):
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(
                    f"fk_{table}_user_id_tenants", "tenants", ["user_id"], ["id"]
                )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Fail fast: the pre-0073 INTEGER default-1 space cannot represent more than
    # the canonical tenant. Refuse rather than silently collapse distinct tenants
    # onto id 1 (which would also violate UNIQUE(user_id, ticker)).
    if _has_table(insp, "tracked_companies"):
        extra = (
            bind.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM tracked_companies WHERE user_id <> '{CANONICAL_TENANT}'"
                )
            ).scalar()
            or 0
        )
        if extra:
            raise RuntimeError(
                f"Cannot downgrade 0073: {extra} tracked_companies row(s) belong to a "
                "non-canonical tenant; the pre-0073 INTEGER user_id space cannot represent "
                "multiple tenants. Reassign or remove them before downgrading."
            )

    # 1. Drop the substrate FKs (recreate each table without it).
    for table in _SUBSTRATE_TABLES:
        if _has_table(insp, table):
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(f"fk_{table}_user_id_tenants", type_="foreignkey")

    # 2. tracked_companies: TEXT + FK → INTEGER, remapping 'bhanu' → 1.
    if _has_table(insp, "tracked_companies"):
        _recreate_tracked(
            user_id_def="user_id INTEGER DEFAULT 1",
            user_id_select=(
                f"CASE WHEN user_id = '{CANONICAL_TENANT}' THEN 1 "
                "ELSE CAST(user_id AS INTEGER) END"
            ),
        )

    # 3. Drop the canonical-key table.
    if _has_table(insp, "tenants"):
        op.drop_table("tenants")
