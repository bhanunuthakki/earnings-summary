"""self_update_triggers — extend dirty propagation to LLM artifacts + insights.

Migration 0026 added 8 SQL triggers that auto-flip
tracked_companies.brief_dirty=1 on any INSERT/UPDATE to financial_facts /
kpi_facts / segment_facts / dcf_runs. This migration extends that pattern:

When upstream facts change, mark the *downstream LLM artifacts and
insights* that depend on them dirty too. The daily drain cron
(execution/refresh_dirty_artifacts.py) regenerates dirty artifacts on
the next tick, keeping briefs current without manual rebuilds.

Dependency map (artifact purpose → which fact-table flips them dirty):

  bear_case             ← financial_facts | kpi_facts | segment_facts | thesis edits
  company_description   ← documents (10-K refresh) | segment_facts (current period)
  filing_intelligence   ← documents (10-K refresh)
  valuation_basis       ← financial_facts (latest period) | thesis edits
  recent_developments   ← TTL (7d) — handled in the section builder, not here
  saydo_filter          ← management_commitments insert/update
  exec_comp_alignment   ← insider_transactions | exec_comp_packages

The same is done for insights: any insight whose lens declares an
upstream dependency gets marked dirty when that table changes. Lens
dependencies live in the lens prompt file's `inputs:` frontmatter,
but for now we apply a conservative blanket rule: any new earnings
ingest dirties per-ticker insights.

Revision ID: 0043_self_update_triggers
Revises: 0042_matching_infra
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_self_update_triggers"
down_revision: str | Sequence[str] | None = "0042_matching_infra"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Each trigger marks a SET of purposes dirty for a ticker when a row in the
# upstream table is inserted or updated. Conservative: we set dirty even
# when the underlying value didn't change (the input_sha256 check in the
# artifact store decides whether re-LLM is actually needed). This keeps
# trigger logic SQLite-simple.
_TRIGGERS_SQL = """
-- financial_facts → bear_case, valuation_basis
CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_financial_insert
AFTER INSERT ON financial_facts
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'financial_facts_insert'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('bear_case', 'valuation_basis');
END;

CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_financial_update
AFTER UPDATE OF value ON financial_facts
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'financial_facts_update'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('bear_case', 'valuation_basis');
END;

-- kpi_facts → bear_case
CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_kpi_insert
AFTER INSERT ON kpi_facts
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'kpi_facts_insert'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('bear_case');
END;

-- segment_facts → bear_case, company_description
CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_segment_insert
AFTER INSERT ON segment_facts
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'segment_facts_insert'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('bear_case', 'company_description');
END;

-- management_commitments → saydo_filter, bear_case (it cites mgmt promises)
CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_commitment_insert
AFTER INSERT ON management_commitments
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'management_commitments_insert'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('saydo_filter', 'bear_case');
END;

-- insider_transactions → exec_comp_alignment
CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_insider_insert
AFTER INSERT ON insider_transactions
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'insider_transactions_insert'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('exec_comp_alignment');
END;

-- exec_comp_packages → exec_comp_alignment
CREATE TRIGGER IF NOT EXISTS trg_artifacts_dirty_on_exec_comp_insert
AFTER INSERT ON exec_comp_packages
BEGIN
  UPDATE llm_artifacts
  SET dirty = 1, dirty_reason = 'exec_comp_packages_insert'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND purpose IN ('exec_comp_alignment');
END;

-- predictions outcome update → mgmt_credibility insights
-- Lens-driven insights get dirty on any prediction grade flip.
CREATE TRIGGER IF NOT EXISTS trg_insights_dirty_on_prediction_grade
AFTER UPDATE OF outcome ON predictions
BEGIN
  UPDATE insights
  SET dirty = 1, dirty_reason = 'prediction_outcome_update'
  WHERE ticker = NEW.ticker
    AND superseded_by_id IS NULL
    AND dirty = 0
    AND lens IN ('mgmt_credibility_score', 'cross_ticker_theme');
END;
"""


def upgrade() -> None:
    # Apply each trigger as an independent statement so SQLite parses them
    # one at a time (its CREATE TRIGGER doesn't multi-statement-parse well).
    for stmt in _TRIGGERS_SQL.strip().split(";\n\n"):
        s = stmt.strip()
        if not s:
            continue
        if not s.endswith(";"):
            s = s + ";"
        op.execute(s)


def downgrade() -> None:
    for trg in (
        "trg_insights_dirty_on_prediction_grade",
        "trg_artifacts_dirty_on_exec_comp_insert",
        "trg_artifacts_dirty_on_insider_insert",
        "trg_artifacts_dirty_on_commitment_insert",
        "trg_artifacts_dirty_on_segment_insert",
        "trg_artifacts_dirty_on_kpi_insert",
        "trg_artifacts_dirty_on_financial_update",
        "trg_artifacts_dirty_on_financial_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trg}")
