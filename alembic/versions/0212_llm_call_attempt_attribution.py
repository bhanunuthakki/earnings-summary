"""Add explicit auth, attempt-count, and fallback-source attribution.

Migration 0210 introduced the first transport-provenance fields.  These
additional nullable columns make the contract unambiguous for new governed
calls while preserving all historical rows and older writers:

* ``auth_class`` distinguishes subscription/membership calls from metered keys.
* ``attempt_count`` / ``retry_count`` use count-shaped names (the 0210
  ``attempts`` / ``retries`` columns remain as compatibility projections).
* ``fallback_from_provider`` / ``fallback_from_transport`` identify the failed
  upstream tier rather than overloading ``fallback_used`` (the selected tier).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0212_llm_call_attempt_attribution"
down_revision: str | Sequence[str] | None = "0211_data_integrity_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_calls"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("auth_class", sa.Text()),
    ("attempt_count", sa.Integer()),
    ("retry_count", sa.Integer()),
    ("fallback_from_provider", sa.Text()),
    ("fallback_from_transport", sa.Text()),
)


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))

    # Preserve the information already written by 0210-era governed callers.
    # Historical rows still retain NULL auth/fallback-source values because
    # those facts cannot be reconstructed safely.
    op.execute(
        sa.text(
            "UPDATE llm_calls "
            "SET attempt_count = attempts, retry_count = retries "
            "WHERE attempt_count IS NULL OR retry_count IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
