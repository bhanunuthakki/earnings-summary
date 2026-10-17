"""Bind unchanged CompanyFacts skips to the completed extractor identity.

Existing receipts keep an unknown identity. Their first run after upgrade must
reprocess bytes before it can authorize any unchanged-payload skip.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0057_companyfacts_extractor_identity"
down_revision = "0056_merge_reconciliation_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sec_companyfacts_fetch_receipts",
        sa.Column("extractor_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sec_companyfacts_fetch_receipts", "extractor_sha256")
