"""prompt_arms + per-arm verdicts — multi-arm prompt A/B with combination
testing (meta_eval_governance.md §4, extended 2026-07-24).

The §4 build (mig 0136) modelled an experiment as exactly TWO arms: the
baseline, and one variant carried on ``prompt_experiments.edits_json``. That
shape cannot express the two things the loop needs to actually improve prompts
over time:

  * **parallel arms** — several independently-proposed variants judged against
    ONE shared case sample. The baseline side is the expensive half (it is
    re-run only when the capture's model differs from the frozen model, but the
    JUDGING is paid per pair), so amortising one sample across k arms is far
    cheaper than k separate two-arm experiments, and it removes the
    sample-to-sample variance that makes separate experiments incomparable.
  * **composed arms** — an arm whose edit list is the UNION of two edit sets.
    Two edits that each win alone can lose together (a length budget added on
    top of a demand for an explicit reasoning chain is the obvious collision).
    With two-arm experiments that interaction is invisible: both promote
    separately, both get applied, and the regression only shows up later as a
    mysterious quality drop. A composed arm measures it directly, which is what
    the new INTERACTION_NEGATIVE verdict records.

Renumbered 0200->0202 post-merge: #1002's 0201_senior_partner_brief_budget
landed first off the same 0199 parent (two heads). Re-chained per the
alembic-collision convention.

Schema:

``prompt_arms`` — one row per non-baseline arm. The baseline is implicit
(never a row): it is defined by the captured prompt itself, and giving it a row
would invite an "edits_json = []" arm that ``apply_edits`` would treat as a
no-op variant.

``prompt_ab_verdicts.arm_label`` — nullable, additive. NULL means a legacy
two-arm verdict written before this migration, which is exactly how the reader
distinguishes the old shape from the new one rather than guessing. There are
zero such rows in production today (the loop never ran), so this is a
forward-compatibility affordance, not a data migration.

``prompt_experiments`` gains the provenance the randomized cycle needs to be
replayable and auditable: ``cycle_id`` (which cycle spawned it), ``rng_seed``
(the seed every draw in that cycle came from), and ``signal_json`` (the
improvement signal + deficit the proposal was steered by). Without the seed a
"randomized process" is unreproducible, and an unreproducible experiment cannot
be re-run to check a surprising verdict.

``edits_json`` on ``prompt_experiments`` stays NOT NULL and keeps carrying the
FIRST arm's edits. That is deliberate: ``tests/test_prompt_ab.py`` and the
single-arm ``--propose`` path still read it, and a reader that finds no
``prompt_arms`` rows falls back to it as a one-arm experiment. The fallback is
distinguishable (arm rows present or absent), never a silent reinterpretation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0202_prompt_ab_arms"
down_revision: str | Sequence[str] | None = "0201_senior_partner_brief_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ARMS = "prompt_arms"
_VERDICTS = "prompt_ab_verdicts"
_EXPERIMENTS = "prompt_experiments"


def _columns(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if _ARMS not in tables:
        op.create_table(
            _ARMS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experiment_id", sa.Text(), nullable=False),
            # 'A', 'B', 'C', ... — stable within an experiment; the baseline is
            # implicit and never carries a label.
            sa.Column("arm_label", sa.Text(), nullable=False),
            sa.Column("edits_json", sa.Text(), nullable=False),
            sa.Column("hypothesis", sa.Text(), nullable=False, server_default=""),
            # The drawn strategy key(s). Comma-separated for a composed arm, so
            # the bandit can attribute the outcome to every strategy involved.
            sa.Column("strategy_key", sa.Text(), nullable=True),
            # 'fresh' (newly proposed) | 'composed' (union of two prior arms)
            sa.Column("source", sa.Text(), nullable=False, server_default="fresh"),
            # For composed arms: the arm labels whose edits were unioned, so a
            # negative interaction can name its components.
            sa.Column("composed_from", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.UniqueConstraint("experiment_id", "arm_label", name="uq_prompt_arms_exp_label"),
        )
        op.create_index("ix_prompt_arms_experiment", _ARMS, ["experiment_id"])

    if _VERDICTS in tables and "arm_label" not in _columns(insp, _VERDICTS):
        # Nullable + no backfill: NULL is the legacy-two-arm marker (see docstring).
        op.add_column(_VERDICTS, sa.Column("arm_label", sa.Text(), nullable=True))

    if _EXPERIMENTS in tables:
        cols = _columns(insp, _EXPERIMENTS)
        if "cycle_id" not in cols:
            op.add_column(_EXPERIMENTS, sa.Column("cycle_id", sa.Text(), nullable=True))
        if "rng_seed" not in cols:
            op.add_column(_EXPERIMENTS, sa.Column("rng_seed", sa.Text(), nullable=True))
        if "signal_json" not in cols:
            op.add_column(_EXPERIMENTS, sa.Column("signal_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if _EXPERIMENTS in tables:
        cols = _columns(insp, _EXPERIMENTS)
        for column in ("signal_json", "rng_seed", "cycle_id"):
            if column in cols:
                op.drop_column(_EXPERIMENTS, column)

    if _VERDICTS in tables and "arm_label" in _columns(insp, _VERDICTS):
        op.drop_column(_VERDICTS, "arm_label")

    if _ARMS in tables:
        op.drop_index("ix_prompt_arms_experiment", table_name=_ARMS)
        op.drop_table(_ARMS)
