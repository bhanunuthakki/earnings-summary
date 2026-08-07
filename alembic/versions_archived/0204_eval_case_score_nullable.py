"""eval_case_results.score — drop NOT NULL so judge-infra cases persist as
"not measured" (adversarial review of the July-2026 transport hardening,
2026-07-25).

#1008 introduced the judge-infra contract: a case whose JUDGE call failed
operationally carries ``score=None`` (nothing was measured) instead of a fake
0.0, and the harness excludes such cases from ``avg_score``. The store was
updated to write NULL — but the live column is ``FLOAT NOT NULL`` (the
original 0083 definition), so the first eval run containing an infra case
would die with IntegrityError at persist time, losing the whole run exactly
on the days the distinction matters. The in-code contract shipped ahead of
the schema; this migration closes the gap.

SQLite cannot drop a NOT NULL in place; batch_alter_table recreates the table
and copies data through (the 0028 precedent). ``eval_case_results`` carries no
FTS triggers (the analyst_notes batch_alter incident does not apply) and one
FK (eval_run_id → eval_runs.id), which batch mode preserves.

Downgrade backfills NULL scores to 0.0 before restoring NOT NULL — lossy by
necessity (0.0 was exactly the lie the nullable column exists to avoid), but
a downgrade must not fail on rows the upgrade made legal.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0204_eval_case_score_nullable"
down_revision: str | Sequence[str] | None = "0203_disclosure_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "eval_case_results"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return  # pre-0083 DB; 0083 creates it (and should be updated in place)
    col = next(c for c in insp.get_columns(_TABLE) if c["name"] == "score")
    if col.get("nullable"):
        return  # already nullable (idempotent re-run)
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column("score", existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    op.execute(f"UPDATE {_TABLE} SET score = 0.0 WHERE score IS NULL")
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column("score", existing_type=sa.Float(), nullable=False)
