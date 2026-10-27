from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast

import pytest

import provenance.source_fact_publication as source_publication
from provenance import fact_read_model
from provenance.fact_plane_v2 import (
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    FactCellV2,
    FactObservationV2,
)
from provenance.fact_read_model import (
    FactEvidenceIntegrityError,
    FactReadModel,
    ObservationUnavailableError,
)
from provenance.source_fact_publication import PublicationMemberKind, VerifiedSourceFactPublication
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


class _R6ProbeResult(TypedDict):
    output: dict[str, object]
    reference_count: int | None
    available_count: int | None
    select_count: int
    full_verification_count: int
    elapsed_ns: int


class _R6CaseMetrics(TypedDict):
    selects: dict[str, int]
    full_recursive_verifications: dict[str, int]
    median_ns: dict[str, int | float]
    complete_json_equal: bool
    resolution_observed_through: str | None


_R6_BASELINE = "c978523d84a4efea07858e7029307dd588d2a841"
_R6_SAMPLE_COUNT = 5
_R6_PROBE = r"""
import json
import sqlite3
import sys
import time
from datetime import date, datetime

import provenance.source_fact_publication as source_publication
from sources.discovery_financials import read_financial_history
from sources.report_financials import read_financial_table

verification_count = 0
original_verify = source_publication._verify_source_fact_publication

def counted_verify(*args, **kwargs):
    global verification_count
    verification_count += 1
    return original_verify(*args, **kwargs)

source_publication._verify_source_fact_publication = counted_verify

def run(request):
    global verification_count
    verification_count = 0
    select_count = 0

    def trace(statement):
        nonlocal select_count
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            select_count += 1

    uri = "file:" + request["database"] + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.set_trace_callback(trace)
    started = time.perf_counter_ns()
    if request["operation"] == "discovery":
        result = read_financial_history(
            conn, "WIX", as_of=date.fromisoformat(request["as_of"])
        )
        reference_count = len(result.references)
        available_count = None
    else:
        observed = request["observed_through"]
        kwargs = {} if observed is None else {
            "resolution_observed_through": datetime.fromisoformat(observed)
        }
        result = read_financial_table(
            conn,
            "SYNTH",
            as_of=datetime.fromisoformat(request["as_of"]),
            **kwargs,
        )
        reference_count = None
        available_count = sum(cell.available for cell in result.cells)
    elapsed_ns = time.perf_counter_ns() - started
    conn.close()
    return {
        "output": result.model_dump(mode="json"),
        "reference_count": reference_count,
        "available_count": available_count,
        "select_count": select_count,
        "full_verification_count": verification_count,
        "elapsed_ns": elapsed_ns,
    }

try:
    print(json.dumps(run(json.load(sys.stdin)), sort_keys=True), flush=True)
except BaseException as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
"""


def _r6_source_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    for package in ("provenance", "sources"):
        for path in sorted((source_root / package).rglob("*.py")):
            relative = path.relative_to(source_root).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            digest.update(b"\n")
    return digest.hexdigest()


