"""Make the cross-process LLM quota breaker migration-owned.

Revision ID: 0004_add_llm_circuit_breakers
Revises: 0003_restore_baseline_defaults
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op

revision = "0004_add_llm_circuit_breakers"
down_revision = "0003_restore_baseline_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS safely adopts a table created by the pre-migration runtime
    # fallback when a quota failure occurred before this revision shipped.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_circuit_breakers (
            provider TEXT PRIMARY KEY,
            blocked_until TEXT NOT NULL,
            reason TEXT,
            set_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_circuit_breakers")
