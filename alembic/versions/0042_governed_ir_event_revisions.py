"""Append-only IR event history and explicit ingestion receipts.

Revision ID: 0042_governed_ir_event_revisions
Revises: 0040_fmp_watchlist_recovery (provisional isolated test parent)
"""

from alembic import op

revision = "0042_governed_ir_event_revisions"
down_revision = "0041_versioned_rate_sensitivity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE ir_event_revisions (
        revision_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        supersedes_revision_id TEXT REFERENCES ir_event_revisions(revision_id),
        issuer_id TEXT NOT NULL REFERENCES issuer_entities(issuer_id),
        ticker TEXT NOT NULL,
        event_kind TEXT NOT NULL CHECK(event_kind IN ('investor_day','analyst_day','capital_markets_day','strategy_day')),
        status TEXT NOT NULL CHECK(status IN ('scheduled','rescheduled','cancelled')),
        title TEXT NOT NULL,
        event_date TEXT NOT NULL,
        source_tier TEXT NOT NULL CHECK(source_tier IN ('publisher_event_authority','issuer_ir_announcement','issuer_regulatory_announcement')),
        source_observation_id TEXT NOT NULL REFERENCES evidence_source_observations(observation_id),
        authority_surface_revision_id TEXT NOT NULL REFERENCES issuer_authority_surface_revisions(surface_revision_id),
        raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256)=64),
        observed_at TEXT NOT NULL,
        observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
        UNIQUE(event_id,revision),
        CHECK((revision=1 AND supersedes_revision_id IS NULL) OR (revision>1 AND supersedes_revision_id IS NOT NULL))
    )""")
    op.execute("""CREATE TABLE ir_event_runs (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, as_of TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('complete','empty','partial','error','disabled')),
        receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json))
    )""")
    for table in ("ir_event_revisions", "ir_event_runs"):
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append-only IR evidence'); END"
            )
    op.execute("ALTER TABLE signals ADD COLUMN ir_event_id TEXT")
    op.execute(
        "ALTER TABLE signals ADD COLUMN ir_event_revision_id TEXT REFERENCES ir_event_revisions(revision_id)"
    )
    op.execute("DROP INDEX ux_signals_event")
    op.execute(
        "CREATE UNIQUE INDEX ux_signals_event ON signals(ticker,signal_type,event_date) WHERE event_date IS NOT NULL AND ir_event_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_signals_ir_event ON signals(ir_event_id) WHERE ir_event_id IS NOT NULL"
    )


def downgrade() -> None:
    if (
        op.get_bind().exec_driver_sql("SELECT 1 FROM ir_event_revisions LIMIT 1").first()
        or op.get_bind().exec_driver_sql("SELECT 1 FROM ir_event_runs LIMIT 1").first()
    ):
        raise RuntimeError("retained IR evidence prevents destructive downgrade")
    op.execute("DROP INDEX ux_signals_ir_event")
    op.execute("DROP INDEX ux_signals_event")
    op.execute("ALTER TABLE signals DROP COLUMN ir_event_revision_id")
    op.execute("ALTER TABLE signals DROP COLUMN ir_event_id")
    op.execute(
        "CREATE UNIQUE INDEX ux_signals_event ON signals(ticker,signal_type,event_date) WHERE event_date IS NOT NULL"
    )
    op.execute("DROP TABLE ir_event_runs")
    op.execute("DROP TABLE ir_event_revisions")
