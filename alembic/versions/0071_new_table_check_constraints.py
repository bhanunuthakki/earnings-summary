"""CHECK constraints on the two substrate-enum columns added after 0068.

0068_substrate_constraints hardened the original Personal-CIO enums (alerts /
queued_actions / user_kpi_registry). Two tables landed *after* it reintroduced
free-TEXT enum columns the v6 re-grade flagged as undefended:

  * ``ir_fetch_status.last_status`` (0070) — the IR auto-fetch crawl outcome.
    Canonical vocabulary is ``ok | failed | skipped`` (set by
    ``execution/discover_ir_documents_all.py``); the command-center "IR Docs"
    coverage panel and the ``--only-failing`` rescan branch on the value, so an
    out-of-vocab string silently mis-buckets a name.

  * ``saydo_historical_metrics.outcome`` (b79cec08ce5b) — the guidance verdict.
    Canonical vocabulary is the ``CommitmentOutcome`` StrEnum
    (``src/compute/say_do.py``): ``hit | miss | beat | no_data``. The value
    drives the ``saydo_due`` thesis-update payload, so an out-of-vocab verdict
    propagates unchecked into ``queued_actions.payload_json``.

This migration adds DB-level CHECK constraints matching those vocabularies,
extending the 0068 guarantee to the columns that slipped in after it. Mirrors
0068 exactly: SQLite cannot ALTER TABLE ADD CONSTRAINT, so each CHECK rides on a
``batch_alter_table`` recreation; every step is guarded so the migration is a
no-op on a DB that predates either table.

``kpi_facts`` is deliberately NOT touched: it already carries
``uq_kpi_facts_provenance`` (0059), the provenance-keyed uniqueness guard that
the restatement chain relies on — a second UNIQUE would conflict with the
intentional multi-row restatement design.

Revision ID: 0071_new_table_check_constraints
Revises: 0070_ir_fetch_status
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0071_new_table_check_constraints"
down_revision: str | Sequence[str] | None = "0070_ir_fetch_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical enum vocabularies — keep in lockstep with the writers named above.
_IR_LAST_STATUS = "('ok', 'failed', 'skipped')"
# CommitmentOutcome StrEnum (src/compute/say_do.py). 'no_data' is currently
# never persisted to this ledger (match_commitment returns before the write when
# there is no realized value), but admitting it keeps the CHECK aligned to the
# full enum value-space so a future code path that does persist it cannot crash.
_SAYDO_OUTCOME = "('hit', 'miss', 'beat', 'no_data')"


def _has_table(inspector: sa.Inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "ir_fetch_status"):
        with op.batch_alter_table("ir_fetch_status") as batch:
            batch.create_check_constraint(
                "ck_ir_fetch_status_last_status", f"last_status IN {_IR_LAST_STATUS}"
            )

    if _has_table(insp, "saydo_historical_metrics"):
        with op.batch_alter_table("saydo_historical_metrics") as batch:
            batch.create_check_constraint(
                "ck_saydo_historical_metrics_outcome", f"outcome IN {_SAYDO_OUTCOME}"
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "saydo_historical_metrics"):
        with op.batch_alter_table("saydo_historical_metrics") as batch:
            batch.drop_constraint("ck_saydo_historical_metrics_outcome", type_="check")

    if _has_table(insp, "ir_fetch_status"):
        with op.batch_alter_table("ir_fetch_status") as batch:
            batch.drop_constraint("ck_ir_fetch_status_last_status", type_="check")
