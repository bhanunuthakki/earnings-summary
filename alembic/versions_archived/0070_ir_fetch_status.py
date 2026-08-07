"""ir_fetch_status — per-ticker IR auto-fetch attempt log.

Records the outcome of each headless IR-document crawl attempt
(``execution/discover_ir_documents_all.py``) so two surfaces can reason about
coverage health:

  * the command-center "IR Docs" panel — which roster names have NO auto-fetched
    IR documents (a manual pull is needed) and, for those, the last crawl outcome
    + reason (bot-protected 403, headless load-stall, no docs found, …);
  * the failing-crawler rescan (``discover_ir_documents_all --only-failing``) —
    the roster names with zero registered IR documents, retried more often than
    the weekly full sweep in case a previously-blocked site starts cooperating.

COVERAGE (how many docs a ticker has) is read live from the ``documents`` table
(``source_type='ir_doc'``); this table holds only the crawl-HEALTH metadata.

Revision ID: 0070_ir_fetch_status
Revises: 0069_recreate_brief_provenance_log
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0070_ir_fetch_status"
down_revision: str | Sequence[str] | None = "0069_recreate_brief_provenance_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ir_fetch_status" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "ir_fetch_status",
        sa.Column("ticker", sa.Text(), primary_key=True),
        sa.Column("last_attempt_at", sa.Text(), nullable=False),
        sa.Column("last_status", sa.Text(), nullable=False),  # ok | failed | skipped
        sa.Column("discovered", sa.Integer(), nullable=True),  # candidate links found
        sa.Column("downloaded", sa.Integer(), nullable=True),  # new docs registered
        sa.Column("reason", sa.Text(), nullable=True),  # why a name found nothing
        sa.Column("updated_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ir_fetch_status" not in inspector.get_table_names():
        return
    op.drop_table("ir_fetch_status")
