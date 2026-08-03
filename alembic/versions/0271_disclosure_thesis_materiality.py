"""disclosure_events.thesis_materiality — the LLM elevation judgment.

Owner ruling (2026-08-02): disclosure drift is far too onerous to surface
raw. An event may be ELEVATED to an owner-facing surface only when an LLM
judges that the change EXCEEDS MATERIALITY in one specific sense — it
fundamentally restricts the ability to MEASURE the thesis (a disclosure the
thesis' tier-1 KPIs / break conditions depend on was dropped, aggregated
away, redefined, or obscured).

This is the "distinct judgment column, NOT another float threshold" contract
that the 2026-07-30 inbox root-cause investigation required before any
disclosure event could be elevated again: the stored ``materiality`` float
mixes three incommensurable per-detector scales (text dissimilarity, a
magnitude ratio, a whole-book percentile) and must never gate a surface.

Columns (all nullable — NULL means "not yet judged", which surfaces treat as
NOT elevated; the judge never fabricates a verdict on a degraded call):

* ``thesis_materiality``       — 'restricts_measurement' | 'not_material'
* ``thesis_materiality_rationale`` — the judge's one-sentence receipt
* ``thesis_materiality_judged_at`` — naive-UTC judgment timestamp

Revision ID: 0271_disclosure_thesis_materiality
Revises: 0270_financial_facts_supersedes_index
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0271_disclosure_thesis_materiality"
down_revision: str | None = "0270_financial_facts_supersedes_index"
branch_labels: None = None
depends_on: None = None

_EVENTS = "disclosure_events"
_INDEX = "ix_disclosure_events_thesis_materiality"

_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("thesis_materiality", sa.String(length=32), nullable=True),
    sa.Column("thesis_materiality_rationale", sa.Text(), nullable=True),
    sa.Column("thesis_materiality_judged_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _EVENTS not in insp.get_table_names():
        return
    existing = {col["name"] for col in insp.get_columns(_EVENTS)}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column(_EVENTS, column)
    existing_indexes = {ix["name"] for ix in insp.get_indexes(_EVENTS)}
    if _INDEX not in existing_indexes:
        op.create_index(_INDEX, _EVENTS, ["ticker", "thesis_materiality"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _EVENTS not in insp.get_table_names():
        return
    existing_indexes = {ix["name"] for ix in insp.get_indexes(_EVENTS)}
    if _INDEX in existing_indexes:
        op.drop_index(_INDEX, table_name=_EVENTS)
    existing = {col["name"] for col in insp.get_columns(_EVENTS)}
    to_drop = [column.name for column in _COLUMNS if column.name in existing]
    if not to_drop:
        return
    # batch_alter_table (table rebuild) under legacy_alter_table, NOT native
    # DROP COLUMN: SQLite re-validates EVERY trigger in the schema both on a
    # native drop and on the rebuild's final RENAME, and partial-schema
    # databases legitimately carry triggers whose subject tables were never
    # created (e.g. trg_ask_answer_audit_record_llm referencing llm_calls on
    # a DB stamped past the llm_calls migration). legacy_alter_table=ON skips
    # that global validation during the rename; disclosure_events itself has
    # no triggers/FTS, so nothing that should be rewritten is skipped.
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        bind.exec_driver_sql("PRAGMA legacy_alter_table = ON")
    try:
        with op.batch_alter_table(_EVENTS) as batch:
            for name in to_drop:
                batch.drop_column(name)
    finally:
        if is_sqlite:
            bind.exec_driver_sql("PRAGMA legacy_alter_table = OFF")
