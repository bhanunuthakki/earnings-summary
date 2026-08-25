"""Add explicit sizing supersessions and approve the current MELI intent.

Revision ID: 0027_add_sizing_intent_supersessions
Revises: 0026_consolidate_earnings_prep_notes
"""

from __future__ import annotations

from alembic import op

revision = "0027_add_sizing_intent_supersessions"
down_revision = "0026_consolidate_earnings_prep_notes"
branch_labels = None
depends_on = None

_MELI_NARRATIVE = (
    "Base target 15% of portfolio. A temporary ceiling of 20% is permitted only if the "
    "2026-08-05 Q2 print was lived through with the thesis intact; any 15%-20% overweight "
    "reverts to 15% at the next quarterly review unless actively re-justified in writing. "
    "Keep NU + MELI at or below 35% combined, recognizing both as one correlated LatAm "
    "consumer-credit sleeve. Carried-forward price ladder from the 2026-07-31 SOTP: add "
    "below $1,743.04 toward the band maximum; deep-add below $1,389.16 for +1% of portfolio; "
    "hold from $1,743.04 to $2,178.81; above $2,178.81, trim to 15% only when also above "
    "the band; above $2,614.57, trim regardless of band. Break rules remain thesis-exit "
    "detectors, not ordinary sizing triggers. Revalidate all valuation-derived price levels "
    "before acting; later DCF-derived ladders supersede these price levels without erasing "
    "this owner-approved history."
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE position_sizing_intent_supersessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES tenants(id),
            superseded_intent_id INTEGER NOT NULL REFERENCES position_sizing_intent(id),
            superseding_intent_id INTEGER NOT NULL REFERENCES position_sizing_intent(id),
            reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
            created_at TEXT NOT NULL CHECK(datetime(created_at) IS NOT NULL),
            CHECK(superseded_intent_id <> superseding_intent_id),
            UNIQUE(user_id, superseded_intent_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sizing_intent_supersessions_current ON "
        "position_sizing_intent_supersessions(user_id,superseding_intent_id)"
    )
    op.execute(
        "CREATE TRIGGER trg_sizing_intent_supersessions_no_update BEFORE UPDATE ON "
        "position_sizing_intent_supersessions BEGIN "
        "SELECT RAISE(ABORT, 'sizing intent supersessions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_sizing_intent_supersessions_validate_insert BEFORE INSERT ON "
        "position_sizing_intent_supersessions WHEN NOT EXISTS ("
        "SELECT 1 FROM position_sizing_intent AS old "
        "JOIN position_sizing_intent AS current "
        "ON current.id=NEW.superseding_intent_id "
        "WHERE old.id=NEW.superseded_intent_id "
        "AND old.user_id=NEW.user_id AND current.user_id=NEW.user_id "
        "AND old.ticker=current.ticker"
        ") BEGIN SELECT RAISE(ABORT, "
        "'sizing supersession rows must share one owner and ticker'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_sizing_intent_supersessions_no_delete BEFORE DELETE ON "
        "position_sizing_intent_supersessions BEGIN "
        "SELECT RAISE(ABORT, 'sizing intent supersessions are append-only'); END"
    )
    escaped = _MELI_NARRATIVE.replace("'", "''")
    op.execute(
        f"""
        INSERT INTO position_sizing_intent(
            user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at
        )
        SELECT 'bhanu','MELI','target_weight_pct',15.0,'{escaped}',
               '2026-08-25T00:00:00','2026-08-25T00:00:00'
        WHERE EXISTS (SELECT 1 FROM tenants WHERE id='bhanu')
          AND EXISTS (
              SELECT 1 FROM position_sizing_intent AS prior
              WHERE prior.user_id='bhanu' AND prior.ticker='MELI'
                AND (
                    (prior.intent_kind='target_weight_pct'
                     AND abs(prior.intent_value - 15.0) < 0.000001
                     AND prior.narrative LIKE 'Two-sided position band (owner-authored 2026-08-03;%')
                    OR
                    (prior.intent_kind='add_rung'
                     AND abs(prior.intent_value - 1389.16) < 0.000001
                     AND prior.narrative LIKE '[RATIFIED by owner 2026-08-03;%')
                )
          )
          AND NOT EXISTS (
              SELECT 1 FROM position_sizing_intent
              WHERE user_id='bhanu' AND ticker='MELI'
                AND intent_kind='target_weight_pct'
                AND created_at='2026-08-25T00:00:00'
                AND narrative='{escaped}'
          )
        """
    )
    op.execute(
        f"""
        INSERT OR IGNORE INTO position_sizing_intent_supersessions(
            user_id,superseded_intent_id,superseding_intent_id,reason,created_at
        )
        SELECT
            old.user_id,
            old.id,
            current.id,
            'Consolidated into the owner-approved 2026-08-25 MELI sizing intent.',
            '2026-08-25T00:00:00'
        FROM position_sizing_intent AS old
        JOIN position_sizing_intent AS current
          ON current.user_id=old.user_id
         AND current.ticker='MELI'
         AND current.intent_kind='target_weight_pct'
         AND current.created_at='2026-08-25T00:00:00'
         AND current.narrative='{escaped}'
        WHERE old.user_id='bhanu'
          AND old.ticker='MELI'
          AND old.id <> current.id
          AND (
              (old.intent_kind='target_weight_pct' AND abs(old.intent_value - 15.0) < 0.000001
               AND old.narrative LIKE 'Two-sided position band (owner-authored 2026-08-03;%')
              OR
              (old.intent_kind='add_rung' AND abs(old.intent_value - 1389.16) < 0.000001
               AND old.narrative LIKE '[RATIFIED by owner 2026-08-03;%')
          )
        """
    )


def downgrade() -> None:
    escaped = _MELI_NARRATIVE.replace("'", "''")
    op.execute(
        f"""
        INSERT OR IGNORE INTO position_sizing_intent_withdrawals(
            user_id,sizing_intent_id,reason,created_at
        )
        SELECT user_id,id,
               'Migration 0027 rolled back; retained as inactive owner-approved audit evidence.',
               CURRENT_TIMESTAMP
        FROM position_sizing_intent
        WHERE user_id='bhanu' AND ticker='MELI'
          AND intent_kind='target_weight_pct'
          AND created_at='2026-08-25T00:00:00'
          AND narrative='{escaped}'
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_sizing_intent_supersessions_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_sizing_intent_supersessions_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_sizing_intent_supersessions_no_update")
    op.execute("DROP INDEX IF EXISTS ix_sizing_intent_supersessions_current")
    op.execute("DROP TABLE IF EXISTS position_sizing_intent_supersessions")