def _extract_r6_baseline_source(destination: Path) -> tuple[Path, str]:
    """Materialize the baseline source packages without changing the checkout."""
    repo_root = Path(__file__).resolve().parents[1]
    names = subprocess.run(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            _R6_BASELINE,
            "--",
            "src/provenance",
            "src/sources",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    source_root = destination / "src"
    for relative in names:
        if not relative.endswith(".py"):
            continue
        content = subprocess.run(
            ["git", "show", f"{_R6_BASELINE}:{relative}"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        ).stdout
        target = source_root / Path(relative).relative_to("src")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return source_root, _r6_source_digest(source_root)


def _r6_probe(
    source_roots: tuple[Path, ...],
    *,
    database: Path,
    operation: str,
    observed_through: datetime | None = None,
) -> _R6ProbeResult:
    from tests.test_canonical_growth_screen import STAMP as DISCOVERY_STAMP

    request = {
        "database": str(database),
        "operation": operation,
        "as_of": STAMP.isoformat()
        if operation == "report"
        else (DISCOVERY_STAMP + timedelta(days=1)).date().isoformat(),
        "observed_through": None if observed_through is None else observed_through.isoformat(),
    }
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in (*source_roots, repo_root / "src"))
    completed = subprocess.run(
        [sys.executable, "-c", _R6_PROBE],
        cwd=repo_root,
        env=env,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    decoded = json.loads(completed.stdout)
    if not isinstance(decoded, dict):
        raise RuntimeError("R6 reader probe returned a non-object result")
    if "error" in decoded:
        raise RuntimeError(f"R6 reader probe failed: {decoded['error']}")
    required_ints = ("select_count", "full_verification_count", "elapsed_ns")
    if (
        not isinstance(decoded.get("output"), dict)
        or any(not isinstance(decoded.get(key), int) for key in required_ints)
        or (
            decoded.get("reference_count") is not None
            and not isinstance(decoded.get("reference_count"), int)
        )
        or (
            decoded.get("available_count") is not None
            and not isinstance(decoded.get("available_count"), int)
        )
    ):
        raise RuntimeError("R6 reader probe returned invalid measurement fields")
    return cast(_R6ProbeResult, decoded)


def _seed_r6_discovery_database(database: Path, tmp_path: Path) -> None:
    from tests.test_canonical_growth_screen import seed_growth_graph

    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        seed_growth_graph(connection, tmp_path, include_screen_financials=True)


def _seed_r6_report_database(database: Path) -> None:
    from tests.test_report_canonical_financials import seed_table

    with sqlite3.connect(database) as connection:
        foundation.seed_foundation(connection)
        periods = (
            ("2025-01-01", "2025-03-31", "Q1"),
            ("2025-04-01", "2025-06-30", "Q2"),
            ("2025-07-01", "2025-09-30", "Q3"),
            ("2025-10-01", "2025-12-31", "Q4"),
        )
        rows = [
            (concept, start, end, period, str(value), "USD")
            for concept, values in (
                ("revenue", (100, 110, 120, 130)),
                ("gross_profit", (40, 44, 48, 52)),
            )
            for (start, end, period), value in zip(periods, values, strict=True)
        ]
        seed_table(connection, rows)
        connection.commit()


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
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )

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

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", counted)
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
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )

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

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", counted)
    results = FactReadModel(conn).provenance_bundles(
        tuple(item.observation.observation_id for item in publication.reported_facts),
        cutoff=STAMP,
    )

    assert len(results) == 24
    assert all(item.bundle is not None for item in results)
    assert verified == [publication.publication_id]


def test_batch_bulk_loads_observation_rows_and_preserves_order_duplicates_and_missing(
    conn: sqlite3.Connection,
) -> None:
    publication = _large_publication(8)
    SourceFactRepository(conn).publish(publication)
    observation_ids = tuple(item.observation.observation_id for item in publication.reported_facts)
    requested = (observation_ids[5], observation_ids[1], observation_ids[5], "missing-id")
    model = FactReadModel(conn)
    bundle_selects: list[str] = []

    def trace_batch(sql: str) -> None:
        if (
            sql.startswith("SELECT observation.*,")
            and "fact_observation_payload_commitments_v2" in sql
        ):
            bundle_selects.append(sql)

    conn.set_trace_callback(trace_batch)
    results = model.provenance_bundles(requested, cutoff=STAMP)
    conn.set_trace_callback(None)

    scalar_selects: list[str] = []
    scalar_results: list[object | None] = []

    def trace_scalar(sql: str) -> None:
        if (
            sql.startswith("SELECT observation.*,")
            and "fact_observation_payload_commitments_v2" in sql
        ):
            scalar_selects.append(sql)

    conn.set_trace_callback(trace_scalar)
    for observation_id in requested:
        try:
            scalar_results.append(model.provenance_bundle(observation_id, cutoff=STAMP))
        except ObservationUnavailableError:
            scalar_results.append(None)
    conn.set_trace_callback(None)

    assert len(bundle_selects) == 1
    assert len(scalar_selects) == len(requested)
    assert len(bundle_selects) < len(scalar_selects)
    assert tuple(item.observation_id for item in results) == requested
    assert [item.bundle for item in results] == scalar_results
    assert results[3].bundle is None and results[3].failure is not None
    assert results[3].failure.reason_code == "observation_unavailable_at_cutoff"


def test_batch_chunks_bulk_observation_rows_to_sqlite_variable_limit(
    conn: sqlite3.Connection,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    observation_ids = tuple(
        item.observation.observation_id for item in publication.reported_facts
    ) + tuple(f"missing-{index}" for index in range(8))
    original_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5)
    bundle_selects: list[str] = []

    def trace_batch(sql: str) -> None:
        if (
            sql.startswith("SELECT observation.*,")
            and "fact_observation_payload_commitments_v2" in sql
        ):
            bundle_selects.append(sql)

    conn.set_trace_callback(trace_batch)
    try:
        results = FactReadModel(conn).provenance_bundles(observation_ids, cutoff=STAMP)
    finally:
        conn.set_trace_callback(None)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_limit)

    assert len(results) == len(observation_ids)
    assert all(result.bundle is not None for result in results[:2])
    assert all(
        result.failure and result.failure.reason_code == "observation_unavailable_at_cutoff"
        for result in results[2:]
    )
    assert len(bundle_selects) == 4


