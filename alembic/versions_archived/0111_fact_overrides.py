"""fact_overrides — company-published documents authoritatively supersede FMP.

See ``directives/provenance_override_2026_06.md``. FMP is a convenience tier, not
authoritative. When an authoritative company document (SEC 8-K / 10-Q / 10-K,
earnings press release, IR deck / supplement) reports a value for a metric, that
value should SYSTEMATICALLY win over FMP's cached/derived value for the same
logical fact — keyed by ``(ticker, period_end, fiscal_period_type, line_item /
segment / KPI)``. The source-quality tier ladder already lets ``sec_official``
beat ``fmp_normalized``, but the documents the owner most wants to win —
press-release / IR-deck exhibits — ingest as ``IR_DOC`` (a TIE with FMP) or
``LLM_EXTRACTED`` (LOSES to FMP), and several readers (the Financials ``metrics``
VIEW, the direct-JSON DCF readers) bypass the tier ladder entirely. So the durable
mechanism is a first-class override record consulted at read/resolve time.

This table is that record. It lives in the DB — OUTSIDE the FMP cache files — so a
``save_fmp_data`` / ``fetch_fmp_historical_data`` re-fetch cannot clobber it. One
ACTIVE row per logical fact; superseded overrides are retained as ``retired`` rows
(a partial-unique index enforces the single-active invariant). The resolver
(``src/provenance/overrides.py``) is the single consult point every read path uses.

Motivating case: FMP served a corrupt GOOG Q4'25 (2025-12-31) product-segmentation
record (Google Cloud $20,941M/+75% vs the SEC 8-K's $17,664M/+48%; seg-sum 1.38x
revenue). It was corrected manually in the raw cache (#592), but a re-fetch
re-introduces the bad data. A record-level ``segment`` override seeded from the 8-K
(accession 0001652044-26-000012, exhibit googexhibit991q42025.htm) makes the
correct figures win at resolve time regardless of cache state.

Revision ID: 0111_fact_overrides
Revises: 0110_pass_decision_source
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0111_fact_overrides"
down_revision: str | Sequence[str] | None = "0110_pass_decision_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FACT_KINDS = "('financial_fact', 'segment', 'kpi')"
_ACTIONS = "('replace', 'drop', 'qualify')"
_STATUS = "('active', 'retired')"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "fact_overrides" in insp.get_table_names():
        return  # idempotent
    op.create_table(
        "fact_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
        # Logical fact key: (ticker, period_end, fiscal_period_type, fact_kind, fact_key).
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Text(), nullable=False),  # 'YYYY-MM-DD' (naive-UTC)
        sa.Column("fiscal_period_type", sa.Text(), nullable=False),  # Q1..Q4 | FY | H1 ...
        sa.Column("fact_kind", sa.Text(), nullable=False),  # financial_fact | segment | kpi
        # Canonical within-period key per kind (see overrides.py):
        #   financial_fact -> line_item ; kpi -> KPI definition name ;
        #   segment record -> dim_type ; segment cell -> "dim_type|dim_name|metric".
        sa.Column("fact_key", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),  # replace | drop | qualify
        # Authoritative payload: scalar (value/unit) for financial_fact/kpi/segment-cell;
        # structured value_json ({segment_name: value, ...}) for a segment-record replace.
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("value_json", sa.Text(), nullable=True),
        # Company-doc provenance (why this override is authoritative).
        sa.Column("source_doc_type", sa.Text(), nullable=False),  # DocType: sec_8k|sec_10q|...
        sa.Column("source_accession", sa.Text(), nullable=True),  # EDGAR accession
        sa.Column("source_exhibit", sa.Text(), nullable=True),  # exhibit filename
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_excerpt", sa.Text(), nullable=True),  # verbatim quote
        sa.Column("source_doc_id", sa.Integer(), nullable=True),  # documents.id when ingested
        # Governance.
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),  # 'manual:bhanu' | 'agent:<id>' | 'cli'
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.Text(), nullable=True),
        sa.CheckConstraint(f"fact_kind IN {_FACT_KINDS}", name="ck_fact_overrides_kind"),
        sa.CheckConstraint(f"action IN {_ACTIONS}", name="ck_fact_overrides_action"),
        sa.CheckConstraint(f"status IN {_STATUS}", name="ck_fact_overrides_status"),
    )
    # One ACTIVE override per logical fact; retired history coexists.
    op.create_index(
        "uq_fact_overrides_active",
        "fact_overrides",
        ["user_id", "ticker", "period_end", "fiscal_period_type", "fact_kind", "fact_key"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_fact_overrides_lookup",
        "fact_overrides",
        ["ticker", "fact_kind", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "fact_overrides" not in insp.get_table_names():
        return
    op.drop_index("ix_fact_overrides_lookup", table_name="fact_overrides")
    op.drop_index("uq_fact_overrides_active", table_name="fact_overrides")
    op.drop_table("fact_overrides")
