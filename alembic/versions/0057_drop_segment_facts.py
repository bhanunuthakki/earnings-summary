"""drop_segment_facts — retire the legacy wide table; junction is the only home now.

`segment_facts` was the original single-dim segment-cell store (one row per
(ticker, period_end, segment_name, metric)). Migration 0055 added the
segment_periods + segment_dimensions junction, and the subsequent code pass
moved every reader + writer onto the junction. This migration finishes the
cutover by dropping the legacy table.

Carried over from segment_facts so prior work isn't lost:

  * `segment_entity_id` column + index — added to segment_dimensions and
    backfilled from segment_facts on (ticker, segment_name) match. Roughly
    14% of segment_facts rows had a non-null mapping; those flow into the
    matching dim rows.

  * `unit` column on segment_dimensions — nullable per-dim override of the
    period's unit. Lets a single (ticker, period_end, fpt, source_doc)
    period anchor host both `Unit.ACTUAL` cells (OI, capex) and `Unit.COUNT`
    cells (headcount) without the uniqueness conflict that PR #145 worked
    around. NULL means "inherit the period's unit"; non-NULL overrides.

  * Headcount data preservation — legacy headcount rows in `segment_facts`
    are migrated into `segment_dimensions` before the drop. Each one becomes
    a new dim row attached to the matching ACTUAL-unit period anchor (when
    one exists for the same ticker/period/source_doc), with `unit='count'`
    set on the dim itself. PR #145's `_mirror_facts_to_junction` skipped
    headcount, so without this step those rows would vanish on drop.

  * Brief-dirty + LLM-artifact-dirty signaling — the 0026 and 0043 triggers
    on segment_facts were auto-dropped along with the table. Equivalent
    triggers are re-created on segment_dimensions so a new junction write
    still flips `tracked_companies.brief_dirty` and the related
    `llm_artifacts.dirty` rows. The replacements only fire on INSERT — the
    segment_entity_id backfill from canonicalize_segments.py / seed_entity_graph.py
    runs UPDATE-only and intentionally does not trip brief regeneration.

Downgrade recreates `segment_facts` with the original 0004 DDL + the 0011
unique index + the 0037 segment_entity_id column + index + the 0026/0043
triggers, restoring the pre-0057 state exactly (modulo row content — the
data is gone once dropped, no attempt is made to reconstruct it).

Chained off `0056_earnings_themes_split_budget` (the orthogonal LLM-budget
seed migration that landed alongside PR #144).

Revision ID: 0057_drop_segment_facts
Revises: 0056_earnings_themes_split_budget
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0057_drop_segment_facts"
down_revision: str | Sequence[str] | None = "0056_earnings_themes_split_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Triggers we re-create on segment_dimensions to preserve the 0026 brief-dirty
# + 0043 llm-artifact-dirty signals that the dropped triggers used to provide.
_NEW_BRIEF_DIRTY_TRIGGER = "trg_dirty_segment_dimensions_ins"
_NEW_ARTIFACTS_DIRTY_TRIGGER = "trg_artifacts_dirty_on_segment_dimensions_insert"


def upgrade() -> None:
    bind = op.get_bind()

    # 1a. Extend segment_dimensions with the entity-spine FK column 0037 placed
    #     on segment_facts. Nullable — backfill populates it where possible.
    existing_dim_cols = {
        c["name"] for c in sa.inspect(bind).get_columns("segment_dimensions")
    }
    if "segment_entity_id" not in existing_dim_cols:
        op.add_column(
            "segment_dimensions",
            sa.Column("segment_entity_id", sa.Integer(), nullable=True),
        )
    # 1b. Add the per-dim unit override column. NULL → inherit period's unit;
    #     non-NULL → store the dim's actual unit. Lets a single period anchor
    #     host both Unit.ACTUAL (revenue / OI / capex) and Unit.COUNT
    #     (headcount) cells without violating the natural-key uniqueness on
    #     segment_periods.
    if "unit" not in existing_dim_cols:
        op.add_column(
            "segment_dimensions",
            sa.Column("unit", sa.String(length=16), nullable=True),
        )
    existing_dim_indexes = {
        idx["name"] for idx in sa.inspect(bind).get_indexes("segment_dimensions")
    }
    if "idx_segment_dimensions_entity" not in existing_dim_indexes:
        op.create_index(
            "idx_segment_dimensions_entity",
            "segment_dimensions",
            ["segment_entity_id"],
        )

    # 2a. Backfill segment_entity_id by joining the legacy table on
    #     (ticker, segment_name) ↔ (period.ticker, dim.dim_name). When a single
    #     legacy (ticker, segment_name) pair carried multiple non-null entity_ids
    #     (shouldn't happen but is defended-against), MAX() picks one
    #     deterministically.
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "segment_facts" in existing_tables:
        op.execute(
            """
            UPDATE segment_dimensions
            SET segment_entity_id = (
                SELECT MAX(sf.segment_entity_id)
                FROM segment_facts sf
                JOIN segment_periods sp ON sp.id = segment_dimensions.period_id
                WHERE sf.ticker = sp.ticker
                  AND sf.segment_name = segment_dimensions.dim_name
                  AND sf.segment_entity_id IS NOT NULL
            )
            WHERE segment_entity_id IS NULL
            """
        )

        # 2b. Migrate headcount rows out of segment_facts. PR #145's mirror
        #     skipped them because the junction's period anchor couldn't carry
        #     a second unit; the per-dim unit column (added above) lifts that
        #     restriction. For each legacy (ticker, period_end, fpt,
        #     source_doc_id) tuple that has an existing ACTUAL-unit period
        #     anchor, INSERT one new segment_dimensions row per headcount
        #     fact with unit='count'. Idempotent: a re-run finds no remaining
        #     legacy headcount rows (the table is dropped below).
        op.execute(
            """
            INSERT INTO segment_dimensions
                (period_id, dim_type, dim_name, value, metric, unit, segment_entity_id)
            SELECT
                sp.id AS period_id,
                'business_unit' AS dim_type,
                sf.segment_name AS dim_name,
                sf.value AS value,
                'headcount' AS metric,
                'count' AS unit,
                sf.segment_entity_id AS segment_entity_id
            FROM segment_facts sf
            JOIN segment_periods sp
              ON sp.ticker = sf.ticker
             AND sp.period_end = sf.period_end
             AND sp.fiscal_period_type = sf.fiscal_period_type
             AND sp.source_doc_id = sf.source_doc_id
            WHERE sf.unit = 'count'
              AND sf.metric = 'headcount'
              AND NOT EXISTS (
                SELECT 1 FROM segment_dimensions sd
                WHERE sd.period_id = sp.id
                  AND sd.dim_type = 'business_unit'
                  AND sd.dim_name = sf.segment_name
                  AND sd.metric = 'headcount'
              )
            """
        )

    # 3. Re-create the brief-dirty trigger that lived on segment_facts. The
    #    dim row has no ticker column; resolve via the period FK in a
    #    subquery. INSERT only — UPDATE on segment_dimensions is dominated by
    #    the segment_entity_id backfill, which is metadata churn, not a fact
    #    change.
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_NEW_BRIEF_DIRTY_TRIGGER}
        AFTER INSERT ON segment_dimensions
        FOR EACH ROW
        BEGIN
            UPDATE tracked_companies
            SET brief_dirty = 1
            WHERE ticker = (
                SELECT ticker FROM segment_periods WHERE id = NEW.period_id
            );
        END
        """
    )

    # 4. Re-create the llm_artifacts dirty trigger from 0043.
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_NEW_ARTIFACTS_DIRTY_TRIGGER}
        AFTER INSERT ON segment_dimensions
        BEGIN
            UPDATE llm_artifacts
            SET dirty = 1, dirty_reason = 'segment_dimensions_insert'
            WHERE ticker = (
                SELECT ticker FROM segment_periods WHERE id = NEW.period_id
            )
              AND superseded_by_id IS NULL
              AND dirty = 0
              AND purpose IN ('bear_case', 'company_description');
        END
        """
    )

    # 5. Drop the legacy table. SQLite auto-drops the segment_facts triggers
    #    and indexes (uq_segment_facts_provenance, ix_segment_facts_ticker_segment_period,
    #    idx_segment_facts_entity) as part of DROP TABLE — no explicit drops needed.
    if "segment_facts" in existing_tables:
        op.drop_table("segment_facts")


def downgrade() -> None:
    bind = op.get_bind()

    # 1. Drop the replacement triggers on segment_dimensions before recreating
    #    the segment_facts table — keep the trigger state consistent with the
    #    pre-0056 world.
    op.execute(f"DROP TRIGGER IF EXISTS {_NEW_ARTIFACTS_DIRTY_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_NEW_BRIEF_DIRTY_TRIGGER}")

    # 2. Recreate segment_facts with the union of the 0004 (base) + 0037
    #    (segment_entity_id) schema. Indexes are recreated separately below
    #    so the order matches the original migration chain.
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "segment_facts" not in existing_tables:
        op.create_table(
            "segment_facts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("fiscal_period_type", sa.String(), nullable=False),
            sa.Column("segment_name", sa.String(), nullable=False),
            sa.Column("metric", sa.String(), nullable=False),
            sa.Column("value", sa.Numeric(precision=24, scale=6), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=True),
            sa.Column("unit", sa.String(), nullable=False),
            sa.Column(
                "source_doc_id",
                sa.Integer(),
                sa.ForeignKey("documents.id"),
                nullable=False,
            ),
            sa.Column("segment_entity_id", sa.Integer(), nullable=True),
        )
        # 0004's secondary index
        op.create_index(
            "ix_segment_facts_ticker_segment_period",
            "segment_facts",
            ["ticker", "segment_name", "period_end"],
        )
        # 0011's UNIQUE provenance index
        op.create_index(
            "uq_segment_facts_provenance",
            "segment_facts",
            [
                "ticker",
                "period_end",
                "fiscal_period_type",
                "segment_name",
                "metric",
                "source_doc_id",
            ],
            unique=True,
        )
        # 0037's entity FK index
        op.create_index(
            "idx_segment_facts_entity",
            "segment_facts",
            ["segment_entity_id"],
        )

    # 3. Recreate the 0026 brief-dirty triggers on segment_facts.
    for trigger_name, event in (
        ("trg_dirty_segment_facts_ins", "INSERT"),
        ("trg_dirty_segment_facts_upd", "UPDATE"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_name}
            AFTER {event} ON segment_facts
            FOR EACH ROW
            BEGIN
                UPDATE tracked_companies
                SET brief_dirty = 1
                WHERE ticker = NEW.ticker;
            END
            """
        )

    # 4. Recreate the 0043 artifacts-dirty trigger on segment_facts.
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_segment_insert
        AFTER INSERT ON segment_facts
        BEGIN
            UPDATE llm_artifacts
            SET dirty = 1, dirty_reason = 'segment_facts_insert'
            WHERE ticker = NEW.ticker
              AND superseded_by_id IS NULL
              AND dirty = 0
              AND purpose IN ('bear_case', 'company_description');
        END
        """
    )

    # 5. Roll back the segment_dimensions column additions (entity FK + per-dim
    #    unit). batch_alter_table groups multiple ALTERs so SQLite only rebuilds
    #    the table once.
    existing_dim_indexes = {
        idx["name"] for idx in sa.inspect(bind).get_indexes("segment_dimensions")
    }
    if "idx_segment_dimensions_entity" in existing_dim_indexes:
        op.drop_index(
            "idx_segment_dimensions_entity", table_name="segment_dimensions"
        )
    existing_dim_cols = {
        c["name"] for c in sa.inspect(bind).get_columns("segment_dimensions")
    }
    cols_to_drop = [
        c for c in ("segment_entity_id", "unit") if c in existing_dim_cols
    ]
    if cols_to_drop:
        with op.batch_alter_table("segment_dimensions") as batch:
            for col in cols_to_drop:
                batch.drop_column(col)
