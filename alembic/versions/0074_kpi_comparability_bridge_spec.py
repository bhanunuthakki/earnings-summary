"""Add nullable bridge_spec_json column and commitment trigger to KPI comparability revisions.

Revision ID: 0074_kpi_comparability_bridge_spec
Revises: 0073_drop_redundant_indexes
"""

from __future__ import annotations

from alembic import op

revision = "0074_kpi_comparability_bridge_spec"
down_revision = "0073_drop_redundant_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE kpi_definition_comparability_revisions "
        "ADD COLUMN bridge_spec_json TEXT "
        "CHECK(bridge_spec_json IS NULL OR "
        "(json_valid(bridge_spec_json) AND json_type(bridge_spec_json)='object'))"
    )
    op.execute(
        """
        CREATE TRIGGER trg_kpi_definition_comparability_bridge_commitment
        BEFORE INSERT ON kpi_definition_comparability_revisions
        WHEN (NEW.bridge_spec_json IS NULL
              AND json_type(NEW.commitment_json,'$.bridge_spec') IS NOT NULL)
          OR (NEW.bridge_spec_json IS NOT NULL
              AND (json_type(NEW.commitment_json,'$.bridge_spec') IS NULL
                   OR json_extract(NEW.commitment_json,'$.bridge_spec')
                        IS NOT json(NEW.bridge_spec_json)))
        BEGIN
            SELECT RAISE(ABORT,'KPI comparability bridge_spec commitment mismatch');
        END
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM kpi_definition_comparability_revisions "
        "WHERE bridge_spec_json IS NOT NULL"
    ).scalar_one()
    if int(retained) > 0:
        raise RuntimeError(
            "cannot downgrade KPI comparability bridge_spec_json without losing bridge history"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_definition_comparability_bridge_commitment")
    op.execute("ALTER TABLE kpi_definition_comparability_revisions DROP COLUMN bridge_spec_json")
