"""discovery_sources — the weighted source-rating registry for discovery.

The Instrument Paradigm requires a ranked surface to score "by a weighted sum
of typed, dated signals through a source-weight registry (never an equal-weight
count)". This is that registry: one editable row per signal SOURCE — every
factor screen, every adjacency channel, and every rostered 13F investor — with
a ``base_weight`` the owner tunes, a ``cik`` (investors only), ``style_tags``
the scorer reads for the new-vs-add action asymmetry, and an ``active`` flag.

Seeded from interaction_paradigm_2026_06 §5:

* the three deterministic factor screens + three adjacency channels (the
  fundamental sources, always active), and
* the TWO investor tiers — the current-cycle crossover funds and the
  multi-decade / multi-cycle growth houses — with the §5 base_weights and
  style tags. Only Appaloosa's CIK is seeded (verified live 2026-06-13,
  CIK 0001656456); the rest carry ``cik = NULL`` until the 13F miner resolves
  them, so a rostered-but-unresolved fund is configured-yet-dormant, not a
  silent gap. The miner reads only ``active = 1 AND cik IS NOT NULL`` rows.

``base_weight`` is the editable lever (the panel's weight-edit surface +
quarterly recalibration against realized forward returns write it).

Revision ID: 0098_discovery_sources
Revises: 0097_discovery_candidate_score_json
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0098_discovery_sources"
down_revision: str | Sequence[str] | None = "0097_discovery_candidate_score_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLASSES = "('screen', 'adjacency', 'investor_13f')"
_TIERS = "('structural', 'crossover', 'multi_cycle')"
_SEED_TS = "2026-06-13T00:00:00+00:00"


# (source_key, signal_class, display_name, base_weight, tier, style_tags, cik)
# fmt: off
_SEED: tuple[tuple[str, str, str, float, str, str, str | None], ...] = (
    # --- fundamental sources (structural; always active) ---
    ("quality_compounder", "screen", "Quality-compounder screen", 1.0, "structural", "screen", None),
    ("fcf_value",          "screen", "Cash-generative value screen", 0.9, "structural", "screen", None),
    ("growth_inflection",  "screen", "Growth-inflection screen", 1.0, "structural", "screen", None),
    ("watchlist",  "adjacency", "On a holding's competitive watchlist", 1.0, "structural", "intentional", None),
    ("transcript", "adjacency", "Named in a holding's earnings calls", 0.7, "structural", "incidental", None),
    ("news",       "adjacency", "Named in a holding's news", 0.5, "structural", "incidental", None),
    # --- investor roster · current-cycle crossover tier (§5) ---
    ("altimeter",     "investor_13f", "Altimeter Capital", 1.0,  "crossover", "hedge,crossover", None),
    ("atreides",      "investor_13f", "Atreides Management", 1.0, "crossover", "hedge,crossover", None),
    ("whale_rock",    "investor_13f", "Whale Rock Capital", 0.9,  "crossover", "hedge,crossover", None),
    ("light_street",  "investor_13f", "Light Street Capital", 0.85, "crossover", "hedge,crossover", None),
    ("coatue",        "investor_13f", "Coatue Management", 0.8,   "crossover", "hedge,crossover", None),
    ("lone_pine",     "investor_13f", "Lone Pine Capital", 0.8,   "crossover", "hedge,crossover", None),
    ("d1",            "investor_13f", "D1 Capital Partners", 0.75, "crossover", "hedge,crossover", None),
    ("baillie_gifford", "investor_13f", "Baillie Gifford", 0.65,  "crossover", "long_only,low_turnover", None),
    ("durable",       "investor_13f", "Durable Capital Partners", 0.6, "crossover", "hedge,crossover", None),
    ("viking",        "investor_13f", "Viking Global Investors", 0.6, "crossover", "hedge,crossover", None),
    ("tiger_global",  "investor_13f", "Tiger Global Management", 0.6, "crossover", "hedge,crossover", None),
    ("maverick",      "investor_13f", "Maverick Capital", 0.6,    "crossover", "hedge,crossover", None),
    ("tremblant",     "investor_13f", "Tremblant Capital", 0.55,  "crossover", "hedge,crossover", None),
    ("dragoneer",     "investor_13f", "Dragoneer Investment Group", 0.55, "crossover", "hedge,crossover", None),
    ("alkeon",        "investor_13f", "Alkeon Capital", 0.55,     "crossover", "hedge,crossover", None),
    ("greenoaks",     "investor_13f", "Greenoaks Capital", 0.5,   "crossover", "hedge,crossover", None),
    ("addition",      "investor_13f", "Addition", 0.45,           "crossover", "hedge,crossover", None),
    ("ark",           "investor_13f", "ARK Investment Management", 0.4, "crossover", "long_only,etf", None),
    ("iconiq",        "investor_13f", "ICONIQ Capital", 0.4,      "crossover", "hedge,crossover", None),
    ("tybourne",      "investor_13f", "Tybourne Capital (wind-down)", 0.3, "crossover", "hedge,crossover", None),
    # --- investor roster · multi-decade / multi-cycle growth tier (§5) ---
    ("appaloosa", "investor_13f", "Appaloosa (Tepper)", 0.85, "multi_cycle", "hedge,concentrated", "0001656456"),
    ("sands",     "investor_13f", "Sands Capital", 0.8,  "multi_cycle", "long_only,low_turnover", None),
    ("contrafund", "investor_13f", "Fidelity Contrafund (Danoff)", 0.7, "multi_cycle", "long_only", None),
    ("wcm",       "investor_13f", "WCM Investment Management", 0.7, "multi_cycle", "long_only,low_turnover", None),
    ("loomis_growth", "investor_13f", "Loomis Sayles Growth (Hamzaogullari)", 0.7, "multi_cycle", "long_only,low_turnover", None),
    ("polen",     "investor_13f", "Polen Capital (Focus Growth)", 0.65, "multi_cycle", "long_only,low_turnover", None),
    ("akre",      "investor_13f", "Akre Capital (Neff)", 0.65, "multi_cycle", "long_only,low_turnover", None),
    ("edgewood",  "investor_13f", "Edgewood Management", 0.65, "multi_cycle", "long_only,low_turnover", None),
    ("jennison",  "investor_13f", "Jennison Associates", 0.6, "multi_cycle", "long_only", None),
    ("counterpoint_global", "investor_13f", "MS Counterpoint Global (Lynch)", 0.55, "multi_cycle", "long_only", None),
    ("sga",       "investor_13f", "Sustainable Growth Advisers", 0.5, "multi_cycle", "long_only", None),
    ("brown_capital", "investor_13f", "Brown Capital (Small Co)", 0.45, "multi_cycle", "long_only,low_turnover,small_cap", None),
    ("capital_group_gfa", "investor_13f", "Capital Group / Growth Fund of America", 0.45, "multi_cycle", "long_only,multi_manager,confirmation", None),
)
# fmt: on


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_sources" in insp.get_table_names():
        return  # idempotent
    table = op.create_table(
        "discovery_sources",
        sa.Column("source_key", sa.Text(), primary_key=True),
        sa.Column("signal_class", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("base_weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("tier", sa.Text(), nullable=False, server_default=sa.text("'structural'")),
        sa.Column("style_tags", sa.Text(), nullable=True),
        sa.Column("cik", sa.Text(), nullable=True),
        sa.Column("active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_calibrated_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(f"signal_class IN {_CLASSES}", name="ck_discovery_sources_class"),
        sa.CheckConstraint(f"tier IN {_TIERS}", name="ck_discovery_sources_tier"),
        sa.CheckConstraint("active IN (0, 1)", name="ck_discovery_sources_active"),
        sa.CheckConstraint("base_weight >= 0", name="ck_discovery_sources_weight_nonneg"),
    )
    op.create_index(
        "ix_discovery_sources_class_active", "discovery_sources", ["signal_class", "active"]
    )
    op.bulk_insert(
        table,
        [
            {
                "source_key": key,
                "signal_class": cls,
                "display_name": name,
                "base_weight": weight,
                "tier": tier,
                "style_tags": tags,
                "cik": cik,
                "active": 1,
                "last_calibrated_at": None,
                "created_at": _SEED_TS,
                "updated_at": _SEED_TS,
            }
            for (key, cls, name, weight, tier, tags, cik) in _SEED
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "discovery_sources" not in insp.get_table_names():
        return
    op.drop_index("ix_discovery_sources_class_active", table_name="discovery_sources")
    op.drop_table("discovery_sources")
