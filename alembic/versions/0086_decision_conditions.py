"""decision_conditions — falsifiable "what would change my mind" on decisions.

Fund-grade build S5 (directives/fund_grade_build_2026_06.md): the decisions
table records WHAT was recommended, but the falsifiability lived only in memo
prose ("## What would change my mind" in five_min_reread artifacts and
Socratic advisor memos) — never schematized, never resurfaceable. This
migration adds the schema half:

  decisions.decision_conditions     — JSON array of falsifiable conditions,
      each ``{metric, metric_source, op, threshold, unit, for_periods, note}``.
      ``op`` uses the break-rule Comparator vocabulary (lt/le/gt/ge/eq) and
      ``unit`` the models.facts.Unit vocabulary, so the evaluator rung can
      reuse ``compute.thesis_evaluator.evaluate_rule`` (shared ``convert_unit``
      reconciliation — the #317/#320 unit-bug class stays collapsed).
  decisions.conditions_extracted_at — when the extraction rung last ran on
      this row; distinguishes "never extracted" (NULL) from "extracted, prose
      had no falsifiable conditions" (stamped + '[]').
  decisions.source_memo_id          — advisor_memos.id when the decision was
      recorded from a Socratic memo (which lives in advisor_memos, not
      llm_artifacts). source_artifact_id is relaxed to nullable; the named
      CHECK keeps every row anchored to at least one source. A partial UNIQUE
      index gives memo-sourced rows the same idempotency the artifact UNIQUE
      gives lens-sourced ones.

Also widens ``ck_alerts_trigger_kind`` to admit 'decision_condition' — the
evaluator rung is a Trigger sensor (src/triggers/decision_condition.py) that
resurfaces a decision into the inbox/alerts when an incoming kpi/financial
fact satisfies one of its conditions. Keep in lockstep with
``alerts.store.TRIGGER_KINDS``.

Seeds an llm_budgets row for the new ``decision_conditions_extract`` purpose
(Haiku, short prose sections, runs only on new memos/artifacts — $5/mo cap).

New-column CHECKs follow 0071's policy (named constraints, guarded +
idempotent ops); the trigger-kind widening follows 0077's batch-recreate
pattern.

Revision ID: 0086_decision_conditions
Revises: 0085_ask_sessions_and_turns
Create Date: 2026-06-12
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0086_decision_conditions"
down_revision: str | Sequence[str] | None = "0085_ask_sessions_and_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of alerts.store.TRIGGER_KINDS after this migration.
_TRIGGER_KIND_WIDENED = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', "
    "'material_news', 'decision_condition')"
)
_TRIGGER_KIND_ORIGINAL = (
    "('kpi_inflection', 'earnings_tone', 'saydo_due', 'thesis_drift', 'material_news')"
)

_BUDGET_PURPOSE = "decision_conditions_extract"
_BUDGET_CAP_USD = 5.0


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp: sa.Inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in insp.get_columns(table))


def _has_index(insp: sa.Inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "decisions") and not _has_column(insp, "decisions", "decision_conditions"):
        # One batch recreate covers the adds, the NOT NULL relaxation, and the
        # source-anchor CHECK (SQLite cannot ALTER constraints in place).
        with op.batch_alter_table("decisions") as batch:
            batch.add_column(sa.Column("decision_conditions", sa.Text(), nullable=True))
            batch.add_column(sa.Column("conditions_extracted_at", sa.DateTime(), nullable=True))
            batch.add_column(sa.Column("source_memo_id", sa.Integer(), nullable=True))
            batch.alter_column(
                "source_artifact_id", existing_type=sa.Integer(), nullable=True
            )
            batch.create_check_constraint(
                "ck_decisions_source_present",
                "source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL",
            )
        if not _has_index(insp, "decisions", "uq_decisions_source_memo"):
            op.create_index(
                "uq_decisions_source_memo",
                "decisions",
                ["source_memo_id"],
                unique=True,
                sqlite_where=sa.text("source_memo_id IS NOT NULL"),
            )

    if _has_table(insp, "alerts"):
        with op.batch_alter_table("alerts") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):  # already absent
                batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
            batch.create_check_constraint(
                "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_WIDENED}"
            )

    if _has_table(insp, "llm_budgets"):
        bind.execute(
            sa.text(
                "INSERT INTO llm_budgets "
                "(purpose, monthly_cap_usd, warn_threshold_pct, hard_block, "
                " created_at, updated_at, notes, on_exceed) "
                "SELECT :purpose, :cap, 0.8, 0, datetime('now'), datetime('now'), "
                "       'seeded by migration 0086', 'warn' "
                "WHERE NOT EXISTS (SELECT 1 FROM llm_budgets WHERE purpose = :purpose)"
            ),
            {"purpose": _BUDGET_PURPOSE, "cap": _BUDGET_CAP_USD},
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "llm_budgets"):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose AND notes = :notes"),
            {"purpose": _BUDGET_PURPOSE, "notes": "seeded by migration 0086"},
        )

    # Restore the original trigger-kind CHECK only when no decision_condition
    # alerts exist — otherwise the recreate would fail row validation; the
    # widened constraint stays (downgrade is safe, never destructive).
    if _has_table(insp, "alerts"):
        widened_rows = bind.execute(
            sa.text("SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'decision_condition'")
        ).scalar()
        if not widened_rows:
            with op.batch_alter_table("alerts") as batch:
                with contextlib.suppress(ValueError, sa.exc.OperationalError):
                    batch.drop_constraint("ck_alerts_trigger_kind", type_="check")
                batch.create_check_constraint(
                    "ck_alerts_trigger_kind", f"trigger_kind IN {_TRIGGER_KIND_ORIGINAL}"
                )

    if _has_table(insp, "decisions") and _has_column(insp, "decisions", "decision_conditions"):
        # Memo-sourced rows (source_artifact_id NULL) cannot survive the NOT
        # NULL restoration — refuse rather than destroy.
        memo_only = bind.execute(
            sa.text("SELECT COUNT(*) FROM decisions WHERE source_artifact_id IS NULL")
        ).scalar()
        if memo_only:
            raise RuntimeError(
                "cannot downgrade 0086: decisions rows exist with source_artifact_id "
                "NULL (memo-sourced); delete or re-anchor them first"
            )
        if _has_index(insp, "decisions", "uq_decisions_source_memo"):
            op.drop_index("uq_decisions_source_memo", table_name="decisions")
        with op.batch_alter_table("decisions") as batch:
            with contextlib.suppress(ValueError, sa.exc.OperationalError):
                batch.drop_constraint("ck_decisions_source_present", type_="check")
            batch.alter_column(
                "source_artifact_id", existing_type=sa.Integer(), nullable=False
            )
            batch.drop_column("source_memo_id")
            batch.drop_column("conditions_extracted_at")
            batch.drop_column("decision_conditions")
