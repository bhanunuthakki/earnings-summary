"""global_dcf_assumptions — one editable source of truth for macro DCF inputs.

Until now the DCF macro inputs that should be the SAME across every model —
the risk-free rate, the equity/market risk premium, and a default tax rate —
lived as hardcoded literals inside each builder (``build_redesigned_dcf`` RF
0.043 / ERP 0.045 / TAX 0.24, ``build_bank_dcf`` rf 0.043 / erp 0.05 / tax
0.257, the platform/fintech models with their own scalars). Moving the macro
baseline meant editing each model separately and risking silent drift between
them.

This table is the single global default. ``src/dcf/global_assumptions.py``
reads it (builders) and writes it (the dashboard panel). Precedence is
**per-ticker override > this global default > hardcoded fallback**, so a
hand-tuned per-name assumption still wins; the global only governs names that
don't override.

Seeded to EXACTLY the current ``build_redesigned_dcf`` literals (rf 0.043,
erp 0.045, tax 0.24) so an FCFF rebuild immediately after this migration
reproduces byte-identical values — the global only bites once an operator
changes it.

Best-effort consumers: every reader degrades to the in-code seed defaults when
this table / the DB is missing (a from-scratch checkout, a test fixture), so a
DCF build never fails because the global store couldn't be read.

Revision ID: 0112_global_dcf_assumptions
Revises: 0111_fact_overrides
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0112_global_dcf_assumptions"
down_revision: str | Sequence[str] | None = "0111_fact_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (field, value) seeds. Field keys match the per-ticker assumption-JSON keys
# (``risk_free_rate`` / ``equity_risk_premium`` / ``tax_rate``) so the resolver's
# override layer is a plain key lookup. Values match the current FCFF literals
# for zero day-one drift.
_SEED: list[tuple[str, float]] = [
    ("risk_free_rate", 0.043),
    ("equity_risk_premium", 0.045),
    ("tax_rate", 0.24),
]


def upgrade() -> None:
    bind = op.get_bind()
    if "global_dcf_assumptions" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "global_dcf_assumptions",
            # Field key (PK): one row per global input. Text, not an enum, so a
            # later field can be seeded by a follow-up migration without a type
            # change — the module's KNOWN_FIELDS is the validation gate.
            sa.Column("field", sa.Text(), primary_key=True),
            # Decimal ratios (0.043 = 4.3%). REAL is fine; these are dials, not
            # money — the readers quantize nowhere and compare as floats.
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),  # ISO-8601 (naive-UTC)
        )

    # Idempotent seed: re-running upgrade on a populated DB leaves operator edits
    # intact (INSERT ... ON CONFLICT DO NOTHING, SQLite >= 3.24).
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    insert_sql = sa.text(
        """
        INSERT INTO global_dcf_assumptions (field, value, updated_at)
        VALUES (:field, :value, :now)
        ON CONFLICT(field) DO NOTHING
        """
    )
    for field, value in _SEED:
        bind.execute(insert_sql, {"field": field, "value": value, "now": now})


def downgrade() -> None:
    bind = op.get_bind()
    if "global_dcf_assumptions" in sa.inspect(bind).get_table_names():
        op.drop_table("global_dcf_assumptions")
