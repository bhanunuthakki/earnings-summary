"""remove_init_db_at_import — design decision: KEEP init_db() as bootstrap fallback.

After implementing migrations 0001-0008, the original Phase 2 plan called for
removing `init_db()` from src/db.py module-level so `import db` no longer
triggers schema creation. On reflection, removal would introduce fragility:
a fresh checkout that hasn't run `alembic upgrade head` would have no tables
at all, and every CLI script would need an explicit init step.

**Decision**: KEEP `init_db()` at import-time in src/db.py as a bootstrap
fallback for the three baseline tables (tracked_companies, quarterly_artifacts,
fmp_endpoint_status). Alembic owns all NEW schema changes (0002+ tables).
The two coexist safely because init_db() is idempotent: CREATE TABLE IF NOT
EXISTS for the three baseline tables, ALTER TABLE only for missing columns.

This migration is therefore a **no-op in DB terms** — it exists as the
revision-ID anchor for the design decision in Alembic history.

Revision ID: 0009_remove_init_db_at_import
Revises: 0008_thesis_state
Create Date: 2026-05-03
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0009_remove_init_db_at_import"
down_revision: str | Sequence[str] | None = "0008_thesis_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op; see module docstring for the design decision this revision records."""


def downgrade() -> None:
    """No-op; this migration only records a design decision, not a DB change."""
