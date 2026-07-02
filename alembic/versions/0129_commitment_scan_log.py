"""commitment_scan_log + saydo_commitment_extract budget seed.

The SayDo commitment extractor (compute/say_do_extractor.py) selected targets
by "transcript has no management_commitments rows" — so every transcript that
legitimately yields ZERO commitments (most of the 96-ticker fetch universe)
was re-scanned by the daily backfill_transcripts cron forever, at 25-50K
prompt chars per Sonnet call. Combined with the call running through the
ungoverned private _call_claude helper (purpose=NULL, no budget), this was
the ledger's single largest anonymous cost line (~$150/mo, ~2.8 scans per
unique transcript per week).

Two seeds here:
  * ``commitment_scan_log`` — one row per transcript ever scanned, including
    zero-commitment scans. Selection adds NOT EXISTS(scan) so a scan happens
    once per (transcript, prompt_version). ``prompt_version`` is recorded so
    a prompt bump can invalidate old scans explicitly.
  * ``llm_budgets`` row for ``saydo_commitment_extract`` — warn mode (batch
    pipeline: a blown cap must surface on the dashboard, not silently stall
    the SayDo pipeline). $40/mo covers the one-time backlog pass of
    never-marked transcripts plus a generous steady-state trickle.

Idempotent: table create guarded by inspector, budget seed ON CONFLICT DO
NOTHING; both skip when the target table is absent (hand-built fixture DBs).

Revision ID: 0129_commitment_scan_log
Revises: 0128_restore_analyst_notes_fts_triggers
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0129_commitment_scan_log"
down_revision: str | Sequence[str] | None = "0128_restore_analyst_notes_fts_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "saydo_commitment_extract"
_BUDGET_CAP_USD = 40.00
_BUDGET_NOTES = (
    "seeded by 0129 — SayDo commitment extraction (warn mode: batch pipeline, "
    "cap hit surfaces on the dashboard; was ungoverned purpose=NULL pre-0129)"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "commitment_scan_log" not in tables:
        op.create_table(
            "commitment_scan_log",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("transcript_id", sa.Integer, nullable=False, unique=True),
            sa.Column("scanned_at", sa.Text, nullable=False),
            sa.Column("n_extracted", sa.Integer, nullable=False),
            sa.Column("prompt_version", sa.Text, nullable=True),
        )
        op.create_index(
            "ix_commitment_scan_log_transcript",
            "commitment_scan_log",
            ["transcript_id"],
            unique=True,
        )

    if "llm_budgets" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'warn', :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    else:  # pre-0066 shape (hand-built fixture DBs)
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    bind.execute(
        sa.text(sql),
        {"purpose": _BUDGET_PURPOSE, "cap": _BUDGET_CAP_USD, "now": now, "notes": _BUDGET_NOTES},
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "commitment_scan_log" in tables:
        op.drop_table("commitment_scan_log")
    if "llm_budgets" in tables:
        bind.execute(sa.text("DELETE FROM llm_budgets WHERE purpose = :p"), {"p": _BUDGET_PURPOSE})
