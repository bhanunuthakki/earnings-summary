from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import pytest

import provenance.population_metric_ontology as population
from provenance.metric_ontology import (
    BindingRevision,
    SourceObservationTaxonomyAssertion,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
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
    Callable[[Mapping[str, object], datetime], BindingRevision],
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
    Callable[[datetime, str, str], str],
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
    Callable[
        [Mapping[str, object], datetime],
        SourceObservationTaxonomyAssertion,
    ],
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
        CREATE TABLE fact_cells_v2 (fact_cell_id TEXT PRIMARY KEY);
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
        INSERT INTO fact_cells_v2 VALUES ('cell-1');
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
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
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
        """
    )
    post_o = _OPERATION_STAMP + timedelta(hours=1)
    for suffix, recorded_at in (("in", _OPERATION_STAMP), ("post", post_o)):
        conn.execute(
            "INSERT INTO fact_cells_v2 VALUES (?,?,?)",
            (f"cell-{suffix}", _STAMP.isoformat(), recorded_at.isoformat()),
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

    rows = list(_source_cells(conn, _STAMP, _OPERATION_STAMP))

    assert [row["fact_cell_id"] for row in rows] == ["cell-in"]


def test_resume_requires_original_manifest_commitments() -> None:
    with pytest.raises(ValueError, match="resume requires manifest commitments"):
        MetricOntologyPopulationRequest(
            apply=True,
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_OPERATION_STAMP,
            after_observation_id="observation-10",
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


def test_ontology_records_use_operation_system_clock() -> None:
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
        _OPERATION_STAMP,
    )
    binding = _binding(
        {
            **_metric_cell(),
            "observation_id": "observation-1",
            "observation_recorded_at": _STAMP.isoformat(),
            "cell_recorded_at": _STAMP.isoformat(),
        },
        _OPERATION_STAMP,
    )

    assert assertion.knowledge_at == _STAMP
    assert assertion.recorded_at == _OPERATION_STAMP
    assert binding.knowledge_at == _STAMP
    assert binding.recorded_at == _OPERATION_STAMP


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


def test_snapshot_identity_commits_to_cutoff_policy_and_member_set() -> None:
    baseline = _snapshot_id(_STAMP, _policy_sha(), "member-set-a")

    assert baseline != _snapshot_id(
        _STAMP + timedelta(seconds=1),
        _policy_sha(),
        "member-set-a",
    )
    assert baseline != _snapshot_id(_STAMP, "other-policy", "member-set-a")
    assert baseline != _snapshot_id(_STAMP, _policy_sha(), "member-set-b")
