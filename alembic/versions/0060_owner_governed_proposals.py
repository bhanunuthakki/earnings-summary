"""Register immutable owner authorization for governed thesis/KPI diffs.

Downgrade refuses to discard owner proposals or authorization evidence. The Ask
insert branch and status CAS remain the migration 0006 authorities.
"""

from __future__ import annotations

from alembic import op

revision = "0060_owner_governed_proposals"
down_revision = "0059_scoped_research_universe_commitments"
branch_labels = None
depends_on = None

_ASK_INSERT = """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_governed_insert_valid
        BEFORE INSERT ON research_proposals
        WHEN NEW.canonical_content_json IS NOT NULL AND (
            NEW.kind NOT IN ('ask_thesis_edit', 'ask_kpi_edit')
            OR NEW.status <> 'pending'
            OR NEW.proposal_revision <> 0
            OR NOT json_valid(NEW.canonical_content_json)
            OR json_type(NEW.canonical_content_json) <> 'object'
            OR length(NEW.canonical_content_sha256) <> 64
            OR length(NEW.target_precondition_sha256) <> 64
            OR NEW.ask_exchange_request_id IS NULL
            OR length(NEW.ask_exchange_request_id) NOT BETWEEN 1 AND 128
            OR NEW.actionable_at IS NOT NULL
            OR NEW.invalidated_at IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM ask_exchanges exchange
                WHERE exchange.request_id=NEW.ask_exchange_request_id
                  AND exchange.status='pending'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid governed research proposal');
        END
        """

_ASK_IMMUTABLE = """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_canonical_content_immutable
        BEFORE UPDATE OF canonical_content_json, canonical_content_sha256,
                         target_precondition_sha256, ask_exchange_request_id ON research_proposals
        WHEN OLD.canonical_content_json IS NOT NULL AND (
            NEW.canonical_content_json IS NOT OLD.canonical_content_json
            OR NEW.canonical_content_sha256 IS NOT OLD.canonical_content_sha256
            OR NEW.target_precondition_sha256 IS NOT OLD.target_precondition_sha256
            OR NEW.ask_exchange_request_id IS NOT OLD.ask_exchange_request_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'governed proposal content is immutable');
        END
        """

_OWNER_INSERT = """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_governed_insert_valid
        BEFORE INSERT ON research_proposals
        WHEN NEW.canonical_content_json IS NOT NULL AND (
            NEW.kind NOT IN ('ask_thesis_edit', 'ask_kpi_edit')
            OR NEW.status <> 'pending'
            OR NEW.proposal_revision <> 0
            OR NOT json_valid(NEW.canonical_content_json)
            OR json_type(NEW.canonical_content_json) <> 'object'
            OR length(NEW.canonical_content_sha256) <> 64
            OR length(NEW.target_precondition_sha256) <> 64
            OR NEW.ask_exchange_request_id IS NULL
            OR length(NEW.ask_exchange_request_id) NOT BETWEEN 1 AND 128
            OR NEW.actionable_at IS NOT NULL
            OR NEW.invalidated_at IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM ask_exchanges exchange
                WHERE exchange.request_id=NEW.ask_exchange_request_id
                  AND exchange.status='pending'
            )
        ) AND NOT COALESCE((
            NEW.kind IN ('owner_thesis_edit', 'owner_kpi_edit')
            AND NEW.status = 'pending'
            AND NEW.proposal_revision = 0
            AND json_valid(NEW.canonical_content_json)
            AND json_type(NEW.canonical_content_json) = 'object'
            AND length(NEW.canonical_content_sha256) = 64
            AND length(NEW.target_precondition_sha256) = 64
            AND NEW.ask_exchange_request_id IS NULL
            AND NEW.invalidated_at IS NULL
            AND NEW.actionable_at IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM owner_proposal_authorizations authorization
                WHERE authorization.authorization_id=NEW.owner_authorization_id
                  AND authorization.canonical_content_sha256=NEW.canonical_content_sha256
                  AND authorization.ticker=NEW.ticker
            )
        ), 0)
        BEGIN
            SELECT RAISE(ABORT, 'invalid governed research proposal');
        END
        """

_OWNER_IMMUTABLE = """
        CREATE TRIGGER IF NOT EXISTS trg_research_proposal_canonical_content_immutable
        BEFORE UPDATE OF canonical_content_json, canonical_content_sha256,
                         target_precondition_sha256, ask_exchange_request_id,
                         owner_authorization_id ON research_proposals
        WHEN OLD.canonical_content_json IS NOT NULL AND (
            NEW.canonical_content_json IS NOT OLD.canonical_content_json
            OR NEW.canonical_content_sha256 IS NOT OLD.canonical_content_sha256
            OR NEW.target_precondition_sha256 IS NOT OLD.target_precondition_sha256
            OR NEW.ask_exchange_request_id IS NOT OLD.ask_exchange_request_id
            OR NEW.owner_authorization_id IS NOT OLD.owner_authorization_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'governed proposal content is immutable');
        END
        """


