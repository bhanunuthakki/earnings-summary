"""llm_calls — trace_id / span_id / parent_span_id / stage
(LLM Quality Program P1, directives/llm_quality_program_2026_07.md).

Why: diagnosing the July-2026 quota incident took ~15 hand-written SQL
queries because a ledger row knows its purpose but not which PIPELINE STAGE
produced it. "Which stage burns the morning's tokens?" required eyeballing
timestamps. With a stage column it is one GROUP BY.

Owner decision 2026-07-25: bespoke storage with OpenTelemetry GenAI-ALIGNED
semantics — no Phoenix/Langfuse service on the box. Mapping, so a future OTLP
export is a projection rather than a rewrite:

    llm_calls.trace_id        -> trace id (OTel trace)
    llm_calls.span_id         -> span id for the emitting stage
    llm_calls.parent_span_id  -> parent span
    llm_calls.stage           -> platform-native (OTel has no stable
                                 "pipeline stage" attribute yet)
    llm_calls.purpose         ~ gen_ai.operation.name
    llm_calls.model           ~ gen_ai.request.model
    llm_calls.input_tokens    ~ gen_ai.usage.input_tokens
    llm_calls.output_tokens   ~ gen_ai.usage.output_tokens

All nullable: an untraced call (ad-hoc script, unmigrated entrypoint) records
NULLs, which is the honest "not traced" — never a fabricated trace id. The
index is on (stage, called_at) because every intended query is "this stage,
this window".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0206_llm_calls_trace_context"
down_revision: str | Sequence[str] | None = "0205_llm_calls_template_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_calls"
_COLUMNS = ("trace_id", "span_id", "parent_span_id", "stage")
_INDEX = "ix_llm_calls_stage_called_at"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, sa.Text(), nullable=True))
    if _INDEX not in {ix["name"] for ix in insp.get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, ["stage", "called_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    if _INDEX in {ix["name"] for ix in insp.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    existing = {c["name"] for c in insp.get_columns(_TABLE)}
    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
