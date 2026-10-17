from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import pytest

import ask.engine as engine
import ask.sealed_retrieval as sealed
import provenance.fact_read_model as fact_reader
from ask.audit_store import canonical_json, digest_text
from ask.sealed_retrieval import (
    PromotionVerificationError,
    ReadyRetrievalScope,
    RetrievalPromotion,
    RetrievalReadiness,
    RetrievalScope,
    SealedEvidenceItem,
    assess_retrieval_readiness,
    build_sealed_retrieval_plan,
    derive_retrieval_scope_id,
    execute_sealed_retrieval_plan,
)
from provenance.fact_plane_v2 import FactCellV2, ReportedFactObservationV2
from provenance.fact_read_model import FactReadModel, ProvenanceBundleRead
from provenance.population_completeness import PopulationTemporalScope
from provenance.source_fact_publication import (
    PublicationVerificationError,
    VerifiedSourceFactPublication,
    record_commitment,
    verify_source_fact_publication,
)
from provenance.source_fact_repository import ReportedSourceFact, SourceFactRepository
from search.heterogeneous_retrieval import HeterogeneousRetrievalReceipt
from tests.test_source_fact_repository import STAMP as FACT_STAMP
from tests.test_source_fact_repository import (
    make_cell,
    make_publication,
    make_report,
    seed_foundation,
)


class _AdmitCutoverCall(Protocol):
    def __call__(self, conn: sqlite3.Connection, *, promotion: RetrievalPromotion) -> None: ...


class _IndexRootCall(Protocol):
    def __call__(
        self, conn: sqlite3.Connection, *, vector_index_run_id: str, configured_root: Path
    ) -> None: ...


