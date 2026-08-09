# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

import ask.audit_store as audit_store
import ask.engine as ask_engine
from ask.audit_store import (
    AnswerAuditPackage,
    AnswerAuditRecord,
    AnswerCitation,
    AnswerClaim,
    AnswerClaimCitation,
    AnswerContextTurn,
    AnswerPromptVariables,
    AnswerRetrieval,
    CitationAuditPayload,
    ClaimAuditPromptVariables,
    RetrievalAssemblyItem,
    audit_answer_audit_integrity,
    digest_text,
    persist_answer_audit,
    retrieval_query_sha256,
    verify_answer_audit,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
SHA = "a" * 64
_MIGRATION_CONNECTIONS: list[tuple[Engine, Connection]] = []


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE ask_sessions (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL
        );
        CREATE TABLE ask_turns (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            purpose TEXT,
            model TEXT NOT NULL,
            provider TEXT,
            transport TEXT,
            template_id TEXT,
            template_version TEXT,
            template_vars_sha256 TEXT,
            prompt_sha256 TEXT NOT NULL,
            response_sha256 TEXT,
            error TEXT
        );
        CREATE TABLE research_snapshot_headers (
            research_snapshot_id TEXT PRIMARY KEY
        );
        CREATE TABLE research_snapshot_seals (
            research_snapshot_id TEXT PRIMARY KEY,
            member_set_sha256 TEXT NOT NULL
        );
        CREATE TABLE research_snapshot_admission_receipts (
            research_snapshot_id TEXT PRIMARY KEY,
            research_snapshot_sha256 TEXT NOT NULL
        );
        CREATE TABLE research_snapshot_universe_commitments (
            research_snapshot_id TEXT PRIMARY KEY,
            issuer_id TEXT NOT NULL,
            reporting_entity_ids_json TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_projection_generations (
            generation_id TEXT PRIMARY KEY
        );
        CREATE TABLE canonical_fact_projection_seals (
            generation_id TEXT PRIMARY KEY,
            projection_seal_sha256 TEXT NOT NULL
        );
        CREATE TABLE heterogeneous_retrieval_trace_headers (
            trace_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            research_snapshot_id TEXT NOT NULL,
            research_snapshot_sha256 TEXT NOT NULL,
            fact_generation_id TEXT NOT NULL,
            query_sha256 TEXT NOT NULL
        );
        CREATE TABLE heterogeneous_retrieval_trace_candidates (
            trace_id TEXT NOT NULL,
            candidate_ordinal INTEGER NOT NULL,
            candidate_kind TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            source_commitment_sha256 TEXT NOT NULL,
            PRIMARY KEY(trace_id,candidate_ordinal)
        );
        CREATE TABLE heterogeneous_retrieval_trace_results (
            trace_id TEXT NOT NULL,
            result_ordinal INTEGER NOT NULL,
            candidate_ordinal INTEGER NOT NULL,
            PRIMARY KEY(trace_id,result_ordinal),
            FOREIGN KEY(trace_id,candidate_ordinal)
              REFERENCES heterogeneous_retrieval_trace_candidates(trace_id,candidate_ordinal)
        );
        CREATE TABLE heterogeneous_retrieval_trace_seals (
            trace_id TEXT PRIMARY KEY,
            trace_sha256 TEXT NOT NULL
        );
        """
    )
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions_archived"
        / "0253_ask_sealed_answer_audit.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0253_ask_sealed_answer_audit", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(
        "sqlite://",
        creator=lambda: conn,
        poolclass=StaticPool,
    )
    sa_conn = engine.connect()
    _MIGRATION_CONNECTIONS.append((engine, sa_conn))
    context = MigrationContext.configure(sa_conn)
    with Operations.context(context):
        migration.upgrade()


def _seed_trace(conn: sqlite3.Connection) -> None:
    query_sha256 = retrieval_query_sha256("question")
    answer = "Revenue grew 20%."
    answer_variables = AnswerPromptVariables(
        system_context="context",
        thread_text="(first turn)",
        evidence_block="fragment",
        question="question",
    )
    answer_prompt = ask_engine._SEALED_ANSWER_TEMPLATE.render(**answer_variables.model_dump())
    claim_variables = ClaimAuditPromptVariables(
        repair_feedback="",
        answer=answer,
        evidence="fragment",
    )
    claim_prompt = ask_engine.CLAIM_AUDIT_TEMPLATE.render(**claim_variables.model_dump())
    conn.execute("INSERT INTO ask_sessions VALUES (?,?)", ("session-1", "portfolio"))
    conn.execute(
        "INSERT INTO ask_turns VALUES (?,?,?,?,?)",
        (1, "session-1", "user", "question", NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            "ask-answer:request-1",
            "ask_answer",
            "model",
            "openai",
            "subscription_cli",
            answer_prompt.template_id,
            answer_prompt.template_version,
            answer_prompt.vars_sha256,
            digest_text(str(answer_prompt)),
            digest_text(answer),
            None,
        ),
    )
    conn.execute(
        "INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            2,
            "ask-claim-audit:request-1",
            "ask_claim_audit",
            "auditor",
            "openai",
            "subscription_cli",
            claim_prompt.template_id,
            claim_prompt.template_version,
            claim_prompt.vars_sha256,
            digest_text(str(claim_prompt)),
            digest_text("audit response"),
            None,
        ),
    )
    conn.execute("INSERT INTO research_snapshot_headers VALUES (?)", ("research-1",))
    conn.execute(
        "INSERT INTO research_snapshot_seals VALUES (?,?)",
        ("research-1", "b" * 64),
    )
    conn.execute(
        "INSERT INTO research_snapshot_admission_receipts VALUES (?,?)",
        ("research-1", "b" * 64),
    )
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?)",
        ("research-1", "issuer-1", '["reporting-1"]'),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?)",
        ("generation-1",),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-1", "f" * 64),
    )
    inventories = '["inventory-1"]'
    bundles = (
        '[{"corpus_manifest_id":"manifest-1",'
        '"embedding_promotion_id":"embedding-1",'
        '"lexical_index_run_id":"lexical-1",'
        '"vector_index_run_id":"vector-1"}]'
    )
    conn.execute(
        "INSERT INTO ask_retrieval_scope_promotions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-1",
            "promotion-1",
            "scope-1",
            1,
            "issuer-1",
            "reporting-1",
            "research-1",
            "b" * 64,
            "generation-1",
            "f" * 64,
            inventories,
            digest_text(inventories),
            bundles,
            digest_text(bundles),
            NOW.isoformat(),
            "1",
            "verifier",
            "1",
            "1" * 64,
            "2" * 64,
            "promoted",
            None,
            NOW.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO heterogeneous_retrieval_trace_headers VALUES (?,?,?,?,?,?)",
        (
            "trace-1",
            f"ask-request:request-1:{query_sha256}:promotion-1",
            "research-1",
            "b" * 64,
            "generation-1",
            query_sha256,
        ),
    )
    conn.execute(
        "INSERT INTO heterogeneous_retrieval_trace_candidates VALUES (?,?,?,?,?)",
        ("trace-1", 0, "narrative", "chunk-1", "c" * 64),
    )
    conn.execute(
        "INSERT INTO heterogeneous_retrieval_trace_results VALUES (?,?,?)",
        ("trace-1", 0, 0),
    )
    conn.execute(
        "INSERT INTO heterogeneous_retrieval_trace_seals VALUES (?,?)",
        ("trace-1", "d" * 64),
    )


def _package() -> AnswerAuditPackage:
    answer = "Revenue grew 20%."
    answer_variables = AnswerPromptVariables(
        system_context="context",
        thread_text="(first turn)",
        evidence_block="fragment",
        question="question",
    )
    answer_prompt = ask_engine._SEALED_ANSWER_TEMPLATE.render(**answer_variables.model_dump())
    claim_variables = ClaimAuditPromptVariables(
        repair_feedback="",
        answer=answer,
        evidence="fragment",
    )
    claim_prompt = ask_engine.CLAIM_AUDIT_TEMPLATE.render(**claim_variables.model_dump())
    return AnswerAuditPackage(
        record=AnswerAuditRecord(
            answer_id="answer-1",
            idempotency_key="answer-1",
            request_id="request-1",
            session_id="session-1",
            surface="portfolio",
            query_sha256=retrieval_query_sha256("question"),
            prompt_sha256=digest_text(str(answer_prompt)),
            prompt_template_id=answer_prompt.template_id,
            prompt_template_version=answer_prompt.template_version,
            prompt_template_vars_sha256=answer_prompt.vars_sha256,
            prompt_variables=answer_variables,
            context_turns=(
                AnswerContextTurn(
                    turn_id=1,
                    session_id="session-1",
                    role="user",
                    text_sha256=digest_text("question"),
                    created_at=NOW.isoformat(),
                ),
            ),
            retrieval_assembly=(
                RetrievalAssemblyItem(
                    citation_number=1,
                    trace_id="trace-1",
                    result_ordinal=0,
                    candidate_kind="narrative",
                    candidate_id="chunk-1",
                    source_commitment_sha256="c" * 64,
                    prompt_text_sha256=digest_text("fragment"),
                ),
            ),
            retrieval_prompt_fragments=("fragment",),
            answer_text=answer,
            llm_purpose="ask_answer",
            llm_model="model",
            llm_provider="openai",
            llm_transport="subscription_cli",
            llm_call_id=1,
            llm_run_id="ask-answer:request-1",
            claim_auditor_version="2",
            claim_audit_purpose="ask_claim_audit",
            claim_audit_template_id=claim_prompt.template_id,
            claim_audit_template_version=claim_prompt.template_version,
            claim_audit_template_vars_sha256=claim_prompt.vars_sha256,
            claim_audit_prompt_variables=claim_variables,
            claim_auditor_model="auditor",
            claim_audit_provider="openai",
            claim_audit_transport="subscription_cli",
            claim_audit_prompt_sha256=digest_text(str(claim_prompt)),
            claim_audit_response_sha256=digest_text("audit response"),
            claim_audit_llm_call_id=2,
            claim_audit_run_id="ask-claim-audit:request-1",
            recorded_at=NOW,
        ),
        retrievals=(
            AnswerRetrieval(
                trace_ordinal=0,
                request_id="request-1",
                query_sha256=retrieval_query_sha256("question"),
                promotion_id="promotion-1",
                trace_id="trace-1",
                trace_sha256="d" * 64,
                research_snapshot_sha256="b" * 64,
                recorded_at=NOW,
            ),
        ),
        citations=(
            AnswerCitation(
                citation_number=1,
                trace_id="trace-1",
                result_ordinal=0,
                candidate_kind="narrative",
                candidate_id="chunk-1",
                source_commitment_sha256="c" * 64,
                citation=CitationAuditPayload(
                    n=1,
                    trace_id="trace-1",
                    result_ordinal=0,
                    candidate_kind="narrative",
                    candidate_id="chunk-1",
                    source_commitment_sha256="c" * 64,
                ),
                recorded_at=NOW,
            ),
        ),
        claims=(
            AnswerClaim(
                claim_ordinal=0,
                char_start=0,
                char_end=len(answer),
                claim_text=answer,
                supported=True,
                recorded_at=NOW,
            ),
        ),
        claim_citations=(
            AnswerClaimCitation(
                claim_ordinal=0,
                citation_number=1,
                recorded_at=NOW,
            ),
        ),
        sealed_at=NOW,
    )


def test_answer_audit_is_exact_idempotent_and_append_only() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    first = persist_answer_audit(conn, _package())
    second = persist_answer_audit(conn, _package())
    assert first == second
    assert verify_answer_audit(conn, "answer-1") == first
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE ask_answer_audit_records SET answer_text='changed' WHERE answer_id='answer-1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="sealed Ask answer"):
        conn.execute(
            "INSERT INTO ask_answer_audit_claims VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "answer-1",
                1,
                0,
                1,
                "x",
                digest_text("x"),
                0,
                "{}",
                digest_text("{}"),
                NOW.isoformat(),
            ),
        )


def test_insert_or_replace_cannot_bypass_append_only_identity() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    package = _package()
    persist_answer_audit(conn, package)
    columns, values = audit_store._record_values(package.record)
    with pytest.raises(sqlite3.IntegrityError, match="identity is append-only"):
        conn.execute(
            f"INSERT OR REPLACE INTO ask_answer_audit_records "
            f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            values,
        )
    assert verify_answer_audit(conn, package.record.answer_id).answer_id == "answer-1"


def test_substantive_answer_cannot_claim_zero_audited_claims() -> None:
    package = _package()
    with pytest.raises(ValueError, match="zero-claim sealed answers"):
        AnswerAuditPackage.model_validate(
            package.model_dump()
            | {
                "claims": [],
                "claim_citations": [],
                "citations": [],
            }
        )


def test_citation_source_substitution_and_incomplete_support_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    package = _package()
    bad_citation = package.citations[0].model_copy(update={"source_commitment_sha256": "e" * 64})
    with pytest.raises(ValueError, match="exact retrieval assembly item"):
        persist_answer_audit(
            conn,
            package.model_copy(update={"citations": (bad_citation,)}),
        )
    assert conn.execute("SELECT COUNT(*) FROM ask_answer_audit_records").fetchone()[0] == 0


def test_claim_span_trace_identity_and_llm_identity_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    package = _package()
    with pytest.raises(ValueError, match="exact answer span"):
        AnswerAuditPackage.model_validate(
            package.model_dump()
            | {"claims": [package.claims[0].model_dump() | {"claim_text": "Revenue fell 20%."}]}
        )
    with pytest.raises(ValueError, match="exact answer request and query"):
        AnswerAuditPackage.model_validate(
            package.model_dump()
            | {"retrievals": [package.retrievals[0].model_dump() | {"query_sha256": "9" * 64}]}
        )
    wrong_model = package.record.model_copy(update={"llm_model": "self-asserted"})
    with pytest.raises(ValueError, match="governed llm_calls"):
        persist_answer_audit(
            conn,
            package.model_copy(update={"record": wrong_model}),
        )


def test_sealed_claim_span_is_append_only() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    package = _package()
    persist_answer_audit(conn, package)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE ask_answer_audit_claims SET claim_text='Revenue fell 20%.' "
            "WHERE answer_id='answer-1'"
        )


def test_claim_span_trigger_and_verifier_reject_out_of_answer_text() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    package = _package()
    second_record = package.record.model_copy(
        update={
            "answer_id": "answer-2",
            "idempotency_key": "answer-2",
            "request_id": "request-2",
            "session_id": None,
            "surface": "api",
            "context_turns": (),
            "llm_call_id": 3,
            "claim_audit_llm_call_id": 4,
        }
    )
    conn.execute(
        "INSERT INTO llm_calls SELECT 3,run_id,purpose,model,provider,transport,"
        "template_id,template_version,template_vars_sha256,prompt_sha256,"
        "response_sha256,error "
        "FROM llm_calls WHERE id=1"
    )
    conn.execute(
        "INSERT INTO llm_calls SELECT 4,run_id,purpose,model,provider,transport,"
        "template_id,template_version,template_vars_sha256,prompt_sha256,"
        "response_sha256,error "
        "FROM llm_calls WHERE id=2"
    )
    columns, values = audit_store._record_values(second_record)
    conn.execute(
        f"INSERT INTO ask_answer_audit_records ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        values,
    )
    with pytest.raises(sqlite3.IntegrityError, match="answer span"):
        conn.execute(
            "INSERT INTO ask_answer_audit_claims VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "answer-2",
                0,
                0,
                8,
                "Earnings",
                digest_text("Earnings"),
                0,
                "{}",
                digest_text("{}"),
                NOW.isoformat(),
            ),
        )


def test_exact_llm_run_ids_are_part_of_the_audit_identity() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    package = _package()
    bad = package.model_copy(
        update={
            "record": package.record.model_copy(
                update={"llm_run_id": "ask-answer:different-request"}
            )
        }
    )
    with pytest.raises(ValueError, match="governed llm_calls"):
        persist_answer_audit(conn, bad)

    persist_answer_audit(conn, package)
    conn.execute("DROP TRIGGER trg_ask_answer_audit_claims_append_only")
    conn.execute(
        "UPDATE ask_answer_audit_claims SET claim_text='Revenue fell 20%.' "
        "WHERE answer_id='answer-1'"
    )
    with pytest.raises(ValueError, match="claim commitment mismatch"):
        verify_answer_audit(conn, "answer-1")
    integrity = audit_answer_audit_integrity(conn)
    assert integrity.invalid_sealed_answer_ids == ("answer-1",)
    assert not integrity.ready


def test_promotion_trigger_binds_research_universe_identity() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _seed_trace(conn)
    with pytest.raises(sqlite3.IntegrityError, match="admitted Research Snapshot"):
        conn.execute(
            "INSERT INTO ask_retrieval_scope_promotions "
            "SELECT 'promotion-bad','promotion-bad','scope-bad',1,issuer_id,"
            "'reporting-other',research_snapshot_id,research_snapshot_sha256,"
            "fact_generation_id,fact_projection_seal_sha256,"
            "source_inventory_set_json,source_inventory_set_sha256,"
            "narrative_bundles_json,narrative_bundles_sha256,cutoff_at,"
            "policy_version,verifier_name,verifier_version,verifier_code_sha256,"
            "verifier_config_sha256,status,NULL,recorded_at "
            "FROM ask_retrieval_scope_promotions WHERE promotion_id='promotion-1'"
        )
