"""Add immutable earnings-surprise observations and quarantine.

Revision ID: 0007_add_earnings_surprise_observations
Revises: 0006_add_ask_proposal_approval
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_add_earnings_surprise_observations"
down_revision = "0006_add_ask_proposal_approval"
branch_labels = None
depends_on = None


def _surprise_columns() -> set[str]:
    bind = op.get_bind()
    return {
        str(row[1])
        for row in bind.exec_driver_sql("PRAGMA table_info(earnings_surprises)").fetchall()
    }


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_surprise_observations (
            observation_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            release_date TEXT NOT NULL,
            eps_estimate NUMERIC,
            eps_actual NUMERIC,
            revenue_estimate NUMERIC,
            revenue_actual NUMERIC,
            eps_surprise_pct NUMERIC,
            revenue_surprise_pct NUMERIC,
            num_analysts_eps INTEGER,
            num_analysts_revenue INTEGER,
            source_name TEXT NOT NULL,
            source_url TEXT,
            fetched_at TEXT NOT NULL,
            cache_path TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL,
            raw_payload_json TEXT NOT NULL,
            raw_payload_sha256 TEXT NOT NULL,
            canonical_payload_json TEXT NOT NULL,
            canonical_payload_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            CHECK(length(observation_id) = 64),
            CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            CHECK(length(ticker) BETWEEN 1 AND 16 AND ticker = upper(ticker)),
            CHECK(release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
            CHECK(record_ordinal >= 0),
            CHECK(json_valid(raw_payload_json)),
            CHECK(json_valid(canonical_payload_json)
                  AND json_type(canonical_payload_json) = 'object'),
            CHECK(length(raw_payload_sha256) = 64),
            CHECK(length(canonical_payload_sha256) = 64)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_earnings_surprise_observations_ticker_release "
        "ON earnings_surprise_observations(ticker, release_date, fetched_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_surprise_quarantine (
            quarantine_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            ticker_hint TEXT,
            cache_path TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL,
            raw_payload_json TEXT NOT NULL,
            raw_payload_sha256 TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason_details_json TEXT NOT NULL,
            reason_details_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            CHECK(length(quarantine_id) = 64),
            CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            CHECK(record_ordinal >= -1),
            CHECK(json_valid(raw_payload_json)),
            CHECK(json_valid(reason_details_json)
                  AND json_type(reason_details_json) = 'object'),
            CHECK(length(raw_payload_sha256) = 64),
            CHECK(length(reason_details_sha256) = 64)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_earnings_surprise_quarantine_ticker_recorded "
        "ON earnings_surprise_quarantine(ticker_hint, recorded_at)"
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_earnings_surprise_observations_no_update
        BEFORE UPDATE ON earnings_surprise_observations
        BEGIN
            SELECT RAISE(ABORT, 'earnings surprise observations are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_earnings_surprise_observations_no_delete
        BEFORE DELETE ON earnings_surprise_observations
        BEGIN
            SELECT RAISE(ABORT, 'earnings surprise observations are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_earnings_surprise_quarantine_no_update
        BEFORE UPDATE ON earnings_surprise_quarantine
        BEGIN
            SELECT RAISE(ABORT, 'earnings surprise quarantine is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_earnings_surprise_quarantine_no_delete
        BEFORE DELETE ON earnings_surprise_quarantine
        BEGIN
            SELECT RAISE(ABORT, 'earnings surprise quarantine is immutable');
        END
        """
    )
    if "source_observation_id" not in _surprise_columns():
        op.add_column("earnings_surprises", sa.Column("source_observation_id", sa.Text()))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_earnings_surprises_source_observation "
        "ON earnings_surprises(source_observation_id)"
    )
    for action in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_earnings_surprises_{action.lower()}_lineage
            BEFORE {action} ON earnings_surprises
            WHEN NEW.source_observation_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM earnings_surprise_observations observation
                WHERE observation.observation_id = NEW.source_observation_id
                  AND observation.ticker = NEW.ticker
                  AND observation.release_date = NEW.release_date
            )
            BEGIN
                SELECT RAISE(ABORT, 'earnings surprise projection requires source observation');
            END
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_earnings_surprises_update_lineage")
    op.execute("DROP TRIGGER IF EXISTS trg_earnings_surprises_insert_lineage")
    op.execute("DROP INDEX IF EXISTS ix_earnings_surprises_source_observation")
    if "source_observation_id" in _surprise_columns():
        op.drop_column("earnings_surprises", "source_observation_id")
    op.execute("DROP TRIGGER IF EXISTS trg_earnings_surprise_quarantine_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_earnings_surprise_quarantine_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_earnings_surprise_observations_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_earnings_surprise_observations_no_update")
    op.execute("DROP INDEX IF EXISTS ix_earnings_surprise_quarantine_ticker_recorded")
    op.execute("DROP TABLE IF EXISTS earnings_surprise_quarantine")
    op.execute("DROP INDEX IF EXISTS ix_earnings_surprise_observations_ticker_release")
    op.execute("DROP TABLE IF EXISTS earnings_surprise_observations")