# Keep internal seams typed for tests without exporting application APIs.
_promotion_from_row = cast(
    Callable[[sqlite3.Row], RetrievalPromotion], vars(sealed)["_promotion_from_row"]
)
_promotion_values = cast(
    Callable[[RetrievalPromotion], tuple[object, ...]], vars(sealed)["_promotion_values"]
)
_promotion_insert_sql = cast(str, vars(sealed)["_PROMOTION_INSERT_SQL"])
_admit_current_population_cutover = cast(
    _AdmitCutoverCall, vars(sealed)["_admit_current_population_cutover"]
)
_verify_index_root = cast(_IndexRootCall, vars(sealed)["_verify_index_root"])
_canonical_artifact_sha256 = cast(Callable[[Path], str], vars(sealed)["_canonical_artifact_sha256"])
_evidence_block = cast(
    Callable[[Sequence[SealedEvidenceItem]], tuple[tuple[str, ...], str]],
    vars(engine)["_sealed_evidence_block"],
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
POPULATION_RUN_ID = "population-run:test"
POPULATION_RECEIPT_SHA256 = "c" * 64


def _promotion_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE ask_retrieval_scope_promotions (
            promotion_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, scope_key TEXT,
            source_scope_key TEXT, source_scope_revision_id TEXT,
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
        CREATE TABLE v_issuer_reporting_scope_current (
            scope_revision_id TEXT, scope_key TEXT, issuer_id TEXT,
            inclusion_state TEXT
        );
        """
    )


def _insert(conn: sqlite3.Connection, scope: str, cutoff: str) -> None:
    conn.execute(
        "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
        (f"scope-revision-{scope}", scope, f"issuer-{scope}", "core"),
    )
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
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"promotion-{scope}",
            f"promotion-{scope}",
            _scope_id(scope),
            scope,
            f"scope-revision-{scope}",
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
        scope_id=_scope_id(key),
        source_scope_key=key,
        source_scope_revision_id=f"scope-revision-{key}",
        ticker=key,
        issuer_id=f"issuer-{key}",
        reporting_entity_id=f"reporter-{key}",
    )


def _scope_id(key: str) -> str:
    return derive_retrieval_scope_id(
        source_scope_key=key,
        issuer_id=f"issuer-{key}",
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
            scope=_scope(str(row["source_scope_key"])),
            promotion=_promotion_from_row(row),
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
        "UPDATE ask_retrieval_scope_promotions SET population_observed_through=? WHERE scope_key=?",
        (NOW.replace(hour=11, minute=30).isoformat(), _scope_id("BBB")),
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
        "UPDATE ask_retrieval_scope_promotions SET source_inventory_set_sha256=? WHERE scope_key=?",
        ("9" * 64, _scope_id("AAA")),
    )
    readiness = assess_retrieval_readiness(conn, (_scope("AAA"),))
    assert readiness.outcome == "unavailable"
    assert readiness.reason_code == "promotion_invalid"
    assert readiness.scopes == ()


def test_promotion_revision_binding_rejects_changed_source_registry() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.execute(
        "UPDATE v_issuer_reporting_scope_current SET scope_revision_id=? "
        "WHERE scope_key=? AND issuer_id=?",
        ("scope-revision-AAA-new", "AAA", "issuer-AAA"),
    )

    readiness = assess_retrieval_readiness(conn, (_scope("AAA"),))

    assert readiness.outcome == "unavailable"
    assert readiness.reason_code == "scope_identity_incomplete"
    assert "exact current composite source scope revision" in readiness.details


def test_exact_promotion_replay_revalidates_current_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ask_retrieval_scope_promotions WHERE scope_key=?",
        (_scope_id("AAA"),),
    ).fetchone()
    assert row is not None
    promotion = _promotion_from_row(row)
    verified: list[str] = []

    def verify(
        connection: sqlite3.Connection,
        stored: sealed.RetrievalPromotion,
        *,
        runtime: object,
    ) -> None:
        del connection, runtime
        verified.append(stored.promotion_id)

    monkeypatch.setattr(sealed, "verify_retrieval_promotion", verify)
    result = sealed.persist_retrieval_promotion_with_outcome(conn, promotion)
    assert result.outcome == "exact_replay"
    assert result.promotion == promotion
    assert verified == [promotion.promotion_id]


def test_insert_conflict_replay_is_revalidated_and_reported_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ask_retrieval_scope_promotions").fetchone()
    assert row is not None
    promotion = _promotion_from_row(row)
    conn.execute("DELETE FROM ask_retrieval_scope_promotions")
    verified: list[str] = []

    def insert_during_verification(
        connection: sqlite3.Connection,
        stored: sealed.RetrievalPromotion,
        *,
        runtime: object,
    ) -> None:
        del runtime
        verified.append(stored.promotion_id)
        if len(verified) == 1:
            connection.execute(
                _promotion_insert_sql,
                _promotion_values(stored),
            )

    monkeypatch.setattr(sealed, "verify_retrieval_promotion", insert_during_verification)
    result = sealed.persist_retrieval_promotion_with_outcome(conn, promotion)

    assert result.outcome == "exact_replay"
    assert result.promotion == promotion
    assert verified == [promotion.promotion_id, promotion.promotion_id]
    count_row = conn.execute("SELECT COUNT(*) FROM ask_retrieval_scope_promotions").fetchone()
    assert count_row is not None
    assert count_row[0] == 1


def test_stale_exact_promotion_replay_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ask_retrieval_scope_promotions").fetchone()
    assert row is not None
    promotion = _promotion_from_row(row)

    def stale(*_args: object, **_kwargs: object) -> None:
        raise PromotionVerificationError(
            "scope_identity_incomplete",
            "source scope revision advanced",
        )

    monkeypatch.setattr(sealed, "verify_retrieval_promotion", stale)
    with pytest.raises(PromotionVerificationError, match="source scope revision advanced"):
        sealed.persist_retrieval_promotion_with_outcome(conn, promotion)


def test_execution_rechecks_current_promotion_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ask_retrieval_scope_promotions WHERE scope_key=?",
        (_scope_id("AAA"),),
    ).fetchone()
    assert row is not None
    ready = ReadyRetrievalScope(
        scope=_scope("AAA"),
        promotion=_promotion_from_row(row),
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
        "UPDATE ask_retrieval_scope_promotions SET status='withdrawn' WHERE scope_key=?",
        (_scope_id("AAA"),),
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
            scope=_scope(str(row["source_scope_key"])),
            promotion=_promotion_from_row(row),
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
        admitted.append(promotion.scope_id)
        if promotion.scope_id == _scope_id("BBB"):
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
    assert admitted == [_scope_id("AAA"), _scope_id("BBB")]


def test_live_ask_requires_current_population_cutover_receipt() -> None:
    conn = sqlite3.connect(":memory:")
    _promotion_table(conn)
    _insert(conn, "AAA", NOW.isoformat())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ask_retrieval_scope_promotions").fetchone()
    assert row is not None

    with pytest.raises(PromotionVerificationError, match="population_cutover_missing"):
        _admit_current_population_cutover(
            conn,
            promotion=_promotion_from_row(row),
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
    promotion = _promotion_from_row(promotion_row)

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
    _admit_current_population_cutover(conn, promotion=promotion)


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
    promotion = _promotion_from_row(promotion_row)

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
        _admit_current_population_cutover(conn, promotion=promotion)


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
        _verify_index_root(
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
    assert _canonical_artifact_sha256(lf) == _canonical_artifact_sha256(crlf)
    manifest = sealed.current_verifier_manifest()
    artifacts = cast(list[dict[str, object]], manifest["artifacts"])
    paths = {str(item["path"]) for item in artifacts}
    assert {
        "src/ask/audit_store.py",
        "src/scope_identity.py",
        "src/provenance/scope_identity.py",
        "src/ask/sealed_retrieval.py",
        "src/provenance/population_completeness.py",
        "src/provenance/research_snapshot.py",
        "src/provenance/fact_read_model.py",
        "src/provenance/source_fact_publication.py",
        "src/search/exact_semantic.py",
        "src/search/heterogeneous_retrieval.py",
    } == paths


@dataclass(frozen=True)
class _FactContextClocks:
    knowledge_at: datetime = FACT_STAMP
    cell_knowledge_at: datetime | None = None
    observation_knowledge_at: datetime | None = None
    recorded_at: datetime = FACT_STAMP
    observed_through: datetime = FACT_STAMP
    fact_count: int = 1


@pytest.fixture
def published_fact_context(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> Iterator[tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row]]:
    """Real source publication plus an isolated, preverified projection adapter input.

    Projection/trace admission is tested separately. This fixture tests that the
    final prompt adapter reuses the sealed source owner and keeps its context.
    """
    clocks = cast(_FactContextClocks, getattr(request, "param", _FactContextClocks()))
    cell_knowledge = clocks.cell_knowledge_at or clocks.knowledge_at
    observation_knowledge = clocks.observation_knowledge_at or clocks.knowledge_at
    publication_knowledge = max(cell_knowledge, observation_knowledge)
    path = migrated_db(tmp_path / "fact-context.db")
    conn = sqlite3.connect(path)
    seed_foundation(conn)
    publication = make_publication()
    original = publication.reported_facts[0]
    cell = FactCellV2.model_validate(
        {
            **original.cell.model_dump(mode="json"),
            "unit_key": "USD_millions",
            "semantic_key_sha256": None,
            "knowledge_at": cell_knowledge,
            "recorded_at": clocks.recorded_at,
            "dimensions": tuple(
                dimension.model_copy(update={"recorded_at": clocks.recorded_at})
                for dimension in original.cell.dimensions
            ),
        }
    )
    observation = ReportedFactObservationV2.model_validate(
        {
            **original.observation.model_dump(mode="json"),
            "precision": "3",
            "knowledge_at": observation_knowledge,
            "recorded_at": clocks.recorded_at,
        }
    )
    publication = publication.model_copy(
        update={
            "created_at": publication_knowledge,
            "recorded_at": clocks.recorded_at,
            "reported_facts": (ReportedSourceFact(cell=cell, observation=observation),),
            "extraction_seals": tuple(
                seal.model_copy(update={"recorded_at": clocks.recorded_at})
                for seal in publication.extraction_seals
            ),
            "resolutions": tuple(
                resolution.model_copy(
                    update={
                        "knowledge_cutoff": publication_knowledge,
                        "recorded_at": clocks.recorded_at,
                        "candidates": tuple(
                            candidate.model_copy(update={"recorded_at": clocks.recorded_at})
                            for candidate in resolution.candidates
                        ),
                    }
                )
                for resolution in publication.resolutions
            ),
        }
    )
    if clocks.fact_count == 2:
        extra_cell = FactCellV2.model_validate(
            {
                **make_cell("batch", period_end=FACT_STAMP - timedelta(days=1)).model_dump(
                    mode="json"
                ),
                "unit_key": "USD_millions",
                "semantic_key_sha256": None,
            }
        )
        publication = publication.model_copy(
            update={
                "reported_facts": (
                    *publication.reported_facts,
                    ReportedSourceFact(
                        cell=extra_cell,
                        observation=make_report(extra_cell, "batch", numeric_value="200"),
                    ),
                )
            }
        )
    SourceFactRepository(conn).publish(publication)
    conn.execute(
        "CREATE TEMP TABLE canonical_fact_projection_generations AS "
        "SELECT * FROM main.canonical_fact_projection_generations WHERE 0"
    )
    conn.execute(
        "CREATE TEMP TABLE canonical_fact_projection_entries AS "
        "SELECT * FROM main.canonical_fact_projection_entries WHERE 0"
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations "
        "(generation_id,cutoff_at,recorded_at) VALUES ('generation',?,?)",
        (FACT_STAMP.isoformat(), clocks.observed_through.isoformat()),
    )
    for ordinal, fact in enumerate(publication.reported_facts):
        cell, observation = fact.cell, fact.observation
        payload: dict[str, object] = {
            "canonical_metric_cell_id": "canonical-revenue"
            if ordinal == 0
            else "canonical-revenue-batch",
            "canonical_metric_name": "Revenue",
            "canonical_search_text": "Revenue | Exact selected revenue definition",
            "canonical_resolution_revision_id": "resolution-selected",
            "change_kind": "upsert",
            "metric_definition_revision_id": "definition-selected-2",
            "metric_definition_commitment_sha256": "b" * 64,
            "binding_revision_id": "binding-selected",
            "binding_commitment_sha256": "c" * 64,
            "selected_observation_id": observation.observation_id,
            "source_fact_cell_id": cell.fact_cell_id,
            "source_record_commitment_sha256": record_commitment(
                conn, "fact_observation", observation.observation_id
            ),
            "reporting_entity_id": cell.reporting_entity_id,
            "scope_security_id": cell.scope_security_id,
            "period_kind": cell.period_kind,
            "period_start": cell.period_start.isoformat() if cell.period_start else None,
            "period_end": cell.period_end.isoformat(),
            "dimensions_json": "[]",
            "unit_key": cell.unit_key,
            "currency": cell.currency,
            "canonical_value": str(observation.numeric_value),
            "value_kind": "numeric",
            "evidence_document_version_id": observation.document_version_id,
            "evidence_node_id": observation.evidence_node_id,
            "evidence_locator_json": canonical_json(observation.source_locator.root),
            "evidence_locator_sha256": observation.source_locator_sha256,
        }
        entry_json = canonical_json(payload)
        entry = {
            **payload,
            "generation_id": "generation",
            "entry_ordinal": ordinal,
            "entry_json": entry_json,
            "entry_sha256": digest_text(entry_json),
        }
        columns = tuple(entry)
        conn.execute(
            "INSERT INTO canonical_fact_projection_entries ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(entry[column] for column in columns),
        )
    conn.commit()
    conn.row_factory = sqlite3.Row
    candidate = conn.execute(
        "SELECT canonical_metric_cell_id AS candidate_id, entry_sha256 AS source_commitment_sha256 "
        "FROM canonical_fact_projection_entries ORDER BY entry_ordinal LIMIT 1"
    ).fetchone()
    receipt = HeterogeneousRetrievalReceipt(
        trace_id="trace",
        trace_sha256="a" * 64,
        research_snapshot_id="research",
        research_snapshot_sha256="a" * 64,
        fact_generation_id="generation",
        fact_projection_seal_sha256="a" * 64,
        candidate_count=1,
        result_count=1,
        ordered_results=(),
        recorded_at=FACT_STAMP,
    )
    try:
        yield conn, receipt, candidate
    finally:
        conn.close()


def _load_fact_items(
    conn: sqlite3.Connection,
    receipt: HeterogeneousRetrievalReceipt,
    candidates: Sequence[sqlite3.Row],
    *,
    cutoff: datetime,
    start_number: int = 1,
    results: tuple[dict[str, object], ...] | None = None,
) -> tuple[SealedEvidenceItem, ...]:
    """Exercise the production batch adapter with only trace admission doubled.

    TEMP trace and projection rows are preverified adapter inputs. Native source
    publications, batch admission, context checks and evidence DTOs are real.
    This does not prove full retrieval promotion or sealed Ask readiness.
    """
    conn.executescript(
        """
        CREATE TEMP TABLE heterogeneous_retrieval_trace_headers (
            trace_id TEXT, cutoff_at TEXT
        );
        CREATE TEMP TABLE heterogeneous_retrieval_trace_results (
            trace_id TEXT, result_ordinal INTEGER, candidate_ordinal INTEGER
        );
        CREATE TEMP TABLE heterogeneous_retrieval_trace_candidates (
            trace_id TEXT, candidate_ordinal INTEGER, candidate_kind TEXT,
            candidate_id TEXT, source_commitment_sha256 TEXT
        );
        """
    )
    ordered_results = results or tuple(
        {"result_ordinal": index, "final_score": str(len(candidates) - index)}
        for index in range(len(candidates))
    )
    trace_receipt = receipt.model_copy(
        update={
            "candidate_count": len(candidates),
            "result_count": len(candidates),
            "ordered_results": ordered_results,
        }
    )
    conn.execute(
        "INSERT INTO heterogeneous_retrieval_trace_headers VALUES (?,?)",
        (receipt.trace_id, cutoff.isoformat()),
    )
    for index, (candidate, result) in enumerate(zip(candidates, ordered_results, strict=True)):
        conn.execute(
            "INSERT INTO heterogeneous_retrieval_trace_results VALUES (?,?,?)",
            (receipt.trace_id, result["result_ordinal"], index),
        )
        conn.execute(
            "INSERT INTO heterogeneous_retrieval_trace_candidates VALUES (?,?,?,?,?)",
            (
                receipt.trace_id,
                index,
                "fact",
                candidate["candidate_id"],
                candidate["source_commitment_sha256"],
            ),
        )

    def preverified_trace(
        source_conn: sqlite3.Connection, requested_trace_id: str, **_kwargs: object
    ) -> HeterogeneousRetrievalReceipt:
        assert source_conn is conn
        assert requested_trace_id == receipt.trace_id
        return trace_receipt

    with pytest.MonkeyPatch.context() as trace_admission:
        trace_admission.setattr(sealed, "verify_heterogeneous_retrieval_trace", preverified_trace)
        return sealed.load_verified_trace_evidence(
            conn, receipt.trace_id, start_number=start_number
        )


def _load_fact_item(
    conn: sqlite3.Connection,
    receipt: HeterogeneousRetrievalReceipt,
    candidate: sqlite3.Row,
    result: dict[str, object],
    *,
    n: int,
    cutoff: datetime,
) -> SealedEvidenceItem:
    return _load_fact_items(
        conn, receipt, (candidate,), cutoff=cutoff, start_number=n, results=(result,)
    )[0]


@pytest.mark.parametrize(
    "published_fact_context", [_FactContextClocks(fact_count=2)], indirect=True
)
def test_fact_batch_keeps_rank_order_and_verifies_shared_publication_once(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, receipt, _ = published_fact_context
    # Rank differs from projection order. Both observations belong to the same
    # real immutable publication and share its complete verification.
    candidates = conn.execute(
        "SELECT canonical_metric_cell_id AS candidate_id, entry_sha256 AS source_commitment_sha256 "
        "FROM canonical_fact_projection_entries ORDER BY entry_ordinal DESC"
    ).fetchall()
    verifications: list[tuple[str, datetime]] = []

    def counted_verification(
        source_conn: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verifications.append((publication_id, cutoff))
        return verify_source_fact_publication(
            source_conn,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(fact_reader, "verify_source_fact_publication", counted_verification)
    items = _load_fact_items(conn, receipt, candidates, cutoff=FACT_STAMP, start_number=7)
    assert tuple(item.candidate_id for item in items) == (
        "canonical-revenue-batch",
        "canonical-revenue",
    )
    assert tuple(item.n for item in items) == (7, 8)
    assert tuple(item.result_ordinal for item in items) == (0, 1)
    assert tuple(item.value for item in items) == ("200 USD USD_millions", "100 USD USD_millions")
    contexts = tuple(
        json.loads(item.text.split("Verified financial context: ", 1)[1]) for item in items
    )
    assert tuple(context["selected_observation_id"] for context in contexts) == (
        "observation-batch",
        "observation-1",
    )
    assert verifications == [("publication-1", FACT_STAMP)]
    fragments, block = _evidence_block(items)
    assert len(fragments) == 2
    assert all(item.text in fragment for item, fragment in zip(items, fragments, strict=True))
    assert block == "\n\n".join(fragments)


@pytest.mark.parametrize(
    "published_fact_context", [_FactContextClocks(fact_count=2)], indirect=True
)
@pytest.mark.parametrize("fault", ["reordered", "missing_result", "missing_bundle"])
def test_fact_batch_rejects_malformed_owner_results_without_partial_evidence(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    conn, receipt, _ = published_fact_context
    candidates = conn.execute(
        "SELECT canonical_metric_cell_id AS candidate_id, entry_sha256 AS source_commitment_sha256 "
        "FROM canonical_fact_projection_entries ORDER BY entry_ordinal"
    ).fetchall()
    real_batch = FactReadModel.provenance_bundles

    def malformed_batch(
        reader: FactReadModel, observation_ids: tuple[str, ...], *, cutoff: datetime
    ) -> tuple[ProvenanceBundleRead, ...]:
        reads = real_batch(reader, observation_ids, cutoff=cutoff)
        assert len(reads) == 2
        assert all(read.bundle is not None and read.failure is None for read in reads)
        if fault == "reordered":
            return tuple(reversed(reads))
        if fault == "missing_result":
            return reads[:1]
        # The owner schema forbids this state. Check the adapter's defensive
        # boundary if a reader defect returns neither bundle nor failure.
        return (reads[0], reads[1].model_copy(update={"bundle": None}))

    monkeypatch.setattr(FactReadModel, "provenance_bundles", malformed_batch)
    with pytest.raises(PromotionVerificationError, match="selected fact context") as failure:
        _load_fact_items(conn, receipt, candidates, cutoff=FACT_STAMP)
    assert failure.value.reason_code == "trace_verification_failed"


def test_fact_batch_rejects_real_source_admission_failure(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
) -> None:
    conn, receipt, candidate = published_fact_context
    conn.execute(
        "CREATE TEMP TABLE source_fact_publication_members AS "
        "SELECT * FROM main.source_fact_publication_members WHERE 0"
    )
    reads = FactReadModel(conn).provenance_bundles(("observation-1",), cutoff=FACT_STAMP)
    assert reads[0].bundle is None
    assert reads[0].failure is not None
    assert reads[0].failure.reason_code == "record_not_in_sealed_publication"
    with pytest.raises(PromotionVerificationError, match="selected fact context") as failure:
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP,
        )
    assert failure.value.reason_code == "trace_verification_failed"


def test_fact_prompt_preserves_verified_financial_context(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
) -> None:
    conn, receipt, candidate = published_fact_context
    item = _load_fact_item(
        conn, receipt, candidate, {"result_ordinal": 0, "final_score": "1"}, n=1, cutoff=FACT_STAMP
    )
    assert item.text.splitlines()[0] == "Revenue: 100 USD USD_millions (2026-07-27)"
    assert item.value == "100 USD USD_millions"
    assert '"unit_key":"USD_millions"' in item.text
    assert '"currency":"USD"' in item.text
    assert '"accounting_basis":"us_gaap"' in item.text
    assert '"consolidation_scope":"consolidated"' in item.text
    assert '"period_kind":"duration"' in item.text
    assert '"period_start":"2025-07-27T12:00:00Z"' in item.text
    assert '"period_end":"2026-07-27T12:00:00Z"' in item.text
    assert '"fiscal_year":2026' in item.text
    assert '"fiscal_period":"FY"' in item.text
    assert '"precision":"3"' in item.text
    assert '"decimals":"-6"' in item.text
    assert '"observation_kind":"reported"' in item.text
    assert '"revision_kind":"initial"' in item.text
    assert '"metric_definition_revision_id":"definition-selected-2"' in item.text
    assert '"binding_revision_id":"binding-selected"' in item.text
    assert '"explicit_member_name":"CloudMember"' in item.text
    assert '"actual_guidance_consensus_status":"unavailable"' in item.text
    assert '"scale":"unavailable"' in item.text
    assert "Preserve this fact's explicit unit_key and its literal meaning" in item.text
    assert "does not erase scale expressed in the unit" in item.text
    assert "does not establish complete decision-grade financial context" in item.text
    assert "storage_uri" not in item.text
    fragments, block = _evidence_block((item,))
    assert item.text in fragments[0]
    assert block == fragments[0]


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("source_fact_cell_id", "wrong-cell"),
        ("evidence_node_id", "wrong-node"),
        ("evidence_locator_sha256", "f" * 64),
        ("reporting_entity_id", "wrong-entity"),
        ("scope_security_id", "wrong-security"),
        ("unit_key", "USD"),
        ("currency", "EUR"),
        ("period_start", "2024-07-27T12:00:00Z"),
        ("canonical_value", "200"),
        ("source_record_commitment_sha256", "f" * 64),
    ],
)
def test_fact_context_rejects_projection_source_mismatch(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
    column: str,
    replacement: str,
) -> None:
    conn, receipt, _ = published_fact_context
    row = conn.execute("SELECT * FROM canonical_fact_projection_entries").fetchone()
    payload = json.loads(str(row["entry_json"]))
    payload[column] = replacement
    entry_json = canonical_json(payload)
    # This unit adapter input has a fresh projection commitment. Its source
    # coordinates must still match the independently admitted publication.
    conn.execute(
        f"UPDATE canonical_fact_projection_entries SET {column}=?,entry_json=?,entry_sha256=?",
        (replacement, entry_json, digest_text(entry_json)),
    )
    candidate = conn.execute(
        "SELECT 'canonical-revenue' AS candidate_id, ? AS source_commitment_sha256",
        (digest_text(entry_json),),
    ).fetchone()
    with pytest.raises(PromotionVerificationError, match="selected fact context"):
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP,
        )


@pytest.mark.parametrize("column", ["metric_definition_revision_id", "binding_revision_id"])
def test_fact_context_rejects_revision_substitution_under_selected_commitment(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
    column: str,
) -> None:
    conn, receipt, candidate = published_fact_context
    conn.execute(f"UPDATE canonical_fact_projection_entries SET {column}='newer-unselected'")
    with pytest.raises(PromotionVerificationError, match="selected fact context"):
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP,
        )


def test_fact_context_rejects_source_unavailable_at_selected_cutoff(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
) -> None:
    conn, receipt, candidate = published_fact_context
    with pytest.raises(PromotionVerificationError, match="selected fact context"):
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP - timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "published_fact_context",
    [
        _FactContextClocks(
            recorded_at=FACT_STAMP + timedelta(hours=2),
            observed_through=FACT_STAMP + timedelta(hours=2),
        )
    ],
    indirect=True,
)
def test_selected_fact_context_preserves_distinct_knowledge_and_observation_clocks(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
) -> None:
    conn, receipt, candidate = published_fact_context
    observed = FACT_STAMP + timedelta(hours=2)
    owner = verify_source_fact_publication(
        conn, publication_id="publication-1", cutoff=FACT_STAMP, observed_through=observed
    )
    assert owner.cutoff == FACT_STAMP
    assert owner.recorded_at == observed
    item = _load_fact_item(
        conn, receipt, candidate, {"result_ordinal": 0, "final_score": "1"}, n=1, cutoff=FACT_STAMP
    )
    assert item.value == "100 USD USD_millions"
    context = json.loads(item.text.split("Verified financial context: ", 1)[1])
    assert context["knowledge_cutoff"] == FACT_STAMP.isoformat()
    assert context["observed_through"] == observed.isoformat()
    assert context["observation_knowledge_at"] == FACT_STAMP.isoformat()
    assert context["observation_recorded_at"] == observed.isoformat()
    assert "relations" not in context
    assert "derivation" not in context


@pytest.mark.parametrize(
    "published_fact_context",
    [
        _FactContextClocks(
            recorded_at=FACT_STAMP + timedelta(hours=2),
            observed_through=FACT_STAMP + timedelta(hours=2),
        )
    ],
    indirect=True,
)
def test_inherited_fact_context_uses_verified_head_clock_not_ancestor_or_query_time(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
) -> None:
    conn, receipt, candidate = published_fact_context
    head_observed = FACT_STAMP + timedelta(hours=3)
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations "
        "(generation_id,parent_generation_id,cutoff_at,recorded_at) VALUES (?,?,?,?)",
        ("head", "generation", FACT_STAMP.isoformat(), head_observed.isoformat()),
    )
    receipt = receipt.model_copy(
        update={"fact_generation_id": "head", "recorded_at": FACT_STAMP + timedelta(days=1)}
    )
    item = _load_fact_item(
        conn, receipt, candidate, {"result_ordinal": 0, "final_score": "1"}, n=1, cutoff=FACT_STAMP
    )
    context = json.loads(item.text.split("Verified financial context: ", 1)[1])
    assert context["observed_through"] == head_observed.isoformat()
    assert context["observation_recorded_at"] == (FACT_STAMP + timedelta(hours=2)).isoformat()
    assert context["knowledge_cutoff"] == FACT_STAMP.isoformat()


@pytest.mark.parametrize(
    "published_fact_context",
    [
        _FactContextClocks(
            knowledge_at=FACT_STAMP + timedelta(microseconds=1),
            recorded_at=FACT_STAMP + timedelta(hours=2),
            observed_through=FACT_STAMP + timedelta(hours=2),
        ),
        _FactContextClocks(
            recorded_at=FACT_STAMP + timedelta(hours=2, microseconds=1),
            observed_through=FACT_STAMP + timedelta(hours=2),
        ),
    ],
    ids=["knowledge_one_microsecond_after_K", "recorded_one_microsecond_after_O"],
    indirect=True,
)
def test_fact_context_rejects_source_just_outside_exact_projection_clocks(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
) -> None:
    conn, receipt, candidate = published_fact_context
    observed = FACT_STAMP + timedelta(hours=2)
    with pytest.raises(PublicationVerificationError):
        verify_source_fact_publication(
            conn, publication_id="publication-1", cutoff=FACT_STAMP, observed_through=observed
        )
    # A later query time cannot widen the immutable projection's observation bound.
    receipt = receipt.model_copy(update={"recorded_at": FACT_STAMP + timedelta(days=1)})
    with pytest.raises(PromotionVerificationError, match="selected fact context"):
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP,
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("cutoff_at", FACT_STAMP + timedelta(microseconds=1)),
        ("recorded_at", FACT_STAMP - timedelta(microseconds=1)),
    ],
    ids=["head_K_differs_from_trace_K", "head_O_precedes_K"],
)
def test_fact_context_rejects_invalid_head_clock_coordinates(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
    column: str,
    replacement: datetime,
) -> None:
    conn, receipt, candidate = published_fact_context
    conn.execute(
        f"UPDATE canonical_fact_projection_generations SET {column}=?",
        (replacement.isoformat(),),
    )
    with pytest.raises(PromotionVerificationError, match="selected fact context"):
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP,
        )


@pytest.mark.parametrize(
    ("published_fact_context", "future_field"),
    [
        (
            _FactContextClocks(
                cell_knowledge_at=FACT_STAMP + timedelta(microseconds=1),
                recorded_at=FACT_STAMP + timedelta(hours=2),
                observed_through=FACT_STAMP + timedelta(hours=2),
            ),
            "cell",
        ),
        (
            _FactContextClocks(
                observation_knowledge_at=FACT_STAMP + timedelta(microseconds=1),
                recorded_at=FACT_STAMP + timedelta(hours=2),
                observed_through=FACT_STAMP + timedelta(hours=2),
            ),
            "observation",
        ),
    ],
    ids=["only_cell_knowledge_after_K", "only_observation_knowledge_after_K"],
    indirect=["published_fact_context"],
)
def test_fact_context_checks_cell_and_observation_knowledge_independently(
    published_fact_context: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt, sqlite3.Row],
    future_field: str,
) -> None:
    conn, receipt, candidate = published_fact_context
    observed = FACT_STAMP + timedelta(hours=2)
    # The actual source writer and reader accept this graph at its own clocks.
    owner = verify_source_fact_publication(
        conn,
        publication_id="publication-1",
        cutoff=FACT_STAMP + timedelta(microseconds=1),
        observed_through=observed,
    )
    assert owner.created_at == FACT_STAMP + timedelta(microseconds=1)
    bundle = FactReadModel(conn).provenance_bundle("observation-1", cutoff=observed)
    assert (bundle.cell.knowledge_at > FACT_STAMP) == (future_field == "cell")
    assert (bundle.observation.knowledge_at > FACT_STAMP) == (future_field == "observation")
    with pytest.raises(PromotionVerificationError, match="selected fact context"):
        _load_fact_item(
            conn,
            receipt,
            candidate,
            {"result_ordinal": 0, "final_score": "1"},
            n=1,
            cutoff=FACT_STAMP,
        )
