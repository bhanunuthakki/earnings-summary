"""Add explicit currency provenance to canonical KPI facts.

Revision ID: 0020_kpi_fact_currency
Revises: 0019_issuer_fact_coverage_receipts
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "0020_kpi_fact_currency"
down_revision = "0019_issuer_fact_coverage_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE kpi_facts ADD COLUMN currency VARCHAR(8)")
    _rewrite_kpi_observation_triggers(use_currency=True)
    _rewrite_kpi_legacy_match_triggers(use_currency=True)


def downgrade() -> None:
    _rewrite_kpi_legacy_match_triggers(use_currency=False)
    _rewrite_kpi_observation_triggers(use_currency=False)
    op.execute("ALTER TABLE kpi_facts DROP COLUMN currency")


def _rewrite_kpi_observation_triggers(*, use_currency: bool) -> None:
    """Project the persisted KPI currency into canonical observations.

    The baseline trigger predates the ``kpi_facts.currency`` column and writes
    NULL deliberately.  Reconstructing its complete evidence guard here would
    risk divergence, so retain the migration-owned trigger definition and make
    only the two exact projection substitutions.
    """
    bind = op.get_bind()
    for name in ("trg_kpi_facts_observation_insert", "trg_kpi_facts_observation_update"):
        row = bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": name},
        ).scalar_one_or_none()
        if not isinstance(row, str):
            raise RuntimeError(f"required KPI observation trigger {name} is absent")
        old = ", NULL, NULL, NEW.unit"
        new = ", NULL, NEW.currency, NEW.unit"
        target = new if use_currency else old
        replacement = old if use_currency else new
        if target in row:
            return_sql = row.replace(target, replacement)
        elif replacement in row:
            return_sql = row.replace(replacement, target)
        else:
            raise RuntimeError(f"KPI observation trigger {name} has no currency projection slot")
        if name.endswith("_update"):
            return_sql = (
                return_sql.replace(
                    "value, unit, source_doc_id", "value, currency, unit, source_doc_id"
                )
                if use_currency
                else return_sql.replace(
                    "value, currency, unit, source_doc_id", "value, unit, source_doc_id"
                )
            )
        op.execute(f"DROP TRIGGER {name}")
        op.execute(return_sql)


def _rewrite_kpi_legacy_match_triggers(*, use_currency: bool) -> None:
    """Keep accepted-match freezing and scope validation currency-exact."""
    bind = op.get_bind()
    trigger_names = (
        "trg_legacy_fact_evidence_match_revisions_kpi_facts_accepted_update",
        "trg_legacy_fact_evidence_match_revisions_kpi_facts_scope",
    )
    for name in trigger_names:
        row = bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": name},
        ).scalar_one_or_none()
        if not isinstance(row, str):
            raise RuntimeError(f"required KPI legacy-match trigger {name} is absent")
        if name.endswith("accepted_update"):
            before = "value, unit, source_doc_id"
            after = "value, currency, unit, source_doc_id"
            target, replacement = (before, after) if use_currency else (after, before)
        else:
            before = "json_extract(NEW.fact_payload_json, '$.unit') IS fact.unit"
            after = (
                "json_extract(NEW.fact_payload_json, '$.currency') IS fact.currency "
                "AND json_extract(NEW.fact_payload_json, '$.unit') IS fact.unit"
            )
            target, replacement = (before, after) if use_currency else (after, before)
        if target in row:
            rewritten = row.replace(target, replacement)
        elif replacement in row:
            rewritten = row.replace(replacement, target)
        else:
            raise RuntimeError(f"KPI legacy-match trigger {name} has no payload currency slot")
        op.execute(f"DROP TRIGGER {name}")
        op.execute(rewritten)
