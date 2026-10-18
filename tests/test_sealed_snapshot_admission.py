"""Later admission cannot rewrite the selection of a retained native seal."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command, op
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
    OntologySnapshot,
    canonical_json,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from tests.test_filing_xbrl_extraction_ledger import (
    STAMP as NOW,
)
from tests.test_filing_xbrl_extraction_ledger import (
    filing_xbrl_entry,
    filing_xbrl_ledger_database,
    filing_xbrl_output,
)

T1 = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",))


def _metric() -> CanonicalMetric:
    return CanonicalMetric(
        metric_id="revenue",
        idempotency_key="metric:revenue",
        canonical_name="Revenue",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )


def _definition(revision: int = 1, *, at: datetime = T1) -> CanonicalMetricDefinitionRevision:
    return CanonicalMetricDefinitionRevision(
        metric_definition_revision_id=f"definition:{revision}",
        idempotency_key=f"definition:{revision}",
        metric_id="revenue",
        revision=revision,
        supersedes_metric_definition_revision_id=None
        if revision == 1
        else f"definition:{revision - 1}",
        lifecycle="active",
        definition_text=f"Reported revenue definition {revision}",
        aliases=(),
        value_kind="numeric",
        period_kind="duration",
        unit_family="currency",
        accounting_basis="us_gaap",
        scope_constraints={},
        effective_at=at,
        knowledge_at=at,
        recorded_at=at,
    )


def _bind_native_source(conn: sqlite3.Connection) -> str:
    for phase in ("registry", "assertions", "bindings"):
        result = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest.model_validate(
                {
                    "knowledge_cutoff": NOW,
                    "operation_recorded_at": NOW,
                    "apply": True,
                    "phase": phase,
                }
            ),
        )
        assert result.outcome == "applied"
    assert not result.reason_codes
    row = conn.execute(
        "SELECT canonical_metric_cell_id FROM fact_cell_canonical_binding_revisions "
        "WHERE binding_status='bound'"
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def ontology_db(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[sqlite3.Connection, MetricOntology]]:
    path = migrated_db(tmp_path / "ontology.db")
    conn = sqlite3.connect(path)
    repository = MetricOntology(conn)
    repository.persist_metric(_metric())
    repository.persist_metric_definition(_definition())
    repository.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="retained-ontology",
            idempotency_key="retained-ontology",
            cutoff_at=T1,
            recorded_at=T1,
        )
    )
    conn.commit()
    try:
        yield conn, repository
    finally:
        conn.close()


@pytest.mark.parametrize("late_microseconds", [0, 1, 999_999])
def test_backdated_definition_is_blocked_without_invalidating_seal(
    ontology_db: tuple[sqlite3.Connection, MetricOntology],
    late_microseconds: int,
) -> None:
    conn, repository = ontology_db
    before = tuple(conn.execute("SELECT * FROM ontology_snapshot_seals").fetchone())
    with pytest.raises(sqlite3.IntegrityError, match="sealed_ontology_admission_conflict"):
        repository.persist_metric_definition(
            _definition(2, at=T1 + timedelta(microseconds=late_microseconds))
        )
    repository.verify_snapshot("retained-ontology")
    assert tuple(conn.execute("SELECT * FROM ontology_snapshot_seals").fetchone()) == before


def test_exact_replay_and_later_definition_keep_old_seal(
    ontology_db: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    _, repository = ontology_db
    repository.persist_metric(_metric())
    repository.persist_metric_definition(_definition())
    with pytest.raises(ValueError, match="idempotency conflict"):
        repository.persist_metric_definition(
            _definition().model_copy(update={"definition_text": "Changed wording"})
        )
    repository.persist_metric_definition(_definition(2, at=T1 + timedelta(seconds=1)))
    repository.verify_snapshot("retained-ontology")


def test_direct_bulk_insert_cannot_bypass_ontology_fence(
    ontology_db: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, repository = ontology_db
    row = dict(conn.execute("SELECT * FROM canonical_metrics").fetchone())
    columns = tuple(row)
    row.update(metric_id="new-metric", idempotency_key="new-metric")
    # A new metric has no committed payload; use the owner to prepare its real row.
    fresh = _metric().model_copy(
        update={"metric_id": "new-metric", "idempotency_key": "new-metric"}
    )
    payload = fresh.commitment_payload
    row.update(commitment_json=canonical_json(payload))
    row.update(commitment_sha256=hashlib.sha256(str(row["commitment_json"]).encode()).hexdigest())
    with pytest.raises(sqlite3.IntegrityError, match="sealed_ontology_admission_conflict"):
        conn.executemany(
            f"INSERT INTO canonical_metrics ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",  # nosec B608 -- fixed fixture schema
            [tuple(row[column] for column in columns)],
        )
    repository.verify_snapshot("retained-ontology")


def test_every_expected_ontology_member_producer_has_a_database_guard(
    ontology_db: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, _ = ontology_db
    view = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='v_ontology_snapshot_expected_members'"
        ).fetchone()[0]
    )
    producers = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)", view, flags=re.IGNORECASE))
    producers.remove("ontology_snapshot_headers")
    guarded = {
        str(row[0])
        for row in conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE '%_sealed_admission'"
        )
    }
    assert producers <= guarded


@pytest.fixture
def native_db(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine]]:
    output = filing_xbrl_output((filing_xbrl_entry(0),))
    conn = filing_xbrl_ledger_database(tmp_path, output, migrated_db)
    conn.row_factory = sqlite3.Row
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell = _bind_native_source(conn)
        yield conn, cell, CanonicalFactResolutionEngine(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("late_microseconds", [0, 1, 999_999])
def test_backdated_resolution_is_blocked_and_later_resolution_is_allowed(
    native_db: tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine],
    late_microseconds: int,
) -> None:
    conn, cell, engine = native_db
    policy = ResolutionPolicy(name="deterministic", version="v1", config={})
    first = engine.resolve(cell, NOW, policy, recorded_at=NOW)
    engine.seal_snapshot("retained-resolution", NOW, NOW, SCOPE)
    before = engine.verify_snapshot("retained-resolution", NOW)
    assert engine.resolve(cell, NOW, policy, recorded_at=NOW).exact_replay
    counts = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608 -- fixed fixture tables
        for table in (
            "canonical_fact_candidate_universe_revisions",
            "canonical_fact_relation_set_revisions",
            "canonical_fact_resolution_revisions",
        )
    )
    revised = ResolutionPolicy(name="deterministic", version="v2", config={"review": 2})
    with pytest.raises(sqlite3.IntegrityError, match="sealed_resolution_admission_conflict"):
        engine.resolve(
            cell, NOW, revised, recorded_at=NOW + timedelta(microseconds=late_microseconds)
        )
    assert counts == tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608 -- fixed fixture tables
        for table in (
            "canonical_fact_candidate_universe_revisions",
            "canonical_fact_relation_set_revisions",
            "canonical_fact_resolution_revisions",
        )
    )
    assert engine.verify_snapshot("retained-resolution", NOW) == before
    later = NOW + timedelta(seconds=1)
    second = engine.resolve(cell, NOW, revised, recorded_at=later)
    assert second.canonical_resolution_revision_id != first.canonical_resolution_revision_id
    engine.seal_snapshot("later-resolution", NOW, later, SCOPE)
    engine.verify_snapshot("retained-resolution", NOW)
    engine.verify_snapshot("later-resolution", NOW, observed_through=later)


def test_backdated_binding_retirement_cannot_change_retained_ontology(
    native_db: tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine],
) -> None:
    conn, cell, _ = native_db
    repository = MetricOntology(conn)
    repository.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="native-ontology",
            idempotency_key="native-ontology",
            cutoff_at=NOW,
            recorded_at=NOW,
        )
    )
    assert _bind_native_source(conn) == cell
    prior = dict(conn.execute("SELECT * FROM fact_cell_canonical_binding_revisions").fetchone())
    prior.pop("commitment_json")
    prior.pop("commitment_sha256")
    prior.pop("reason_details_sha256")
    prior.pop("reason_details_json")
    prior.update(
        binding_revision_id="retired-later",
        idempotency_key="retired-later",
        revision=2,
        supersedes_binding_revision_id=prior["binding_revision_id"],
        binding_status="retired",
    )
    with pytest.raises(sqlite3.IntegrityError, match="sealed_ontology_admission_conflict"):
        repository.persist_binding(BindingRevision.model_validate(prior))
    repository.verify_snapshot("native-ontology")


def test_resolution_fence_uses_reporting_scope_and_each_seal_observation_clock(
    native_db: tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine],
) -> None:
    conn, cell, engine = native_db
    policy = ResolutionPolicy(name="deterministic", version="v1", config={})
    engine.resolve(cell, NOW, policy, recorded_at=NOW)
    engine.seal_snapshot("first-issuer", NOW, NOW, SCOPE)
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-2", "issuer-2", "operating_company", NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        ("reporting-2", "reporting-2", "issuer-2", "legal_registrant", "Second", NOW.isoformat()),
    )
    row = dict(conn.execute("SELECT * FROM canonical_metric_cells").fetchone())
    row.pop("dimension_count")
    row.update(
        canonical_metric_cell_id="other-cell",
        idempotency_key="other-cell",
        reporting_entity_id="reporting-2",
    )
    MetricOntology(conn).persist_canonical_metric_cell(CanonicalMetricCell.model_validate(row))
    # An unrelated issuer can still acquire its first resolution at these clocks.
    engine.resolve("other-cell", NOW, policy, recorded_at=NOW)
    other_scope = ResolutionSnapshotScope(
        issuer_id="issuer-2", reporting_entity_ids=("reporting-2",)
    )
    later = NOW + timedelta(seconds=2)
    engine.seal_snapshot("second-issuer", NOW, later, other_scope)
    revised = ResolutionPolicy(name="deterministic", version="v2", config={})
    # O is later than K here. Protect the retained O, not only the K instant.
    with pytest.raises(sqlite3.IntegrityError, match="sealed_resolution_admission_conflict"):
        engine.resolve("other-cell", NOW, revised, recorded_at=NOW + timedelta(seconds=1))
    engine.verify_snapshot("first-issuer", NOW)
    engine.verify_snapshot("second-issuer", NOW, observed_through=later)


def test_direct_resolution_insert_cannot_bypass_the_fence(
    native_db: tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine],
) -> None:
    conn, cell, engine = native_db
    engine.resolve(
        cell, NOW, ResolutionPolicy(name="deterministic", version="v1", config={}), recorded_at=NOW
    )
    engine.seal_snapshot("retained-resolution", NOW, NOW, SCOPE)
    row = dict(conn.execute("SELECT * FROM canonical_fact_resolution_revisions").fetchone())
    row.update(
        canonical_resolution_revision_id="direct-revision",
        idempotency_key="direct-revision",
        revision=2,
        supersedes_resolution_revision_id=row["canonical_resolution_revision_id"],
    )
    with pytest.raises(sqlite3.IntegrityError, match="sealed_resolution_admission_conflict"):
        conn.execute(
            f"INSERT INTO canonical_fact_resolution_revisions ({','.join(row)}) "  # nosec B608 -- fixed fixture schema
            f"VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    engine.verify_snapshot("retained-resolution", NOW)


def test_migration_keeps_preexisting_rows_and_seals(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    before: list[tuple[object, ...]] = []

    def retain_old_seal(path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            repository = MetricOntology(conn)
            repository.persist_metric(_metric())
            repository.persist_metric_definition(_definition())
            repository.seal_snapshot(
                OntologySnapshot(
                    ontology_snapshot_id="old-seal",
                    idempotency_key="old-seal",
                    cutoff_at=T1,
                    recorded_at=T1,
                )
            )
            conn.commit()
            before.extend(
                tuple(row) for row in conn.execute("SELECT * FROM ontology_snapshot_seals")
            )
        finally:
            conn.close()

    path = migrated_db(
        tmp_path / "upgraded.db",
        upgrade_from="0057_companyfacts_extractor_identity",
        before_upgrade=retain_old_seal,
    )
    conn = sqlite3.connect(path)
    try:
        repository = MetricOntology(conn)
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM ontology_snapshot_seals")
        ] == before
        repository.verify_snapshot("old-seal")
        repository.persist_metric_definition(_definition())
        with pytest.raises(sqlite3.IntegrityError, match="sealed_ontology_admission_conflict"):
            repository.persist_metric_definition(_definition(2))
    finally:
        conn.close()


def test_seal_and_admission_are_serialized_by_the_database(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "serialized.db")
    first = sqlite3.connect(path)
    second = sqlite3.connect(path, timeout=0)
    contender = MetricOntology(second)
    blocked: list[bool] = []
    metric = _metric().model_copy(update={"metric_id": "contender", "idempotency_key": "contender"})

    class ContendedOntology(MetricOntology):
        def _expected_snapshot_members(
            self, snapshot: OntologySnapshot
        ) -> Iterator[tuple[str, str, str]]:
            # This callback runs after the seal header acquires the write lock,
            # before selection. A second connection cannot append in between.
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.persist_metric(metric)
            blocked.append(True)
            yield from super()._expected_snapshot_members(snapshot)

    try:
        repository = ContendedOntology(first)
        repository.persist_metric(_metric())
        repository.persist_metric_definition(_definition())
        first.commit()
        repository.seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="serialized",
                idempotency_key="serialized",
                cutoff_at=T1,
                recorded_at=T1,
            )
        )
        assert blocked == [True]
        with pytest.raises(sqlite3.IntegrityError, match="sealed_ontology_admission_conflict"):
            contender.persist_metric(metric)
        repository.verify_snapshot("serialized")
    finally:
        second.close()
        first.close()


def test_resolution_selection_holds_the_writer_lock_until_sealed(
    native_db: tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine],
) -> None:
    conn, cell, engine = native_db
    engine.resolve(
        cell, NOW, ResolutionPolicy(name="deterministic", version="v1", config={}), recorded_at=NOW
    )
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    other = sqlite3.connect(path, timeout=0)
    contender = CanonicalFactResolutionEngine(other)
    revised = ResolutionPolicy(name="deterministic", version="v2", config={})
    blocked: list[bool] = []

    class ContendedResolution(CanonicalFactResolutionEngine):
        def _latest_resolution_members(
            self,
            cutoff: datetime,
            scope: ResolutionSnapshotScope,
            *,
            recorded_cutoff: datetime | None = None,
        ) -> list[dict[str, object]]:
            members = super()._latest_resolution_members(
                cutoff, scope, recorded_cutoff=recorded_cutoff
            )
            if not blocked:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.resolve(cell, NOW, revised, recorded_at=NOW)
                blocked.append(True)
            return members

    try:
        repository = ContendedResolution(conn)
        repository.seal_snapshot("serialized-resolution", NOW, NOW, SCOPE)
        assert blocked == [True]
        with pytest.raises(sqlite3.IntegrityError, match="sealed_resolution_admission_conflict"):
            contender.resolve(cell, NOW, revised, recorded_at=NOW)
        repository.verify_snapshot("serialized-resolution", NOW)
    finally:
        other.close()


def test_interrupted_resolution_seal_releases_its_writer_transaction(
    native_db: tuple[sqlite3.Connection, str, CanonicalFactResolutionEngine],
) -> None:
    conn, cell, engine = native_db
    engine.resolve(
        cell, NOW, ResolutionPolicy(name="deterministic", version="v1", config={}), recorded_at=NOW
    )
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    other = sqlite3.connect(path, timeout=0)

    class InterruptedResolution(CanonicalFactResolutionEngine):
        def _latest_resolution_members(
            self,
            cutoff: datetime,
            scope: ResolutionSnapshotScope,
            *,
            recorded_cutoff: datetime | None = None,
        ) -> list[dict[str, object]]:
            raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            InterruptedResolution(conn).seal_snapshot("interrupted", NOW, NOW, SCOPE)
        assert not conn.in_transaction
        assert (
            conn.execute(
                "SELECT 1 FROM canonical_fact_resolution_snapshot_seals WHERE resolution_snapshot_id=?",
                ("interrupted",),
            ).fetchone()
            is None
        )
        # A retained connection after cancellation must not block another writer.
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
    finally:
        other.close()
        conn.close()


def _migration_config(path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_partial_admission_migration_can_resume_without_replacing_guards(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(
        tmp_path / "interrupted-upgrade.db", target="0057_companyfacts_extractor_identity"
    )
    config = _migration_config(path)
    execute = op.execute

    def interrupted(sql: str) -> None:
        if sql.startswith("CREATE TRIGGER"):
            raise RuntimeError("synthetic migration interruption")
        execute(sql)

    with monkeypatch.context() as fault:
        fault.setattr(op, "execute", interrupted)
        with pytest.raises(RuntimeError, match="synthetic migration interruption"):
            command.upgrade(config, "head")
    with sqlite3.connect(path) as conn:
        partial = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_%_snapshot_admission_%'"
        ).fetchall()
        assert partial, "fixture must reproduce retained partial SQLite DDL"
    command.upgrade(config, "head")
    with sqlite3.connect(path) as conn:
        guards = conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE '%_sealed_admission' ORDER BY name"
        ).fetchall()
        assert len(guards) == 12
        # Re-running an interrupted operation may verify existing matching DDL;
        # it must not silently accept a conflicting object with the same name.
        conn.execute(
            "UPDATE alembic_version SET version_num='0057_companyfacts_extractor_identity'"
        )
    command.upgrade(config, "head")
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE '%_sealed_admission' ORDER BY name"
            ).fetchall()
            == guards
        )


def test_admission_migration_downgrade_upgrade_preserves_retained_seal(
    ontology_db: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, _repository = ontology_db
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    before = tuple(conn.execute("SELECT * FROM ontology_snapshot_seals").fetchone())
    conn.commit()
    conn.close()
    config = _migration_config(path)
    command.downgrade(config, "0057_companyfacts_extractor_identity")
    with sqlite3.connect(path) as prior:
        assert (
            prior.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE '%_sealed_admission'"
            ).fetchone()[0]
            == 0
        )
    command.upgrade(config, "head")
    with sqlite3.connect(path) as restored:
        repository = MetricOntology(restored)
        repository.verify_snapshot("retained-ontology")
        assert tuple(restored.execute("SELECT * FROM ontology_snapshot_seals").fetchone()) == before
        with pytest.raises(sqlite3.IntegrityError, match="sealed_ontology_admission_conflict"):
            repository.persist_metric_definition(_definition(2))


def test_admission_migration_rejects_conflicting_partial_schema(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(
        tmp_path / "conflicting-upgrade.db", target="0057_companyfacts_extractor_identity"
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE INDEX ix_ontology_snapshot_admission_cutoff "
            "ON ontology_snapshot_headers(ontology_snapshot_id)"
        )
    with pytest.raises(RuntimeError, match="sealed admission migration object conflicts"):
        command.upgrade(_migration_config(path), "head")
