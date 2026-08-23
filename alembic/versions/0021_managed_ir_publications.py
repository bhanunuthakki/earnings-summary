"""Seal managed issuer-document publication episodes.

Revision ID: 0021_managed_ir_publications
Revises: 0020_kpi_fact_currency
"""

from __future__ import annotations

from alembic import op

revision = "0021_managed_ir_publications"
down_revision = "0020_kpi_fact_currency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE managed_ir_publications (
        attempt_id TEXT PRIMARY KEY,
        staging_receipt_sha256 TEXT NOT NULL CHECK(length(staging_receipt_sha256)=64),
        document_set_sha256 TEXT NOT NULL CHECK(length(document_set_sha256)=64),
        inserted_ids_json TEXT NOT NULL,
        reused_ids_json TEXT NOT NULL,
        canonical_paths_json TEXT NOT NULL,
        created_paths_json TEXT NOT NULL,
        staging_receipt_path TEXT NOT NULL,
        inventory_receipt_path TEXT NOT NULL,
        publication_result_path TEXT NOT NULL,
        intent_sha256 TEXT NOT NULL CHECK(length(intent_sha256)=64),
        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64),
        committed_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state='committed'),
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
        )"""
    )
    op.execute(
        """CREATE TRIGGER managed_ir_publications_append_only_insert
        BEFORE INSERT ON managed_ir_publications
        WHEN EXISTS (SELECT 1 FROM managed_ir_publications WHERE attempt_id=NEW.attempt_id)
        BEGIN SELECT RAISE(ABORT, 'managed_ir_publications is append-only'); END"""
    )
    op.execute(
        """CREATE TRIGGER managed_ir_publications_append_only_update
        BEFORE UPDATE ON managed_ir_publications
        BEGIN SELECT RAISE(ABORT, 'managed_ir_publications is append-only'); END"""
    )
    op.execute(
        """CREATE TABLE managed_ir_inventory_evidence (
        attempt_id TEXT PRIMARY KEY,
        publication_payload_sha256 TEXT NOT NULL CHECK(length(publication_payload_sha256)=64),
        inventory_receipt_path TEXT NOT NULL,
        inventory_receipt_sha256 TEXT NOT NULL CHECK(length(inventory_receipt_sha256)=64),
        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64),
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
        )"""
    )
    for operation in ("insert", "update", "delete"):
        when = (
            " WHEN EXISTS (SELECT 1 FROM managed_ir_inventory_evidence WHERE attempt_id=NEW.attempt_id)"
            if operation == "insert"
            else ""
        )
        op.execute(
            f"""CREATE TRIGGER managed_ir_inventory_evidence_append_only_{operation}
            BEFORE {operation.upper()} ON managed_ir_inventory_evidence{when}
            BEGIN SELECT RAISE(ABORT, 'managed_ir_inventory_evidence is append-only'); END"""
        )
    op.execute(
        """CREATE TRIGGER managed_ir_publications_append_only_delete
        BEFORE DELETE ON managed_ir_publications
        BEGIN SELECT RAISE(ABORT, 'managed_ir_publications is append-only'); END"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER managed_ir_inventory_evidence_append_only_delete")
    op.execute("DROP TRIGGER managed_ir_inventory_evidence_append_only_update")
    op.execute("DROP TRIGGER managed_ir_inventory_evidence_append_only_insert")
    op.execute("DROP TABLE managed_ir_inventory_evidence")
    op.execute("DROP TRIGGER managed_ir_publications_append_only_delete")
    op.execute("DROP TRIGGER managed_ir_publications_append_only_update")
    op.execute("DROP TRIGGER managed_ir_publications_append_only_insert")
    op.execute("DROP TABLE managed_ir_publications")
