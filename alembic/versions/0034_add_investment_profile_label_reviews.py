"""Persist append-only owner review of derived investment-profile labels.

Revision ID: 0034_add_investment_profile_label_reviews
Revises: 0033_add_report_kpi_reference_resolutions
"""

from __future__ import annotations

from alembic import op

revision = "0034_add_investment_profile_label_reviews"
down_revision = "0033_add_report_kpi_reference_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE investment_profile_label_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
            label TEXT NOT NULL CHECK(label IN (
                'long_term_compounder','garp','elite_growth_expensive','turnaround',
                'narrative_rerating','growth_inflection','cash_yield_value','optionality',
                'core_beta','factor_sleeve','thematic_exposure','diversifier',
                'defensive_hedge','income','tactical_cyclical'
            )),
            action TEXT NOT NULL CHECK(action IN ('ratify','reject','retire')),
            suggestion_fingerprint TEXT NOT NULL
                CHECK(length(suggestion_fingerprint) = 64
                  AND suggestion_fingerprint NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL
                CHECK(json_valid(evidence_json) AND json_type(evidence_json)='object'),
            reviewed_by TEXT NOT NULL CHECK(length(trim(reviewed_by)) > 0),
            reviewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            idempotency_key TEXT NOT NULL UNIQUE
                CHECK(length(idempotency_key) = 64
                  AND idempotency_key NOT GLOB '*[^0-9a-f]*')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_investment_profile_label_review_current "
        "ON investment_profile_label_reviews(ticker,label,id DESC)"
    )
    op.execute(
        "CREATE TRIGGER trg_investment_profile_label_reviews_no_update BEFORE UPDATE ON "
        "investment_profile_label_reviews BEGIN SELECT RAISE(ABORT, "
        "'Investment profile label reviews are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_investment_profile_label_reviews_no_delete BEFORE DELETE ON "
        "investment_profile_label_reviews BEGIN SELECT RAISE(ABORT, "
        "'Investment profile label reviews are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_investment_profile_label_reviews_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_investment_profile_label_reviews_no_update")
    op.execute("DROP INDEX IF EXISTS ix_investment_profile_label_review_current")
    op.execute("DROP TABLE IF EXISTS investment_profile_label_reviews")
