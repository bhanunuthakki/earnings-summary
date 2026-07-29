# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import ask.sealed_retrieval as sealed
from ask.audit_store import canonical_json, digest_text
from ask.sealed_retrieval import (
    PromotionVerificationError,
    ReadyRetrievalScope,
    RetrievalReadiness,
    RetrievalScope,
    assess_retrieval_readiness,
    build_sealed_retrieval_plan,
    execute_sealed_retrieval_plan,
    persist_retrieval_promotion,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _promotion_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE ask_retrieval_scope_promotions (
            promotion_id TEXT, idempotency_key TEXT, scope_key TEXT,
            revision INTEGER, issuer_id TEXT, reporting_entity_id TEXT,
            research_snapshot_id TEXT, research_snapshot_sha256 TEXT,
            fact_generation_id TEXT, fact_projection_seal_sha256 TEXT,
            source_inventory_set_json TEXT, source_inventory_set_sha256 TEXT,
            narrative_bundles_json TEXT, narrative_bundles_sha256 TEXT,
            cutoff_at TEXT, policy_version TEXT, verifier_name TEXT,
            verifier_version TEXT, verifier_code_sha256 TEXT,
            verifier_config_sha256 TEXT, status TEXT,
            supersedes_promotion_id TEXT, recorded_at TEXT
        );
        CREATE VIEW v_ask_retrieval_scope_current AS
        SELECT * FROM ask_retrieval_scope_promotions;
        """
    )


def _insert(conn: sqlite3.Connection, scope: str, cutoff: str) -> None:
    bundle = {
        "corpus_manifest_id": f"manifest-{scope}",
        "lexical_index_run_id": f"lex-{scope}",
        "vector_index_run_id": f"vec-{scope}",
        "embedding_promotion_id": f"embed-{scope}",
    }
    inventory_json = canonical_json([f"inventory-{scope}"])
    bundle_json = canonical_json([bundle])
    conn.execute(
        "INSERT INTO ask_retrieval_scope_promotions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"promotion-{scope}",
            f"promotion-{scope}",
            scope,
            1,
            f"issuer-{scope}",
            f"reporter-{scope}",
            f"research-{scope}",
            "a" * 64,
            f"generation-{scope}",
            "b" * 64,
            inventory_json,
            digest_text(inventory_json),
            bundle_json,
            digest_text(bundle_json),
            cutoff,
            "1",
            "verifier",
            "1",
            "e" * 64,
            "f" * 64,
            "promoted",
            None,
            NOW.isoformat(),
        ),
    )


def _scope(key: str) -> RetrievalScope:
    return RetrievalScope(
        scope_key=key,
        ticker=key,
        issuer_id=f"issuer-{key}",
        reporting_entity_id=f"reporter-{key}",
    )


def test_multi_issuer_readiness_is_all_or_nothing() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    readiness = assess_retrieval_readiness(conn, (_scope("BBB"), _scope("AAA")))
    assert readiness.outcome == "coverage_incomplete"
    assert readiness.reason_code == "promotion_missing"
    assert readiness.scopes == ()


def test_multi_issuer_requires_common_cutoff_and_builds_exact_requests() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    _insert(conn, "BBB", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ask_retrieval_scope_promotions ORDER BY scope_key"
    ).fetchall()
    ready_scopes = tuple(
        ReadyRetrievalScope(
            scope=_scope(str(row["scope_key"])),
            promotion=sealed._promotion_from_row(row),
        )
        for row in rows
    )
    readiness = RetrievalReadiness(
        outcome="ready",
        reason_code="ready",
        details="test",
        scopes=ready_scopes,
    )
    plan = build_sealed_retrieval_plan(
        readiness,
        request_id="request-1",
        question="  compare revenue  ",
        created_at=NOW,
    )
    assert len(plan.requests) == 2
    assert plan.question == "compare revenue"
    assert {request.filters.reporting_entity_id for request in plan.requests} == {
        "reporter-AAA",
        "reporter-BBB",
    }
    assert all(request.narrative_bundles[0].vector_index_run_id for request in plan.requests)


def test_promotion_json_hash_tampering_returns_stable_fail_closed_reason() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.execute(
        "UPDATE ask_retrieval_scope_promotions "
        "SET source_inventory_set_sha256=? WHERE scope_key='AAA'",
        ("9" * 64,),
    )
    readiness = assess_retrieval_readiness(conn, (_scope("AAA"),))
    assert readiness.outcome == "unavailable"
    assert readiness.reason_code == "promotion_invalid"
    assert readiness.scopes == ()


def test_exact_promotion_replay_precedes_mutable_readiness_checks() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ask_retrieval_scope_promotions WHERE scope_key='AAA'"
    ).fetchone()
    assert row is not None
    promotion = sealed._promotion_from_row(row)
    assert persist_retrieval_promotion(conn, promotion) == promotion


def test_execution_rechecks_current_promotion_before_retrieval() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ask_retrieval_scope_promotions WHERE scope_key='AAA'"
    ).fetchone()
    assert row is not None
    ready = ReadyRetrievalScope(
        scope=_scope("AAA"),
        promotion=sealed._promotion_from_row(row),
    )
    plan = build_sealed_retrieval_plan(
        RetrievalReadiness(
            outcome="ready",
            reason_code="ready",
            details="test",
            scopes=(ready,),
        ),
        request_id="request-1",
        question="question",
        created_at=NOW,
    )
    conn.execute(
        "UPDATE ask_retrieval_scope_promotions SET status='withdrawn' WHERE scope_key='AAA'"
    )
    with pytest.raises(PromotionVerificationError, match="promotion_stale"):
        execute_sealed_retrieval_plan(conn, plan)


def test_vector_projection_must_be_under_configured_index_root(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE search_projection_seals (index_run_id TEXT,index_kind TEXT,storage_uri TEXT)"
    )
    allowed = tmp_path / "indexes"
    escaped = tmp_path / "other" / "run-1"
    conn.execute(
        "INSERT INTO search_projection_seals VALUES (?,?,?)",
        (
            "vector-1",
            "vector",
            f"lance://{escaped / 'vectors.lance'}#evidence_chunks",
        ),
    )
    with pytest.raises(ValueError, match="escapes configured index root"):
        sealed._verify_index_root(
            conn,
            vector_index_run_id="vector-1",
            configured_root=allowed,
        )


def test_trace_loader_verifies_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise ValueError("tampered")

    monkeypatch.setattr(sealed, "verify_heterogeneous_retrieval_trace", _blocked)
    with pytest.raises(ValueError, match="tampered"):
        sealed.load_verified_trace_evidence(conn, "trace-tampered")


def test_verifier_artifact_hash_is_line_ending_insensitive(tmp_path: Path) -> None:
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(b"one\ntwo\n")
    crlf.write_bytes(b"one\r\ntwo\r\n")
    assert sealed._canonical_artifact_sha256(lf) == sealed._canonical_artifact_sha256(crlf)
    manifest = sealed.current_verifier_manifest()
    artifacts = cast(list[dict[str, object]], manifest["artifacts"])
    paths = {str(item["path"]) for item in artifacts}
    assert {
        "src/ask/audit_store.py",
        "src/ask/sealed_retrieval.py",
        "src/provenance/research_snapshot.py",
        "src/search/exact_semantic.py",
        "src/search/heterogeneous_retrieval.py",
    } == paths
