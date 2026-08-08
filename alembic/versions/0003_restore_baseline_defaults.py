"""Restore migration-owned defaults omitted by the squashed baseline.

Revision ID: 0003_restore_baseline_defaults
Revises: 0002_drop_dead_tables
Create Date: 2026-08-07
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0003_restore_baseline_defaults"
down_revision = "0002_drop_dead_tables"
branch_labels = None
depends_on = None


# purpose, monthly cap, cap-exceeded behavior. These are the final defaults from
# the archived chain, not values read from the operator's live database.
_LLM_BUDGETS: tuple[tuple[str, float, str], ...] = (
    ("__default__", 25, "warn"),
    ("artifact_brief", 15, "warn"),
    ("ask_answer", 25, "warn"),
    ("ask_claim_audit", 5, "block"),
    ("ask_claim_grounding", 5, "skip"),
    ("ask_evidence_followup", 10, "skip"),
    ("ask_pack_router", 5, "skip"),
    ("bear_case", 50, "warn"),
    ("bear_case_grading", 15, "warn"),
    ("behavior_distill", 10, "skip"),
    ("canonicalize_segments", 5, "warn"),
    ("capture_intent", 5, "warn"),
    ("capture_triage", 5, "warn"),
    ("case_difficulty_classify", 2, "warn"),
    ("coach_reply_intent", 2, "warn"),
    ("company_description", 20, "warn"),
    ("decision_conditions_extract", 5, "warn"),
    ("decision_draft_parse", 8, "skip"),
    ("earnings_themes_split", 30, "warn"),
    ("eval_judge", 10, "warn"),
    ("exec_comp_alignment", 15, "warn"),
    ("exec_comp_extraction", 30, "warn"),
    ("exit_postmortem_draft", 5, "warn"),
    ("footnote_extraction", 20, "warn"),
    ("incremental_dollar_recommendation", 10, "block"),
    ("investment_decision_card", 8, "skip"),
    ("investor_deck_extraction", 5, "warn"),
    ("lens:bull_case", 20, "warn"),
    ("lens:catalyst_calendar", 15, "warn"),
    ("lens:cross_portfolio_synthesis", 20, "warn"),
    ("lens:filing_diff_narrative", 10, "warn"),
    ("lens:five_min_reread", 30, "warn"),
    ("lens:footnote_anomaly", 10, "warn"),
    ("lens:reverse_dcf", 15, "warn"),
    ("lens:thesis_drift_qoq", 25, "warn"),
    ("lens:underweighted_facts", 20, "warn"),
    ("model_frontier_research", 3, "warn"),
    ("optimizer_nominator", 3, "warn"),
    ("pairwise_analysis", 30, "warn"),
    ("podcast_takeaway_summary", 5, "skip"),
    ("positioning_coach_turn", 10, "warn"),
    ("positioning_encode", 10, "warn"),
    ("post_earnings_readout", 5, "skip"),
    ("pre_earnings_brief", 5, "skip"),
    ("prompt_variant_propose", 5, "warn"),
    ("query_criteria_derive", 5, "warn"),
    ("recent_developments", 80, "warn"),
    ("research_adversarial_assess", 10, "skip"),
    ("research_fetch", 30, "warn"),
    ("research_narrate", 15, "warn"),
    ("research_triage", 3, "warn"),
    ("risk_factor_classify", 5, "warn"),
    ("saydo_commitment_extract", 40, "warn"),
    ("sector_benchmark_proposal", 5, "skip"),
    ("segment_10q_period_disambiguate", 10, "skip"),
    ("senior_partner_brief", 6, "block"),
    ("session_distill", 15, "warn"),
    ("tenet_accountability", 10, "warn"),
    ("tenet_distill", 10, "skip"),
    ("theme_seed_cluster", 10, "skip"),
    ("theme_synthesis", 10, "skip"),
    ("transcript_summary", 80, "warn"),
    ("valuation_basis", 15, "warn"),
    ("viewspec_compile", 5, "skip"),
    ("weekly_packet_predraft", 5, "warn"),
    ("wondering_detect", 5, "skip"),
)


# ticker, KPI routing summary, primary source, fallback source, IR URL.
_KPI_ROUTES: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    (
        "GOOG",
        "Segment OI (Services / Cloud / Other Bets); Cloud RPO; paid-clicks; CPC; YouTube ads",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    (
        "META",
        "Family-of-Apps OI vs Reality Labs OI; DAP; ad impressions; ad price; capex by category",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    ("AMZN", "AWS / NA-retail / Intl OI; ads revenue; Prime metrics", "sec_xbrl", "ir_doc", None),
    (
        "NOW",
        "cRPO; RPO; NRR; customers >$1M ACV; sub gross margin",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "VEEV",
        "Subscription rev split; billings; NRR; customer count",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "RBRK",
        "Subscription ARR; NRR; $100k+ customers; FCF margin",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "WIX",
        "Bookings; Creative Subs vs Business Solutions; take rate; GPV",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "NVO",
        "Drug-level rev (Wegovy/Ozempic/Rybelsus); insulin volume; region mix; pipeline",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    (
        "MELI",
        "GMV; items sold; TPV; fintech MAU; credit NPL; take rate",
        "ir_doc",
        "transcript_audio",
        None,
    ),
    (
        "NU",
        "15-90d NPL; 90+ NPL; NIM; NIMAL; ARPAC; MAU; deposit cost; cost-to-serve",
        "ir_doc",
        "sec_xbrl",
        "https://investors.nu/",
    ),
    (
        "BN",
        "Distributable earnings/share; fee-bearing capital; BAM stake; deployed capital",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    (
        "LLY",
        "Drug-level rev (Mounjaro/Zepbound/Trulicity/Verzenio/Taltz); pipeline readouts",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    ("AMAT", "Bookings/backlog by segment; DRAM vs NAND mix", "ir_doc", "transcript_audio", None),
    ("FCX", "Copper volumes; gold by-product; AISC; Grasberg ramp", "sec_xbrl", "ir_doc", None),
    (
        "WY",
        "SF housing demand; mill volumes; log/lumber pricing",
        "sec_xbrl",
        "transcript_audio",
        None,
    ),
    (
        "JPM",
        "NII; NIM; ROTCE; CET1; charge-offs; IB fees; deposit beta",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    ("TOL", "Orders; backlog; ASP; community count; cancellation rate", "ir_doc", "sec_xbrl", None),
    ("ASML", "Bookings (EUV/DUV); backlog; China revenue mix", "ir_doc", "transcript_audio", None),
    ("BHP", "Production by commodity; AISC; capex by project", "ir_doc", "sec_xbrl", None),
    (
        "RIO",
        "Production by commodity; AISC; project capex (Simandou, Oyu Tolgoi)",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    (
        "VALE",
        "Iron ore production; premium product mix; AISC; base metals",
        "ir_doc",
        "sec_xbrl",
        None,
    ),
    ("ABNB", "Nights & Experiences; GBV; ADR; take rate; regional mix", "sec_xbrl", "ir_doc", None),
    (
        "SOFI",
        "Members; products per member; NIM; charge-offs; Galileo accounts",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    (
        "LMND",
        "In-force premium; customer count; loss ratio; gross loss ratio; IFP/customer",
        "sec_xbrl",
        "ir_doc",
        None,
    ),
    (
        "HDB",
        "NIM; gross/net NPA; deposit growth; advances growth; cost-to-income",
        "ir_doc",
        "manual_csv",
        None,
    ),
    ("FNV", "GEOs sold; mining royalty mix; energy royalty mix", "sec_xbrl", "ir_doc", None),
    ("CNQ", "Production by basin; AECO/WCS realized prices; FCF", "sec_xbrl", "ir_doc", None),
)


# source key, class, name, weight, tier, tags, CIK.
_DISCOVERY_SOURCES: tuple[tuple[str, str, str, float, str, str, str | None], ...] = (
    (
        "quality_compounder",
        "screen",
        "Quality-compounder screen",
        1.0,
        "structural",
        "screen",
        None,
    ),
    ("fcf_value", "screen", "Cash-generative value screen", 0.9, "structural", "screen", None),
    ("growth_inflection", "screen", "Growth-inflection screen", 1.0, "structural", "screen", None),
    (
        "watchlist",
        "adjacency",
        "On a holding's competitive watchlist",
        1.0,
        "structural",
        "intentional",
        None,
    ),
    (
        "transcript",
        "adjacency",
        "Named in a holding's earnings calls",
        0.7,
        "structural",
        "incidental",
        None,
    ),
    ("news", "adjacency", "Named in a holding's news", 0.5, "structural", "incidental", None),
    (
        "altimeter",
        "investor_13f",
        "Altimeter Capital",
        1.0,
        "crossover",
        "hedge,crossover",
        "0001541617",
    ),
    ("atreides", "investor_13f", "Atreides Management", 1.0, "crossover", "hedge,crossover", None),
    (
        "whale_rock",
        "investor_13f",
        "Whale Rock Capital",
        0.9,
        "crossover",
        "hedge,crossover",
        "0001387322",
    ),
    (
        "light_street",
        "investor_13f",
        "Light Street Capital",
        0.85,
        "crossover",
        "hedge,crossover",
        None,
    ),
    (
        "coatue",
        "investor_13f",
        "Coatue Management",
        0.8,
        "crossover",
        "hedge,crossover",
        "0001135730",
    ),
    (
        "lone_pine",
        "investor_13f",
        "Lone Pine Capital",
        0.8,
        "crossover",
        "hedge,crossover",
        "0001061165",
    ),
    ("d1", "investor_13f", "D1 Capital Partners", 0.75, "crossover", "hedge,crossover", None),
    (
        "baillie_gifford",
        "investor_13f",
        "Baillie Gifford",
        0.65,
        "crossover",
        "long_only,low_turnover",
        None,
    ),
    (
        "durable",
        "investor_13f",
        "Durable Capital Partners",
        0.6,
        "crossover",
        "hedge,crossover",
        None,
    ),
    (
        "viking",
        "investor_13f",
        "Viking Global Investors",
        0.6,
        "crossover",
        "hedge,crossover",
        None,
    ),
    (
        "tiger_global",
        "investor_13f",
        "Tiger Global Management",
        0.6,
        "crossover",
        "hedge,crossover",
        "0001167483",
    ),
    ("maverick", "investor_13f", "Maverick Capital", 0.6, "crossover", "hedge,crossover", None),
    ("tremblant", "investor_13f", "Tremblant Capital", 0.55, "crossover", "hedge,crossover", None),
    (
        "dragoneer",
        "investor_13f",
        "Dragoneer Investment Group",
        0.55,
        "crossover",
        "hedge,crossover",
        None,
    ),
    ("alkeon", "investor_13f", "Alkeon Capital", 0.55, "crossover", "hedge,crossover", None),
    ("greenoaks", "investor_13f", "Greenoaks Capital", 0.5, "crossover", "hedge,crossover", None),
    ("addition", "investor_13f", "Addition", 0.45, "crossover", "hedge,crossover", None),
    ("ark", "investor_13f", "ARK Investment Management", 0.4, "crossover", "long_only,etf", None),
    ("iconiq", "investor_13f", "ICONIQ Capital", 0.4, "crossover", "hedge,crossover", None),
    (
        "tybourne",
        "investor_13f",
        "Tybourne Capital (wind-down)",
        0.3,
        "crossover",
        "hedge,crossover",
        None,
    ),
    (
        "appaloosa",
        "investor_13f",
        "Appaloosa (Tepper)",
        0.85,
        "multi_cycle",
        "hedge,concentrated",
        "0001656456",
    ),
    ("sands", "investor_13f", "Sands Capital", 0.8, "multi_cycle", "long_only,low_turnover", None),
    (
        "contrafund",
        "investor_13f",
        "Fidelity Contrafund (Danoff)",
        0.7,
        "multi_cycle",
        "long_only",
        None,
    ),
    (
        "wcm",
        "investor_13f",
        "WCM Investment Management",
        0.7,
        "multi_cycle",
        "long_only,low_turnover",
        None,
    ),
    (
        "loomis_growth",
        "investor_13f",
        "Loomis Sayles Growth (Hamzaogullari)",
        0.7,
        "multi_cycle",
        "long_only,low_turnover",
        None,
    ),
    (
        "polen",
        "investor_13f",
        "Polen Capital (Focus Growth)",
        0.65,
        "multi_cycle",
        "long_only,low_turnover",
        None,
    ),
    (
        "akre",
        "investor_13f",
        "Akre Capital (Neff)",
        0.65,
        "multi_cycle",
        "long_only,low_turnover",
        "0001112520",
    ),
    (
        "edgewood",
        "investor_13f",
        "Edgewood Management",
        0.65,
        "multi_cycle",
        "long_only,low_turnover",
        "0000860561",
    ),
    ("jennison", "investor_13f", "Jennison Associates", 0.6, "multi_cycle", "long_only", None),
    (
        "counterpoint_global",
        "investor_13f",
        "MS Counterpoint Global (Lynch)",
        0.55,
        "multi_cycle",
        "long_only",
        None,
    ),
    ("sga", "investor_13f", "Sustainable Growth Advisers", 0.5, "multi_cycle", "long_only", None),
    (
        "brown_capital",
        "investor_13f",
        "Brown Capital (Small Co)",
        0.45,
        "multi_cycle",
        "long_only,low_turnover,small_cap",
        None,
    ),
    (
        "capital_group_gfa",
        "investor_13f",
        "Capital Group / Growth Fund of America",
        0.45,
        "multi_cycle",
        "long_only,multi_manager,confirmation",
        None,
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC).isoformat()

    # Budget checks filter by both columns before every LLM call. The squashed
    # baseline retained separate/partial indexes, which still scans all calls
    # for a purpose when applying the monthly boundary.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_llm_calls_purpose_called_at ON llm_calls(purpose,called_at)"
    )

    identity = bind.execute(
        sa.text("SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1")
    ).scalar_one_or_none()
    if identity is None:
        evidence_rows = sum(
            int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in (
                "document_processing_operation_ledger",
                "metric_ontology_operation_ledger",
                "canonical_resolution_operation_ledger",
            )
        )
        if evidence_rows:
            raise RuntimeError(
                "database_runtime_identity is missing while immutable operation "
                "evidence exists; restore the original identity from backup"
            )
        bind.execute(
            sa.text(
                "INSERT INTO database_runtime_identity"
                "(singleton,database_instance_id) VALUES (1,:identity)"
            ),
            {"identity": f"database-instance:{uuid4().hex}"},
        )
    bind.execute(
        sa.text(
            "INSERT INTO source_fact_publication_stream_clock"
            "(singleton_key,next_sequence) VALUES "
            "(1,(SELECT COALESCE(MAX(publication_sequence), 0) + 1 "
            "FROM source_fact_publication_stream)) "
            "ON CONFLICT(singleton_key) DO NOTHING"
        )
    )

    for field, value in (
        ("risk_free_rate", 0.043),
        ("equity_risk_premium", 0.045),
        ("tax_rate", 0.24),
    ):
        bind.execute(
            sa.text(
                "INSERT INTO global_dcf_assumptions"
                "(field,value,updated_at) VALUES (:field,:value,:now) "
                "ON CONFLICT(field) DO NOTHING"
            ),
            {"field": field, "value": value, "now": now},
        )

    budget_sql = sa.text(
        "INSERT INTO llm_budgets"
        "(purpose,monthly_cap_usd,warn_threshold_pct,hard_block,created_at,"
        "updated_at,notes,on_exceed) "
        "VALUES (:purpose,:cap,0.80,:hard_block,:now,:now,:notes,:mode) "
        "ON CONFLICT(purpose) DO NOTHING"
    )
    for purpose, cap, mode in _LLM_BUDGETS:
        bind.execute(
            budget_sql,
            {
                "purpose": purpose,
                "cap": cap,
                "hard_block": mode == "block",
                "now": now,
                "notes": "restored by migration 0003",
                "mode": mode,
            },
        )

    discovery_sql = sa.text(
        "INSERT INTO discovery_sources"
        "(source_key,signal_class,display_name,base_weight,tier,style_tags,cik,"
        "active,last_calibrated_at,created_at,updated_at) "
        "VALUES (:key,:class,:name,:weight,:tier,:tags,:cik,1,NULL,:now,:now) "
        "ON CONFLICT(source_key) DO NOTHING"
    )
    for key, signal_class, name, weight, tier, tags, cik in _DISCOVERY_SOURCES:
        bind.execute(
            discovery_sql,
            {
                "key": key,
                "class": signal_class,
                "name": name,
                "weight": weight,
                "tier": tier,
                "tags": tags,
                "cik": cik,
                "now": now,
            },
        )

    kpi_sql = sa.text(
        "INSERT INTO kpi_definitions"
        "(ticker,name,unit,primary_source,fallback_source,ir_url) "
        "VALUES (:ticker,:name,'actual',:primary,:fallback,:ir_url) "
        "ON CONFLICT(ticker,name) DO NOTHING"
    )
    for ticker, name, primary, fallback, ir_url in _KPI_ROUTES:
        bind.execute(
            kpi_sql,
            {
                "ticker": ticker,
                "name": name,
                "primary": primary,
                "fallback": fallback,
                "ir_url": ir_url,
            },
        )

    required_budgets = {row[0] for row in _LLM_BUDGETS}
    present_budgets = {
        str(row[0]) for row in bind.execute(sa.text("SELECT purpose FROM llm_budgets"))
    }
    required_sources = {row[0] for row in _DISCOVERY_SOURCES}
    present_sources = {
        str(row[0]) for row in bind.execute(sa.text("SELECT source_key FROM discovery_sources"))
    }
    required_kpis = {(row[0], row[1]) for row in _KPI_ROUTES}
    present_kpis = {
        (str(row[0]), str(row[1]))
        for row in bind.execute(sa.text("SELECT ticker,name FROM kpi_definitions"))
    }
    missing = {
        "llm_budgets": sorted(required_budgets - present_budgets),
        "discovery_sources": sorted(required_sources - present_sources),
        "kpi_definitions": sorted(required_kpis - present_kpis),
    }
    if any(missing.values()):
        raise RuntimeError(f"migration-owned defaults missing after 0003 upgrade: {missing}")


def downgrade() -> None:
    # These rows become operator-owned immediately. Removing them during a
    # downgrade would destroy edits, so data is forward-only. The query index
    # is migration-owned and can be removed safely with the schema revision.
    op.execute("DROP INDEX IF EXISTS ix_llm_calls_purpose_called_at")
