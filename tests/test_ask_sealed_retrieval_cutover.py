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
from provenance.population_completeness import PopulationTemporalScope

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
POPULATION_RUN_ID = "population-run:test"
POPULATION_RECEIPT_SHA256 = "c" * 64


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
            cutoff_at TEXT, population_run_id TEXT,
            population_receipt_set_sha256 TEXT,
            population_observed_through TEXT,
            policy_version TEXT, verifier_name TEXT,
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
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            POPULATION_RUN_ID,
            POPULATION_RECEIPT_SHA256,
            NOW.isoformat(),
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
    assert {
        (
            request.population_run_id,
            request.population_receipt_set_sha256,
            request.population_observed_through,
        )
        for request in plan.requests
    } == {(POPULATION_RUN_ID, POPULATION_RECEIPT_SHA256, NOW)}


def test_multi_issuer_same_cutoff_requires_one_exact_population_coordinate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutoff = NOW.replace(hour=11)
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", cutoff.isoformat())
    _insert(conn, "BBB", cutoff.isoformat())
    conn.execute(
        "UPDATE ask_retrieval_scope_promotions "
        "SET population_observed_through=? WHERE scope_key='BBB'",
        (NOW.replace(hour=11, minute=30).isoformat(),),
    )

    def _verified(
        _conn: sqlite3.Connection,
        promotion: sealed.RetrievalPromotion,
        *,
        runtime: object | None = None,
    ) -> None:
        del _conn, promotion, runtime

    def _must_not_admit(
        _conn: sqlite3.Connection,
        *,
        promotion: sealed.RetrievalPromotion,
    ) -> None:
        del _conn, promotion
        pytest.fail("mixed population coordinates must fail before current admission")

    monkeypatch.setattr(sealed, "verify_retrieval_promotion", _verified)
    monkeypatch.setattr(
        sealed,
        "_admit_current_population_cutover",
        _must_not_admit,
    )

    readiness = assess_retrieval_readiness(conn, (_scope("AAA"), _scope("BBB")))

    assert readiness.outcome == "unavailable"
    assert readiness.reason_code == "population_cutover_stale"
    assert readiness.details == (
        "multi-issuer Ask requires one exact common population cutover "
        "(run, receipt, knowledge cutoff, observed through)"
    )
    assert readiness.scopes == ()


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


def test_execution_rechecks_current_promotion_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    def _admitted(
        _conn: sqlite3.Connection,
        *,
        promotion: sealed.RetrievalPromotion,
    ) -> None:
        del _conn, promotion

    monkeypatch.setattr(sealed, "_admit_current_population_cutover", _admitted)
    with pytest.raises(PromotionVerificationError, match="promotion_stale"):
        execute_sealed_retrieval_plan(conn, plan)


def test_execution_current_admits_every_planned_scope_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    plan = build_sealed_retrieval_plan(
        RetrievalReadiness(
            outcome="ready",
            reason_code="ready",
            details="test",
            scopes=ready_scopes,
        ),
        request_id="request-all-scopes",
        question="compare revenue",
        created_at=NOW,
    )
    admitted: list[str] = []

    def _admit(
        _conn: sqlite3.Connection,
        *,
        promotion: sealed.RetrievalPromotion,
    ) -> None:
        del _conn
        admitted.append(promotion.scope_key)
        if promotion.scope_key == "BBB":
            raise PromotionVerificationError(
                "population_cutover_stale",
                "BBB no longer resolves to the common current cutover",
            )

    def _must_not_retrieve(*_args: object, **_kwargs: object) -> object:
        pytest.fail("all population coordinates must be admitted before retrieval")

    monkeypatch.setattr(sealed, "_admit_current_population_cutover", _admit)
    monkeypatch.setattr(sealed, "retrieve_heterogeneous", _must_not_retrieve)

    with pytest.raises(
        PromotionVerificationError,
        match="BBB no longer resolves to the common current cutover",
    ):
        execute_sealed_retrieval_plan(conn, plan)
    assert admitted == ["AAA", "BBB"]


def test_live_ask_requires_current_population_cutover_receipt() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ask_retrieval_scope_promotions").fetchone()
    assert row is not None

    with pytest.raises(PromotionVerificationError, match="population_cutover_missing"):
        sealed._admit_current_population_cutover(
            conn,
            promotion=sealed._promotion_from_row(row),
        )


