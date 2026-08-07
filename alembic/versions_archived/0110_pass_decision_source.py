"""pass/avoid decisions as a first-class source (Close-the-Loops L11 PR1).

The decisions ledger (0046/0086) only ever filled from artifact verdicts
(``lens:five_min_reread``) and Socratic memos — both of which run on HELD
names. A name the owner PASSED on (the discovery "dismissed" action writes only
queue-state) left no graded trace, so the calibration loop (L1/L8) was blind to
errors of omission — the largest hidden cost (a passed name that later triples).

This migration adds the source half so a pass/avoid can become a first-class,
falsifiable, gradeable ``decisions`` row (``recommendation_kind='avoid'``):

  decisions.source_dismissal_id — discovery_candidates.id when the avoid was
      authored from the discovery dismiss action; NULL for a manual pass. A
      partial UNIQUE index gives the dismiss path the same idempotency the
      artifact/memo UNIQUEs give the lens/Socratic paths (re-dismissing the same
      candidate updates rather than duplicates).
  decisions.source_prose       — the owner-authored "what would change my mind"
      text for a pass that has no artifact/memo prose. The same
      ``attach_conditions`` / ``attach_qualitative_conditions`` rungs extract the
      numeric + qualitative conditions out of it (deferred, off the request
      path) so the avoid reuses L9's news/earnings bridge unchanged.

And widens ``ck_decisions_source_present`` (0086) to admit a source-less avoid:
an owner-authored pass is anchored by being a pass, not by a memo. The CHECK
escape is ``recommendation_kind = 'avoid'`` so the existing
artifact-OR-memo invariant still binds every add/trim/hold/sell/initiate row.

Batch-recreated per 0077/0086 (SQLite can't ALTER a CHECK in place); guarded +
idempotent per 0071. The CHECK widening only fires when the referands
(source_artifact_id, source_memo_id) exist, so a minimal pre-0086 table is
left alone.

Revision ID: 0110_pass_decision_source
Revises: 0109_qualitative_conditions
Create Date: 2026-06-14
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0110_pass_decision_source"
down_revision: str | Sequence[str] | None = "0109_qualitative_conditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# An owner-authored pass/avoid is anchored by being a pass; every other kind
# still needs an artifact or memo source. Keep in lockstep with
# decision_conditions / decision_extractor write paths.
_SOURCE_PRESENT_WIDENED = (
    "source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL OR recommendation_kind = 'avoid'"
)
_SOURCE_PRESENT_ORIGINAL = "source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL"


def _columns(insp: sa.Inspector) -> set[str]:
    if "decisions" not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns("decisions")}


def _has_index(insp: sa.Inspector, name: str) -> bool:
    if "decisions" not in insp.get_table_names():
        return False
    return any(ix["name"] == name for ix in insp.get_indexes("decisions"))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = _columns(insp)
    if not cols:
        return  # no decisions table yet (pre-0046) — nothing to extend

    adds: list[sa.Column[object]] = []
    if "source_dismissal_id" not in cols:
        adds.append(sa.Column("source_dismissal_id", sa.Integer(), nullable=True))
    if "source_prose" not in cols:
        adds.append(sa.Column("source_prose", sa.Text(), nullable=True))

    # The CHECK widening only makes sense once the source columns it names are
    # present (the 0086 schema). On a minimal pre-0086 table we still add the new
    # columns but leave the constraint alone.
    widen_check = "source_artifact_id" in cols and "source_memo_id" in cols

    if adds or widen_check:
        with op.batch_alter_table("decisions") as batch:
            for col in adds:
                batch.add_column(col)
            if widen_check:
                with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
                    batch.drop_constraint("ck_decisions_source_present", type_="check")
                batch.create_check_constraint(
                    "ck_decisions_source_present", _SOURCE_PRESENT_WIDENED
                )

    if not _has_index(sa.inspect(bind), "uq_decisions_source_dismissal"):
        op.create_index(
            "uq_decisions_source_dismissal",
            "decisions",
            ["source_dismissal_id"],
            unique=True,
            sqlite_where=sa.text("source_dismissal_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = _columns(insp)
    if not cols:
        return

    # An avoid row authored without an artifact/memo cannot survive the narrower
    # CHECK — refuse rather than destroy (mirrors 0086's memo-only guard).
    avoid_only = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM decisions WHERE recommendation_kind = 'avoid' "
            "AND source_artifact_id IS NULL AND source_memo_id IS NULL"
        )
    ).scalar()
    restore_check = not avoid_only and "source_artifact_id" in cols and "source_memo_id" in cols

    if _has_index(insp, "uq_decisions_source_dismissal"):
        op.drop_index("uq_decisions_source_dismissal", table_name="decisions")

    with op.batch_alter_table("decisions") as batch:
        if restore_check:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_decisions_source_present", type_="check")
            batch.create_check_constraint("ck_decisions_source_present", _SOURCE_PRESENT_ORIGINAL)
        if "source_prose" in cols:
            batch.drop_column("source_prose")
        if "source_dismissal_id" in cols:
            batch.drop_column("source_dismissal_id")
