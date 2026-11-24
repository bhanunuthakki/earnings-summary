"""Index current source coverage by its exact document version.

Per-document evidence continuation reads coverage for one legacy document's
versions. Without this index that lookup scanned the whole assessment ledger,
which cost seconds per document on the production-sized database.
"""

from __future__ import annotations

from alembic import op

revision = "0070_source_coverage_document_version_index"
down_revision = "0069_retire_obsolete_input_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_coverage_document_version "
        "ON source_coverage_assessments (document_version_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_source_coverage_document_version")
