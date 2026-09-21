from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import provenance.fact_read_model as fact_read_model
from provenance.fact_plane_v2 import (
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    FactCellV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.source_fact_publication import VerifiedSourceFactPublication
from provenance.source_fact_repository import (
    DerivedSourceFact,
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from tests import test_source_fact_repository as foundation

STAMP = foundation.STAMP
sha256 = foundation.sha256
make_cell = foundation.make_cell
make_report = foundation.make_report
make_resolution = foundation.make_resolution
make_publication = foundation.make_publication


@pytest.fixture
def conn(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> Generator[sqlite3.Connection, None, None]:
    database = sqlite3.connect(migrated_db(tmp_path / "fact-read-model-batch.db"))
    database.execute("PRAGMA foreign_keys = ON")
    foundation.seed_foundation(database)
    database.commit()
    try:
        yield database
    finally:
        database.close()


def _two_observation_publication() -> SourceFactPublication:
    first = make_publication()
    second_cell = make_cell("second", period_end=STAMP - timedelta(days=365))
    second = make_report(second_cell, "second")
    return first.model_copy(
        update={
            "reported_facts": (
                first.reported_facts[0],
                ReportedSourceFact(cell=second_cell, observation=second),
            ),
            "resolutions": (
                first.resolutions[0],
                make_resolution(second_cell, second, "second"),
            ),
        }
    )


def _large_publication(count: int) -> SourceFactPublication:
    publication = make_publication()
    facts = [publication.reported_facts[0]]
    resolutions = [publication.resolutions[0]]
    for ordinal in range(1, count):
        cell = make_cell(
            f"large-{ordinal}",
            period_end=STAMP - timedelta(days=365 * ordinal),
        )
        observation = make_report(cell, f"large-{ordinal}")
        facts.append(ReportedSourceFact(cell=cell, observation=observation))
        resolutions.append(make_resolution(cell, observation, f"large-{ordinal}"))
    return publication.model_copy(
        update={"reported_facts": tuple(facts), "resolutions": tuple(resolutions)}
    )


def _derived_publication(source_observation_id: str) -> SourceFactPublication:
    cell = make_cell("independent", period_end=STAMP - timedelta(days=730))
    cell = FactCellV2.model_validate(
        {
            **cell.model_dump(),
            "concept_name": "IndependentDerivedRevenue",
            "semantic_key_sha256": None,
        }
    )
    observation = DerivedFactObservationV2(
        observation_id="observation-independent",
        idempotency_key="observation-key-independent",
        fact_cell_id=cell.fact_cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value="200",
        method_name="formula-engine",
        method_version="v1",
        method_config_sha256=sha256("formula-method-independent"),
        revision_kind="initial",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        formula_id="independent",
        formula_version="v1",
    )
    edge = DerivationInputV2(
        edge_id="edge-independent",
        idempotency_key="edge-key-independent",
        derived_observation_id=observation.observation_id,
        input_position=0,
        input_observation_id=source_observation_id,
        input_role="base",
        recorded_at=STAMP,
    )
    derivation = DerivationSealV2(
        derivation_seal_id="derivation-seal-independent",
        idempotency_key="derivation-seal-key-independent",
        derived_observation_id=observation.observation_id,
        ordered_inputs=(edge,),
        input_basis="as_reported",
        formula_definition_sha256=sha256("formula-definition-independent"),
        formula_config_sha256=sha256("formula-config-independent"),
        seal_method="canonical-json",
        seal_method_version="v1",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    return SourceFactPublication(
        publication_id="publication-independent",
        idempotency_key="publication-key-independent",
        derived_facts=(DerivedSourceFact(cell=cell, observation=observation),),
        derivations=(derivation,),
    )


def test_batch_reuses_full_verification_and_matches_single_reads(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    observation_ids = tuple(item.observation.observation_id for item in publication.reported_facts)
    verified: list[tuple[str, datetime]] = []
    original = fact_read_model.verify_source_fact_publication

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verified.append((publication_id, cutoff))
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", counted)
    results = FactReadModel(conn).provenance_bundles(observation_ids, cutoff=STAMP)

    assert [item.observation_id for item in results] == list(observation_ids)
    assert all(item.failure is None and item.bundle is not None for item in results)
    assert verified == [(publication.publication_id, STAMP)]
    assert tuple(item.bundle for item in results) == tuple(
        FactReadModel(conn).provenance_bundle(observation_id, cutoff=STAMP)
        for observation_id in observation_ids
    )


def test_large_publication_is_fully_verified_once_per_batch(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _large_publication(24)
    SourceFactRepository(conn).publish(publication)
    verified: list[str] = []
    original = fact_read_model.verify_source_fact_publication

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verified.append(publication_id)
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", counted)
    results = FactReadModel(conn).provenance_bundles(
        tuple(item.observation.observation_id for item in publication.reported_facts),
        cutoff=STAMP,
    )

    assert len(results) == 24
    assert all(item.bundle is not None for item in results)
    assert verified == [publication.publication_id]


def test_batch_reports_a_future_observation_as_unavailable(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    observation_id = publication.reported_facts[0].observation.observation_id

    result = FactReadModel(conn).provenance_bundles(
        (observation_id,), cutoff=STAMP - timedelta(microseconds=1)
    )

    assert result[0].bundle is None
    assert result[0].failure is not None
    assert result[0].failure.reason_code == "observation_unavailable_at_cutoff"
    assert result[0].failure.disposition == "missing_provenance"


def test_batch_quarantines_each_request_from_a_tampered_publication(
    conn: sqlite3.Connection,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members SET record_commitment_sha256 = ? "
        "WHERE publication_id = ? AND record_kind = 'extraction_seal'",
        (sha256("tampered"), publication.publication_id),
    )
    results = FactReadModel(conn).provenance_bundles(
        tuple(item.observation.observation_id for item in publication.reported_facts),
        cutoff=STAMP,
    )

    assert all(item.bundle is None for item in results)
    assert [item.failure.reason_code if item.failure else None for item in results] == [
        "publication_member_tampered",
        "publication_member_tampered",
    ]
    assert all(item.failure and item.failure.disposition == "quarantined" for item in results)


def test_batch_preserves_valid_publications_and_revalidates_each_call(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupt = _two_observation_publication()
    valid = _derived_publication(corrupt.reported_facts[0].observation.observation_id)
    repository = SourceFactRepository(conn)
    repository.publish(corrupt)
    repository.publish(valid)
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members SET record_commitment_sha256 = ? "
        "WHERE publication_id = ? AND record_kind = 'extraction_seal'",
        (sha256("tampered"), corrupt.publication_id),
    )
    conn.commit()
    verified: list[str] = []
    original = fact_read_model.verify_source_fact_publication

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verified.append(publication_id)
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", counted)
    first = FactReadModel(conn).provenance_bundles(
        (
            corrupt.reported_facts[0].observation.observation_id,
            valid.derived_facts[0].observation.observation_id,
        ),
        cutoff=STAMP,
    )
    second = FactReadModel(conn).provenance_bundles(
        (valid.derived_facts[0].observation.observation_id,), cutoff=STAMP
    )

    assert first[0].failure is not None
    assert first[1].bundle is not None
    assert second[0].bundle is not None
    assert verified.count(corrupt.publication_id) == 1
    assert verified.count(valid.publication_id) == 2


def test_batch_revalidates_after_a_committed_publication_mutation(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    verified: list[str] = []
    original = fact_read_model.verify_source_fact_publication

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verified.append(publication_id)
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", counted)
    observation_id = publication.reported_facts[0].observation.observation_id
    assert FactReadModel(conn).provenance_bundles((observation_id,), cutoff=STAMP)[0].bundle
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members SET record_commitment_sha256 = ? "
        "WHERE publication_id = ? AND record_kind = 'extraction_seal'",
        (sha256("tampered-after-read"), publication.publication_id),
    )
    conn.commit()
    after_mutation = FactReadModel(conn).provenance_bundles((observation_id,), cutoff=STAMP)

    assert after_mutation[0].failure is not None
    assert after_mutation[0].failure.reason_code == "publication_member_tampered"
    assert verified == [publication.publication_id, publication.publication_id]


def test_batch_preserves_caller_transaction_and_row_factory(
    conn: sqlite3.Connection,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    results = FactReadModel(conn).provenance_bundles(
        (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
    )

    assert results[0].bundle is not None
    assert conn.in_transaction
    assert conn.row_factory is sqlite3.Row
    conn.rollback()


def test_batch_rejects_commit_and_rebegin_that_replaces_its_snapshot(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    original = fact_read_model.verify_source_fact_publication

    def replace_snapshot(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verified = original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )
        connection.commit()
        connection.execute("BEGIN")
        return verified

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", replace_snapshot)
    with pytest.raises(RuntimeError, match="changed transaction state"):
        FactReadModel(conn).provenance_bundles(
            (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
        )

    assert not conn.in_transaction


def test_batch_rejects_an_internal_write(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    conn.execute("CREATE TEMP TABLE caller_write_guard (value INTEGER)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_write_guard VALUES (1)")
    original = fact_read_model.verify_source_fact_publication

    def writes_during_verification(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        verified = original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )
        connection.execute("CREATE TEMP TABLE batch_write_guard (value INTEGER)")
        connection.execute("INSERT INTO batch_write_guard VALUES (1)")
        return verified

    monkeypatch.setattr(
        fact_read_model,
        "verify_source_fact_publication",
        writes_during_verification,
    )
    with pytest.raises(RuntimeError, match="changed database state"):
        FactReadModel(conn).provenance_bundles(
            (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
        )

    assert conn.in_transaction
    assert conn.execute("SELECT value FROM caller_write_guard").fetchall() == [(1,)]
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type = 'table' AND name = 'batch_write_guard'"
        ).fetchone()
        is None
    )
    conn.rollback()


def test_batch_keeps_one_wal_snapshot_across_a_concurrent_commit(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    database_row = conn.execute("PRAGMA database_list").fetchone()
    assert database_row is not None
    database_path = str(database_row[2])
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer = sqlite3.connect(database_path, timeout=1)
    original = fact_read_model.verify_source_fact_publication
    committed = False

    def commit_tamper_after_first_verify(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        nonlocal committed
        verified = original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )
        if not committed:
            writer.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
            writer.execute(
                "UPDATE source_fact_publication_members SET record_commitment_sha256 = ? "
                "WHERE publication_id = ? AND record_kind = 'extraction_seal'",
                (sha256("concurrent-tamper"), publication.publication_id),
            )
            writer.commit()
            committed = True
        return verified

    monkeypatch.setattr(
        fact_read_model,
        "verify_source_fact_publication",
        commit_tamper_after_first_verify,
    )
    try:
        first = FactReadModel(conn).provenance_bundles(
            tuple(item.observation.observation_id for item in publication.reported_facts),
            cutoff=STAMP,
        )
    finally:
        writer.close()

    assert all(item.bundle is not None for item in first)
    after_commit = FactReadModel(conn).provenance_bundles(
        (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
    )
    assert after_commit[0].failure is not None
    assert after_commit[0].failure.reason_code == "publication_member_tampered"
