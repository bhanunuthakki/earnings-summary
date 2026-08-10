"""Add governed Copilot Ask proposal approval authority.

Revision ID: 0006_add_ask_proposal_approval
Revises: 0005_add_ask_exchange_store
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_add_ask_proposal_approval"
down_revision = "0005_add_ask_exchange_store"
branch_labels = None
depends_on = None


def _proposal_columns() -> set[str]:
    bind = op.get_bind()
    return {
        str(row[1])
        for row in bind.exec_driver_sql("PRAGMA table_info(research_proposals)").fetchall()
    }


def upgrade() -> None:
    columns = _proposal_columns()
    additions = (
        sa.Column("canonical_content_json", sa.Text(), nullable=True),
        sa.Column("canonical_content_sha256", sa.Text(), nullable=True),
        sa.Column(
            "proposal_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("target_precondition_sha256", sa.Text(), nullable=True),
        sa.Column("target_postcondition_sha256", sa.Text(), nullable=True),
        sa.Column("ask_exchange_request_id", sa.Text(), nullable=True),
        sa.Column("actionable_at", sa.Text(), nullable=True),
        sa.Column("invalidated_at", sa.Text(), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("research_proposals", column)

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS research_proposal_decision_receipts (
            decision_request_id TEXT PRIMARY KEY,
            proposal_id INTEGER NOT NULL,
            request_sha256 TEXT NOT NULL,
            decision TEXT NOT NULL,
            expected_proposal_revision INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES research_proposals(id) ON DELETE CASCADE,
            CHECK(length(decision_request_id) BETWEEN 1 AND 128),
            CHECK(decision IN ('approve', 'reject')),
            CHECK(expected_proposal_revision >= 0),
            CHECK(json_valid(response_json) AND json_type(response_json) = 'object'),
            CHECK(length(request_sha256) = 64),
            CHECK(length(response_sha256) = 64)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_proposal_decision_receipts_proposal "
        "ON research_proposal_decision_receipts(proposal_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_research_proposals_ask_exchange "
        "ON research_proposals(ask_exchange_request_id)"
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_governed_insert_valid
        BEFORE INSERT ON research_proposals
        WHEN NEW.canonical_content_json IS NOT NULL AND (
            NEW.kind NOT IN ('ask_thesis_edit', 'ask_kpi_edit')
            OR NEW.status <> 'pending'
            OR NEW.proposal_revision <> 0
            OR NOT json_valid(NEW.canonical_content_json)
            OR json_type(NEW.canonical_content_json) <> 'object'
            OR length(NEW.canonical_content_sha256) <> 64
            OR length(NEW.target_precondition_sha256) <> 64
            OR NEW.ask_exchange_request_id IS NULL
            OR length(NEW.ask_exchange_request_id) NOT BETWEEN 1 AND 128
            OR NEW.actionable_at IS NOT NULL
            OR NEW.invalidated_at IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM ask_exchanges exchange
                WHERE exchange.request_id=NEW.ask_exchange_request_id
                  AND exchange.status='pending'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid governed research proposal');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_ask_exchange_invalidate_governed_proposals
        BEFORE DELETE ON ask_exchanges
        BEGIN
            UPDATE research_proposals
            SET status='superseded', proposal_revision=proposal_revision+1,
                invalidated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                invalidation_reason='exchange_deleted',
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE ask_exchange_request_id=OLD.request_id AND status='pending';
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_canonical_content_immutable
        BEFORE UPDATE OF canonical_content_json, canonical_content_sha256,
                         target_precondition_sha256, ask_exchange_request_id ON research_proposals
        WHEN OLD.canonical_content_json IS NOT NULL AND (
            NEW.canonical_content_json IS NOT OLD.canonical_content_json
            OR NEW.canonical_content_sha256 IS NOT OLD.canonical_content_sha256
            OR NEW.target_precondition_sha256 IS NOT OLD.target_precondition_sha256
            OR NEW.ask_exchange_request_id IS NOT OLD.ask_exchange_request_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'governed proposal content is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_governed_status_cas
        BEFORE UPDATE OF status, proposal_revision ON research_proposals
        WHEN OLD.canonical_content_json IS NOT NULL AND (
            OLD.status <> 'pending'
            OR NEW.status NOT IN ('approved', 'rejected', 'superseded')
            OR NEW.proposal_revision <> OLD.proposal_revision + 1
            OR (NEW.status IN ('approved', 'rejected') AND (
                OLD.actionable_at IS NULL OR OLD.invalidated_at IS NOT NULL
            ))
            OR (NEW.status = 'superseded' AND NEW.invalidated_at IS NULL)
        )
        BEGIN
            SELECT RAISE(ABORT, 'governed proposal status CAS failed');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ask_exchange_invalidate_governed_proposals")
    op.execute("DROP TRIGGER IF EXISTS trg_research_proposal_governed_status_cas")
    op.execute("DROP TRIGGER IF EXISTS trg_research_proposal_canonical_content_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_research_proposal_governed_insert_valid")
    op.execute("DROP INDEX IF EXISTS ix_research_proposals_ask_exchange")
    op.execute("DROP INDEX IF EXISTS ix_research_proposal_decision_receipts_proposal")
    op.execute("DROP TABLE IF EXISTS research_proposal_decision_receipts")
    columns = _proposal_columns()
    for name in (
        "target_postcondition_sha256",
        "invalidation_reason",
        "invalidated_at",
        "actionable_at",
        "ask_exchange_request_id",
        "target_precondition_sha256",
        "proposal_revision",
        "canonical_content_sha256",
        "canonical_content_json",
    ):
        if name in columns:
            op.drop_column("research_proposals", name)
