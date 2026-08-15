"""Add append-only transcript acquisition receipts.

Revision ID: 0018_add_transcript_acquisition_receipts
Revises: 0017_add_owner_decision_checkpoints
"""

from __future__ import annotations

from alembic import op

revision = "0018_add_transcript_acquisition_receipts"
down_revision = "0017_add_owner_decision_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcript_acquisition_receipts (
            receipt_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            document_id INTEGER REFERENCES documents(id),
            canonical_ticker TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL CHECK(fiscal_year BETWEEN 2000 AND 2100),
            fiscal_quarter INTEGER NOT NULL CHECK(fiscal_quarter BETWEEN 1 AND 4),
            canonical_document_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            artifact_size_bytes INTEGER NOT NULL CHECK(artifact_size_bytes >= 0),
            source_url TEXT,
            provider TEXT NOT NULL CHECK(provider = 'issuer_ir'),
            source_type TEXT NOT NULL CHECK(source_type = 'ir_doc'),
            document_type TEXT NOT NULL CHECK(document_type = 'earnings_call_transcript'),
            source_regime TEXT NOT NULL CHECK(source_regime = 'combined'),
            source_regime_contract_sha256 TEXT NOT NULL,
            authorization_json TEXT NOT NULL CHECK(json_valid(authorization_json)),
            artifact_json TEXT NOT NULL CHECK(json_valid(artifact_json)),
            recorded_at TEXT NOT NULL,
            CHECK(length(receipt_id)=64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
            CHECK(idempotency_key GLOB 'transcript:[0-9a-f]*' AND length(idempotency_key)=75),
            CHECK(length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(
                length(source_regime_contract_sha256)=64
                AND source_regime_contract_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            CHECK(json_extract(authorization_json,'$.schema_version') =
                'transcript-acquisition-authorization@1'),
            CHECK(json_extract(authorization_json,'$.status') = 'authorized'),
            CHECK(json_extract(authorization_json,'$.idempotency_key') = idempotency_key),
            CHECK(json_extract(authorization_json,'$.request.provider') = provider),
            CHECK(json_extract(authorization_json,'$.request.canonical_ticker') = canonical_ticker),
            CHECK(json_extract(authorization_json,'$.request.fiscal_year') = fiscal_year),
            CHECK(json_extract(authorization_json,'$.request.fiscal_quarter') = fiscal_quarter),
            CHECK(json_extract(authorization_json,'$.request.source_type') = source_type),
            CHECK(json_extract(authorization_json,'$.request.document_type') = document_type),
            CHECK(json_extract(authorization_json,'$.request.source_regime_identity.regime') =
                source_regime),
            CHECK(json_extract(
                authorization_json,'$.request.source_regime_identity.contract_sha256'
            ) = source_regime_contract_sha256),
            CHECK(json_extract(artifact_json,'$.schema_version') =
                'authorized-transcript-artifact@1'),
            CHECK(json_extract(artifact_json,'$.staged.sha256') = artifact_sha256),
            CHECK(json_extract(artifact_json,'$.staged.size_bytes') = artifact_size_bytes),
            CHECK(json_extract(artifact_json,'$.document_id') IS document_id),
            CHECK(json_extract(artifact_json,'$.canonical_document_path') =
                canonical_document_path),
            CHECK(json_extract(artifact_json,'$.source_url') IS source_url),
            CHECK(json_extract(artifact_json,'$.authorization.idempotency_key') = idempotency_key),
            CHECK(json(authorization_json) = json_extract(artifact_json,'$.authorization'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_transcript_acquisition_receipts_target "
        "ON transcript_acquisition_receipts(idempotency_key,recorded_at,receipt_id)"
    )
    op.execute(
        "CREATE TRIGGER trg_transcript_acquisition_receipts_no_update "
        "BEFORE UPDATE ON transcript_acquisition_receipts "
        "BEGIN SELECT RAISE(ABORT, 'transcript acquisition receipts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_transcript_acquisition_receipts_no_delete "
        "BEFORE DELETE ON transcript_acquisition_receipts "
        "BEGIN SELECT RAISE(ABORT, 'transcript acquisition receipts are append-only'); END"
    )
    op.execute(
        """
        CREATE TRIGGER trg_transcript_acquisition_receipts_validate
        BEFORE INSERT ON transcript_acquisition_receipts
        BEGIN
            SELECT CASE WHEN transcript_receipt_valid(
                NEW.receipt_id, NEW.idempotency_key, NEW.document_id,
                NEW.canonical_ticker, NEW.fiscal_year, NEW.fiscal_quarter,
                NEW.canonical_document_path, NEW.artifact_sha256,
                NEW.artifact_size_bytes, NEW.source_url, NEW.provider,
                NEW.source_type, NEW.document_type, NEW.source_regime,
                NEW.source_regime_contract_sha256, NEW.authorization_json,
                NEW.artifact_json, NEW.recorded_at
            ) != 1 THEN RAISE(ABORT, 'invalid transcript acquisition receipt') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_transcript_acquisition_receipts_document_binding
        BEFORE INSERT ON transcript_acquisition_receipts
        WHEN NEW.document_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM documents AS d
            WHERE d.id = NEW.document_id
              AND UPPER(d.ticker) = NEW.canonical_ticker
              AND d.source_type = 'ir_doc'
              AND d.doc_type = 'ir_transcript'
              AND d.file_path = NEW.canonical_document_path
              AND d.sha256 = NEW.artifact_sha256
              AND d.raw_bytes_size = NEW.artifact_size_bytes
              AND d.source_url IS NEW.source_url
        )
        BEGIN
            SELECT RAISE(ABORT, 'transcript receipt does not match canonical document');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_transcript_acquisition_receipts_document_binding")
    op.execute("DROP TRIGGER IF EXISTS trg_transcript_acquisition_receipts_validate")
    op.execute("DROP TRIGGER IF EXISTS trg_transcript_acquisition_receipts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_transcript_acquisition_receipts_no_update")
    op.execute("DROP INDEX IF EXISTS ix_transcript_acquisition_receipts_target")
    op.execute("DROP TABLE IF EXISTS transcript_acquisition_receipts")