def test_live_ask_admits_exact_verified_population_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_sha = "a" * 64
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE current_population_cutover (
            population_run_id TEXT,
            knowledge_cutoff TEXT,
            observed_through TEXT,
            receipt_set_sha256 TEXT
        );
        CREATE TABLE population_run_headers (
            population_run_id TEXT,
            knowledge_cutoff TEXT,
            observed_through TEXT
        );
        CREATE TABLE population_cutover_receipts (
            population_run_id TEXT,
            receipt_set_sha256 TEXT
        );
        CREATE VIEW v_population_cutover_current AS
        SELECT * FROM current_population_cutover;
        """
    )
    conn.execute(
        "INSERT INTO current_population_cutover VALUES (?,?,?,?)",
        ("population-run:verified", NOW.isoformat(), NOW.isoformat(), receipt_sha),
    )
    conn.execute(
        "INSERT INTO population_run_headers VALUES (?,?,?)",
        ("population-run:verified", NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO population_cutover_receipts VALUES (?,?)",
        ("population-run:verified", receipt_sha),
    )
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.execute(
        "UPDATE ask_retrieval_scope_promotions SET "
        "population_run_id=?,population_receipt_set_sha256=?",
        ("population-run:verified", receipt_sha),
    )
    conn.row_factory = sqlite3.Row
    promotion_row = conn.execute("SELECT * FROM ask_retrieval_scope_promotions").fetchone()
    assert promotion_row is not None
    promotion = sealed._promotion_from_row(promotion_row)

    class _VerifiedReceipt:
        receipt_set_sha256 = receipt_sha
        temporal_scope = PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=NOW,
        )

    class _Ledger:
        def __init__(self, connection: sqlite3.Connection) -> None:
            assert connection is conn

        def verify(self, population_run_id: str) -> _VerifiedReceipt:
            assert population_run_id == "population-run:verified"
            return _VerifiedReceipt()

    monkeypatch.setattr(sealed, "PopulationCompletenessLedger", _Ledger)
    sealed._admit_current_population_cutover(conn, promotion=promotion)


def test_same_cutoff_newer_population_run_invalidates_old_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_observed = NOW
    newer_observed = NOW.replace(hour=13)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE population_run_headers (
            population_run_id TEXT,
            knowledge_cutoff TEXT,
            observed_through TEXT
        );
        CREATE TABLE population_cutover_receipts (
            population_run_id TEXT,
            receipt_set_sha256 TEXT
        );
        CREATE TABLE current_population_cutover (
            population_run_id TEXT,
            knowledge_cutoff TEXT,
            observed_through TEXT,
            receipt_set_sha256 TEXT
        );
        CREATE VIEW v_population_cutover_current AS
        SELECT * FROM current_population_cutover;
        """
    )
    conn.execute(
        "INSERT INTO population_run_headers VALUES (?,?,?)",
        (POPULATION_RUN_ID, NOW.isoformat(), old_observed.isoformat()),
    )
    conn.execute(
        "INSERT INTO population_cutover_receipts VALUES (?,?)",
        (POPULATION_RUN_ID, POPULATION_RECEIPT_SHA256),
    )
    conn.execute(
        "INSERT INTO current_population_cutover VALUES (?,?,?,?)",
        (
            "population-run:newer",
            NOW.isoformat(),
            newer_observed.isoformat(),
            "d" * 64,
        ),
    )
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    promotion_row = conn.execute("SELECT * FROM ask_retrieval_scope_promotions").fetchone()
    assert promotion_row is not None
    promotion = sealed._promotion_from_row(promotion_row)

    class _VerifiedReceipt:
        receipt_set_sha256 = POPULATION_RECEIPT_SHA256
        temporal_scope = PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=old_observed,
        )

    class _Ledger:
        def __init__(self, _connection: sqlite3.Connection) -> None:
            pass

        def verify(self, population_run_id: str) -> _VerifiedReceipt:
            assert population_run_id == POPULATION_RUN_ID
            return _VerifiedReceipt()

    monkeypatch.setattr(sealed, "PopulationCompletenessLedger", _Ledger)
    with pytest.raises(PromotionVerificationError, match="population_cutover_stale"):
        sealed._admit_current_population_cutover(conn, promotion=promotion)


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
        "src/provenance/population_completeness.py",
        "src/provenance/research_snapshot.py",
        "src/search/exact_semantic.py",
        "src/search/heterogeneous_retrieval.py",
    } == paths
