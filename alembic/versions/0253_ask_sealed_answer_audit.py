"""Seal production Ask retrieval promotions and answer/claim provenance.

Revision ID: 0253_ask_sealed_answer_audit
Revises: 0252_research_universe_closure
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0253_ask_sealed_answer_audit"
down_revision: str | Sequence[str] | None = "0252_research_universe_closure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "ask_retrieval_scope_promotions",
    "ask_answer_audit_records",
    "ask_answer_audit_retrievals",
    "ask_answer_audit_citations",
    "ask_answer_audit_claims",
    "ask_answer_audit_claim_citations",
    "ask_answer_audit_seals",
)
_CLAIM_AUDIT_PURPOSE = "ask_claim_audit"


def _hex(column: str) -> str:
    return (
        f"length({column})=64 AND lower({column})={column} "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'sealed Ask audit data is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'sealed Ask audit data is append-only'); END"
    )


def _reject_identity_replace(table: str, predicate: str) -> None:
    """Block SQLite ``INSERT OR REPLACE`` without relying on recursive triggers.

    SQLite's REPLACE conflict handler deletes the old row before inserting the
    replacement, and DELETE triggers only fire for that implicit delete when
    ``recursive_triggers`` is enabled.  The application intentionally does not
    change that process-wide pragma.  A BEFORE INSERT identity guard is
    therefore the durable append-only boundary: governed exact replays read and
    compare the existing row before issuing INSERT, while direct SQL replacement
    attempts fail before SQLite can delete anything.
    """
    op.execute(
        f"CREATE TRIGGER trg_{table}_identity_immutable BEFORE INSERT ON {table} "
        f"WHEN EXISTS (SELECT 1 FROM {table} existing WHERE {predicate}) "
        "BEGIN SELECT RAISE(ABORT, 'sealed Ask audit identity is append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "ask_retrieval_scope_promotions",
        sa.Column("promotion_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("issuer_id", sa.String(128), nullable=False),
        sa.Column("reporting_entity_id", sa.String(128), nullable=False),
        sa.Column(
            "research_snapshot_id",
            sa.String(128),
            sa.ForeignKey("research_snapshot_headers.research_snapshot_id"),
            nullable=False,
        ),
        sa.Column("research_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column(
            "fact_generation_id",
            sa.String(128),
            sa.ForeignKey("canonical_fact_projection_generations.generation_id"),
            nullable=False,
        ),
        sa.Column("fact_projection_seal_sha256", sa.String(64), nullable=False),
        sa.Column("source_inventory_set_json", sa.Text, nullable=False),
        sa.Column("source_inventory_set_sha256", sa.String(64), nullable=False),
        sa.Column("narrative_bundles_json", sa.Text, nullable=False),
        sa.Column("narrative_bundles_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime, nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("verifier_name", sa.String(128), nullable=False),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("verifier_code_sha256", sa.String(64), nullable=False),
        sa.Column("verifier_config_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "supersedes_promotion_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_scope_promotions.promotion_id"),
        ),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "scope_key",
            "revision",
            name="uq_ask_retrieval_scope_promotion_revision",
        ),
        sa.CheckConstraint(
            "revision>0 AND status IN ('promoted','withdrawn') "
            "AND ((revision=1 AND supersedes_promotion_id IS NULL) "
            "OR (revision>1 AND supersedes_promotion_id IS NOT NULL)) "
            "AND recorded_at>=cutoff_at",
            name="ck_ask_retrieval_scope_promotion_shape",
        ),
        sa.CheckConstraint(
            "json_valid(source_inventory_set_json) "
            "AND json_type(source_inventory_set_json)='array' "
            "AND json_valid(narrative_bundles_json) "
            "AND json_type(narrative_bundles_json)='array'",
            name="ck_ask_retrieval_scope_promotion_json",
        ),
        sa.CheckConstraint(
            _hex("research_snapshot_sha256")
            + " AND "
            + _hex("fact_projection_seal_sha256")
            + " AND "
            + _hex("source_inventory_set_sha256")
            + " AND "
            + _hex("narrative_bundles_sha256")
            + " AND "
            + _hex("verifier_code_sha256")
            + " AND "
            + _hex("verifier_config_sha256"),
            name="ck_ask_retrieval_scope_promotion_hashes",
        ),
    )
    op.create_index(
        "ix_ask_retrieval_scope_promotion_current",
        "ask_retrieval_scope_promotions",
        ["scope_key", "revision"],
    )

    op.create_table(
        "ask_answer_audit_records",
        sa.Column("answer_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("request_id", sa.String(128), nullable=False, unique=True),
        sa.Column("session_id", sa.Text, sa.ForeignKey("ask_sessions.id")),
        sa.Column("surface", sa.String(32), nullable=False),
        sa.Column("query_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_template_id", sa.String(128), nullable=False),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("prompt_template_vars_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_variables_json", sa.Text, nullable=False),
        sa.Column("prompt_variables_sha256", sa.String(64), nullable=False),
        sa.Column("context_turn_set_json", sa.Text, nullable=False),
        sa.Column("context_turn_set_sha256", sa.String(64), nullable=False),
        sa.Column("retrieval_assembly_json", sa.Text, nullable=False),
        sa.Column("retrieval_assembly_sha256", sa.String(64), nullable=False),
        sa.Column("retrieval_prompt_fragments_json", sa.Text, nullable=False),
        sa.Column("retrieval_prompt_fragments_sha256", sa.String(64), nullable=False),
        sa.Column("answer_text", sa.Text, nullable=False),
        sa.Column("answer_sha256", sa.String(64), nullable=False),
        sa.Column("llm_purpose", sa.String(128), nullable=False),
        sa.Column("llm_model", sa.String(128), nullable=False),
        sa.Column("llm_provider", sa.String(64), nullable=False),
        sa.Column("llm_transport", sa.String(64), nullable=False),
        sa.Column(
            "llm_call_id",
            sa.Integer,
            sa.ForeignKey("llm_calls.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("llm_run_id", sa.String(256), nullable=False),
        sa.Column("claim_auditor_version", sa.String(64), nullable=False),
        sa.Column("claim_audit_purpose", sa.String(128), nullable=False),
        sa.Column("claim_audit_template_id", sa.String(128), nullable=False),
        sa.Column("claim_audit_template_version", sa.String(64), nullable=False),
        sa.Column("claim_audit_template_vars_sha256", sa.String(64), nullable=False),
        sa.Column("claim_audit_prompt_variables_json", sa.Text, nullable=False),
        sa.Column("claim_audit_prompt_variables_sha256", sa.String(64), nullable=False),
        sa.Column("claim_auditor_model", sa.String(128), nullable=False),
        sa.Column("claim_audit_provider", sa.String(64), nullable=False),
        sa.Column("claim_audit_transport", sa.String(64), nullable=False),
        sa.Column("claim_audit_prompt_sha256", sa.String(64), nullable=False),
        sa.Column("claim_audit_response_sha256", sa.String(64), nullable=False),
        sa.Column(
            "claim_audit_llm_call_id",
            sa.Integer,
            sa.ForeignKey("llm_calls.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("claim_audit_run_id", sa.String(256), nullable=False),
        sa.Column("no_claim_exemption", sa.String(64)),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "surface IN ('portfolio','ticker','report','api') "
            "AND ((surface='portfolio' AND session_id IS NOT NULL) "
            "OR surface<>'portfolio') "
            "AND (no_claim_exemption IS NULL "
            "OR no_claim_exemption='deterministic_non_substantive.v1') "
            "AND length(trim(answer_text))>0",
            name="ck_ask_answer_audit_record_shape",
        ),
        sa.CheckConstraint(
            "json_valid(context_turn_set_json) "
            "AND json_type(context_turn_set_json)='array' "
            "AND json_valid(prompt_variables_json) "
            "AND json_type(prompt_variables_json)='object' "
            "AND json_valid(retrieval_assembly_json) "
            "AND json_type(retrieval_assembly_json)='array' "
            "AND json_valid(retrieval_prompt_fragments_json) "
            "AND json_type(retrieval_prompt_fragments_json)='array' "
            "AND json_valid(claim_audit_prompt_variables_json) "
            "AND json_type(claim_audit_prompt_variables_json)='object'",
            name="ck_ask_answer_audit_record_json",
        ),
        sa.CheckConstraint(
            _hex("query_sha256")
            + " AND "
            + _hex("prompt_sha256")
            + " AND "
            + _hex("prompt_template_vars_sha256")
            + " AND "
            + _hex("prompt_variables_sha256")
            + " AND "
            + _hex("context_turn_set_sha256")
            + " AND "
            + _hex("retrieval_assembly_sha256")
            + " AND "
            + _hex("retrieval_prompt_fragments_sha256")
            + " AND "
            + _hex("answer_sha256")
            + " AND "
            + _hex("claim_audit_prompt_sha256")
            + " AND "
            + _hex("claim_audit_response_sha256")
            + " AND "
            + _hex("claim_audit_template_vars_sha256")
            + " AND "
            + _hex("claim_audit_prompt_variables_sha256"),
            name="ck_ask_answer_audit_record_hashes",
        ),
    )
    op.create_index(
        "ix_ask_answer_audit_session",
        "ask_answer_audit_records",
        ["session_id", "recorded_at"],
    )

    op.create_table(
        "ask_answer_audit_retrievals",
        sa.Column(
            "answer_id",
            sa.String(128),
            sa.ForeignKey("ask_answer_audit_records.answer_id"),
            primary_key=True,
        ),
        sa.Column("trace_ordinal", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("query_sha256", sa.String(64), nullable=False),
        sa.Column(
            "promotion_id",
            sa.String(128),
            sa.ForeignKey("ask_retrieval_scope_promotions.promotion_id"),
            nullable=False,
        ),
        sa.Column(
            "trace_id",
            sa.String(128),
            sa.ForeignKey("heterogeneous_retrieval_trace_headers.trace_id"),
            nullable=False,
        ),
        sa.Column("trace_sha256", sa.String(64), nullable=False),
        sa.Column("research_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint(
            "answer_id",
            "trace_id",
            name="uq_ask_answer_audit_retrieval_trace",
        ),
        sa.CheckConstraint(
            "trace_ordinal>=0 AND "
            + _hex("query_sha256")
            + " AND "
            + _hex("trace_sha256")
            + " AND "
            + _hex("research_snapshot_sha256"),
            name="ck_ask_answer_audit_retrieval_shape",
        ),
    )

    op.create_table(
        "ask_answer_audit_citations",
        sa.Column(
            "answer_id",
            sa.String(128),
            sa.ForeignKey("ask_answer_audit_records.answer_id"),
            primary_key=True,
        ),
        sa.Column("citation_number", sa.Integer, primary_key=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("result_ordinal", sa.Integer, nullable=False),
        sa.Column("candidate_kind", sa.String(16), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("source_commitment_sha256", sa.String(64), nullable=False),
        sa.Column("citation_json", sa.Text, nullable=False),
        sa.Column("citation_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(
            ["trace_id", "result_ordinal"],
            [
                "heterogeneous_retrieval_trace_results.trace_id",
                "heterogeneous_retrieval_trace_results.result_ordinal",
            ],
        ),
        sa.UniqueConstraint(
            "answer_id",
            "trace_id",
            "result_ordinal",
            name="uq_ask_answer_audit_citation_result",
        ),
        sa.CheckConstraint(
            "citation_number>0 AND result_ordinal>=0 "
            "AND candidate_kind IN ('narrative','fact') "
            "AND json_valid(citation_json) "
            "AND json_type(citation_json)='object' AND "
            + _hex("source_commitment_sha256")
            + " AND "
            + _hex("citation_sha256"),
            name="ck_ask_answer_audit_citation_shape",
        ),
    )

    op.create_table(
        "ask_answer_audit_claims",
        sa.Column(
            "answer_id",
            sa.String(128),
            sa.ForeignKey("ask_answer_audit_records.answer_id"),
            primary_key=True,
        ),
        sa.Column("claim_ordinal", sa.Integer, primary_key=True),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("claim_text", sa.Text, nullable=False),
        sa.Column("claim_sha256", sa.String(64), nullable=False),
        sa.Column("supported", sa.Boolean, nullable=False),
        sa.Column("claim_json", sa.Text, nullable=False),
        sa.Column("claim_json_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "claim_ordinal>=0 AND char_start>=0 AND char_end>char_start "
            "AND length(claim_text)=char_end-char_start "
            "AND json_valid(claim_json) AND json_type(claim_json)='object' "
            "AND "
            + _hex("claim_sha256")
            + " AND "
            + _hex("claim_json_sha256"),
            name="ck_ask_answer_audit_claim_shape",
        ),
    )

    op.create_table(
        "ask_answer_audit_claim_citations",
        sa.Column("answer_id", sa.String(128), primary_key=True),
        sa.Column("claim_ordinal", sa.Integer, primary_key=True),
        sa.Column("citation_number", sa.Integer, primary_key=True),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(
            ["answer_id", "claim_ordinal"],
            ["ask_answer_audit_claims.answer_id", "ask_answer_audit_claims.claim_ordinal"],
        ),
        sa.ForeignKeyConstraint(
            ["answer_id", "citation_number"],
            [
                "ask_answer_audit_citations.answer_id",
                "ask_answer_audit_citations.citation_number",
            ],
        ),
    )

    op.create_table(
        "ask_answer_audit_seals",
        sa.Column(
            "answer_id",
            sa.String(128),
            sa.ForeignKey("ask_answer_audit_records.answer_id"),
            primary_key=True,
        ),
        sa.Column("retrieval_count", sa.Integer, nullable=False),
        sa.Column("citation_count", sa.Integer, nullable=False),
        sa.Column("claim_count", sa.Integer, nullable=False),
        sa.Column("unsupported_claim_count", sa.Integer, nullable=False),
        sa.Column("claim_citation_count", sa.Integer, nullable=False),
        sa.Column("record_sha256", sa.String(64), nullable=False),
        sa.Column("retrieval_set_sha256", sa.String(64), nullable=False),
        sa.Column("citation_set_sha256", sa.String(64), nullable=False),
        sa.Column("claim_set_sha256", sa.String(64), nullable=False),
        sa.Column("claim_citation_set_sha256", sa.String(64), nullable=False),
        sa.Column("audit_json", sa.Text, nullable=False),
        sa.Column("audit_sha256", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime, nullable=False),
        sa.CheckConstraint(
            "retrieval_count>0 AND citation_count>=0 AND claim_count>=0 "
            "AND unsupported_claim_count BETWEEN 0 AND claim_count "
            "AND claim_citation_count>=0 "
            "AND json_valid(audit_json) AND json_type(audit_json)='object' "
            "AND "
            + _hex("record_sha256")
            + " AND "
            + _hex("retrieval_set_sha256")
            + " AND "
            + _hex("citation_set_sha256")
            + " AND "
            + _hex("claim_set_sha256")
            + " AND "
            + _hex("claim_citation_set_sha256")
            + " AND "
            + _hex("audit_sha256"),
            name="ck_ask_answer_audit_seal_shape",
        ),
    )

    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_scope_promotion_chain "
        "BEFORE INSERT ON ask_retrieval_scope_promotions "
        "WHEN NEW.revision>1 AND NOT EXISTS ("
        "SELECT 1 FROM ask_retrieval_scope_promotions prior "
        "WHERE prior.promotion_id=NEW.supersedes_promotion_id "
        "AND prior.scope_key=NEW.scope_key AND prior.revision=NEW.revision-1) "
        "BEGIN SELECT RAISE(ABORT, 'Ask retrieval promotion chain is incomplete'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_scope_promotion_snapshot "
        "BEFORE INSERT ON ask_retrieval_scope_promotions "
        "WHEN NOT EXISTS (SELECT 1 FROM research_snapshot_seals seal "
        "JOIN research_snapshot_admission_receipts receipt "
        "ON receipt.research_snapshot_id=seal.research_snapshot_id "
        "JOIN research_snapshot_universe_commitments universe "
        "ON universe.research_snapshot_id=seal.research_snapshot_id "
        "WHERE seal.research_snapshot_id=NEW.research_snapshot_id "
        "AND seal.member_set_sha256=NEW.research_snapshot_sha256 "
        "AND receipt.research_snapshot_sha256=NEW.research_snapshot_sha256 "
        "AND universe.issuer_id=NEW.issuer_id "
        "AND EXISTS (SELECT 1 FROM json_each(universe.reporting_entity_ids_json) "
        "WHERE value=NEW.reporting_entity_id)) "
        "BEGIN SELECT RAISE(ABORT, 'Ask promotion requires an admitted Research Snapshot'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_retrieval_scope_promotion_projection "
        "BEFORE INSERT ON ask_retrieval_scope_promotions "
        "WHEN NOT EXISTS (SELECT 1 FROM canonical_fact_projection_seals seal "
        "WHERE seal.generation_id=NEW.fact_generation_id "
        "AND seal.projection_seal_sha256=NEW.fact_projection_seal_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'Ask promotion requires a sealed fact projection'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_retrieval_exact "
        "BEFORE INSERT ON ask_answer_audit_retrievals "
        "WHEN NOT EXISTS (SELECT 1 FROM ask_answer_audit_records answer "
        "JOIN ask_retrieval_scope_promotions promotion "
        "ON promotion.promotion_id=NEW.promotion_id "
        "JOIN heterogeneous_retrieval_trace_headers header "
        "ON header.trace_id=NEW.trace_id "
        "JOIN heterogeneous_retrieval_trace_seals seal ON seal.trace_id=header.trace_id "
        "WHERE answer.answer_id=NEW.answer_id "
        "AND answer.request_id=NEW.request_id "
        "AND answer.query_sha256=NEW.query_sha256 "
        "AND header.query_sha256=NEW.query_sha256 "
        "AND header.idempotency_key='ask-request:'||NEW.request_id||':'"
        "||NEW.query_sha256||':'||NEW.promotion_id "
        "AND header.research_snapshot_id=promotion.research_snapshot_id "
        "AND header.fact_generation_id=promotion.fact_generation_id "
        "AND seal.trace_sha256=NEW.trace_sha256 "
        "AND header.research_snapshot_sha256=NEW.research_snapshot_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'answer retrieval must reference an exact sealed trace'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_record_session "
        "BEFORE INSERT ON ask_answer_audit_records "
        "WHEN NEW.session_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM ask_sessions session WHERE session.id=NEW.session_id "
        "AND session.scope=NEW.surface) "
        "BEGIN SELECT RAISE(ABORT, 'Ask answer session identity mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_record_llm "
        "BEFORE INSERT ON ask_answer_audit_records "
        "WHEN NOT EXISTS (SELECT 1 FROM llm_calls call "
        "WHERE call.id=NEW.llm_call_id AND call.purpose=NEW.llm_purpose "
        "AND call.run_id=NEW.llm_run_id "
        "AND call.model=NEW.llm_model AND call.provider=NEW.llm_provider "
        "AND call.transport=NEW.llm_transport "
        "AND call.template_id=NEW.prompt_template_id "
        "AND call.template_version=NEW.prompt_template_version "
        "AND call.template_vars_sha256=NEW.prompt_template_vars_sha256 "
        "AND call.prompt_sha256=NEW.prompt_sha256 "
        "AND call.response_sha256=NEW.answer_sha256 "
        "AND call.error IS NULL) "
        "BEGIN SELECT RAISE(ABORT, 'Ask answer LLM call identity mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_record_claim_llm "
        "BEFORE INSERT ON ask_answer_audit_records "
        "WHEN NOT EXISTS (SELECT 1 FROM llm_calls call "
        "WHERE call.id=NEW.claim_audit_llm_call_id "
        "AND call.run_id=NEW.claim_audit_run_id "
        "AND call.purpose=NEW.claim_audit_purpose "
        "AND call.template_id=NEW.claim_audit_template_id "
        "AND call.template_version=NEW.claim_audit_template_version "
        "AND call.template_vars_sha256=NEW.claim_audit_template_vars_sha256 "
        "AND call.model=NEW.claim_auditor_model "
        "AND call.provider=NEW.claim_audit_provider "
        "AND call.transport=NEW.claim_audit_transport "
        "AND call.prompt_sha256=NEW.claim_audit_prompt_sha256 "
        "AND call.response_sha256=NEW.claim_audit_response_sha256 "
        "AND call.error IS NULL) "
        "BEGIN SELECT RAISE(ABORT, 'Ask claim-audit LLM call identity mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_claim_exact_span "
        "BEFORE INSERT ON ask_answer_audit_claims "
        "WHEN NOT EXISTS (SELECT 1 FROM ask_answer_audit_records answer "
        "WHERE answer.answer_id=NEW.answer_id "
        "AND NEW.char_end<=length(answer.answer_text) "
        "AND substr(answer.answer_text,NEW.char_start+1,"
        "NEW.char_end-NEW.char_start)=NEW.claim_text) "
        "BEGIN SELECT RAISE(ABORT, 'Ask claim text does not equal its answer span'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_citation_exact "
        "BEFORE INSERT ON ask_answer_audit_citations "
        "WHEN NOT EXISTS (SELECT 1 FROM ask_answer_audit_retrievals bound "
        "JOIN heterogeneous_retrieval_trace_results result "
        "ON result.trace_id=bound.trace_id AND result.result_ordinal=NEW.result_ordinal "
        "JOIN heterogeneous_retrieval_trace_candidates candidate "
        "ON candidate.trace_id=result.trace_id "
        "AND candidate.candidate_ordinal=result.candidate_ordinal "
        "WHERE bound.answer_id=NEW.answer_id AND bound.trace_id=NEW.trace_id "
        "AND candidate.candidate_kind=NEW.candidate_kind "
        "AND candidate.candidate_id=NEW.candidate_id "
        "AND candidate.source_commitment_sha256=NEW.source_commitment_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'citation must reference an exact bound trace result'); END"
    )
    for table in (
        "ask_answer_audit_retrievals",
        "ask_answer_audit_citations",
        "ask_answer_audit_claims",
        "ask_answer_audit_claim_citations",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_unsealed BEFORE INSERT ON {table} "
            "WHEN EXISTS (SELECT 1 FROM ask_answer_audit_seals "
            "WHERE answer_id=NEW.answer_id) "
            "BEGIN SELECT RAISE(ABORT, 'sealed Ask answer cannot receive children'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_seal_counts "
        "BEFORE INSERT ON ask_answer_audit_seals WHEN "
        "NEW.retrieval_count<>(SELECT COUNT(*) FROM ask_answer_audit_retrievals "
        "WHERE answer_id=NEW.answer_id) OR "
        "NEW.citation_count<>(SELECT COUNT(*) FROM ask_answer_audit_citations "
        "WHERE answer_id=NEW.answer_id) OR "
        "NEW.claim_count<>(SELECT COUNT(*) FROM ask_answer_audit_claims "
        "WHERE answer_id=NEW.answer_id) OR "
        "NEW.unsupported_claim_count<>(SELECT COUNT(*) FROM ask_answer_audit_claims "
        "WHERE answer_id=NEW.answer_id AND supported=0) OR "
        "NEW.claim_citation_count<>(SELECT COUNT(*) "
        "FROM ask_answer_audit_claim_citations WHERE answer_id=NEW.answer_id) "
        "BEGIN SELECT RAISE(ABORT, 'Ask answer audit seal counts do not match'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_seal_ordinals "
        "BEFORE INSERT ON ask_answer_audit_seals WHEN "
        "(NEW.retrieval_count>0 AND ("
        "(SELECT MIN(trace_ordinal) FROM ask_answer_audit_retrievals "
        "WHERE answer_id=NEW.answer_id)<>0 OR "
        "(SELECT MAX(trace_ordinal) FROM ask_answer_audit_retrievals "
        "WHERE answer_id=NEW.answer_id)<>NEW.retrieval_count-1)) OR "
        "(NEW.claim_count>0 AND ("
        "(SELECT MIN(claim_ordinal) FROM ask_answer_audit_claims "
        "WHERE answer_id=NEW.answer_id)<>0 OR "
        "(SELECT MAX(claim_ordinal) FROM ask_answer_audit_claims "
        "WHERE answer_id=NEW.answer_id)<>NEW.claim_count-1)) "
        "BEGIN SELECT RAISE(ABORT, 'Ask answer audit ordinals are not contiguous'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_ask_answer_audit_seal_support "
        "BEFORE INSERT ON ask_answer_audit_seals WHEN EXISTS ("
        "SELECT 1 FROM ask_answer_audit_claims claim "
        "WHERE claim.answer_id=NEW.answer_id AND ("
        "(claim.supported=1 AND NOT EXISTS (SELECT 1 "
        "FROM ask_answer_audit_claim_citations edge "
        "WHERE edge.answer_id=claim.answer_id "
        "AND edge.claim_ordinal=claim.claim_ordinal)) OR "
        "(claim.supported=0 AND EXISTS (SELECT 1 "
        "FROM ask_answer_audit_claim_citations edge "
        "WHERE edge.answer_id=claim.answer_id "
        "AND edge.claim_ordinal=claim.claim_ordinal)))) "
        "OR EXISTS (SELECT 1 FROM ask_answer_audit_citations citation "
        "WHERE citation.answer_id=NEW.answer_id AND NOT EXISTS ("
        "SELECT 1 FROM ask_answer_audit_claim_citations edge "
        "WHERE edge.answer_id=citation.answer_id "
        "AND edge.citation_number=citation.citation_number)) "
        "BEGIN SELECT RAISE(ABORT, 'Ask answer support graph is incomplete'); END"
    )
    op.execute(
        "CREATE VIEW v_ask_retrieval_scope_current AS "
        "SELECT promotion.* FROM ask_retrieval_scope_promotions promotion "
        "WHERE NOT EXISTS (SELECT 1 FROM ask_retrieval_scope_promotions newer "
        "WHERE newer.scope_key=promotion.scope_key "
        "AND newer.revision>promotion.revision)"
    )
    for table in _TABLES:
        _append_only(table)
    _reject_identity_replace(
        "ask_retrieval_scope_promotions",
        "existing.promotion_id=NEW.promotion_id "
        "OR existing.idempotency_key=NEW.idempotency_key "
        "OR (existing.scope_key=NEW.scope_key AND existing.revision=NEW.revision)",
    )
    _reject_identity_replace(
        "ask_answer_audit_records",
        "existing.answer_id=NEW.answer_id "
        "OR existing.idempotency_key=NEW.idempotency_key "
        "OR existing.request_id=NEW.request_id",
    )
    _reject_identity_replace(
        "ask_answer_audit_retrievals",
        "existing.answer_id=NEW.answer_id AND "
        "(existing.trace_ordinal=NEW.trace_ordinal OR existing.trace_id=NEW.trace_id)",
    )
    _reject_identity_replace(
        "ask_answer_audit_citations",
        "existing.answer_id=NEW.answer_id AND "
        "(existing.citation_number=NEW.citation_number "
        "OR (existing.trace_id=NEW.trace_id "
        "AND existing.result_ordinal=NEW.result_ordinal))",
    )
    _reject_identity_replace(
        "ask_answer_audit_claims",
        "existing.answer_id=NEW.answer_id "
        "AND existing.claim_ordinal=NEW.claim_ordinal",
    )
    _reject_identity_replace(
        "ask_answer_audit_claim_citations",
        "existing.answer_id=NEW.answer_id "
        "AND existing.claim_ordinal=NEW.claim_ordinal "
        "AND existing.citation_number=NEW.citation_number",
    )
    _reject_identity_replace(
        "ask_answer_audit_seals",
        "existing.answer_id=NEW.answer_id",
    )
    bind = op.get_bind()
    if "llm_budgets" in set(sa.inspect(bind).get_table_names()):
        columns = {
            str(item["name"])
            for item in sa.inspect(bind).get_columns("llm_budgets")
        }
        now = datetime.now(UTC).isoformat()
        if "on_exceed" not in columns:
            raise RuntimeError(
                "ask_claim_audit requires llm_budgets.on_exceed fail-closed support"
            )
        statement = sa.text(
            "INSERT INTO llm_budgets "
            "(purpose,monthly_cap_usd,warn_threshold_pct,hard_block,"
            "on_exceed,created_at,updated_at,notes) "
            "VALUES (:purpose,5.0,0.80,1,'block',:now,:now,:notes) "
            "ON CONFLICT(purpose) DO NOTHING"
        )
        bind.execute(
            statement,
            {
                "purpose": _CLAIM_AUDIT_PURPOSE,
                "now": now,
                "notes": (
                    "0253 sealed Ask exact-span claim audit; hard block because "
                    "an unaudited answer must never reach the user"
                ),
            },
        )
        budget = bind.execute(
            sa.text(
                "SELECT hard_block,on_exceed FROM llm_budgets WHERE purpose=:purpose"
            ),
            {"purpose": _CLAIM_AUDIT_PURPOSE},
        ).one_or_none()
        if budget is None or int(budget[0]) != 1 or str(budget[1]) != "block":
            raise RuntimeError(
                "ask_claim_audit budget must be configured hard_block/on_exceed=block"
            )
    # Minimal historical migration fixtures may intentionally omit the LLM
    # governance plane. Production cutover independently requires this table
    # and the exact fail-closed row before sealed mode can activate.


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_budgets" in set(sa.inspect(bind).get_table_names()):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose=:purpose"),
            {"purpose": _CLAIM_AUDIT_PURPOSE},
        )
    op.execute("DROP VIEW IF EXISTS v_ask_retrieval_scope_current")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_identity_immutable")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    for trigger in (
        "trg_ask_answer_audit_seal_support",
        "trg_ask_answer_audit_seal_ordinals",
        "trg_ask_answer_audit_seal_counts",
        "trg_ask_answer_audit_claim_citations_unsealed",
        "trg_ask_answer_audit_claims_unsealed",
        "trg_ask_answer_audit_citations_unsealed",
        "trg_ask_answer_audit_retrievals_unsealed",
        "trg_ask_answer_audit_claim_exact_span",
        "trg_ask_answer_audit_record_claim_llm",
        "trg_ask_answer_audit_record_llm",
        "trg_ask_answer_audit_record_session",
        "trg_ask_answer_audit_citation_exact",
        "trg_ask_answer_audit_retrieval_exact",
        "trg_ask_retrieval_scope_promotion_projection",
        "trg_ask_retrieval_scope_promotion_snapshot",
        "trg_ask_retrieval_scope_promotion_chain",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index(
        "ix_ask_answer_audit_session",
        table_name="ask_answer_audit_records",
    )
    op.drop_index(
        "ix_ask_retrieval_scope_promotion_current",
        table_name="ask_retrieval_scope_promotions",
    )
    for table in reversed(_TABLES):
        op.drop_table(table)
