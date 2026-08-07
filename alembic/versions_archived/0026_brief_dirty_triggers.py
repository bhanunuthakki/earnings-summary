"""brief_dirty_triggers — auto-set tracked_companies.brief_dirty=1 on fact churn.

Replaces the per-call-site instrumentation approach with SQL triggers: any
INSERT or UPDATE on financial_facts / kpi_facts / segment_facts / dcf_runs
fires an AFTER trigger that flips brief_dirty=1 for the affected ticker.

Daily fetch+brief worker queries `WHERE brief_dirty = 1`, regenerates briefs,
clears the flag. Single source of truth for invalidation; no risk of
forgetting at new fact writers.

Tables NOT covered here (deliberate):
  - thesis_evaluations: written by the daily worker itself after the brief
    regen pipeline. Wrapping that in a self-triggering dirty flag would loop.
  - management_commitments: outcome updates write infrequently and the worker
    re-derives Say-Do on every brief regen anyway.
  - transcripts / transcript_segments: orthogonal — the §5 builder reads
    files directly, not transcript_segments, so a dirty trigger here wouldn't
    help.

Note: SQLite triggers run inside the same transaction as the INSERT/UPDATE,
so the dirty flag is observable to the same connection that wrote the fact.

Revision ID: 0026_brief_dirty_triggers
Revises: 0025_expected_earnings
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_brief_dirty_triggers"
down_revision: str | Sequence[str] | None = "0025_expected_earnings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Triggers we maintain — (name, table, event) tuples. Keeping them in a list
# so upgrade + downgrade stay symmetric and easy to extend.
_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("trg_dirty_financial_facts_ins", "financial_facts", "INSERT"),
    ("trg_dirty_financial_facts_upd", "financial_facts", "UPDATE"),
    ("trg_dirty_kpi_facts_ins", "kpi_facts", "INSERT"),
    ("trg_dirty_kpi_facts_upd", "kpi_facts", "UPDATE"),
    ("trg_dirty_segment_facts_ins", "segment_facts", "INSERT"),
    ("trg_dirty_segment_facts_upd", "segment_facts", "UPDATE"),
    ("trg_dirty_dcf_runs_ins", "dcf_runs", "INSERT"),
    ("trg_dirty_dcf_runs_upd", "dcf_runs", "UPDATE"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for trigger_name, table_name, event in _TRIGGERS:
        bind.execute(sa.text(_trigger_sql(trigger_name, table_name, event)))


def downgrade() -> None:
    bind = op.get_bind()
    for trigger_name, _table_name, _event in _TRIGGERS:
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))


def _trigger_sql(trigger_name: str, table_name: str, event: str) -> str:
    """SQLite trigger body: flip brief_dirty=1 for the row's ticker.

    Uses NEW.ticker for INSERT and UPDATE — both events expose the affected
    row, and ticker is part of every fact table's row.
    """
    return f"""
    CREATE TRIGGER IF NOT EXISTS {trigger_name}
    AFTER {event} ON {table_name}
    FOR EACH ROW
    BEGIN
        UPDATE tracked_companies
        SET brief_dirty = 1
        WHERE ticker = NEW.ticker;
    END
    """