@pytest.mark.skipif(
    os.environ.get("RUN_R6_COMPARISON") != "1",
    reason="explicit historical performance comparison; run with RUN_R6_COMPARISON=1",
)
def test_replayable_r6_reader_comparison(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    """Compare complete WIX21/SYNTH8 reads with the frozen pre-R6 source closure.

    Run with `RUN_R6_COMPARISON=1` and:
    ``pytest -s tests/test_fact_read_model_batch.py -k replayable_r6_reader_comparison``.
    The baseline packages are loaded from Git into a temporary directory. The
    checked-out branch and all production data remain unchanged.
    """
    discovery_database = migrated_db(tmp_path / "r6-discovery.db")
    report_database = migrated_db(tmp_path / "r6-report.db")
    _seed_r6_discovery_database(discovery_database, tmp_path / "inputs")
    _seed_r6_report_database(report_database)
    with tempfile.TemporaryDirectory(prefix="r6-reader-baseline-") as baseline_dir:
        baseline_source, baseline_source_sha256 = _extract_r6_baseline_source(Path(baseline_dir))
        current_source_sha256 = _r6_source_digest(Path(__file__).resolve().parents[1] / "src")
        cases: tuple[tuple[str, datetime | None, Path], ...] = (
            ("discovery", None, discovery_database),
            ("report", None, report_database),
            ("report", STAMP + timedelta(seconds=1), report_database),
        )
        samples: dict[str, dict[str, list[_R6ProbeResult]]] = {
            _r6_case_name(operation, observed): {"baseline": [], "current": []}
            for operation, observed, _database in cases
        }
        for operation, observed, database in cases:
            name = _r6_case_name(operation, observed)
            for _side, roots in (("baseline", (baseline_source,)), ("current", ())):
                warmup = _r6_probe(
                    roots,
                    database=database,
                    operation=operation,
                    observed_through=observed,
                )
                _assert_r6_population(name, warmup)
        for sample in range(_R6_SAMPLE_COUNT):
            sides = (
                (("baseline", (baseline_source,)), ("current", ()))
                if sample % 2 == 0
                else (("current", ()), ("baseline", (baseline_source,)))
            )
            for operation, observed, database in cases:
                name = _r6_case_name(operation, observed)
                for side, roots in sides:
                    samples[name][side].append(
                        _r6_probe(
                            roots,
                            database=database,
                            operation=operation,
                            observed_through=observed,
                        )
                    )
        report: dict[str, object] = {
            "baseline_commit": _R6_BASELINE,
            "source_package_sha256": {
                "baseline": baseline_source_sha256,
                "current": current_source_sha256,
            },
            "samples_per_side": _R6_SAMPLE_COUNT,
            "fixture_counts": {
                "WIX_discovery_references": 21,
                "SYNTH_report_available_cells": 8,
            },
            "cases": {},
        }
        case_report: dict[str, _R6CaseMetrics] = {}
        for operation, observed, _database in cases:
            name = _r6_case_name(operation, observed)
            sides = samples[name]
            before, after = sides["baseline"], sides["current"]
            baseline_json = before[0]["output"]
            current_json = after[0]["output"]
            assert baseline_json == current_json, f"complete output changed for {name}"
            assert all(item["output"] == baseline_json for item in before + after)
            before_selects = {item["select_count"] for item in before}
            after_selects = {item["select_count"] for item in after}
            before_verifications = {item["full_verification_count"] for item in before}
            after_verifications = {item["full_verification_count"] for item in after}
            assert len(before_selects) == len(after_selects) == 1
            assert len(before_verifications) == len(after_verifications) == 1
            before_median = statistics.median(item["elapsed_ns"] for item in before)
            after_median = statistics.median(item["elapsed_ns"] for item in after)
            limit = before_median + max(before_median // 10, 5_000_000)
            assert after_median <= limit, f"median regression exceeded allowance for {name}"
            metrics: _R6CaseMetrics = {
                "selects": {
                    "baseline": before_selects.pop(),
                    "current": after_selects.pop(),
                },
                "full_recursive_verifications": {
                    "baseline": before_verifications.pop(),
                    "current": after_verifications.pop(),
                },
                "median_ns": {"baseline": before_median, "current": after_median},
                "complete_json_equal": True,
                "resolution_observed_through": (None if observed is None else observed.isoformat()),
            }
            if name in {"WIX21_discovery", "SYNTH8_report_K_eq_O"}:
                old_selects = metrics["selects"]["baseline"]
                new_selects = metrics["selects"]["current"]
                assert old_selects > 0 and new_selects <= old_selects * 0.8
            case_report[name] = metrics
        report["cases"] = case_report
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))


