"""strategic_targets — long-term forward guidance + capital allocation framework.

The investor-deck extractor (src/table_extractors/investor_decks.py) reads
cached IR decks under `ir_documents/<TICKER>/<period>/` and surfaces every
multi-year target a filer commits to: revenue trajectory, FCF/OI margin
targets, segment KPIs, buyback authorizations, dividend policy,
M&A intent, qualitative strategic priorities.

Today these commitments are buried in PDF slides and not searchable.
Promoting them to typed rows lets:
  * the bear case anchor inline "this is what management has on record"
    so failure-mode analysis grounds in real targets, not the LLM's prior;
  * subsequent quarters' actuals get auto-compared (Phase 2 — separate PR);
  * cross-ticker queries answer "which holdings have multi-year FCF targets?".

Also merges the two parallel alembic heads (0047 doc-table-extractions and
0051 tracked-processing-tier) back into a single chain so the next migration
has one predecessor.

Revision ID: 0053_strategic_targets
Revises: 0047_document_table_extractions, 0051_tracked_processing_tier
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0053_strategic_targets"
down_revision: str | Sequence[str] | None = (
    "0047_document_table_extractions",
    "0051_tracked_processing_tier",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "strategic_targets" not in existing:
        op.create_table(
            "strategic_targets",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            # FK to documents.id; the deck the row was extracted from. Nullable
            # for the rare case where the file is on disk but not yet registered
            # in documents (categorize_ir_uploads.py hasn't run, etc.) — the row
            # still lands so the analyst can use it.
            sa.Column("deck_doc_id", sa.Integer(), nullable=True),
            # Enum of supported target shapes:
            #   revenue | fcf_margin | oi_margin | gross_margin | segment_kpi |
            #   headcount | m_a_intent | buyback_authorization | dividend_policy |
            #   capital_return_pct | capex_intent | strategic_priority
            sa.Column("target_kind", sa.String(length=32), nullable=False),
            # NULL for qualitative targets (m_a_intent, strategic_priority,
            # dividend_policy without a specific $ figure).
            sa.Column("target_value", sa.Numeric(20, 4), nullable=True),
            # '%' | 'USD_M' | 'count' | 'qualitative'. Plus ISO codes for
            # non-USD filers (EUR_M, DKK_M, BRL_M).
            sa.Column("target_unit", sa.String(length=16), nullable=False),
            # 'FY2027' | 'LT' (long-term, no specific year) | '2030' | 'QoQ' |
            # 'next 5 years', etc. Kept as string because filers use many shapes
            # and forcing-to-date loses information.
            sa.Column("target_period", sa.String(length=16), nullable=False),
            sa.Column("target_currency", sa.String(length=8), nullable=True),
            sa.Column("narrative_excerpt", sa.Text(), nullable=False),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        )
        op.create_index(
            "idx_strategic_targets_ticker", "strategic_targets", ["ticker"]
        )
        op.create_index(
            "idx_strategic_targets_kind",
            "strategic_targets",
            ["ticker", "target_kind"],
        )
        op.create_index(
            "idx_strategic_targets_deck", "strategic_targets", ["deck_doc_id"]
        )
        # Functional unique index — the leading 128 chars of narrative_excerpt
        # are part of the key so cosmetic whitespace/punctuation changes in
        # re-extraction don't multiply rows. SQLite supports expression
        # uniqueness via INDEX (not via UniqueConstraint).
        op.execute(
            """
            CREATE UNIQUE INDEX uq_strategic_targets
            ON strategic_targets (
                ticker,
                deck_doc_id,
                target_kind,
                target_period,
                substr(narrative_excerpt, 1, 128)
            )
            """
        )

    # Seed the investor_deck_extraction budget if llm_budgets exists. Idempotent
    # via ON CONFLICT DO NOTHING — operator-edited caps survive.
    if "llm_budgets" in existing:
        now = datetime.now(UTC).isoformat()
        bind.execute(
            sa.text(
                """
                INSERT INTO llm_budgets
                    (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                     created_at, updated_at, notes)
                VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
                ON CONFLICT(purpose) DO NOTHING
                """
            ),
            {
                "purpose": "investor_deck_extraction",
                "cap": 5.00,
                "now": now,
                "notes": "seeded by migration 0053 (investor-deck extractor)",
            },
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM llm_budgets WHERE purpose = 'investor_deck_extraction'"
    )
    op.execute("DROP INDEX IF EXISTS uq_strategic_targets")
    op.drop_index("idx_strategic_targets_deck", table_name="strategic_targets")
    op.drop_index("idx_strategic_targets_kind", table_name="strategic_targets")
    op.drop_index("idx_strategic_targets_ticker", table_name="strategic_targets")
    op.drop_table("strategic_targets")
