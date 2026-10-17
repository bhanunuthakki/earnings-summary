"""Join the fact-selection and retained financial-evidence migration branches.

Both branches retain their historical revision identities. An existing database
at either head applies the missing branch before it records this merge.
"""

from __future__ import annotations

revision = "0056_merge_reconciliation_heads"
down_revision = (
    "0050_sec_companyfacts_fetch_receipts",
    "0055_kpi_definition_subject_bindings",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