def _r6_case_name(operation: str, observed_through: datetime | None) -> str:
    if operation == "discovery":
        return "WIX21_discovery"
    if observed_through is None:
        return "SYNTH8_report_K_eq_O"
    return "SYNTH8_report_K_lt_O"


def _assert_r6_population(name: str, result: _R6ProbeResult) -> None:
    if name == "WIX21_discovery":
        assert result["reference_count"] == 21
        return
    assert result["available_count"] == 8


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


def test_native_recursive_admission_counts_root_and_rejects_depth_and_cycles(
    conn: sqlite3.Connection,
) -> None:
    template = _derived_publication("source-observation").derived_facts[0].observation
    graph: dict[str, tuple[str, ...]] = {}

    class GraphReader(FactReadModel):
        def _admit_observation_and_inputs(
            self,
            observation: FactObservationV2,
            *,
            cutoff: datetime,
            path: set[str],
            admitted: dict[str, int],
        ) -> int:
            height = 1
            for child_id in graph.get(observation.observation_id, ()):
                child = template.model_copy(update={"observation_id": child_id})
                child_height = self._admit_observation_graph(
                    child, cutoff=cutoff, _path=path, _admitted=admitted
                )
                height = max(height, child_height + 1)
            return height

        def admit(self, observation: FactObservationV2) -> None:
            self._admit_observation_graph(observation, cutoff=STAMP)

    reader = GraphReader(conn)
    root = template.model_copy(update={"observation_id": "root"})
    graph["root"] = tuple(f"leaf-{index}" for index in range(499))
    reader.admit(root)
    graph["root"] = tuple(f"leaf-{index}" for index in range(500))
    with pytest.raises(fact_read_model.FactAdmissionError) as width_error:
        reader.admit(root)
    assert width_error.value.reason_code == "derived_input_graph_unbounded"
    graph.clear()
    graph.update({f"node-{index}": (f"node-{index + 1}",) for index in range(31)})
    depth_root = template.model_copy(update={"observation_id": "node-0"})
    reader.admit(depth_root)
    graph["node-31"] = ("node-32",)
    with pytest.raises(fact_read_model.FactAdmissionError) as depth_error:
        reader.admit(depth_root)
    assert depth_error.value.reason_code == "derived_input_graph_unbounded"
    graph.clear()
    graph["root"] = ("other",)
    graph["other"] = ("root",)
    with pytest.raises(fact_read_model.FactAdmissionError) as cycle_error:
        reader.admit(root)
    assert cycle_error.value.reason_code == "derived_input_graph_unbounded"


def _shared_subtree_graph(
    *, shared_height: int, total_levels: int, short_first: bool
) -> dict[str, tuple[str, ...]]:
    chain_length = total_levels - shared_height - 1
    graph: dict[str, tuple[str, ...]] = {
        f"shared-{index}": (f"shared-{index + 1}",) for index in range(shared_height - 1)
    }
    graph.update({f"chain-{index}": (f"chain-{index + 1}",) for index in range(chain_length - 1)})
    graph[f"chain-{chain_length - 1}"] = ("shared-0",)
    graph["root"] = ("shared-0", "chain-0") if short_first else ("chain-0", "shared-0")
    return graph


def test_native_shared_subtree_depth_is_independent_of_edge_order(
    conn: sqlite3.Connection,
) -> None:
    template = _derived_publication("source-observation").derived_facts[0].observation
    graph: dict[str, tuple[str, ...]] = {}
    visited: list[str] = []

    class GraphReader(FactReadModel):
        def admit(self, observation: FactObservationV2) -> int:
            return self._admit_observation_graph(observation, cutoff=STAMP)

        def _admit_observation_and_inputs(
            self,
            observation: FactObservationV2,
            *,
            cutoff: datetime,
            path: set[str],
            admitted: dict[str, int],
        ) -> int:
            observation_id = observation.observation_id
            visited.append(observation_id)
            height = 1
            for child_id in graph.get(observation_id, ()):
                child = template.model_copy(update={"observation_id": child_id})
                child_height = self._admit_observation_graph(
                    child, cutoff=cutoff, _path=path, _admitted=admitted
                )
                height = max(height, child_height + 1)
            return height

    root = template.model_copy(update={"observation_id": "root"})
    for shared_height in (1, 3):
        for total_levels in (32, 33):
            for short_first in (True, False):
                graph = _shared_subtree_graph(
                    shared_height=shared_height,
                    total_levels=total_levels,
                    short_first=short_first,
                )
                visited.clear()
                reader = GraphReader(conn)
                if total_levels == 32:
                    assert reader.admit(root) == 32
                    assert len(visited) == total_levels
                    assert visited.count("shared-0") == 1
                else:
                    with pytest.raises(fact_read_model.FactAdmissionError) as error:
                        reader.admit(root)
                    assert error.value.reason_code == "derived_input_graph_unbounded"


