# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

import pytest
from alembic.config import Config

import provenance.population_metric_ontology as population
from alembic import command
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.filing_xbrl_fact_adapter import FilingXbrlNormalizedOutput
from provenance.metric_ontology import (
    BindingRevision,
    SourceObservationTaxonomyAssertion,
)
from provenance.population_metric_ontology import (
    MetricOntologyOperationReceipt,
    MetricOntologyPopulationRequest,
    MetricOntologyPopulationResult,
    build_metric_ontology_receipt,
    populate_metric_ontology,
    verify_metric_ontology_receipt,
)
from tests.test_canonical_fact_resolution import ROOT, _resolution_database
from tests.test_filing_xbrl_extraction_ledger import (
    STAMP as SOURCE_STAMP,
)
from tests.test_filing_xbrl_extraction_ledger import (
    _entry,
    _insert_extraction_run,
    _output,
)

_STAMP = datetime(2026, 7, 29, tzinfo=UTC)
_OPERATION_STAMP = _STAMP + timedelta(hours=1)
_Clock = tuple[datetime, datetime, datetime]


class _VerifyCallerCommitments(Protocol):
    def __call__(
        self,
        request: MetricOntologyPopulationRequest,
        *,
        input_sha: str,
        plan_sha: str,
    ) -> None: ...


