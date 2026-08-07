"""eval_runs + eval_case_results — the LLM eval harness's score-and-transcript store.

Why this exists (directives/llm_evals_plan.md)
----------------------------------------------
``prompt_calibration_scores`` (0058) stores one aggregate score per grading
event; it has no room for the per-case evidence an eval run produces — the
question asked, expected vs actual output, the judge's verdict and rationale,
and the FULL prompt/response transcript (the production ``llm_calls`` ledger
deliberately stores sha256 only; eval volume is tiny, so storing text here is
what makes failures debuggable and judges auditable).

Two tables:

``eval_runs`` — one row per harness invocation. ``run_id`` is also passed as
``call_llm(run_id=)`` for every call the run makes, so cost/latency join back
from ``llm_calls``. ``prompt_version`` + ``golden_set_sha`` + ``model`` +
``git_sha`` make two runs comparable.

``eval_case_results`` — one row per golden case: deterministic grade
(``passed``/``score``/``failure_stage``), judge verdict + rationale when the
judge fired, and the transcript.

Also seeds an ``llm_budgets`` row for the new ``eval_judge`` purpose (the
judge-model calls the harness makes), mirroring 0080's pattern: warn-mode so a
blown cap surfaces in the dashboard without silently disabling evals; the cap
is modest because a judge call is a short Haiku round-trip (fractions of a
cent — see the cost model in the directive).

Revision ID: 0083_eval_runs
Revises: 0082_expected_earnings_revival
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0083_eval_runs"
down_revision: str | Sequence[str] | None = "0082_expected_earnings_revival"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_JUDGE_PURPOSE = "eval_judge"
# One judge verdict is a ~1.5K-token Haiku call (~$0.003); $10/month covers
# weekly rubric-judging of the whole prose surface with headroom.
_JUDGE_MONTHLY_CAP_USD = 10.00


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "eval_runs" not in existing:
        op.create_table(
            "eval_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("purpose", sa.String(length=64), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False, server_default="live"),
            sa.Column("prompt_version", sa.String(length=32), nullable=False),
            sa.Column("model", sa.String(length=64), nullable=False),
            sa.Column("judge_model", sa.String(length=64), nullable=True),
            sa.Column("golden_set_sha", sa.String(length=64), nullable=True),
            sa.Column("n_cases", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("n_pass", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("avg_score", sa.Float(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("git_sha", sa.String(length=40), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
        )
        op.create_index("ix_eval_runs_purpose_started", "eval_runs", ["purpose", "started_at"])
        op.create_index("ix_eval_runs_run_id", "eval_runs", ["run_id"])

    if "eval_case_results" not in existing:
        op.create_table(
            "eval_case_results",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "eval_run_id",
                sa.Integer(),
                sa.ForeignKey("eval_runs.id"),
                nullable=False,
            ),
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("expected_json", sa.Text(), nullable=True),
            sa.Column("actual_json", sa.Text(), nullable=True),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("failure_stage", sa.String(length=32), nullable=True),
            sa.Column("judge_verdict", sa.Text(), nullable=True),
            sa.Column("judge_rationale", sa.Text(), nullable=True),
            sa.Column("prompt_text", sa.Text(), nullable=True),
            sa.Column("response_text", sa.Text(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_eval_case_results_run", "eval_case_results", ["eval_run_id"])

    _seed_judge_budget(bind)


def _seed_judge_budget(bind: sa.engine.Connection) -> None:
    existing = set(sa.inspect(bind).get_table_names())
    if "llm_budgets" not in existing:
        return  # fixture DBs stamped past 0052 — nothing to seed
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'warn', :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    else:  # pre-0066 shape (hand-built fixture DBs)
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    bind.execute(
        sa.text(sql),
        {
            "purpose": _JUDGE_PURPOSE,
            "cap": _JUDGE_MONTHLY_CAP_USD,
            "now": now,
            "notes": "seeded by migration 0083 — eval-harness judge calls "
            "(warn mode: a blown cap surfaces on the dashboard, evals keep running)",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "eval_case_results" in existing:
        op.drop_index("ix_eval_case_results_run", table_name="eval_case_results")
        op.drop_table("eval_case_results")
    if "eval_runs" in existing:
        op.drop_index("ix_eval_runs_run_id", table_name="eval_runs")
        op.drop_index("ix_eval_runs_purpose_started", table_name="eval_runs")
        op.drop_table("eval_runs")
    if "llm_budgets" in existing:
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _JUDGE_PURPOSE},
        )
