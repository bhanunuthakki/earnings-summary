"""entity_concept_spine — foundational semantic layer.

Adds nine tables that ground every fact, every LLM artifact, and every
extracted claim in a typed entity + concept graph with provenance:

  entities                 — canonical rows for companies / segments / products /
                             people / geographies / events / customers / competitors.
                             Entity kind decides which "fact tables" use this row.
  entity_aliases           — every observed name for an entity (10-K name, transcript
                             name, press name, analyst name). Growth is self-updating:
                             unseen names land here via the mapping_proposals queue.
  entity_relationships     — typed edges with temporal validity:
                             segment_of, product_of, employed_by, sits_on_board_of,
                             customer_of, supplier_to, competes_with, subsidiary_of,
                             invested_in, partners_with, sued_by, acquired_by.
                             Powers cross-ticker queries (board interlocks, supplier
                             dependency graphs, customer concentration aggregates).
  entity_mentions          — every textual mention of an entity in any document.
                             Lets the brief surface a "where else has 'Wegovy' been
                             cited" view. Captures char offsets for source-link UX.

  concepts                 — canonical metric/KPI rows: Revenue, ARPAC, NPL ratio,
                             stock-based comp, etc. Specialization of entities for
                             concept-kind things; gets its own tables because the
                             definition history is its own dimension.
  concept_aliases          — every reported name that maps to a concept. NU's
                             'Active Customers' / 'MAU' / 'Engaged Customers' all
                             point at the same concept row.
  concept_definitions      — per-(concept, ticker, period) definition text. Tracks
                             "NU's definition of Active Customers changed Q3'24
                             from >$1 in 30d to >$2 in 90d." Renderers can show
                             definition-change footnotes automatically.

  extractions              — every fact / mention / relationship / claim has a
                             provenance row with source_doc_id + char offsets +
                             extractor_version. When an extractor improves, re-run
                             produces a new row with superseded_by_extraction_id
                             pointing at the prior. Auditable to char offsets.
  mapping_proposals        — the self-updating queue. When the extractor sees a
                             name it can't resolve, it writes a proposal here
                             with confidence. Auto-applied at confidence >= 0.85;
                             surfaces in the 'Mappings to Review' dashboard tab
                             at 0.50-0.85; logged-only below 0.50.

Why one migration and not five: these tables reference each other (entity_aliases
→ entities, concept_definitions → concepts, etc.). Splitting would require ordering
that's harder to debug than a single transactional create. Each table is documented
in the upgrade() function.

Revision ID: 0036_entity_concept_spine
Revises: 0035_llm_artifacts
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_entity_concept_spine"
down_revision: str | Sequence[str] | None = "0035_llm_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # ------------------------------------------------------------------
    # entities — the canonical graph node
    # ------------------------------------------------------------------
    # Kinds: 'company', 'segment', 'product', 'person', 'geography', 'event',
    # 'customer', 'supplier', 'competitor', 'drug', 'project'. Free-form so
    # new kinds (e.g. 'datacenter', 'patent', 'litigation') don't need a
    # schema change. Renderers / extractors hard-code the kinds they consume.
    if "entities" not in existing:
        op.create_table(
            "entities",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("canonical_name", sa.String(length=255), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=True),
            # External IDs as JSON: cik, figi, isin, lei, ticker, drug_atc_code, etc.
            sa.Column("external_ids", sa.Text(), nullable=True),
            # Parent for hierarchies: segment.parent_entity_id → company,
            # product.parent_entity_id → segment, drug.parent_entity_id → company.
            sa.Column(
                "parent_entity_id",
                sa.Integer(),
                sa.ForeignKey("entities.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("meta_json", sa.Text(), nullable=True),  # kind-specific fields
            sa.Column("effective_from", sa.DateTime(), nullable=True),
            sa.Column("effective_to", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("kind", "canonical_name", name="uq_entities_kind_name"),
        )
        op.create_index("idx_entities_kind", "entities", ["kind"])
        op.create_index("idx_entities_parent", "entities", ["parent_entity_id"])

    # ------------------------------------------------------------------
    # entity_aliases — every observed name → entity_id
    # ------------------------------------------------------------------
    if "entity_aliases" not in existing:
        op.create_table(
            "entity_aliases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "entity_id",
                sa.Integer(),
                sa.ForeignKey("entities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("alias_text", sa.String(length=255), nullable=False),
            # Where this alias was seen — informs resolution heuristics.
            sa.Column("alias_kind", sa.String(length=32), nullable=False),  # 'reported_in_10k', 'reported_in_transcript', 'press_name', 'analyst_name', 'manual'
            sa.Column("first_observed_at", sa.DateTime(), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(), nullable=True),
            sa.Column("observation_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("exemplar_source_doc_id", sa.Integer(), nullable=True),  # FK to documents (loose)
            sa.Column("exemplar_excerpt", sa.Text(), nullable=True),
            sa.UniqueConstraint("entity_id", "alias_text", name="uq_entity_aliases"),
        )
        op.create_index("idx_aliases_text", "entity_aliases", ["alias_text"])
        op.create_index("idx_aliases_entity", "entity_aliases", ["entity_id"])

    # ------------------------------------------------------------------
    # entity_relationships — typed temporal edges
    # ------------------------------------------------------------------
    if "entity_relationships" not in existing:
        op.create_table(
            "entity_relationships",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "from_entity_id",
                sa.Integer(),
                sa.ForeignKey("entities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("relationship_kind", sa.String(length=48), nullable=False),
            sa.Column(
                "to_entity_id",
                sa.Integer(),
                sa.ForeignKey("entities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("effective_from", sa.DateTime(), nullable=True),
            sa.Column("effective_to", sa.DateTime(), nullable=True),
            sa.Column("evidence_doc_id", sa.Integer(), nullable=True),
            sa.Column("evidence_excerpt", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("meta_json", sa.Text(), nullable=True),  # e.g. {"role": "CEO", "comp_total": 25e6}
            sa.UniqueConstraint(
                "from_entity_id",
                "relationship_kind",
                "to_entity_id",
                "effective_from",
                name="uq_entity_rel",
            ),
        )
        op.create_index(
            "idx_rel_from_kind", "entity_relationships", ["from_entity_id", "relationship_kind"]
        )
        op.create_index(
            "idx_rel_to_kind", "entity_relationships", ["to_entity_id", "relationship_kind"]
        )

    # ------------------------------------------------------------------
    # entity_mentions — every textual reference, char-offset granular
    # ------------------------------------------------------------------
    if "entity_mentions" not in existing:
        op.create_table(
            "entity_mentions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source_doc_id", sa.Integer(), nullable=False),
            sa.Column(
                "entity_id",
                sa.Integer(),
                sa.ForeignKey("entities.id", ondelete="SET NULL"),
                nullable=True,  # null when the extractor saw a candidate but couldn't resolve
            ),
            sa.Column("char_offset_start", sa.Integer(), nullable=True),
            sa.Column("char_offset_end", sa.Integer(), nullable=True),
            sa.Column("mention_text", sa.String(length=255), nullable=False),
            sa.Column("surrounding_context", sa.Text(), nullable=True),  # ~280 chars
            sa.Column("extractor_id", sa.String(length=64), nullable=False),
            sa.Column("extractor_version", sa.String(length=16), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            # When entity_id is NULL: the unresolved text the extractor saw
            sa.Column("unresolved_alias_text", sa.String(length=255), nullable=True),
        )
        op.create_index("idx_mentions_doc", "entity_mentions", ["source_doc_id"])
        op.create_index("idx_mentions_entity", "entity_mentions", ["entity_id"])
        op.create_index("idx_mentions_unresolved", "entity_mentions", ["unresolved_alias_text"])

    # ------------------------------------------------------------------
    # concepts — canonical metric ontology
    # ------------------------------------------------------------------
    if "concepts" not in existing:
        op.create_table(
            "concepts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("kind", sa.String(length=32), nullable=False),  # 'financial_line_item', 'kpi', 'ratio', 'segment_metric'
            sa.Column("canonical_name", sa.String(length=128), nullable=False),
            sa.Column("unit_kind", sa.String(length=32), nullable=True),  # 'currency', 'count', 'ratio', 'duration', 'pct', 'bps'
            sa.Column("taxonomy_xbrl_tag", sa.String(length=128), nullable=True),  # e.g. 'us-gaap:Revenues'
            sa.Column("generic_definition_md", sa.Text(), nullable=True),
            sa.Column("computation_kind", sa.String(length=32), nullable=True),  # 'reported', 'derived', 'estimated'
            sa.Column("computation_formula_md", sa.Text(), nullable=True),  # for derived
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("canonical_name", name="uq_concepts_name"),
        )
        op.create_index("idx_concepts_kind", "concepts", ["kind"])
        op.create_index("idx_concepts_xbrl", "concepts", ["taxonomy_xbrl_tag"])

    # ------------------------------------------------------------------
    # concept_aliases — reported names → concept
    # ------------------------------------------------------------------
    if "concept_aliases" not in existing:
        op.create_table(
            "concept_aliases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "concept_id",
                sa.Integer(),
                sa.ForeignKey("concepts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ticker", sa.String(length=16), nullable=True),  # null = universal
            sa.Column("alias_text", sa.String(length=255), nullable=False),
            sa.Column("alias_kind", sa.String(length=32), nullable=False),  # 'reported', 'analyst', 'manual'
            sa.Column("first_observed_at", sa.DateTime(), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.UniqueConstraint("concept_id", "ticker", "alias_text", name="uq_concept_aliases"),
        )
        op.create_index("idx_concept_aliases_text", "concept_aliases", ["alias_text"])
        op.create_index("idx_concept_aliases_ticker", "concept_aliases", ["ticker"])

    # ------------------------------------------------------------------
    # concept_definitions — per-(concept, ticker, period) definition history
    # ------------------------------------------------------------------
    if "concept_definitions" not in existing:
        op.create_table(
            "concept_definitions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "concept_id",
                sa.Integer(),
                sa.ForeignKey("concepts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("effective_from", sa.DateTime(), nullable=False),
            sa.Column("effective_to", sa.DateTime(), nullable=True),
            sa.Column("definition_md", sa.Text(), nullable=False),
            sa.Column("evidence_doc_id", sa.Integer(), nullable=True),
            sa.Column("evidence_excerpt", sa.Text(), nullable=True),
            # LLM-extracted summary of what changed vs prior definition
            sa.Column("computation_change_md", sa.Text(), nullable=True),
            sa.Column(
                "superseded_by_id",
                sa.Integer(),
                sa.ForeignKey("concept_definitions.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "idx_concept_defs", "concept_definitions", ["concept_id", "ticker", "effective_from"]
        )

    # ------------------------------------------------------------------
    # extractions — provenance for every extracted claim
    # ------------------------------------------------------------------
    # Polymorphic on extraction_kind. payload_json holds the kind-specific
    # structure. links_to_json holds FK-like references to the rows this
    # extraction populated (e.g. {"financial_facts_id": 123, "entity_id": 456}).
    if "extractions" not in existing:
        op.create_table(
            "extractions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source_doc_id", sa.Integer(), nullable=False),
            sa.Column("char_offset_start", sa.Integer(), nullable=True),
            sa.Column("char_offset_end", sa.Integer(), nullable=True),
            sa.Column("extraction_kind", sa.String(length=48), nullable=False),
            # 'fact' | 'mention' | 'relationship' | 'commitment' | 'risk' | 'compensation' |
            # 'metric_definition' | 'forward_statement' | 'litigation' | 'capital_allocation' |
            # 'customer_concentration' | 'supplier_dependency' | 'geographic_detail' |
            # 'numerical_claim'
            sa.Column("extractor_id", sa.String(length=64), nullable=False),
            sa.Column("extractor_version", sa.String(length=16), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.Column(
                "superseded_by_extraction_id",
                sa.Integer(),
                sa.ForeignKey("extractions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("links_to_json", sa.Text(), nullable=True),
        )
        op.create_index("idx_extractions_doc", "extractions", ["source_doc_id"])
        op.create_index("idx_extractions_kind", "extractions", ["extraction_kind"])
        op.create_index(
            "idx_extractions_extractor", "extractions", ["extractor_id", "extractor_version"]
        )

    # ------------------------------------------------------------------
    # mapping_proposals — the self-updating queue
    # ------------------------------------------------------------------
    if "mapping_proposals" not in existing:
        op.create_table(
            "mapping_proposals",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("kind", sa.String(length=48), nullable=False),
            # 'new_alias' | 'new_entity' | 'new_concept' | 'new_concept_alias' |
            # 'new_relationship' | 'concept_redefinition' | 'segment_restructure'
            sa.Column("ticker", sa.String(length=16), nullable=True),
            sa.Column("proposed_by", sa.String(length=64), nullable=False),  # 'llm_resolver_v1', 'regex_v2', 'human:bhanu'
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_doc_id", sa.Integer(), nullable=True),
            sa.Column("source_excerpt", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending_review"),
            # 'auto_applied' | 'pending_review' | 'rejected' | 'manually_revised'
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("applied_to_entity_id", sa.Integer(), nullable=True),
            sa.Column("applied_to_concept_id", sa.Integer(), nullable=True),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decided_by", sa.String(length=64), nullable=True),
            sa.Column("decision_notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_proposals_status", "mapping_proposals", ["status"])
        op.create_index("idx_proposals_kind", "mapping_proposals", ["kind", "status"])
        op.create_index("idx_proposals_ticker", "mapping_proposals", ["ticker"])


def downgrade() -> None:
    for table in (
        "mapping_proposals",
        "extractions",
        "concept_definitions",
        "concept_aliases",
        "concepts",
        "entity_mentions",
        "entity_relationships",
        "entity_aliases",
        "entities",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