_INPUT_MANIFEST_TABLES = cast(
    tuple[str, ...],
    getattr(population, "_INPUT_MANIFEST_TABLES"),
)
_OUTPUT_MANIFEST_TABLES = cast(
    tuple[str, ...],
    getattr(population, "_OUTPUT_MANIFEST_TABLES"),
)
_atomic_population = cast(
    Callable[[sqlite3.Connection], AbstractContextManager[None]],
    getattr(population, "_atomic_population"),
)
_binding = cast(
    Callable[[Mapping[str, object]], BindingRevision],
    getattr(population, "_binding"),
)
_concept_component_id = cast(
    Callable[[Mapping[str, object]], str],
    getattr(population, "_concept_component_id"),
)
_input_commitment = cast(
    Callable[[sqlite3.Connection], str],
    getattr(population, "_input_commitment"),
)
_metric_id = cast(
    Callable[[Mapping[str, object]], str],
    getattr(population, "_metric_id"),
)
_object_clocks = cast(
    Callable[
        [
            Iterable[Mapping[str, object]],
            Callable[[Mapping[str, object]], str],
        ],
        dict[str, _Clock],
    ],
    getattr(population, "_object_clocks"),
)
_output_commitment = cast(
    Callable[[sqlite3.Connection], str],
    getattr(population, "_output_commitment"),
)
_policy_sha = cast(Callable[[], str], getattr(population, "_policy_sha"))
_snapshot_id = cast(
    Callable[[datetime, datetime, str, str], str],
    getattr(population, "_snapshot_id"),
)
_source_cells = cast(
    Callable[
        [sqlite3.Connection, datetime | None, datetime | None],
        Iterator[dict[str, object]],
    ],
    getattr(population, "_source_cells"),
)
_source_definition_commitment = cast(
    Callable[[Mapping[str, object]], str],
    getattr(population, "_source_definition_commitment"),
)
_taxonomy_assertion = cast(
    Callable[[Mapping[str, object]], SourceObservationTaxonomyAssertion],
    getattr(population, "_taxonomy_assertion"),
)
_verify_caller_commitments = cast(
    _VerifyCallerCommitments,
    getattr(population, "_verify_caller_commitments"),
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _source_cell_connection(value_kinds: tuple[str, ...]) -> sqlite3.Connection:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            reporting_entity_id TEXT NOT NULL
        );
        CREATE TABLE reporting_entities (
            reporting_entity_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE fact_cell_identity_seals_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            semantic_key_sha256 TEXT NOT NULL
        );
        CREATE TABLE fact_observations_v2 (
            observation_id TEXT PRIMARY KEY,
            fact_cell_id TEXT NOT NULL,
            observation_kind TEXT NOT NULL,
            value_kind TEXT NOT NULL
        );
        CREATE TABLE fact_dimensions_normalized_v2 (
            dimension_id TEXT PRIMARY KEY,
            member_kind TEXT NOT NULL
        );
        CREATE TABLE fact_reported_observation_anchors_v2 (
            observation_id TEXT PRIMARY KEY,
            source_taxonomy_version TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        INSERT INTO fact_cells_v2 VALUES ('cell-1','issuer-a');
        INSERT INTO reporting_entities VALUES ('issuer-a','2026-07-29T00:00:00+00:00');
        INSERT INTO fact_cell_identity_seals_v2 VALUES ('cell-1','seal-1');
        """
    )
    conn.executemany(
        "INSERT INTO fact_observations_v2 VALUES (?,?,?,?)",
        [
            (f"observation-{index}", "cell-1", "reported", value_kind)
            for index, value_kind in enumerate(value_kinds, start=1)
        ],
    )
    conn.executemany(
        "INSERT INTO fact_reported_observation_anchors_v2 VALUES (?,?,?)",
        [
            (f"observation-{index}", "2026", _STAMP.isoformat())
            for index in range(1, len(value_kinds) + 1)
        ],
    )
    return conn


@pytest.mark.parametrize("value_kind", ["numeric", "text"])
def test_nil_observation_is_absence_not_metric_type(value_kind: str) -> None:
    conn = _source_cell_connection(("nil", value_kind))

    cells = list(_source_cells(conn, None, None))

    assert len(cells) == 1
    assert cells[0]["value_kind"] == value_kind


def test_all_nil_cell_fails_closed_as_unresolved() -> None:
    conn = _source_cell_connection(("nil", "nil"))

    cells = list(_source_cells(conn, None, None))

    assert cells == []


def test_numeric_text_evolution_fails_closed() -> None:
    conn = _source_cell_connection(("nil", "numeric", "text"))

    cells = list(_source_cells(conn, None, None))

    assert cells == []


def test_typed_dimension_fails_closed_before_population() -> None:
    conn = _source_cell_connection(("numeric",))
    conn.execute(
        "INSERT INTO fact_dimensions_normalized_v2 VALUES (?,?)",
        ("dimension-1", "typed"),
    )

    cells = list(_source_cells(conn, None, None))

    assert len(cells) == 1


def test_scoped_source_cells_ignore_post_observation_clock_artifacts() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            reporting_entity_id TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE reporting_entities (
            reporting_entity_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE fact_cell_identity_seals_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            semantic_key_sha256 TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE fact_observations_v2 (
            observation_id TEXT PRIMARY KEY,
            fact_cell_id TEXT NOT NULL,
            observation_kind TEXT NOT NULL,
            value_kind TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_reported_observation_anchors_v2 (
            observation_id TEXT PRIMARY KEY,
            source_taxonomy_version TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        INSERT INTO reporting_entities VALUES ('issuer-a','2026-07-29T00:00:00+00:00');
        """
    )
    post_o = _OPERATION_STAMP + timedelta(hours=1)
    for suffix, recorded_at in (("in", _OPERATION_STAMP), ("post", post_o)):
        conn.execute(
            "INSERT INTO fact_cells_v2 VALUES (?,?,?,?)",
            (
                f"cell-{suffix}",
                "issuer-a",
                _STAMP.isoformat(),
                recorded_at.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO fact_cell_identity_seals_v2 VALUES (?,?,?)",
            (f"cell-{suffix}", f"seal-{suffix}", recorded_at.isoformat()),
        )
        conn.execute(
            "INSERT INTO fact_observations_v2 VALUES (?,?,?,?,?,?)",
            (
                f"observation-{suffix}",
                f"cell-{suffix}",
                "reported",
                "numeric",
                _STAMP.isoformat(),
                recorded_at.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO fact_reported_observation_anchors_v2 VALUES (?,?,?)",
            (f"observation-{suffix}", "2026", recorded_at.isoformat()),
        )

    rows = list(_source_cells(conn, _STAMP, _OPERATION_STAMP))

    assert [row["fact_cell_id"] for row in rows] == ["cell-in"]


def test_resume_requires_original_manifest_commitments() -> None:
    with pytest.raises(ValueError, match="bounded ontology apply"):
        MetricOntologyPopulationRequest(
            apply=True,
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_OPERATION_STAMP,
            after_observation_id="observation-10",
        )


def test_max_only_bounded_apply_requires_manifest_commitments() -> None:
    with pytest.raises(ValueError, match="bounded ontology apply"):
        MetricOntologyPopulationRequest(
            apply=True,
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_OPERATION_STAMP,
            max_observations=10,
        )


def test_bounded_population_is_restricted_to_resumable_all_phase() -> None:
    with pytest.raises(ValueError, match="requires phase='all'"):
        MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_OPERATION_STAMP,
            phase="assertions",
            max_observations=10,
        )


def test_resume_reverifies_original_manifest_commitments() -> None:
    request = MetricOntologyPopulationRequest(
        apply=True,
        knowledge_cutoff=_STAMP,
        operation_recorded_at=_OPERATION_STAMP,
        after_observation_id="observation-10",
        input_commitment_sha256="a" * 64,
        plan_commitment_sha256="b" * 64,
    )

    with pytest.raises(ValueError, match="input commitment"):
        _verify_caller_commitments(
            request,
            input_sha="c" * 64,
            plan_sha="b" * 64,
        )
    with pytest.raises(ValueError, match="plan commitment"):
        _verify_caller_commitments(
            request,
            input_sha="a" * 64,
            plan_sha="c" * 64,
        )


def test_ontology_records_use_stable_source_clock() -> None:
    assertion = _taxonomy_assertion(
        {
            "observation_id": "observation-1",
            "recorded_at": _STAMP.isoformat(),
            "extraction_run_id": "run-1",
            "taxonomy_name": "legacy",
            "source_taxonomy_version": "v1",
            "semantic_key_sha256": "a" * 64,
            "anchor_payload_sha256": "b" * 64,
            "observation_payload_sha256": "c" * 64,
            "extraction_output_sha256": "d" * 64,
            "raw_entry_sha256": "e" * 64,
            "observation_set_sha256": "f" * 64,
        },
    )
    binding = _binding(
        {
            **_metric_cell(),
            "observation_id": "observation-1",
            "observation_recorded_at": _STAMP.isoformat(),
            "cell_recorded_at": _STAMP.isoformat(),
        },
    )

    assert assertion.knowledge_at == _STAMP
    assert assertion.recorded_at == _STAMP
    assert binding.knowledge_at == _STAMP
    assert binding.recorded_at == _STAMP


def test_population_replays_o1_objects_when_temporal_scope_advances(
    tmp_path: Path,
) -> None:
    observed_o2 = SOURCE_STAMP + timedelta(hours=1)
    late_locator = {"path": "/xbrl/late"}
    late_entry = _entry(0, numeric_value=Decimal("200")).model_copy(
        update={
            "evidence_node_id": "node-late",
            "knowledge_at": observed_o2,
            "recorded_at": observed_o2,
            "source_context_id": "context-late",
            "source_entry_sha256": "b" * 64,
            "source_locator": late_locator,
            "source_locator_sha256": hashlib.sha256(b'{"path":"/xbrl/late"}').hexdigest(),
            "source_unit_id": "unit-late",
        }
    )
    initial = _output((_entry(0, numeric_value=Decimal("100")),))
    conn = _resolution_database(tmp_path, initial)
    database_path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.commit()
    conn.close()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        FilingXbrlExtractionLedger(conn).publish(initial)
        first = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=SOURCE_STAMP,
                operation_recorded_at=SOURCE_STAMP,
                apply=True,
            ),
        )
        conn.commit()

        late_template = _output(
            (late_entry,),
            extraction_run_id="run-2",
            extractor_config_sha256="a" * 64,
        )
        late_output = FilingXbrlNormalizedOutput.with_computed_digest(
            extraction=late_template.extraction.model_copy(
                update={
                    "knowledge_at": observed_o2,
                    "recorded_at": observed_o2,
                }
            ),
            subject=late_template.subject,
            entries=late_template.entries,
        )
        _insert_extraction_run(conn, late_output)
        FilingXbrlExtractionLedger(conn).publish(late_output)

        second = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=observed_o2,
                operation_recorded_at=observed_o2,
                apply=True,
            ),
        )

        assert first.source_observation_count == 1
        assert second.source_observation_count == 2
        assert second.assertion_count == 2
        assert second.binding_count == 2
        assert second.snapshot_eligible is True
    finally:
        conn.close()


