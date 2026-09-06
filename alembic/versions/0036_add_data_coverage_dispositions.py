"""Add provider-neutral, append-only data coverage dispositions.

Revision ID: 0036_add_data_coverage_dispositions
Revises: 0035_add_report_kpi_reference_resolution_states
"""

from __future__ import annotations

from alembic import op

revision = "0036_add_data_coverage_dispositions"
down_revision = "0035_add_report_kpi_reference_resolution_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE commitment_scan_receipts (
            receipt_id TEXT PRIMARY KEY
                CHECK(length(receipt_id)=64
                  AND receipt_id NOT GLOB '*[^0-9a-f]*'),
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id),
            document_id INTEGER NOT NULL REFERENCES documents(id),
            transcript_acquisition_receipt_id TEXT NOT NULL
                REFERENCES transcript_acquisition_receipts(receipt_id),
            transcript_sha256 TEXT NOT NULL
                CHECK(length(transcript_sha256)=64
                  AND transcript_sha256 NOT GLOB '*[^0-9a-f]*'),
            prompt_version TEXT NOT NULL CHECK(length(trim(prompt_version)) BETWEEN 1 AND 128),
            n_extracted INTEGER NOT NULL CHECK(n_extracted >= 0),
            output_manifest_json TEXT NOT NULL
                CHECK(json_valid(output_manifest_json)
                  AND json_type(output_manifest_json)='array'
                  AND json_array_length(output_manifest_json)=n_extracted),
            output_manifest_sha256 TEXT NOT NULL
                CHECK(length(output_manifest_sha256)=64
                  AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            recorded_at TEXT NOT NULL CHECK(datetime(recorded_at) IS NOT NULL),
            UNIQUE(transcript_id,prompt_version,output_manifest_sha256)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_commitment_scan_receipts_current "
        "ON commitment_scan_receipts(transcript_id,prompt_version,recorded_at,receipt_id)"
    )
    op.execute(
        "CREATE TRIGGER trg_commitment_scan_receipts_no_update "
        "BEFORE UPDATE ON commitment_scan_receipts BEGIN "
        "SELECT RAISE(ABORT,'commitment scan receipts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_commitment_scan_receipts_no_delete "
        "BEFORE DELETE ON commitment_scan_receipts BEGIN "
        "SELECT RAISE(ABORT,'commitment scan receipts are append-only'); END"
    )
    op.execute(
        """
        CREATE TABLE data_coverage_dispositions (
            disposition_id TEXT PRIMARY KEY
                CHECK(length(disposition_id)=64
                  AND disposition_id NOT GLOB '*[^0-9a-f]*'),
            idempotency_key TEXT NOT NULL UNIQUE
                CHECK(length(idempotency_key)=64
                  AND idempotency_key NOT GLOB '*[^0-9a-f]*'),
            artifact_kind TEXT NOT NULL
                CHECK(artifact_kind IN (
                    'text_transcript','commitment_scan','earnings_surprise'
                )),
            ticker TEXT NOT NULL
                CHECK(length(ticker) BETWEEN 1 AND 16 AND ticker=upper(ticker)),
            fiscal_year INTEGER NOT NULL CHECK(fiscal_year BETWEEN 2000 AND 2100),
            fiscal_quarter INTEGER NOT NULL CHECK(fiscal_quarter BETWEEN 1 AND 4),
            period_end TEXT NOT NULL CHECK(date(period_end) IS NOT NULL),
            status TEXT NOT NULL CHECK(status IN (
                'satisfied','source_unavailable','policy_blocked',
                'provider_coverage_gap','repair_evidence_missing','operational_error'
            )),
            reason_code TEXT NOT NULL
                CHECK(length(reason_code) BETWEEN 1 AND 128
                  AND reason_code NOT GLOB '*[^a-z0-9_]*'),
            attempts_json TEXT NOT NULL
                CHECK(json_valid(attempts_json) AND json_type(attempts_json)='array'),
            attempts_sha256 TEXT NOT NULL
                CHECK(length(attempts_sha256)=64
                  AND attempts_sha256 NOT GLOB '*[^0-9a-f]*'),
            policy_name TEXT NOT NULL CHECK(length(trim(policy_name)) BETWEEN 1 AND 128),
            policy_version TEXT NOT NULL CHECK(length(trim(policy_version)) BETWEEN 1 AND 128),
            policy_config_sha256 TEXT NOT NULL
                CHECK(length(policy_config_sha256)=64
                  AND policy_config_sha256 NOT GLOB '*[^0-9a-f]*'),
            evidence_reference TEXT,
            evidence_sha256 TEXT CHECK(
                evidence_sha256 IS NULL OR
                (length(evidence_sha256)=64
                 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*')
            ),
            operation_id TEXT,
            observed_at TEXT NOT NULL CHECK(datetime(observed_at) IS NOT NULL),
            retry_after TEXT CHECK(retry_after IS NULL OR datetime(retry_after) IS NOT NULL),
            revision INTEGER NOT NULL CHECK(revision > 0),
            supersedes_disposition_id TEXT UNIQUE
                REFERENCES data_coverage_dispositions(disposition_id),
            recorded_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                CHECK(datetime(recorded_at) IS NOT NULL),
            UNIQUE(artifact_kind,ticker,fiscal_year,fiscal_quarter,revision),
            CHECK(
                (status='satisfied' AND evidence_reference IS NOT NULL
                 AND evidence_sha256 IS NOT NULL AND retry_after IS NULL)
                OR
                (status<>'satisfied' AND evidence_reference IS NULL
                 AND evidence_sha256 IS NULL)
            ),
            CHECK(
                status NOT IN ('source_unavailable','provider_coverage_gap','operational_error')
                OR retry_after IS NOT NULL
            ),
            CHECK(datetime(recorded_at) >= datetime(observed_at)),
            CHECK(
                (revision=1 AND supersedes_disposition_id IS NULL)
                OR (revision>1 AND supersedes_disposition_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_data_coverage_dispositions_target_revision "
        "ON data_coverage_dispositions(artifact_kind,ticker,fiscal_year,fiscal_quarter,revision)"
    )
    op.execute(
        """
        CREATE VIEW v_data_coverage_dispositions_current AS
        SELECT disposition.*
        FROM data_coverage_dispositions AS disposition
        WHERE NOT EXISTS (
            SELECT 1
            FROM data_coverage_dispositions AS newer
            WHERE newer.artifact_kind=disposition.artifact_kind
              AND newer.ticker=disposition.ticker
              AND newer.fiscal_year=disposition.fiscal_year
              AND newer.fiscal_quarter=disposition.fiscal_quarter
              AND newer.revision>disposition.revision
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_data_coverage_dispositions_predecessor
        BEFORE INSERT ON data_coverage_dispositions
        WHEN NEW.revision > 1
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM data_coverage_dispositions AS prior
                WHERE prior.disposition_id=NEW.supersedes_disposition_id
                  AND prior.artifact_kind=NEW.artifact_kind
                  AND prior.ticker=NEW.ticker
                  AND prior.fiscal_year=NEW.fiscal_year
                  AND prior.fiscal_quarter=NEW.fiscal_quarter
                  AND prior.revision=NEW.revision-1
            ) THEN RAISE(ABORT,'data coverage disposition predecessor mismatch') END;
        END
        """
    )
    op.execute(
        "CREATE TRIGGER trg_data_coverage_dispositions_no_update "
        "BEFORE UPDATE ON data_coverage_dispositions BEGIN "
        "SELECT RAISE(ABORT,'data coverage dispositions are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_data_coverage_dispositions_no_delete "
        "BEFORE DELETE ON data_coverage_dispositions BEGIN "
        "SELECT RAISE(ABORT,'data coverage dispositions are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_data_coverage_dispositions_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_data_coverage_dispositions_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_data_coverage_dispositions_predecessor")
    op.execute("DROP VIEW IF EXISTS v_data_coverage_dispositions_current")
    op.execute("DROP INDEX IF EXISTS ix_data_coverage_dispositions_target_revision")
    op.execute("DROP TABLE IF EXISTS data_coverage_dispositions")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_scan_receipts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_commitment_scan_receipts_no_update")
    op.execute("DROP INDEX IF EXISTS ix_commitment_scan_receipts_current")
    op.execute("DROP TABLE IF EXISTS commitment_scan_receipts")
