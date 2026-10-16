"""Projection-mode control tests; financial/source closure remains separately verified."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import provenance.population_research_snapshots as population
from execution import populate_research_snapshots as cli
from provenance.population_completeness import PopulationTemporalScope, canonical_json, digest_text
from provenance.population_research_snapshots import (
    ProjectionMode,
    ResearchSnapshotPlanError,
    ResearchSnapshotPopulationRequest,
    select_retrieval_coordinates,
)
from provenance.research_snapshot import CorpusProjectionBundle, ResearchSnapshotRequest

CUTOFF = datetime(2026, 10, 1, tzinfo=UTC)
_population_input_commitment: Callable[..., str] = getattr(
    population, "_population_input_commitment"
)
_population_plan_commitment: Callable[..., str] = getattr(population, "_population_plan_commitment")


@pytest.fixture
def coordinates() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE search_projection_seals (manifest_id TEXT, index_kind TEXT, "
        "index_run_id TEXT, sealed_at TEXT);"
    )
    conn.execute(
        "INSERT INTO search_projection_seals VALUES (?,?,?,?)",
        ("corpus", "lexical", "lexical", CUTOFF.isoformat()),
    )
    yield conn
    conn.close()


def test_lexical_only_needs_no_vector_or_promotion_table(coordinates: sqlite3.Connection) -> None:
    assert select_retrieval_coordinates(
        coordinates, "corpus", CUTOFF, projection_mode="lexical_only"
    ) == ("lexical", None, None)


def test_default_semantic_does_not_fall_back(coordinates: sqlite3.Connection) -> None:
    with pytest.raises(ResearchSnapshotPlanError, match="vector_projection_seal"):
        select_retrieval_coordinates(coordinates, "corpus", CUTOFF)


def test_projection_mode_is_explicit_and_typed() -> None:
    request = ResearchSnapshotPopulationRequest(cutoff_at=CUTOFF, operation_recorded_at=CUTOFF)
    assert request.projection_mode == "semantic"
    with pytest.raises(ValidationError):
        ResearchSnapshotPopulationRequest.model_validate(
            {"cutoff_at": CUTOFF, "recorded_at": CUTOFF, "projection_mode": "automatic"}
        )


def test_lexical_only_still_requires_lexical_seal(coordinates: sqlite3.Connection) -> None:
    coordinates.execute("DELETE FROM search_projection_seals")
    with pytest.raises(ResearchSnapshotPlanError, match="lexical_projection_seal"):
        select_retrieval_coordinates(coordinates, "corpus", CUTOFF, projection_mode="lexical_only")


@pytest.fixture
def population_control(
    coordinates: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> sqlite3.Connection:
    """Isolate population identity/commitment control; do not certify financial fixtures."""
    coordinates.executescript(
        "CREATE TABLE research_snapshot_headers (research_snapshot_id TEXT, request_json TEXT, "
        "request_sha256 TEXT, cutoff_at TEXT, recorded_at TEXT);"
        "CREATE TABLE research_snapshot_seals (research_snapshot_id TEXT, member_set_sha256 TEXT, "
        "sealed_at TEXT);"
        "CREATE TABLE research_snapshot_universe_commitments (research_snapshot_id TEXT, "
        "issuer_id TEXT, canonical_universe_json TEXT, universe_sha256 TEXT, "
        "cutoff_at TEXT, recorded_at TEXT);"
    )

    def noop(*_args: object, **_kwargs: object) -> None:
        return None

    def issuer(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("issuer",)

    def processing(*_args: object, **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return "processing", ("document",)

    def coordinate(*_args: object, **_kwargs: object) -> str:
        return "corpus"

    def manifest(_conn: sqlite3.Connection, request: ResearchSnapshotRequest) -> dict[str, object]:
        return {"issuer_id": "issuer", "request": request.model_dump(mode="json")}

    def count(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(population, "_require_schema", noop)
    monkeypatch.setattr(population, "_issuer_ids", issuer)
    monkeypatch.setattr(population, "_processing_coordinate", processing)
    monkeypatch.setattr(population, "_reporting_entities", issuer)
    monkeypatch.setattr(population, "select_exact_corpus_coordinate", coordinate)
    monkeypatch.setattr(population, "_ontology_coordinate", coordinate)
    monkeypatch.setattr(population, "_resolution_coordinate", coordinate)
    monkeypatch.setattr(population, "_canonical_projection_coordinate", coordinate)
    monkeypatch.setattr(population, "_obligation_coordinates", issuer)
    monkeypatch.setattr(population, "_publication_coordinates", issuer)
    monkeypatch.setattr(population, "_request_input_manifest", manifest)
    monkeypatch.setattr(population, "_issuer_document_count", count)
    monkeypatch.setattr(population, "_output_commitment", coordinate)
    return coordinates


def _request(
    mode: ProjectionMode = "lexical_only",
    clock: population.SourcePublicationReferenceClock = "cutoff_v1",
) -> ResearchSnapshotPopulationRequest:
    return ResearchSnapshotPopulationRequest(
        cutoff_at=CUTOFF,
        operation_recorded_at=CUTOFF,
        issuer_ids=("issuer",),
        projection_mode=mode,
        source_publication_reference_clock=clock,
    )


def _store_control_terminal(conn: sqlite3.Connection, request: ResearchSnapshotRequest) -> None:
    """Persist control-plane fixtures only; the source verifier is not fabricated as real."""
    request_json = canonical_json(request)
    universe_json = canonical_json(request.research_universe)
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?)",
        (
            request.research_snapshot_id,
            request_json,
            digest_text(request_json),
            CUTOFF.isoformat(),
            CUTOFF.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO research_snapshot_seals VALUES (?,?,?)",
        (request.research_snapshot_id, "a" * 64, CUTOFF.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?)",
        (
            request.research_snapshot_id,
            "issuer",
            universe_json,
            digest_text(universe_json),
            CUTOFF.isoformat(),
            CUTOFF.isoformat(),
        ),
    )
    conn.commit()


def test_mode_switch_rejects_preview_pin_before_any_write(
    population_control: sqlite3.Connection,
) -> None:
    preview = population.populate_research_snapshots(population_control, _request())
    assert preview.ready_issuer_count == 1
    before = population_control.total_changes
    switched = _request("semantic").model_copy(
        update={
            "apply": True,
            "input_commitment_sha256": preview.input_commitment_sha256,
            "plan_commitment_sha256": preview.plan_commitment_sha256,
        }
    )
    with pytest.raises(ValueError, match="input commitment changed"):
        population.populate_research_snapshots(population_control, switched)
    assert population_control.total_changes == before


def test_modes_have_distinct_identity_preserving_semantic_id(
    population_control: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    lexical = population.assemble_research_snapshot_request(
        population_control, "issuer", CUTOFF, projection_mode="lexical_only"
    )

    def semantic(*_args: object, **_kwargs: object) -> tuple[str, str, str]:
        return "lexical", "vector", "promotion"

    monkeypatch.setattr(population, "select_retrieval_coordinates", semantic)
    default = population.assemble_research_snapshot_request(population_control, "issuer", CUTOFF)
    explicit = population.assemble_research_snapshot_request(
        population_control, "issuer", CUTOFF, projection_mode="semantic"
    )
    assert default == explicit
    legacy_payload = {
        "canonical_fact_projection_run_id": "corpus",
        "canonical_fact_resolution_snapshot_id": "corpus",
        "corpus_manifest_id": "corpus",
        "cutoff_at": "2026-10-01T00:00:00+00:00",
        "issuer_id": "issuer",
        "ontology_snapshot_id": "corpus",
        "processing_snapshot_id": "processing",
        "source_fact_publication_ids": ["issuer"],
    }
    assert default.research_snapshot_id == "research-snapshot:" + digest_text(
        canonical_json(legacy_payload)
    )
    assert lexical.research_snapshot_id != default.research_snapshot_id
    assert lexical.corpus_bundles[0].vector_index_run_id is None
    assert lexical.corpus_bundles[0].embedding_promotion_id is None


def test_other_mode_terminal_blocks_before_any_write(
    population_control: sqlite3.Connection,
) -> None:
    lexical = population.assemble_research_snapshot_request(
        population_control, "issuer", CUTOFF, projection_mode="lexical_only"
    )
    semantic = lexical.model_copy(
        update={
            "research_snapshot_id": "other-mode",
            "idempotency_key": "other-mode",
            "corpus_bundles": (
                CorpusProjectionBundle(
                    corpus_manifest_id="corpus",
                    lexical_index_run_id="lexical",
                    vector_index_run_id="vector",
                    embedding_promotion_id="promotion",
                ),
            ),
        }
    )
    _store_control_terminal(population_control, semantic)
    preview = population.populate_research_snapshots(population_control, _request())
    assert preview.statuses[0].blockers == ("research_snapshot_terminal_scope_conflict",)
    before = population_control.total_changes
    applied = population.populate_research_snapshots(
        population_control,
        _request().model_copy(
            update={
                "apply": True,
                "input_commitment_sha256": preview.input_commitment_sha256,
                "plan_commitment_sha256": preview.plan_commitment_sha256,
            }
        ),
    )
    assert applied.created_snapshot_count == 0
    assert applied.blocked_issuer_count == 1
    assert population_control.total_changes == before


@pytest.mark.parametrize("clock", ["cutoff_v1", "publication_created_v2"])
def test_lexical_replay_and_terminal_parity_call_public_source_verifier(
    population_control: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    clock: population.SourcePublicationReferenceClock,
) -> None:
    plan = population.assemble_research_snapshot_request(
        population_control,
        "issuer",
        CUTOFF,
        projection_mode="lexical_only",
        source_publication_reference_clock=clock,
    )
    _store_control_terminal(population_control, plan)
    verified: list[str] = []

    def verify(_conn: sqlite3.Connection, snapshot_id: str) -> SimpleNamespace:
        verified.append(snapshot_id)
        return SimpleNamespace(member_set_sha256="a" * 64)

    monkeypatch.setattr(population, "verify_research_snapshot", verify)
    preview = population.populate_research_snapshots(population_control, _request(clock=clock))
    before = population_control.total_changes
    applied = population.populate_research_snapshots(
        population_control,
        _request(clock=clock).model_copy(
            update={
                "apply": True,
                "input_commitment_sha256": preview.input_commitment_sha256,
                "plan_commitment_sha256": preview.plan_commitment_sha256,
            }
        ),
    )
    assert applied.created_snapshot_count == 0
    assert applied.ready_issuer_count == 1
    assert population_control.total_changes == before
    result = population.verify_research_snapshots(
        population_control,
        PopulationTemporalScope(
            knowledge_cutoff=CUTOFF,
            observed_through=CUTOFF,
        ),
    )
    assert result.materialized_count == 1
    assert verified == [plan.research_snapshot_id, plan.research_snapshot_id]

    def reject(_conn: sqlite3.Connection, _snapshot_id: str) -> None:
        raise ValueError("incomplete source closure")

    monkeypatch.setattr(population, "verify_research_snapshot", reject)
    with pytest.raises(ValueError, match="incomplete source closure"):
        population.verify_research_snapshots(
            population_control,
            PopulationTemporalScope(
                knowledge_cutoff=CUTOFF,
                observed_through=CUTOFF,
            ),
        )


def test_lexical_mode_keeps_corpus_completeness_blocker(
    population_control: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def incomplete(*_args: object, **_kwargs: object) -> str:
        raise ResearchSnapshotPlanError("exact_search_corpus_missing_or_ambiguous")

    monkeypatch.setattr(population, "select_exact_corpus_coordinate", incomplete)
    preview = population.populate_research_snapshots(population_control, _request())
    assert preview.ready_issuer_count == 0
    assert preview.statuses[0].blockers == ("exact_search_corpus_missing_or_ambiguous",)


@pytest.mark.parametrize("mode", ["semantic", "lexical_only"])
def test_cli_forwards_explicit_projection_mode(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def capture(args: argparse.Namespace) -> int:
        seen.append(str(args.projection_mode))
        return 0

    monkeypatch.setattr(cli, "_run", capture)
    assert (
        cli.main(
            [
                "--db",
                "disposable-fixture.db",
                "--cutoff-at",
                CUTOFF.isoformat(),
                "--recorded-at",
                CUTOFF.isoformat(),
                "--projection-mode",
                mode,
            ]
        )
        == 0
    )
    assert seen == [mode]


@pytest.mark.parametrize("clock", ["cutoff_v1", "publication_created_v2"])
def test_population_creates_once_then_verifies_replay(
    population_control: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    clock: population.SourcePublicationReferenceClock,
) -> None:
    built: list[str] = []
    verified: list[str] = []

    def build(conn: sqlite3.Connection, request: ResearchSnapshotRequest) -> SimpleNamespace:
        built.append(request.research_snapshot_id)
        _store_control_terminal(conn, request)
        return SimpleNamespace(member_set_sha256="a" * 64)

    def verify(_conn: sqlite3.Connection, snapshot_id: str) -> SimpleNamespace:
        verified.append(snapshot_id)
        return SimpleNamespace(member_set_sha256="a" * 64)

    monkeypatch.setattr(population, "build_research_snapshot", build)
    monkeypatch.setattr(population, "verify_research_snapshot", verify)
    preview = population.populate_research_snapshots(population_control, _request(clock=clock))
    apply = _request(clock=clock).model_copy(
        update={
            "apply": True,
            "input_commitment_sha256": preview.input_commitment_sha256,
            "plan_commitment_sha256": preview.plan_commitment_sha256,
        }
    )
    first = population.populate_research_snapshots(population_control, apply)
    changes = population_control.total_changes
    replay = population.populate_research_snapshots(population_control, apply)
    assert first.created_snapshot_count == 1
    assert replay.created_snapshot_count == 0
    assert replay.ready_issuer_count == 1
    assert built == verified
    assert len(built) == 1
    assert population_control.total_changes == changes


@pytest.mark.parametrize("clock", [None, "cutoff_v1", "publication_created_v2"])
def test_cli_forwards_clock_to_actual_population(
    population_control: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    clock: str | None,
) -> None:
    captured: list[ResearchSnapshotPopulationRequest] = []
    owner = cli.populate_research_snapshots

    def populate(
        conn: sqlite3.Connection, request: ResearchSnapshotPopulationRequest
    ) -> population.ResearchSnapshotPopulationResult:
        captured.append(request)
        return owner(conn, request)

    def connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        return population_control

    monkeypatch.setattr(cli, "connect_sqlite", connect)
    monkeypatch.setattr(cli, "populate_research_snapshots", populate)
    args = [
        "--db",
        "disposable-fixture.db",
        "--cutoff-at",
        CUTOFF.isoformat(),
        "--recorded-at",
        CUTOFF.isoformat(),
        "--projection-mode",
        "lexical_only",
    ]
    if clock is not None:
        args.extend(["--source-publication-reference-clock", clock])
    assert cli.main(args) == 0
    assert len(captured) == 1
    assert captured[0].source_publication_reference_clock == (clock or "cutoff_v1")


def test_cli_rejects_unknown_clock_without_opening_db(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def connect(*_args: object, **_kwargs: object) -> None:
        calls.append("open")

    monkeypatch.setattr(cli, "connect_sqlite", connect)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "--db",
                "disposable-fixture.db",
                "--cutoff-at",
                CUTOFF.isoformat(),
                "--recorded-at",
                CUTOFF.isoformat(),
                "--source-publication-reference-clock",
                "unknown",
            ]
        )
    assert exc.value.code == 2 and calls == []


def test_unknown_copied_clock_fails_before_any_db_statement() -> None:
    request = _request().model_copy(update={"source_publication_reference_clock": "unknown"})
    with sqlite3.connect(":memory:") as conn:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        original = conn.row_factory
        with pytest.raises(ValueError, match="unknown source publication reference clock"):
            population.populate_research_snapshots(conn, request)
        with pytest.raises(ValueError, match="unknown source publication reference clock"):
            population.assemble_research_snapshot_request(
                conn,
                "issuer",
                CUTOFF,
                source_publication_reference_clock=request.source_publication_reference_clock,
            )
        assert statements == [] and conn.row_factory is original
    conn.close()


def test_clock_mode_changes_identity_and_preserves_exact_v1_dump(
    population_control: sqlite3.Connection,
) -> None:
    default = population.assemble_research_snapshot_request(
        population_control, "issuer", CUTOFF, projection_mode="lexical_only"
    )
    explicit = population.assemble_research_snapshot_request(
        population_control,
        "issuer",
        CUTOFF,
        projection_mode="lexical_only",
        source_publication_reference_clock="cutoff_v1",
    )
    revised = population.assemble_research_snapshot_request(
        population_control,
        "issuer",
        CUTOFF,
        projection_mode="lexical_only",
        source_publication_reference_clock="publication_created_v2",
    )
    assert default == explicit
    assert canonical_json(default) == canonical_json(explicit)
    assert "source_publication_reference_clock" not in default.model_dump(mode="json")
    assert revised.research_snapshot_id != default.research_snapshot_id
    assert (
        revised.model_dump(mode="json")["source_publication_reference_clock"]
        == "publication_created_v2"
    )


def test_v1_input_and_plan_digests_preserved_and_v2_bound() -> None:
    legacy_input = {
        "cutoff_at": CUTOFF.isoformat(),
        "projection_mode": "lexical_only",
        "expected_issuer_ids": ["issuer"],
        "selected_issuer_ids": ["issuer"],
        "issuer_inputs": [],
    }
    expected_input = digest_text(canonical_json(legacy_input))
    assert (
        _population_input_commitment(
            CUTOFF, ("issuer",), ("issuer",), [], projection_mode="lexical_only"
        )
        == expected_input
    )
    expected_plan = digest_text(
        canonical_json(
            {
                "cutoff_at": CUTOFF.isoformat(),
                "input_commitment_sha256": expected_input,
                "projection_mode": "lexical_only",
                "operation_recorded_at": CUTOFF.isoformat(),
                "selected_issuer_ids": ["issuer"],
            }
        )
    )
    assert (
        _population_plan_commitment(
            _request(), input_commitment=expected_input, selected_issuer_ids=("issuer",)
        )
        == expected_plan
    )
    assert (
        _population_input_commitment(
            CUTOFF,
            ("issuer",),
            ("issuer",),
            [],
            projection_mode="lexical_only",
            source_publication_reference_clock="publication_created_v2",
        )
        != expected_input
    )
    assert (
        _population_plan_commitment(
            _request(clock="publication_created_v2"),
            input_commitment=expected_input,
            selected_issuer_ids=("issuer",),
        )
        != expected_plan
    )


def test_clock_switch_cannot_reuse_subset_preview_pins(
    population_control: sqlite3.Connection,
) -> None:
    preview = population.populate_research_snapshots(population_control, _request())
    before = population_control.total_changes
    apply = _request(clock="publication_created_v2").model_copy(
        update={
            "apply": True,
            "input_commitment_sha256": preview.input_commitment_sha256,
            "plan_commitment_sha256": preview.plan_commitment_sha256,
        }
    )
    with pytest.raises(ValueError, match="input commitment changed"):
        population.populate_research_snapshots(population_control, apply)
    assert population_control.total_changes == before


def test_v2_does_not_adopt_existing_v1_terminal(population_control: sqlite3.Connection) -> None:
    legacy = population.assemble_research_snapshot_request(
        population_control, "issuer", CUTOFF, projection_mode="lexical_only"
    )
    _store_control_terminal(population_control, legacy)
    before = population_control.total_changes
    result = population.populate_research_snapshots(
        population_control, _request(clock="publication_created_v2")
    )
    assert result.statuses[0].blockers == ("research_snapshot_terminal_scope_conflict",)
    assert population_control.total_changes == before
