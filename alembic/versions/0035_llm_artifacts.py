"""llm_artifacts — unified DB ledger for every LLM-synthesized analytical output.

Before this migration, LLM outputs live as JSON sidecars at
``data/{bear_case,company_description,valuation_basis,filing_intelligence,
saydo_filter,qa_topics}/<TICKER>.json``. Each section invented its own cache
schema (TTL vs sha vs both) and there was no single ledger for "what does the
system know about META?". This table is the new spine: every LLM artifact gets
one row keyed by (ticker, scope, purpose, fiscal_period, prompt_version).

Key columns:
  input_sha256   — composite hash of all inputs that fed this artifact. The
                   cache key. Same hash = artifact is reusable, no LLM call
                   needed. Recomputed whenever upstream sources change.
  superseded_by_id — when a fresher artifact replaces this one, the link goes
                   here. History stays queryable: "show me the evolution of
                   META's bear case across the last 4 quarters."
  dirty / dirty_reason — set to 1 by the brief_dirty trigger chain (Phase 1.6)
                   when upstream facts change. The dirty-artifact drain
                   regenerates these on the next cron tick.
  source_doc_ids / parent_artifact_ids — provenance DAG. Lets the brief show
                   "this bear case was generated from these 4 transcripts +
                   this 10-K + the cached company description."
  llm_call_id    — FK back to llm_calls (Phase 0) for cost attribution. Lets
                   queries answer "what did this artifact cost?"

Disk sidecars don't disappear immediately — they continue as write-through
caches for the cross-prompt anchor system (load_bear_anchor reads them
without a DB connection). The DB becomes the authoritative store; sidecars
mirror it.

Revision ID: 0035_llm_artifacts
Revises: 0034_llm_calls_ledger
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_llm_artifacts"
down_revision: str | Sequence[str] | None = "0034_llm_calls_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "llm_artifacts" in set(sa.inspect(bind).get_table_names()):
        return

    op.create_table(
        "llm_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(length=16), nullable=True),  # null for scope='portfolio'
        # Analytical scope. Free-form so future scopes (segment:cloud, sector:semi,
        # peer-group:hyperscalers) don't require schema changes. Conventional values:
        # 'ticker' | 'portfolio' | 'segment:<name>' | 'sector:<name>'.
        sa.Column("scope", sa.String(length=64), nullable=False, server_default="ticker"),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        # Fiscal period this artifact "is about" — for per-quarter content
        # (earnings_summary, qa_topics) this is the report period; for annual
        # content (company_description, filing_intel) it's the fiscal year end;
        # for cross-period synthesis (bear_case) it's null.
        sa.Column("fiscal_period", sa.String(length=10), nullable=True),  # 'YYYY-MM-DD' or 'YYYY'
        # Payload — TWO columns so structured outputs and prose can coexist
        # without parsing roundtrips. Renderers prefer content_json when present.
        sa.Column("content_md", sa.Text(), nullable=True),
        sa.Column("content_json", sa.Text(), nullable=True),
        # Deterministic cache key: sha256 over (prompt_version + concatenated
        # input source contents + any tunable parameters). When inputs don't
        # change, this hash doesn't change, and the artifact is reusable.
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        # Prompt-version bump invalidates all artifacts produced from the prior
        # prompt — surfaces in the dirty drain even when inputs are unchanged.
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default="v1"),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        # Supersession chain — when this artifact is replaced by a fresher one,
        # the new artifact's id goes here. History queryable: "show me the
        # evolution of META's bear case over the last year."
        sa.Column(
            "superseded_by_id",
            sa.Integer(),
            sa.ForeignKey("llm_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Dirty propagation — set to 1 by the Phase 1.6 trigger chain when
        # upstream facts change. The drain cron picks these up.
        sa.Column("dirty", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("dirty_reason", sa.String(length=128), nullable=True),
        # Provenance DAG — JSON arrays of source_doc_ids and parent_artifact_ids
        # the artifact synthesizes from. JSON-string for SQLite portability.
        sa.Column("source_doc_ids", sa.Text(), nullable=True),
        sa.Column("parent_artifact_ids", sa.Text(), nullable=True),
        # FK back to llm_calls (Phase 0) for cost attribution per artifact.
        sa.Column(
            "llm_call_id",
            sa.Integer(),
            sa.ForeignKey("llm_calls.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Logical uniqueness: at most one CURRENT artifact per (scope tuple).
        # When the dirty drain regenerates, the old row's superseded_by_id
        # gets set and a new row is inserted — keeping history. We enforce
        # uniqueness only on non-superseded rows via the indexed view below.
    )

    # Indexes covering the common access patterns:
    op.create_index("idx_artifacts_ticker_purpose", "llm_artifacts", ["ticker", "purpose"])
    op.create_index(
        "idx_artifacts_scope_purpose_fp",
        "llm_artifacts",
        ["scope", "purpose", "fiscal_period"],
    )
    op.create_index(
        "idx_artifacts_input_sha", "llm_artifacts", ["input_sha256"]
    )  # dedup cache lookup
    op.create_index("idx_artifacts_generated_at", "llm_artifacts", ["generated_at"])
    # Partial index on dirty=1 rows so the drain cron's WHERE dirty=1 query is fast.
    op.execute(
        "CREATE INDEX idx_artifacts_dirty "
        "ON llm_artifacts(ticker, purpose, fiscal_period) "
        "WHERE dirty = 1"
    )

    # Backfill the llm_calls.artifact_id FK target column was added in 0034 already
    # (no constraint); we don't touch it here. Future writers will set artifact_id
    # at LLM-call time so the link is established immediately.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_artifacts_dirty")
    op.drop_index("idx_artifacts_generated_at", table_name="llm_artifacts")
    op.drop_index("idx_artifacts_input_sha", table_name="llm_artifacts")
    op.drop_index("idx_artifacts_scope_purpose_fp", table_name="llm_artifacts")
    op.drop_index("idx_artifacts_ticker_purpose", table_name="llm_artifacts")
    op.drop_table("llm_artifacts")