def _metric_cell(
    *,
    reporting_entity_id: str = "issuer-a",
    recorded_at: datetime = _STAMP,
) -> dict[str, object]:
    return {
        "accounting_basis": "management",
        "concept_name": "ActiveCustomers",
        "concept_namespace": "issuer-extension",
        "consolidation_scope": "consolidated",
        "currency": None,
        "effective_at": recorded_at.isoformat(),
        "fact_cell_id": f"cell-{reporting_entity_id}-{recorded_at.isoformat()}",
        "knowledge_at": recorded_at.isoformat(),
        "period_kind": "duration",
        "recorded_at": recorded_at.isoformat(),
        "reporting_entity_id": reporting_entity_id,
        "semantic_key_sha256": f"seal-{reporting_entity_id}",
        "source_taxonomy_version": "2026",
        "taxonomy_name": "issuer-taxonomy",
        "unit_key": "pure",
        "value_kind": "numeric",
    }


def test_provisional_management_metric_identity_is_issuer_scoped() -> None:
    issuer_a = _metric_cell(reporting_entity_id="issuer-a")
    issuer_b = _metric_cell(reporting_entity_id="issuer-b")

    assert _metric_id(issuer_a) != _metric_id(issuer_b)
    assert _source_definition_commitment(issuer_a) != _source_definition_commitment(issuer_b)


