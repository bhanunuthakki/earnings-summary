"""llm_calls_ledger — one row per LLM invocation across the pipeline.

Captures: prompt sha + size, response sha + size, token usage broken out by
cache-create / cache-read / output, model, elapsed_ms, cost (from CLI), cache
hit boolean, fallback flag (Gemini), error text, and a run_id grouping calls
that belong to the same logical refresh.

Read by execution/show_llm_spend.py to surface weekly cost by purpose, cache-
hit rate, p50/p95 latency, and the top prompt-sha repeats (dedup candidates).

The table is the cost-observability spine; Phase 1's llm_artifacts will FK
its artifact_id back here once that migration lands. Nullable today.

Revision ID: 0034_llm_calls_ledger
Revises: b79cec08ce5b
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_llm_calls_ledger"
down_revision: str | Sequence[str] | None = "b79cec08ce5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {t for t in sa.inspect(bind).get_table_names()}
    if "llm_calls" in existing:
        return

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # When the call started (UTC). Set by the writer, not DB default — the
        # writer's clock is what matters for latency math, and SQLite's
        # CURRENT_TIMESTAMP is second-resolution.
        sa.Column("called_at", sa.DateTime(), nullable=False),
        # Purpose key from llm_client.LLM_MODELS (e.g. "bear_case",
        # "company_description"). Nullable for legacy callers that haven't been
        # wired through call_llm(purpose=...) yet — those rows still capture
        # cost/latency.
        sa.Column("purpose", sa.String(length=64), nullable=True),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        # Optional analytical scope ("ticker:META", "portfolio", "segment:cloud").
        # Lets the spend report group cross-ticker work separately.
        sa.Column("scope", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        # SHA-256 of the exact prompt text sent. The dedup signal: same hash =
        # we could have served a cached output.
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("prompt_chars", sa.Integer(), nullable=False),
        sa.Column("response_chars", sa.Integer(), nullable=True),
        # Token usage parsed from claude -p --output-format json.
        # input_tokens is "fresh" tokens (not cache). cache_creation_input_tokens
        # are written into Anthropic's prefix cache (1.25x cost). cache_read are
        # served from cache (0.1x cost). Together they reveal cache effectiveness.
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        # Anthropic-computed total cost for this call (already includes cache
        # discounts and any web-search surcharges). Authoritative — don't try
        # to re-derive from token counts.
        sa.Column("cost_estimate_usd", sa.Float(), nullable=True),
        # True when we short-circuited the LLM call (returned a cached artifact
        # without calling out). Phase 1 enables this; Phase 0 always records 0.
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("fallback_used", sa.String(length=16), nullable=True),  # 'gemini' | NULL
        # FK target lands in Phase 1 (llm_artifacts). Nullable column today;
        # no FK constraint yet so this migration is self-contained.
        sa.Column("artifact_id", sa.Integer(), nullable=True),
        # When the call errored (CLI non-zero, timeout, empty stdout, JSON parse
        # failure, Gemini also down). Cost/usage may be NULL on error rows.
        sa.Column("error", sa.Text(), nullable=True),
        # Groups calls that belong to a single logical refresh (e.g. one
        # build_artifacts run). Free-form string set by the caller; uuid4 hex
        # is conventional. Nullable so ad-hoc one-off calls can omit it.
        sa.Column("run_id", sa.String(length=64), nullable=True),
    )
    op.create_index("idx_llm_calls_called_at", "llm_calls", ["called_at"])
    op.create_index("idx_llm_calls_purpose_ticker", "llm_calls", ["purpose", "ticker"])
    op.create_index("idx_llm_calls_prompt_sha", "llm_calls", ["prompt_sha256"])
    op.create_index("idx_llm_calls_run_id", "llm_calls", ["run_id"])


def downgrade() -> None:
    op.drop_index("idx_llm_calls_run_id", table_name="llm_calls")
    op.drop_index("idx_llm_calls_prompt_sha", table_name="llm_calls")
    op.drop_index("idx_llm_calls_purpose_ticker", table_name="llm_calls")
    op.drop_index("idx_llm_calls_called_at", table_name="llm_calls")
    op.drop_table("llm_calls")
