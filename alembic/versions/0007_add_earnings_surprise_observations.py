"""Add immutable earnings-surprise observations and quarantine.

Revision ID: 0007_add_earnings_surprise_observations
Revises: 0006_add_ask_proposal_approval
Create Date: 2026-08-11
"""

from __future__ import annotations

import hashlib
import json

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


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _backfill_legacy_projections() -> None:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        """
        SELECT id,ticker,release_date,eps_estimate,eps_actual,revenue_estimate,
               revenue_actual,eps_surprise_pct,revenue_surprise_pct,
               num_analysts_eps,num_analysts_revenue,source_name,source_url,
               fetched_at,ingested_at
        FROM earnings_surprises
        WHERE source_observation_id IS NULL
        ORDER BY id
        """
    ).mappings()
    for row in rows:
        payload = {
            "eps_actual": row["eps_actual"],
            "eps_estimate": row["eps_estimate"],
            "eps_surprise_pct": row["eps_surprise_pct"],
            "fetched_at": str(row["fetched_at"]),
            "num_analysts_eps": row["num_analysts_eps"],
            "num_analysts_revenue": row["num_analysts_revenue"],
            "provenance_status": "legacy_projection_uncertain",
            "release_date": str(row["release_date"]),
            "revenue_actual": row["revenue_actual"],
            "revenue_estimate": row["revenue_estimate"],
            "revenue_surprise_pct": row["revenue_surprise_pct"],
            "source_name": str(row["source_name"]),
            "source_url": row["source_url"],
            "ticker": str(row["ticker"]),
        }
        canonical_json = _canonical_json(payload)
        observation_id = _sha256(canonical_json)
        generation_id = f"legacy-projection:{observation_id}"
        bind.exec_driver_sql(
            """
            INSERT OR IGNORE INTO earnings_surprise_observations (
                observation_id,idempotency_key,ticker,release_date,eps_estimate,
                eps_actual,revenue_estimate,revenue_actual,eps_surprise_pct,
                revenue_surprise_pct,num_analysts_eps,num_analysts_revenue,
                source_name,source_url,fetched_at,cache_path,record_ordinal,
                raw_payload_json,raw_payload_sha256,canonical_payload_json,
                canonical_payload_sha256,provenance_status,ingestion_run_id,
                cache_generation_id,recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observation_id,
                f"earnings-surprise-observation:{observation_id}",
                row["ticker"],
                row["release_date"],
                row["eps_estimate"],
                row["eps_actual"],
                row["revenue_estimate"],
                row["revenue_actual"],
                row["eps_surprise_pct"],
                row["revenue_surprise_pct"],
                row["num_analysts_eps"],
                row["num_analysts_revenue"],
                row["source_name"],
                row["source_url"],
                str(row["fetched_at"]),
                "legacy://earnings_surprises",
                int(row["id"]),
                canonical_json,
                _sha256(canonical_json),
                canonical_json,
                observation_id,
                "legacy_projection_uncertain",
                "migration:0007",
                generation_id,
                str(row["ingested_at"]),
            ),
        )
        bind.exec_driver_sql(
            "UPDATE earnings_surprises SET source_observation_id=? WHERE id=?",
            (observation_id, row["id"]),
        )


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
            provenance_status TEXT NOT NULL,
            ingestion_run_id TEXT NOT NULL,
            cache_generation_id TEXT NOT NULL,
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
            CHECK(length(canonical_payload_sha256) = 64),
            CHECK(provenance_status IN ('source_observed','legacy_projection_uncertain')),
            CHECK(length(ingestion_run_id) BETWEEN 1 AND 256),
            CHECK(length(cache_generation_id) BETWEEN 1 AND 256)
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
            ingestion_run_id TEXT NOT NULL,
            cache_generation_id TEXT NOT NULL,
            CHECK(length(quarantine_id) = 64),
            CHECK(length(idempotency_key) BETWEEN 1 AND 256),
            CHECK(record_ordinal >= -1),
            CHECK(json_valid(raw_payload_json)),
            CHECK(json_valid(reason_details_json)
                  AND json_type(reason_details_json) = 'object'),
            CHECK(length(raw_payload_sha256) = 64),
            CHECK(length(reason_details_sha256) = 64),
            CHECK(length(ingestion_run_id) BETWEEN 1 AND 256),
            CHECK(length(cache_generation_id) BETWEEN 1 AND 256)
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
    _backfill_legacy_projections()
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
                  AND observation.ticker IS NEW.ticker
                  AND observation.release_date IS NEW.release_date
                  AND observation.eps_estimate IS NEW.eps_estimate
                  AND observation.eps_actual IS NEW.eps_actual
                  AND observation.revenue_estimate IS NEW.revenue_estimate
                  AND observation.revenue_actual IS NEW.revenue_actual
                  AND observation.eps_surprise_pct IS NEW.eps_surprise_pct
                  AND observation.revenue_surprise_pct IS NEW.revenue_surprise_pct
                  AND observation.num_analysts_eps IS NEW.num_analysts_eps
                  AND observation.num_analysts_revenue IS NEW.num_analysts_revenue
                  AND observation.source_name IS NEW.source_name
                  AND observation.source_url IS NEW.source_url
                  AND observation.fetched_at IS NEW.fetched_at
            )
            BEGIN
                SELECT RAISE(ABORT, 'earnings surprise projection requires source observation with matching payload');
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