def test_native_batch_shared_subtree_depth_is_independent_of_edge_order(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    root_id = publication.reported_facts[0].observation.observation_id
    template = _derived_publication("source-observation").derived_facts[0].observation
    graph: dict[str, tuple[str, ...]] = {}
    visited: list[str] = []

    class GraphBatchReader(FactReadModel):
        def _admit_observation_and_inputs(
            self,
            observation: FactObservationV2,
            *,
            cutoff: datetime,
            path: set[str],
            admitted: dict[str, int],
        ) -> int:
            observation_id = observation.observation_id
            visited.append(observation_id)
            height = 1
            for child_id in graph.get("root" if observation_id == root_id else observation_id, ()):
                child = template.model_copy(update={"observation_id": child_id})
                child_height = self._admit_observation_graph(
                    child, cutoff=cutoff, _path=path, _admitted=admitted
                )
                height = max(height, child_height + 1)
            return height

    for shared_height in (1, 3):
        for total_levels in (32, 33):
            for short_first in (True, False):
                graph = _shared_subtree_graph(
                    shared_height=shared_height,
                    total_levels=total_levels,
                    short_first=short_first,
                )
                visited.clear()
                reads = GraphBatchReader(conn).provenance_bundles((root_id, root_id), cutoff=STAMP)
                if total_levels == 32:
                    assert all(read.bundle is not None and read.failure is None for read in reads)
                    assert visited.count(root_id) == 2
                    assert visited.count("shared-0") == 2
                else:
                    assert [
                        read.failure.reason_code if read.failure else None for read in reads
                    ] == ["derived_input_graph_unbounded"] * 2


def test_native_direct_edge_query_accepts_500_and_detects_501() -> None:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(
        "CREATE TABLE fact_derivation_seals_v2 ("
        "derivation_seal_id TEXT,output_observation_id TEXT,input_count INTEGER,"
        "knowledge_at TEXT,recorded_at TEXT);"
        "CREATE TABLE fact_derivation_input_edges_v2 ("
        "output_observation_id TEXT,input_observation_id TEXT,input_ordinal INTEGER,"
        "recorded_at TEXT);"
        "CREATE TABLE fact_observations_v2 ("
        "observation_id TEXT,knowledge_at TEXT,recorded_at TEXT);"
    )
    database.execute(
        "INSERT INTO fact_derivation_seals_v2 VALUES ('seal','root',500,?,?)",
        (STAMP, STAMP),
    )
    database.executemany(
        "INSERT INTO fact_derivation_input_edges_v2 VALUES ('root',?,?,?)",
        ((f"child-{index}", index, STAMP) for index in range(500)),
    )

    class GraphReader(FactReadModel):
        def _admit_record(
            self, record_kind: PublicationMemberKind, record_id: str, *, cutoff: datetime
        ) -> None:
            return

        def admit(self, observation: FactObservationV2) -> None:
            self._admit_observation_graph(observation, cutoff=STAMP)

    root = _derived_publication("source-observation").derived_facts[0].observation
    root = root.model_copy(update={"observation_id": "root"})
    try:
        with pytest.raises(fact_read_model.FactAdmissionError) as accepted_width:
            GraphReader(database).admit(root)
        assert accepted_width.value.reason_code == "derived_input_observation_unavailable"
        database.execute(
            "UPDATE fact_derivation_seals_v2 SET input_count=501 WHERE output_observation_id='root'"
        )
        database.execute(
            "INSERT INTO fact_derivation_input_edges_v2 VALUES ('root','child-500',500,?)",
            (STAMP,),
        )
        with pytest.raises(fact_read_model.FactAdmissionError) as rejected_width:
            GraphReader(database).admit(root)
        assert rejected_width.value.reason_code == "derived_input_graph_incomplete"
    finally:
        database.close()


def test_batch_rejects_corrupt_descendants_and_revalidates_each_call(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupt = _two_observation_publication()
    # This publication has its own valid seal but still depends on corrupt's
    # observation. Shared admission must validate that descendant on each read.
    dependent = _derived_publication(corrupt.reported_facts[0].observation.observation_id)
    repository = SourceFactRepository(conn)
    repository.publish(corrupt)
    repository.publish(dependent)
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members SET record_commitment_sha256 = ? "
        "WHERE publication_id = ? AND record_kind = 'extraction_seal'",
        (sha256("tampered"), corrupt.publication_id),
    )
    conn.commit()
    verified: list[str] = []
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )

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

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", counted)
    first = FactReadModel(conn).provenance_bundles(
        (
            corrupt.reported_facts[0].observation.observation_id,
            dependent.derived_facts[0].observation.observation_id,
        ),
        cutoff=STAMP,
    )
    second = FactReadModel(conn).provenance_bundles(
        (dependent.derived_facts[0].observation.observation_id,), cutoff=STAMP
    )

    assert first[0].failure is not None
    assert first[1].bundle is None
    assert second[0].bundle is None
    assert first[1].failure is not None
    assert second[0].failure is not None
    assert first[1].failure.reason_code == "publication_member_tampered"
    assert second[0].failure.reason_code == "publication_member_tampered"
    assert first[1].failure.record_id == corrupt.reported_facts[0].observation.observation_id
    assert verified.count(corrupt.publication_id) == 2
    assert verified.count(dependent.publication_id) == 2


