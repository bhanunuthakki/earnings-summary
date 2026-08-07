"""Seal multi-observation source inventories and link them to search manifests."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0220_source_inventory_seals"
down_revision: str | Sequence[str] | None = "0219_source_coverage_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "source_inventory_components",
    "source_inventory_snapshot_seals",
    "search_manifest_source_inventories",
)


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'source inventory provenance is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'source inventory provenance is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "source_inventory_components",
        sa.Column("component_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column(
            "snapshot_id",
            sa.String(128),
            sa.ForeignKey("source_inventory_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("component_key", sa.String(256), nullable=False),
        sa.Column("component_kind", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "source_observation_id",
            sa.String(128),
            sa.ForeignKey("evidence_source_observations.observation_id"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "component_key",
            name="uq_source_inventory_component_key",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "ordinal",
            name="uq_source_inventory_component_ordinal",
        ),
        sa.CheckConstraint(
            "component_kind IN ('primary', 'historical_page', 'crawl_page', "
            "'event_feed', 'other')",
            name="ck_source_inventory_component_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed')",
            name="ck_source_inventory_component_outcome",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_source_inventory_component_ordinal"),
        sa.CheckConstraint(
            "(outcome = 'succeeded' AND source_observation_id IS NOT NULL "
            "AND failure_reason IS NULL) OR "
            "(outcome = 'failed' AND source_observation_id IS NULL "
            "AND failure_reason IS NOT NULL)",
            name="ck_source_inventory_component_lineage",
        ),
    )
    op.create_index(
        "ix_source_inventory_component_snapshot",
        "source_inventory_components",
        ["snapshot_id", "ordinal"],
    )
    op.create_table(
        "source_inventory_snapshot_seals",
        sa.Column(
            "snapshot_id",
            sa.String(128),
            sa.ForeignKey("source_inventory_snapshots.snapshot_id"),
            primary_key=True,
        ),
        sa.Column("expected_component_count", sa.Integer(), nullable=False),
        sa.Column("component_digest_sha256", sa.String(64), nullable=False),
        sa.Column("completion_status", sa.String(16), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "expected_component_count > 0",
            name="ck_source_inventory_seal_count",
        ),
        sa.CheckConstraint(
            "length(component_digest_sha256) = 64",
            name="ck_source_inventory_seal_digest",
        ),
        sa.CheckConstraint(
            "completion_status IN ('complete', 'incomplete')",
            name="ck_source_inventory_seal_status",
        ),
    )
    op.create_table(
        "search_manifest_source_inventories",
        sa.Column(
            "manifest_id",
            sa.String(128),
            sa.ForeignKey("search_corpus_manifests.manifest_id"),
            primary_key=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(128),
            sa.ForeignKey("source_inventory_snapshots.snapshot_id"),
            primary_key=True,
        ),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_search_manifest_source_inventory_snapshot",
        "search_manifest_source_inventories",
        ["snapshot_id", "manifest_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_source_inventory_component_sealed "
        "BEFORE INSERT ON source_inventory_components "
        "WHEN EXISTS (SELECT 1 FROM source_inventory_snapshot_seals "
        "WHERE snapshot_id = NEW.snapshot_id) "
        "BEGIN SELECT RAISE(ABORT, 'source inventory snapshot is sealed'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_inventory_seal_count "
        "BEFORE INSERT ON source_inventory_snapshot_seals "
        "WHEN NEW.expected_component_count <> "
        "(SELECT COUNT(*) FROM source_inventory_components "
        "WHERE snapshot_id = NEW.snapshot_id) "
        "BEGIN SELECT RAISE(ABORT, 'source inventory seal count mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_inventory_seal_status "
        "BEFORE INSERT ON source_inventory_snapshot_seals "
        "WHEN (NEW.completion_status = 'complete') <> "
        "(NOT EXISTS (SELECT 1 FROM source_inventory_components "
        "WHERE snapshot_id = NEW.snapshot_id AND required = 1 "
        "AND outcome <> 'succeeded')) "
        "BEGIN SELECT RAISE(ABORT, 'source inventory seal completion mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_search_manifest_source_inventory_sealed "
        "BEFORE INSERT ON search_manifest_source_inventories "
        "WHEN NOT EXISTS (SELECT 1 FROM source_inventory_snapshot_seals "
        "WHERE snapshot_id = NEW.snapshot_id) "
        "BEGIN SELECT RAISE(ABORT, 'search manifest source inventory must be sealed'); END"
    )
    for table in _TABLES:
        _append_only(table)
    op.execute(
        "CREATE VIEW v_source_inventory_sealed AS "
        "SELECT inventory.*, seal.expected_component_count, "
        "seal.component_digest_sha256, seal.completion_status, seal.sealed_at "
        "FROM v_source_inventory_current AS inventory "
        "JOIN source_inventory_snapshot_seals AS seal "
        "ON seal.snapshot_id = inventory.snapshot_id"
    )
    op.execute(
        "CREATE VIEW v_source_inventory_sealed_complete AS "
        "SELECT * FROM v_source_inventory_sealed "
        "WHERE completion_status = 'complete'"
    )


def downgrade() -> None:
    for view in (
        "v_source_inventory_sealed_complete",
        "v_source_inventory_sealed",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for trigger in (
        "trg_source_inventory_component_sealed",
        "trg_source_inventory_seal_count",
        "trg_source_inventory_seal_status",
        "trg_search_manifest_source_inventory_sealed",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    op.drop_index(
        "ix_search_manifest_source_inventory_snapshot",
        table_name="search_manifest_source_inventories",
    )
    op.drop_table("search_manifest_source_inventories")
    op.drop_table("source_inventory_snapshot_seals")
    op.drop_index(
        "ix_source_inventory_component_snapshot",
        table_name="source_inventory_components",
    )
    op.drop_table("source_inventory_components")