def test_legacy_source_concept_variants_have_distinct_component_identity() -> None:
    currency_variant = {
        **_metric_cell(),
        "currency": "USD",
        "unit_key": "actual",
    }
    unknown_unit_variant = {
        **_metric_cell(),
        "currency": None,
        "unit_key": "actual",
    }

    assert _concept_component_id(currency_variant) != _concept_component_id(unknown_unit_variant)
    assert _metric_id(currency_variant) != _metric_id(unknown_unit_variant)
    assert _source_definition_commitment(currency_variant) != _source_definition_commitment(
        unknown_unit_variant
    )


def test_operator_keeps_same_metric_definition_qualified_without_stale_conflict() -> None:
    consolidated = _metric_cell()
    unconsolidated = {**consolidated, "consolidation_scope": "unconsolidated"}

    assert _metric_id(consolidated) != _metric_id(unconsolidated)
    assert _source_definition_commitment(consolidated) != _source_definition_commitment(
        unconsolidated
    )
    assert _concept_component_id(consolidated) != _concept_component_id(unconsolidated)


def test_source_definition_identity_includes_exact_taxonomy_coordinates() -> None:
    baseline = _metric_cell()
    renamed_taxonomy = {**baseline, "taxonomy_name": "issuer-taxonomy-successor"}
    revised_taxonomy = {**baseline, "source_taxonomy_version": "2027"}

    assert _metric_id(baseline) != _metric_id(renamed_taxonomy)
    assert _metric_id(baseline) != _metric_id(revised_taxonomy)
    assert _source_definition_commitment(baseline) != _source_definition_commitment(
        renamed_taxonomy
    )
    assert _source_definition_commitment(baseline) != _source_definition_commitment(
        revised_taxonomy
    )


def test_object_clock_is_stable_when_later_cells_arrive() -> None:
    first = _metric_cell(recorded_at=_STAMP)
    second = _metric_cell(recorded_at=_STAMP + timedelta(days=1))
    third = _metric_cell(recorded_at=_STAMP + timedelta(days=2))

    before = _object_clocks([first, second], _metric_id)
    after = _object_clocks([first, second, third], _metric_id)

    assert before == after
    assert before[_metric_id(first)][2] == _STAMP


def _manifest_connection(tables: tuple[str, ...]) -> sqlite3.Connection:
    conn = _connection()
    for table in tables:
        conn.execute(
            f'CREATE TABLE "{table}" (manifest_id TEXT PRIMARY KEY,payload TEXT)'  # nosec B608 -- fixed test table inventory
        )
        conn.execute(
            f'INSERT INTO "{table}" VALUES (?,?)',  # nosec B608 -- fixed test table inventory
            (f"{table}-1", "original"),
        )
    return conn


