"""llm_calls — template_id / template_version / template_vars_sha256
(LLM Quality Program P0, directives/llm_quality_program_2026_07.md).

The registry (`llm.prompt_registry`) makes prompts versioned templates with
separated variables; these three nullable columns are where that identity
lands per call. NULL = an unmigrated call site passing a raw string — the
honest representation of partial migration, never backfilled or faked.

OTel GenAI mapping note (P1 groundwork, owner decision: bespoke storage with
OTel-aligned SEMANTICS): purpose ≈ gen_ai.operation.name, model ≈
gen_ai.request.model, template_id/version have no stable OTel attribute yet
(the conventions are still experimental there) — kept as platform-native
names so a future OTLP export is a projection, not a rewrite.

Plain nullable ADD COLUMNs — no table rewrite, cheap on the hot table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0205_llm_calls_template_fields"
down_revision: str | Sequence[str] | None = "0204_eval_case_score_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_calls"
_COLUMNS = ("template_id", "template_version", "template_vars_sha256")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(_TABLE)}
    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
