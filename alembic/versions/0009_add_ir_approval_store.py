"""Add immutable owner approval and exact IR document selection ledgers.

Revision ID: 0009_add_ir_approval_store
Revises: 0008_add_fmp_recovery
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0009_add_ir_approval_store"
down_revision = "0008_add_fmp_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ir_approval_candidates (
            candidate_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            catalog_sha256 TEXT NOT NULL,
            issuer_policy_sha256 TEXT NOT NULL,
            authority_url TEXT NOT NULL,
            quarter_end TEXT NOT NULL,
            title TEXT NOT NULL,
            candidate_url TEXT NOT NULL,
            disposition TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            observation_key TEXT NOT NULL,
            observation_raw_sha256 TEXT NOT NULL,
            evidence_locator TEXT NOT NULL,
            recorded_by TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            CHECK(length(candidate_id) = 64
                  AND candidate_id NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(request_id) BETWEEN 1 AND 128),
            CHECK(length(request_sha256) = 64
                  AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(issuer_id) BETWEEN 1 AND 128),
            CHECK(length(ticker) BETWEEN 1 AND 16 AND ticker = upper(ticker)),
            CHECK(length(catalog_sha256) = 64
                  AND catalog_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(issuer_policy_sha256) = 64
                  AND issuer_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(authority_url) BETWEEN 1 AND 2048),
            CHECK(length(quarter_end) = 10),
            CHECK(length(title) BETWEEN 1 AND 1024),
            CHECK(length(candidate_url) BETWEEN 1 AND 4096),
            CHECK(disposition IN ('ir_document','transcript_candidate')),
            CHECK(doc_type IN (
                'ir_press_release','ir_presentation','ir_supplement',
                'ir_investor_update','ir_transcript','ir_event'
            )),
            CHECK(length(observation_key) BETWEEN 1 AND 256),
            CHECK(length(observation_raw_sha256) = 64
                  AND observation_raw_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(evidence_locator) BETWEEN 1 AND 2048),
            CHECK(length(recorded_by) BETWEEN 1 AND 256),
            CHECK(length(reason) BETWEEN 1 AND 4096),
            CHECK(json_valid(evidence_json) AND json_type(evidence_json) = 'array'),
            CHECK(length(evidence_sha256) = 64
                  AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
            UNIQUE(issuer_id,catalog_sha256,observation_raw_sha256,candidate_url)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ir_approval_candidates_issuer_period "
        "ON ir_approval_candidates(issuer_id,quarter_end,recorded_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ir_approval_decisions (
            decision_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            action TEXT NOT NULL,
            expected_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            supersedes_decision_id TEXT,
            owner_actor TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            selected_url TEXT,
            selected_doc_type TEXT,
            selected_content_sha256 TEXT,
            FOREIGN KEY(candidate_id) REFERENCES ir_approval_candidates(candidate_id),
            FOREIGN KEY(supersedes_decision_id) REFERENCES ir_approval_decisions(decision_id),
            CHECK(length(decision_id) = 64
                  AND decision_id NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(request_id) BETWEEN 1 AND 128),
            CHECK(length(request_sha256) = 64
                  AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(candidate_id) = 64
                  AND candidate_id NOT GLOB '*[^0-9a-f]*'),
            CHECK(action IN ('approve','reject','select_exact')),
            CHECK(expected_revision >= 0),
            CHECK(revision = expected_revision + 1),
            CHECK(
                (revision = 1 AND supersedes_decision_id IS NULL)
                OR (revision > 1 AND supersedes_decision_id IS NOT NULL)
            ),
            CHECK(length(owner_actor) BETWEEN 1 AND 256),
            CHECK(length(reason) BETWEEN 1 AND 4096),
            CHECK(json_valid(evidence_json) AND json_type(evidence_json) = 'array'),
            CHECK(length(evidence_sha256) = 64
                  AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
            CHECK(supersedes_decision_id IS NULL OR (
                length(supersedes_decision_id) = 64
                AND supersedes_decision_id NOT GLOB '*[^0-9a-f]*'
            )),
            CHECK(selected_content_sha256 IS NULL OR (
                length(selected_content_sha256) = 64
                AND selected_content_sha256 NOT GLOB '*[^0-9a-f]*'
            )),
            CHECK(
                (action = 'select_exact' AND selected_url IS NOT NULL
                    AND selected_doc_type IS NOT NULL
                    AND selected_content_sha256 IS NOT NULL)
                OR (action != 'select_exact' AND selected_url IS NULL
                    AND selected_doc_type IS NULL
                    AND selected_content_sha256 IS NULL)
            ),
            UNIQUE(candidate_id,revision),
            UNIQUE(supersedes_decision_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ir_approval_decisions_candidate_revision "
        "ON ir_approval_decisions(candidate_id,revision DESC)"
    )
    for table, label in (
        ("ir_approval_candidates", "IR approval candidates"),
        ("ir_approval_decisions", "IR approval decisions"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{label} are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{label} are immutable');
            END
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ir_approval_decisions_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_ir_approval_decisions_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_ir_approval_candidates_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_ir_approval_candidates_no_update")
    op.execute("DROP INDEX IF EXISTS ix_ir_approval_decisions_candidate_revision")
    op.execute("DROP TABLE IF EXISTS ir_approval_decisions")
    op.execute("DROP INDEX IF EXISTS ix_ir_approval_candidates_issuer_period")
    op.execute("DROP TABLE IF EXISTS ir_approval_candidates")
