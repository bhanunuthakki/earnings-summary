"""Remove retired position and personal-process coaching configuration.

Source retirement does not delete retained decisions or process tenets. Recovery
requires the reviewed predecessor snapshot; downgrade cannot restore history.
"""

from __future__ import annotations

from alembic import op

revision = "0068_retire_personal_coaching"
down_revision = "0067_retire_theme_generation"
branch_labels = None
depends_on = None

_TABLES = ("llm_budgets", "llm_budget_alerts", "model_eval_verdicts")
_PURPOSES = (
    "'position_review','tenet_distill','tenet_accountability',"
    "'tenet_semantic_tension','exit_postmortem_draft'"
)


def upgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        columns = {
            str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
        }
        if not {"id", "purpose"}.issubset(columns):
            raise RuntimeError(f"personal coaching retirement schema unavailable: {table}")
    # These stores have no inbound foreign keys or deletion triggers in the
    # admitted schema. Reject extensions before changing any row.
    for row in connection.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'"):
        name = str(row[0]).replace('"', '""')
        for foreign_key in connection.exec_driver_sql(f'PRAGMA foreign_key_list("{name}")'):
            if str(foreign_key[2]) in _TABLES:
                raise RuntimeError(
                    "personal coaching retirement has an unsupported dependent table"
                )
    if connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_schema WHERE type='trigger' "
        "AND tbl_name IN ('llm_budgets','llm_budget_alerts','model_eval_verdicts') LIMIT 1"
    ).first():
        raise RuntimeError("personal coaching retirement has an unsupported deletion trigger")
    for table in _TABLES:
        connection.exec_driver_sql(f"DELETE FROM {table} WHERE purpose IN ({_PURPOSES})")


def downgrade() -> None:
    raise RuntimeError(
        "Restore a reviewed predecessor database snapshot to recover coaching configuration"
    )