def test_batch_revalidates_after_a_committed_publication_mutation(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    verified: list[str] = []
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )

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

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", counted)
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


@pytest.mark.parametrize("replacement_write", [False, True])
def test_batch_rejects_commit_and_rebegin_that_replaces_its_snapshot(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    replacement_write: bool,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    conn.execute("CREATE TEMP TABLE replacement_caller_write (value INTEGER)")
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )

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
        if replacement_write:
            connection.execute("INSERT INTO replacement_caller_write VALUES (7)")
        return verified

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", replace_snapshot)
    with pytest.raises(RuntimeError, match=r"changed .* state"):
        FactReadModel(conn).provenance_bundles(
            (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
        )

    # The replaced transaction is not the read operation's transaction to undo.
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM replacement_caller_write").fetchall() == (
        [(7,)] if replacement_write else []
    )
    conn.rollback()


def test_batch_rejects_caller_transaction_commit_and_rebegin(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.commit()
    conn.execute("BEGIN")
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
        assert connection.in_transaction
        connection.commit()
        connection.execute("BEGIN")
        return verified

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", replace_snapshot)
    with pytest.raises(RuntimeError, match="changed transaction state"):
        FactReadModel(conn).provenance_bundles(
            (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
        )


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
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )

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
        source_publication,
        "_verify_source_fact_publication",
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
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )
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
        source_publication,
        "_verify_source_fact_publication",
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


