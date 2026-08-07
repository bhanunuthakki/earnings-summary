"""insider_and_exec_comp — Form 4 insider activity + DEF 14A exec compensation.

Three tables underpin the executive compensation report:

  insider_transactions  — Form 4 (US) + Form 144 + foreign-regime equivalents
                          (Canadian SEDI, UK PDMR, Indian SEBI). Captures
                          open-market buys, sells, exercises, gifts, 10b5-1
                          status. The conviction signal: CEO/CFO open-market
                          buys (not exercises, not 10b5-1 sells) are the
                          highest-signal events.

  exec_comp_packages    — Per-NEO compensation from DEF 14A: salary, bonus
                          target/actual, equity grant value + breakdown,
                          performance metrics with targets and weights,
                          peer group for benchmarking, change-in-control
                          terms, hedging/pledging policy, CEO pay ratio.
                          Annual extractor runs on new proxies (Opus call).

  exec_holdings         — Total beneficial ownership snapshots from DEF 14A
                          Item 12 + Form 4 running balances. Powers
                          "how much skin in the game" view.

Cross-references:
  - insider entity_id FK into entities(kind='person'); the same person row
    is reused across insider_transactions, exec_comp_packages, and the
    'sits_on_board_of' relationship table for board interlocks.
  - exec_comp_packages.executive_entity_id likewise.
  - performance_metrics_json fields enable the alignment lens: do the
    metrics in management's pay design actually match the thesis tier-1
    KPIs?

Revision ID: 0041_insider_and_exec_comp
Revises: 0040_filing_detail_tables
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_insider_and_exec_comp"
down_revision: str | Sequence[str] | None = "0040_filing_detail_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # ------------------------------------------------------------------
    # insider_transactions — Form 4 + equivalents
    # ------------------------------------------------------------------
    if "insider_transactions" not in existing:
        op.create_table(
            "insider_transactions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            # Insider identity — names appear inconsistently across filings
            # (with/without middle initial, all-caps, etc.); resolve to a
            # canonical person entity for deduplication.
            sa.Column("insider_entity_id", sa.Integer(), nullable=True),
            sa.Column("insider_name", sa.String(length=255), nullable=False),
            sa.Column("insider_title", sa.String(length=255), nullable=True),  # 'Chief Executive Officer'
            sa.Column("insider_relation", sa.String(length=64), nullable=True),
            # 'officer' | 'director' | '10% owner' | 'officer_director' (overlap)
            sa.Column("transaction_date", sa.DateTime(), nullable=False),
            sa.Column("filing_date", sa.DateTime(), nullable=False),
            sa.Column("filing_doc_id", sa.Integer(), nullable=True),
            sa.Column("transaction_type", sa.String(length=32), nullable=False),
            # The signal-carrying values:
            #   'open_market_buy'   — discretionary purchase (highest signal)
            #   'open_market_sell'  — discretionary sale (signal context-dependent)
            #   'option_exercise'   — usually noise; only signal if held post-exercise
            #   'option_exercise_and_sell' — neutral cash-out
            #   'stock_grant'       — comp event, not insider conviction
            #   'rsu_vesting'       — comp event
            #   'gift'              — usually charitable; not signal
            #   'other'             — catch-all
            sa.Column("shares", sa.Numeric(24, 6), nullable=False),
            sa.Column("price_per_share", sa.Numeric(24, 6), nullable=True),
            sa.Column("transaction_value", sa.Numeric(24, 6), nullable=True),  # shares * price
            sa.Column("shares_owned_after", sa.Numeric(24, 6), nullable=True),
            sa.Column("ownership_pct", sa.Float(), nullable=True),
            # Section 16(a) plan flag — 10b5-1 sells are pre-scheduled and
            # carry MUCH less signal than discretionary trades.
            sa.Column("is_10b5_1", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("acquired_or_disposed", sa.String(length=1), nullable=True),  # 'A' | 'D'
            sa.Column("ownership_form", sa.String(length=1), nullable=True),  # 'D' direct | 'I' indirect
            sa.Column("form_filed", sa.String(length=8), nullable=False, server_default="4"),
            # 'Form 4' | 'Form 144' | 'Form 5' | 'Form 3' (initial) | 'SEDI' | 'PDMR' | 'SEBI'
            sa.Column("regulator", sa.String(length=16), nullable=False, server_default="SEC"),
            # 'SEC' | 'SEDI' (Canada) | 'PDMR' (UK) | 'SEBI' (India) | 'AFM' (NL) | 'Finanstilsynet' (DK)
            sa.Column("source_url", sa.String(length=1024), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("ingested_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ticker",
                "insider_name",
                "transaction_date",
                "transaction_type",
                "shares",
                "form_filed",
                name="uq_insider_tx",
            ),
        )
        op.create_index(
            "idx_insider_tx_ticker_date", "insider_transactions", ["ticker", "transaction_date"]
        )
        op.create_index(
            "idx_insider_tx_type_date", "insider_transactions", ["transaction_type", "transaction_date"]
        )
        op.create_index(
            "idx_insider_tx_entity", "insider_transactions", ["insider_entity_id"]
        )
        op.create_index("idx_insider_tx_regulator", "insider_transactions", ["regulator"])

    # ------------------------------------------------------------------
    # exec_comp_packages — DEF 14A NEO comp
    # ------------------------------------------------------------------
    if "exec_comp_packages" not in existing:
        op.create_table(
            "exec_comp_packages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=False),
            sa.Column("proxy_doc_id", sa.Integer(), nullable=True),
            sa.Column("executive_entity_id", sa.Integer(), nullable=True),
            sa.Column("executive_name", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=128), nullable=True),  # 'CEO' | 'CFO' | etc.
            sa.Column("is_ceo", sa.Boolean(), nullable=False, server_default="0"),
            # Comp breakdown — all in reporting currency
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
            sa.Column("base_salary", sa.Numeric(24, 2), nullable=True),
            sa.Column("cash_bonus_target", sa.Numeric(24, 2), nullable=True),
            sa.Column("cash_bonus_actual", sa.Numeric(24, 2), nullable=True),
            sa.Column("equity_grant_value", sa.Numeric(24, 2), nullable=True),
            sa.Column("equity_grant_breakdown_json", sa.Text(), nullable=True),
            # JSON: {"rsu_value": X, "psu_value": Y, "options_value": Z, "psu_target_shares": N, ...}
            sa.Column("other_comp", sa.Numeric(24, 2), nullable=True),
            sa.Column("total_comp_granted", sa.Numeric(24, 2), nullable=True),
            sa.Column("total_comp_realized", sa.Numeric(24, 2), nullable=True),  # realized pay (actual vests)
            # Performance design — KEY for alignment analysis
            sa.Column("performance_metrics_json", sa.Text(), nullable=True),
            # [{"metric": "Revenue growth", "weight": 0.4, "threshold": 0.08, "target": 0.12, "max": 0.18, "actual": 0.14}, ...]
            sa.Column("peer_group_json", sa.Text(), nullable=True),  # list of peer tickers/names
            sa.Column("cic_terms_json", sa.Text(), nullable=True),  # change-in-control treatment
            sa.Column("hedging_pledging_policy", sa.String(length=512), nullable=True),
            sa.Column("ceo_pay_ratio", sa.Numeric(12, 2), nullable=True),  # CEO comp / median employee
            sa.Column("source_excerpt", sa.Text(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("ticker", "fiscal_year", "executive_name", name="uq_exec_comp"),
        )
        op.create_index(
            "idx_exec_comp_ticker_year", "exec_comp_packages", ["ticker", "fiscal_year"]
        )
        op.create_index("idx_exec_comp_entity", "exec_comp_packages", ["executive_entity_id"])
        op.create_index("idx_exec_comp_role", "exec_comp_packages", ["ticker", "role"])

    # ------------------------------------------------------------------
    # exec_holdings — beneficial ownership snapshots
    # ------------------------------------------------------------------
    if "exec_holdings" not in existing:
        op.create_table(
            "exec_holdings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("as_of_date", sa.DateTime(), nullable=False),
            sa.Column("executive_entity_id", sa.Integer(), nullable=True),
            sa.Column("executive_name", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=128), nullable=True),
            sa.Column("shares_beneficially_owned", sa.Numeric(24, 6), nullable=False),
            sa.Column("pct_of_class", sa.Float(), nullable=True),
            # Components — sometimes broken out in DEF 14A Item 12
            sa.Column("shares_direct", sa.Numeric(24, 6), nullable=True),
            sa.Column("shares_indirect", sa.Numeric(24, 6), nullable=True),
            sa.Column("shares_exercisable_within_60d", sa.Numeric(24, 6), nullable=True),
            sa.Column("source_doc_id", sa.Integer(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ticker", "executive_name", "as_of_date", name="uq_exec_holdings"
            ),
        )
        op.create_index(
            "idx_exec_holdings_ticker_date", "exec_holdings", ["ticker", "as_of_date"]
        )
        op.create_index("idx_exec_holdings_entity", "exec_holdings", ["executive_entity_id"])


def downgrade() -> None:
    for t in ("exec_holdings", "exec_comp_packages", "insider_transactions"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
