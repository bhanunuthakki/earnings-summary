"""Join retirement and current source-contract migration histories.

Both parent chains retain their stable revision identities. This revision has
no schema or data changes.
"""

from __future__ import annotations

revision = "0066_merge_retirement_main"
down_revision = (
    "0065_retire_optional_research_history",
    "0063_transcript_reported_period_receipts",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("Restore a reviewed predecessor database snapshot to undo retirement")
