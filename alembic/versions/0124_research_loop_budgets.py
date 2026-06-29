"""research loop — per-purpose monthly budget seeds (Ledger Phase-1 W1-5/W1-6).

The three two-pass purposes resolve through ``LLM_MODELS`` (Sonnet tier). Without
their own ``llm_budgets`` rows they fall back to ``__default__`` — wrong attribution
+ the wrong cap-exceeded mode. Seeded:
  * research_fetch              warn  (the web pass — priciest; warn, don't block,
                                      so a busy week degrades to "no fresh fetch"
                                      rather than a hard stop; per-RUN $-cap is
                                      separately clamped by the budget tier).
  * research_adversarial_assess skip  (autonomous internal check — skip on cap).
  * research_narrate            warn  (the writer pass; warn near the cap).

These are MONTHLY aggregate caps; the per-run agentic $-cap is enforced separately
in call_llm_with_web (clamped to the tier budget). Idempotent (ON CONFLICT DO
NOTHING); skips when ``llm_budgets`` is absent.

Revision ID: 0124_research_loop_budgets
Revises: 0123_wondering_detect_budget
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0124_research_loop_budgets"
down_revision: str | Sequence[str] | None = "0123_wondering_detect_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (purpose, monthly_cap_usd, on_exceed)
_SEEDS: tuple[tuple[str, float, str], ...] = (
    ("research_fetch", 30.00, "warn"),
    ("research_adversarial_assess", 10.00, "skip"),
    ("research_narrate", 15.00, "warn"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_budgets" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("llm_budgets")}
    has_on_exceed = "on_exceed" in cols
    now = datetime.now(UTC).isoformat()
    for purpose, cap, on_exceed in _SEEDS:
        if has_on_exceed:
            sql = """
                INSERT INTO llm_budgets
                    (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                     on_exceed, created_at, updated_at, notes)
                VALUES (:purpose, :cap, 0.80, 0, :on_exceed, :now, :now, :notes)
                ON CONFLICT(purpose) DO NOTHING
                """
            params = {
                "purpose": purpose,
                "cap": cap,
                "on_exceed": on_exceed,
                "now": now,
                "notes": "seeded by 0124 — Ledger Phase-1 two-pass research loop",
            }
        else:  # pre-0066 shape (hand-built fixture DBs)
            sql = """
                INSERT INTO llm_budgets
                    (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                     created_at, updated_at, notes)
                VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
                ON CONFLICT(purpose) DO NOTHING
                """
            params = {
                "purpose": purpose,
                "cap": cap,
                "now": now,
                "notes": "seeded by 0124 — Ledger Phase-1 two-pass research loop",
            }
        bind.execute(sa.text(sql), params)


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" not in set(sa.inspect(bind).get_table_names()):
        return
    for purpose, _cap, _mode in _SEEDS:
        bind.execute(sa.text("DELETE FROM llm_budgets WHERE purpose = :p"), {"p": purpose})