@pytest.mark.parametrize(
    "table",
    [
        "fact_cells_v2",
        "fact_observations_v2",
        "fact_dimensions_normalized_v2",
        "fact_cell_identity_seals_v2",
        "fact_reported_observation_anchors_v2",
        "fact_extraction_run_completeness_seals_v2",
    ],
)
def test_input_manifest_commits_to_every_source_plane(table: str) -> None:
    conn = _manifest_connection(_INPUT_MANIFEST_TABLES)
    before = _input_commitment(conn)

    conn.execute(
        f'UPDATE "{table}" SET payload=?',  # nosec B608 -- parametrized fixed test inventory
        ("changed",),
    )

    assert _input_commitment(conn) != before


@pytest.mark.parametrize("table", _OUTPUT_MANIFEST_TABLES)
def test_output_manifest_commits_to_every_persisted_ontology_table(
    table: str,
) -> None:
    conn = _manifest_connection(_OUTPUT_MANIFEST_TABLES)
    before = _output_commitment(conn)

    conn.execute(
        f'UPDATE "{table}" SET payload=?',  # nosec B608 -- parametrized fixed test inventory
        ("changed",),
    )

    assert _output_commitment(conn) != before


def test_atomic_population_rolls_back_every_phase_write() -> None:
    conn = _connection()
    conn.execute("CREATE TABLE writes (value TEXT)")

    with (
        pytest.raises(RuntimeError, match="stop"),
        _atomic_population(conn),
    ):
        conn.execute("INSERT INTO writes VALUES ('registry')")
        conn.execute("INSERT INTO writes VALUES ('assertions')")
        raise RuntimeError("stop")

    assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0


def test_snapshot_identity_commits_to_both_clocks_policy_and_member_set() -> None:
    baseline = _snapshot_id(_STAMP, _OPERATION_STAMP, _policy_sha(), "member-set-a")

    assert baseline != _snapshot_id(
        _STAMP + timedelta(seconds=1),
        _OPERATION_STAMP,
        _policy_sha(),
        "member-set-a",
    )
    assert baseline != _snapshot_id(
        _STAMP,
        _OPERATION_STAMP + timedelta(seconds=1),
        _policy_sha(),
        "member-set-a",
    )
    assert baseline != _snapshot_id(_STAMP, _OPERATION_STAMP, "other-policy", "member-set-a")
    assert baseline != _snapshot_id(_STAMP, _OPERATION_STAMP, _policy_sha(), "member-set-b")


def test_operation_receipt_binds_exact_source_definition_admission() -> None:
    request = MetricOntologyPopulationRequest(
        knowledge_cutoff=_STAMP,
        operation_recorded_at=_OPERATION_STAMP,
    )
    result = MetricOntologyPopulationResult(
        mode="dry_run",
        phase="all",
        outcome="planned",
        reason_codes=("ontology_assertions_incomplete", "ontology_bindings_incomplete"),
        snapshot_eligible=False,
        source_cell_count=2,
        source_observation_count=2,
        metric_count=0,
        source_component_count=0,
        canonical_cell_count=0,
        assertion_count=0,
        binding_count=0,
        missing_assertion_count=2,
        missing_binding_count=2,
        processed_observation_count=0,
        last_observation_id=None,
        remaining_observation_count=2,
        safe_to_seal=False,
        snapshot_id=None,
        policy_config_sha256="a" * 64,
        plan_commitment_sha256="b" * 64,
        input_commitment_sha256="c" * 64,
        post_state_commitment_sha256="d" * 64,
        output_commitment_sha256="d" * 64,
    )

    receipt = build_metric_ontology_receipt(
        database_path="C:/candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0265_metric_ontology_operation_ledger",
        request=request,
        result=result,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
    )

    assert isinstance(receipt, MetricOntologyOperationReceipt)
    assert receipt.outcome == "planned"
    assert receipt.blocker_counts == {
        "missing_assertion": 2,
        "missing_binding": 2,
    }
    assert verify_metric_ontology_receipt(receipt)