@pytest.mark.parametrize(
    "error",
    [
        ValueError("programmer failure"),
        TypeError("programmer failure"),
        KeyError("programmer failure"),
        RuntimeError("programmer failure"),
    ],
)
def test_batch_propagates_programmer_errors(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)

    def broken(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", broken)
    with pytest.raises(type(error), match="programmer failure"):
        FactReadModel(conn).provenance_bundles(
            (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
        )


def test_batch_preserves_operation_database_error(conn: sqlite3.Connection) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    conn.execute(
        "ALTER TABLE source_fact_publication_members RENAME TO missing_publication_members"
    )
    with pytest.raises(sqlite3.OperationalError):
        FactReadModel(conn).provenance_bundles(
            (publication.reported_facts[0].observation.observation_id,), cutoff=STAMP
        )


def test_nested_verification_scope_uses_complete_normalized_clock_keys(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )
    keys: list[tuple[str, datetime, datetime]] = []

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        keys.append((publication_id, cutoff, observed_through or cutoff))
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", counted)
    later = STAMP + timedelta(seconds=1)
    with source_publication.publication_verification_scope(conn):
        for observed in (None, STAMP, later, STAMP):
            with source_publication.publication_verification_scope(conn):
                source_publication.verify_source_fact_publication(
                    conn,
                    publication_id=publication.publication_id,
                    cutoff=STAMP,
                    observed_through=observed,
                )
    assert keys == [
        (publication.publication_id, STAMP, STAMP),
        (publication.publication_id, STAMP, later),
    ]
    source_publication.verify_source_fact_publication(
        conn,
        publication_id=publication.publication_id,
        cutoff=STAMP,
    )
    assert len(keys) == 3


def test_malformed_stored_observation_clock_is_typed_and_independent_publication_survives(
    conn: sqlite3.Connection,
) -> None:
    valid = _two_observation_publication()
    valid_id = valid.reported_facts[0].observation.observation_id
    # The source is independent of its derived output, not the reverse.
    broken = _derived_publication(valid_id)
    repository = SourceFactRepository(conn)
    repository.publish(valid)
    repository.publish(broken)
    expected_valid = FactReadModel(conn).provenance_bundle(valid_id, cutoff=STAMP)
    broken_id = broken.derived_facts[0].observation.observation_id
    conn.execute("DROP TRIGGER trg_fact_observations_v2_append_only")
    conn.execute(
        "UPDATE fact_observations_v2 SET effective_at='0000-01-01T00:00:00+00:00' WHERE observation_id=?",
        (broken_id,),
    )
    with pytest.raises(FactEvidenceIntegrityError, match="clock"):
        FactReadModel(conn).provenance_bundle(broken_id, cutoff=STAMP)
    reads = FactReadModel(conn).provenance_bundles(
        (broken_id, valid_id),
        cutoff=STAMP,
    )
    assert reads[0].bundle is None
    assert reads[0].failure is not None
    assert reads[0].failure.reason_code == "fact_evidence_malformed"
    assert reads[0].failure.record_kind == "fact_observation"
    assert reads[0].failure.record_id == broken_id
    assert reads[0].failure.disposition == "quarantined"
    assert reads[1].failure is None
    assert reads[1].bundle == expected_valid


@pytest.mark.parametrize("primitive", ["plane_clock", "plane_integer", "numeric"])
def test_stored_primitive_decode_failures_have_narrow_types(
    conn: sqlite3.Connection,
    primitive: str,
) -> None:
    from decimal import Decimal

    from provenance.fact_plane_v2 import FactPlaneDecodeError, FactPlaneV2

    if primitive == "plane_clock":
        decode: Callable[[object], datetime | None] = getattr(FactPlaneV2, "_as_datetime")
        with pytest.raises(FactPlaneDecodeError, match="clock"):
            decode("malformed-clock")
    elif primitive == "plane_integer":
        integer: Callable[[object], int] = getattr(FactPlaneV2, "_stored_integer")
        with pytest.raises(FactPlaneDecodeError, match="integer"):
            integer("malformed-integer")
    else:
        numeric: Callable[[str], Decimal] = getattr(FactReadModel, "_decimal")
        for raw in ("invalid-decimal", "NaN", "Infinity"):
            with pytest.raises(FactEvidenceIntegrityError, match="numeric"):
                numeric(raw)


@pytest.mark.parametrize("error", ["decode", "programmer", "database"])
def test_fact_plane_decode_boundary_preserves_operation_and_programmer_errors(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
) -> None:
    from provenance.fact_plane_v2 import FactPlaneDecodeError, FactPlaneV2

    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    failure = (
        FactPlaneDecodeError("malformed stored primitive")
        if error == "decode"
        else ValueError("programmer failure")
        if error == "programmer"
        else sqlite3.OperationalError("database failure")
    )

    def broken(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(FactPlaneV2, "as_reported", broken)
    expected = FactEvidenceIntegrityError if error == "decode" else type(failure)
    with pytest.raises(expected):
        FactReadModel(conn).cell(publication.reported_facts[0].cell.fact_cell_id, cutoff=STAMP)


def test_verification_scope_refuses_new_keys_at_limit_without_evicting(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    monkeypatch.setattr(
        source_publication, "MAX_SCOPED_PUBLICATION_VERIFICATIONS", 2, raising=False
    )
    original: Callable[..., VerifiedSourceFactPublication] = getattr(
        source_publication, "_verify_source_fact_publication"
    )
    calls: list[datetime] = []

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        calls.append(cutoff)
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(source_publication, "_verify_source_fact_publication", counted)
    with source_publication.publication_verification_scope(conn):
        for cutoff in (STAMP, STAMP + timedelta(seconds=1), STAMP):
            source_publication.verify_source_fact_publication(
                conn, publication_id=publication.publication_id, cutoff=cutoff
            )
        with pytest.raises(RuntimeError, match=r"verification.*limit"):
            source_publication.verify_source_fact_publication(
                conn, publication_id=publication.publication_id, cutoff=STAMP + timedelta(seconds=2)
            )
        source_publication.verify_source_fact_publication(
            conn, publication_id=publication.publication_id, cutoff=STAMP
        )
    assert calls == [STAMP, STAMP + timedelta(seconds=1)]


def test_cached_publication_failure_reuses_metadata_without_reusing_tracebacks(
    conn: sqlite3.Connection,
) -> None:
    from provenance.source_fact_publication import PublicationVerificationError

    errors: list[PublicationVerificationError] = []
    with source_publication.publication_verification_scope(conn):
        for _ in range(32):
            try:
                source_publication.verify_source_fact_publication(
                    conn, publication_id="missing-publication", cutoff=STAMP
                )
            except PublicationVerificationError as exc:
                errors.append(exc)
    assert len(errors) == 32
    assert len({id(exc) for exc in errors}) == len(errors)
    assert {exc.reason_code for exc in errors} == {"publication_graph_missing"}


@pytest.mark.parametrize(
    ("value", "reader_iso", "publication_iso"),
    [
        (datetime(2026, 7, 27, 12), "2026-07-27T12:00:00", "2026-07-27T12:00:00+00:00"),
        (
            datetime(2026, 7, 27, 12, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            "2026-07-27T12:00:00+05:30",
            "2026-07-27T06:30:00+00:00",
        ),
        ("2026-07-27T08:00:00-04:00", "2026-07-27T08:00:00-04:00", "2026-07-27T12:00:00+00:00"),
        ("2026-07-27T12:00:00Z", "2026-07-27T12:00:00+00:00", "2026-07-27T12:00:00+00:00"),
        ("2026-07-27", "2026-07-27T00:00:00", "2026-07-27T00:00:00+00:00"),
    ],
)
def test_domain_clock_parsers_preserve_pre_quarantine_valid_results(
    value: object, reader_iso: str, publication_iso: str
) -> None:
    """Pin c978523d results: the reader keeps offsets; publications normalize UTC."""
    reader_clock: Callable[[object], datetime] = getattr(FactReadModel, "_datetime")
    publication_clock: Callable[[object], datetime] = getattr(source_publication, "_datetime")
    assert reader_clock(value).isoformat() == reader_iso
    assert publication_clock(value).isoformat() == publication_iso
    assert publication_clock(value).tzinfo is UTC


@pytest.mark.parametrize("value", ["private-malformed-clock", "0000-01-01T00:00:00+00:00"])
def test_domain_clock_failures_are_typed_without_raw_values(value: str) -> None:
    reader_clock: Callable[[object], datetime] = getattr(FactReadModel, "_datetime")
    publication_clock: Callable[[object], datetime] = getattr(source_publication, "_datetime")
    publication_error: type[ValueError] = getattr(
        source_publication, "_PublicationValueIntegrityError"
    )
    with pytest.raises(
        FactEvidenceIntegrityError, match=r"^stored fact clock is malformed$"
    ) as reader:
        reader_clock(value)
    with pytest.raises(publication_error, match=r"^invalid publication clock$") as publication:
        publication_clock(value)
    assert type(reader.value) is FactEvidenceIntegrityError
    assert type(publication.value) is publication_error
    assert value not in str(reader.value)
    assert value not in str(publication.value)


def test_malformed_publication_clock_is_quarantined_by_real_verifier(
    conn: sqlite3.Connection,
) -> None:
    publication = _two_observation_publication()
    SourceFactRepository(conn).publish(publication)
    conn.execute("DROP TRIGGER trg_source_fact_publications_append_only")
    conn.execute(
        "UPDATE source_fact_publications SET created_at='0000-01-01T00:00:00+00:00' "
        "WHERE publication_id=?",
        (publication.publication_id,),
    )
    with pytest.raises(source_publication.PublicationVerificationError) as error:
        source_publication.verify_source_fact_publication(
            conn, publication_id=publication.publication_id, cutoff=STAMP
        )
    assert error.value.reason_code == "publication_clock_tampered"
    assert error.value.disposition == "quarantined"
    assert "0000-01-01T00:00:00+00:00" not in str(error.value)
    reads = FactReadModel(conn).provenance_bundles(
        tuple(item.observation.observation_id for item in publication.reported_facts), cutoff=STAMP
    )
    assert all(read.bundle is None for read in reads)
    assert all(
        read.failure and read.failure.reason_code == "publication_clock_tampered" for read in reads
    )
    assert all(read.failure and read.failure.disposition == "quarantined" for read in reads)


def test_publication_scope_borrows_protected_snapshot_without_transaction_commands() -> None:
    from sqlite_runtime import ManagedConnection

    class ProtectedConnection(ManagedConnection):
        @property
        def protected_read_snapshot_active(self) -> bool:
            return self.in_transaction

    controls: list[int] = []

    def authorizer(
        action: int,
        _first: str | None,
        _second: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
            controls.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection = sqlite3.connect(":memory:", factory=ProtectedConnection)
    try:
        connection.execute("BEGIN")
        connection.set_authorizer(authorizer)
        with source_publication.publication_verification_scope(connection):
            assert connection.execute("SELECT 1").fetchone() == (1,)
        assert connection.in_transaction
        assert controls == []
    finally:
        connection.set_authorizer(None)
        connection.rollback()
        connection.close()
