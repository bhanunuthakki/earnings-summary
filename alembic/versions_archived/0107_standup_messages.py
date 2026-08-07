"""standup_messages — the proactive-analyst-standup delivery ledger (L9).

The standup rung (``src/standup``) watches falsifiable decision_conditions,
journal open-items, DCF assumption staleness and live position drift; when one
trips it composes a grounded, cited brief through the Ask engine, eval-gates it
for relevance, and — if it clears the bar — writes it into a persistent
``ask_sessions`` thread the owner sees as a waiting advisory message.

This table is that rung's ledger, and it carries three jobs at once:

  * **dedup** — ``signature_sha`` (kind|ticker|trip-key) so the same trip never
    re-composes (and re-pays the LLM) within the dedup window;
  * **rate limiting** — the ``created_at`` / ``ticker`` / ``status`` columns
    back the per-day cap and the per-name cooldown (a chatty, miscalibrated
    advisor that pushes unprompted is worse than silence);
  * **cross-session memory stub** — ``conclusion`` holds the one-line distilled
    read the NEXT standup for the same ticker references as a *prior* (with the
    explicit "the current evidence may have invalidated this — re-derive"
    hazard caveat; a real per-ticker conclusion store is a deferred follow-on).

Every compose attempt lands a row (``status`` in delivered | suppressed_eval |
compose_failed) so the ledger is the audit trail of what the advisor decided to
say AND what it decided to stay silent on. ``score`` records the eval overall on
delivered/suppressed rows. A growing append-only ledger, not a single-row cache.

Revision ID: 0107_standup_messages
Revises: 0106_confidence_observations
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0107_standup_messages"
down_revision: str | Sequence[str] | None = "0106_confidence_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "standup_messages" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "standup_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        # NULL for book-level signals (a journal note with no ticker); the
        # watched name otherwise.
        sa.Column("ticker", sa.Text(), nullable=True),
        # decision_condition | journal_open_item | dcf_staleness | position_drift
        sa.Column("signal_kind", sa.Text(), nullable=False),
        # kind|ticker|trip-key content hash (alerts.store.compute_signature_sha)
        # — the dedup key so one trip composes at most once per dedup window.
        sa.Column("signature_sha", sa.Text(), nullable=False),
        # delivered | suppressed_eval | compose_failed
        sa.Column("status", sa.Text(), nullable=False),
        # The eval overall (mean of rubric facets) on compose attempts that
        # reached the judge; NULL when compose itself failed.
        sa.Column("score", sa.Float(), nullable=True),
        # The persistent ask thread + the assistant brief's turn id, on delivery.
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        # Memory stub: the one-line distilled read the NEXT standup references.
        sa.Column("conclusion", sa.Text(), nullable=True),
        # The deterministic one-line trip summary (the persisted user turn / feed).
        sa.Column("headline", sa.Text(), nullable=True),
        # The signal's structured evidence snapshot (audit / re-render).
        sa.Column("evidence_json", sa.Text(), nullable=True),
        # naive-UTC ISO, repo-wide convention.
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_standup_messages_user_sig",
        "standup_messages",
        ["user_id", "signature_sha"],
    )
    op.create_index(
        "ix_standup_messages_user_ticker_created",
        "standup_messages",
        ["user_id", "ticker", "created_at"],
    )
    op.create_index(
        "ix_standup_messages_user_status_created",
        "standup_messages",
        ["user_id", "status", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "standup_messages" not in insp.get_table_names():
        return
    op.drop_index("ix_standup_messages_user_status_created", table_name="standup_messages")
    op.drop_index("ix_standup_messages_user_ticker_created", table_name="standup_messages")
    op.drop_index("ix_standup_messages_user_sig", table_name="standup_messages")
    op.drop_table("standup_messages")