def _replace_proposal_triggers(insert_sql: str, immutable_sql: str) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_research_proposal_governed_insert_valid")
    op.execute("DROP TRIGGER IF EXISTS trg_research_proposal_canonical_content_immutable")
    op.execute(insert_sql)
    op.execute(immutable_sql)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS owner_proposal_authorizations (
            authorization_id TEXT PRIMARY KEY NOT NULL,
            ticker TEXT NOT NULL,
            user_id TEXT NOT NULL CHECK(length(user_id) BETWEEN 1 AND 128),
            statement TEXT NOT NULL CHECK(length(statement) BETWEEN 1 AND 2000),
            source_ref TEXT NOT NULL CHECK(length(source_ref) <= 512),
            evidence_json TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
            canonical_content_sha256 TEXT NOT NULL CHECK(length(canonical_content_sha256) = 64),
            created_at TEXT NOT NULL,
            CHECK(length(authorization_id) BETWEEN 1 AND 128),
            CHECK(authorization_id GLOB '[A-Za-z0-9]*'
                  AND authorization_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
            CHECK(length(ticker) BETWEEN 1 AND 24),
            CHECK(json_valid(evidence_json)
                  AND json_type(evidence_json) = 'array'
                  AND json_array_length(evidence_json) > 0)
        ) WITHOUT ROWID
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_owner_proposal_authorization_evidence_valid
        BEFORE INSERT ON owner_proposal_authorizations
        WHEN EXISTS (
            SELECT 1 FROM json_each(NEW.evidence_json) item
            WHERE CASE WHEN item.type = 'object' THEN
                json_type(item.value, '$.path') IS NOT 'text'
                OR length(json_extract(item.value, '$.path')) = 0
                OR json_type(item.value, '$.sha256') IS NOT 'text'
                OR length(json_extract(item.value, '$.sha256')) <> 64
                ELSE 1 END
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid owner authorization evidence');
        END
        """
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_owner_proposal_authorization_{action.lower()} "
            f"BEFORE {action} ON owner_proposal_authorizations BEGIN "
            "SELECT RAISE(ABORT, 'owner authorization is immutable'); END"
        )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_owner_proposal_authorization_no_replace
        BEFORE INSERT ON owner_proposal_authorizations
        WHEN EXISTS (SELECT 1 FROM owner_proposal_authorizations
                     WHERE authorization_id=NEW.authorization_id)
        BEGIN
            SELECT RAISE(ABORT, 'owner authorization is immutable');
        END
        """
    )
    columns = {
        str(row[1])
        for row in op.get_bind().exec_driver_sql("PRAGMA table_info(research_proposals)")
    }
    if "owner_authorization_id" not in columns:
        op.execute(
            # Code-level referential integrity, like every research table: the insert
            # trigger requires a matching immutable authorization row.
            "ALTER TABLE research_proposals ADD COLUMN owner_authorization_id TEXT"
        )
    _replace_proposal_triggers(_OWNER_INSERT, _OWNER_IMMUTABLE)
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_owner_research_proposal_no_replace
        BEFORE INSERT ON research_proposals
        WHEN EXISTS (SELECT 1 FROM research_proposals
                     WHERE id=NEW.id AND owner_authorization_id IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT, 'owner proposal is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_owner_research_proposal_identity_immutable
        BEFORE UPDATE ON research_proposals
        WHEN NEW.id IS NOT OLD.id AND (
            OLD.owner_authorization_id IS NOT NULL
            OR EXISTS (SELECT 1 FROM research_proposals
                       WHERE id=NEW.id AND owner_authorization_id IS NOT NULL)
        )
        BEGIN
            SELECT RAISE(ABORT, 'owner proposal identity is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_owner_research_proposal_no_delete
        BEFORE DELETE ON research_proposals
        WHEN OLD.owner_authorization_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'owner proposal is immutable');
        END
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.exec_driver_sql(
            "SELECT 1 FROM research_proposals WHERE owner_authorization_id IS NOT NULL "
            "OR kind IN ('owner_thesis_edit','owner_kpi_edit') LIMIT 1"
        ).first()
        or bind.exec_driver_sql("SELECT 1 FROM owner_proposal_authorizations LIMIT 1").first()
    ):
        raise RuntimeError("cannot downgrade while owner proposals or authorizations exist")
    _replace_proposal_triggers(_ASK_INSERT, _ASK_IMMUTABLE)
    op.execute("DROP TRIGGER IF EXISTS trg_owner_research_proposal_no_replace")
    op.execute("DROP TRIGGER IF EXISTS trg_owner_research_proposal_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_owner_research_proposal_identity_immutable")
    op.drop_column("research_proposals", "owner_authorization_id")
    op.execute("DROP TABLE IF EXISTS owner_proposal_authorizations")
