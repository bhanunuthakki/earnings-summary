"""baseline — schema as established by src/db.py:init_db().

The baseline reflects three tables created in src/db.py at module import:
tracked_companies, quarterly_artifacts, fmp_endpoint_status.

Design note: src/db.py:init_db() is KEPT at import-time as a bootstrap
fallback for fresh checkouts that have not yet run `alembic upgrade head`.
Alembic owns all NEW schema changes (this revision and forward). The two
coexist safely because init_db() uses CREATE TABLE IF NOT EXISTS and
column-existence checks before ALTER TABLE.

Revision ID: 0000_baseline
Revises:
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0000_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op; baseline records the schema established by src/db.py:init_db()."""


def downgrade() -> None:
    """No-op; reverting baseline would mean dropping init_db()'s tables."""
