"""stance_scores — graded outcomes for advisor memos (master build P2.5).

One row per scoring pass over a matured memo (created_at + horizon_days in
the past). Socratic memos grade their forced stance against the ticker's
dividend-adjusted price move over the horizon — SPY-relative when the
tracker's benchmark series is reachable (the directive's "reuse the
tracker's benchmarks"), absolute otherwise, with ``benchmark_basis``
recording which. Swap checks grade the SCREEN mechanically (did the
candidate out-return the holding?) since the memo itself never directs.
Next-dollar memos are marked unscoreable in v1 (their recommendations live
in prose, not structure).

``advisor_memos.score_status`` flips pending -> scored/unscoreable as rows
land here; a memo the scorer must defer (immature horizon, price cache not
yet covering the window, tracker down for a SPY-basis grade) simply stays
pending for the next run — the job is idempotent over the pending set.

New-table CHECKs per 0071's policy.

Revision ID: 0078_stance_scores
Revises: 0077_advisor_memos
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0078_stance_scores"
down_revision: str | Sequence[str] | None = "0077_advisor_memos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERDICTS = "('correct', 'wrong', 'mixed', 'screen_validated', 'screen_refuted', 'unscoreable')"
_BASES = "('tracker_spy', 'absolute', 'none')"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "stance_scores" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "stance_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("memo_id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'bhanu'"),
        ),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("benchmark_basis", sa.Text(), nullable=False, server_default=sa.text("'none'")),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Text(), nullable=True),
        sa.Column("end_date", sa.Text(), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("counter_ticker", sa.Text(), nullable=True),
        sa.Column("ticker_return_pct", sa.Float(), nullable=True),
        sa.Column("benchmark_return_pct", sa.Float(), nullable=True),
        sa.Column("excess_return_pct", sa.Float(), nullable=True),
        sa.Column("counter_return_pct", sa.Float(), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"verdict IN {_VERDICTS}", name="ck_stance_scores_verdict"),
        sa.CheckConstraint(f"benchmark_basis IN {_BASES}", name="ck_stance_scores_benchmark_basis"),
    )
    op.create_index("ix_stance_scores_memo", "stance_scores", ["memo_id"])
    op.create_index("ix_stance_scores_user_verdict", "stance_scores", ["user_id", "verdict"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "stance_scores" not in insp.get_table_names():
        return
    op.drop_index("ix_stance_scores_user_verdict", table_name="stance_scores")
    op.drop_index("ix_stance_scores_memo", table_name="stance_scores")
    op.drop_table("stance_scores")
