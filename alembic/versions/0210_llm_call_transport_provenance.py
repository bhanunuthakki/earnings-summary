"""llm_calls -- provider, transport, retries and failure provenance.

The original ledger captured model and a free-form error, but could not tell
whether a call used subscription credentials or a metered endpoint.  That made
the intended Claude -> Codex -> explicitly opted-in OpenRouter routing chain
unauditable.  Fields are nullable so old writers and historical rows retain
their honest unknown state.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0210_llm_call_transport_provenance"
down_revision: str | Sequence[str] | None = "0209_transcripts_period_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_calls"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("provider", sa.Text()),
    ("transport", sa.Text()),
    ("attempts", sa.Integer()),
    ("retries", sa.Integer()),
    ("outcome", sa.Text()),
    ("failure_class", sa.Text()),
)


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
