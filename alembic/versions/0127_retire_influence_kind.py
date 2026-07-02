"""Retire the analyst_notes 'influence' kind — superseded by On My Mind readings.

PR #701 added kind='influence' (0125) to classify Telegram docs/links at ingest
time. That ingest-time classification was rejected in favor of the Thought
Partner model: documents/links land as ordinary On My Mind *readings*
(kind='observation'), and the durable belief layer is the Worldview (Tenets),
not a passive 'influence' note.

This migration (1) BACKFILLS every existing kind='influence' row to
kind='observation' — repointing it into the On My Mind feed model (item_type +
ledger in context_json) — and only THEN (2) narrows the kind CHECK back to the
six pre-0125 kinds. Order matters: a live influence row would fail the narrowed
CHECK, so rows must be repointed before the constraint is recreated.

SQLite cannot ALTER a CHECK, so batch_alter_table recreates the table with the
narrowed constraint. Mirrors the pattern in 0115 / 0125.

Revision ID: 0127_retire_influence_kind
Revises: 0126_research_proposal_artifact
Create Date: 2026-07-01
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0127_retire_influence_kind"
down_revision: str | Sequence[str] | None = "0126_research_proposal_artifact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITH_INFLUENCE = (
    "('question', 'decision', 'watch', 'assumption', 'observation', 'musing', 'influence')"
)
_KINDS_PRIOR = "('question', 'decision', 'watch', 'assumption', 'observation', 'musing')"

# Repoint influence rows into the On My Mind reading model: item_type derived
# from the original media_kind, ledger flipped from 'influence' to 'onmymind'.
_BACKFILL = sa.text(
    "UPDATE analyst_notes "
    "SET kind = 'observation', "
    "    context_json = json_set("
    "        coalesce(context_json, '{}'), "
    "        '$.item_type', CASE WHEN json_extract(context_json, '$.media_kind') = 'url' "
    "                            THEN 'link' ELSE 'doc' END, "
    "        '$.ledger', 'onmymind') "
    "WHERE kind = 'influence'"
)


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    bind.execute(_BACKFILL)  # repoint influence rows BEFORE narrowing the CHECK
    with op.batch_alter_table("analyst_notes") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_analyst_notes_kind", type_="check")
        batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_PRIOR}")


def downgrade() -> None:
    # Re-widen the CHECK to admit 'influence' again (mirror 0125.upgrade). The
    # backfill is intentionally NOT reversed — repointed rows stay observations
    # (lossless: media_kind is still in context_json), the safe direction.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "analyst_notes"):
        return
    with op.batch_alter_table("analyst_notes") as batch:
        with contextlib.suppress(ValueError, sa.exc.OperationalError):
            batch.drop_constraint("ck_analyst_notes_kind", type_="check")
        batch.create_check_constraint("ck_analyst_notes_kind", f"kind IN {_KINDS_WITH_INFLUENCE}")
