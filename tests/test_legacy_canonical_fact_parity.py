from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from provenance.legacy_canonical_parity import (
    LegacyFactCursor,
    ParityDisposition,
    ParityRequest,
    ProjectionCoordinate,
    run_legacy_canonical_parity,
)
from provenance.population_completeness import PopulationTemporalScope

NOW = datetime(2026, 1, 15, tzinfo=UTC)
OBSERVED = datetime(2026, 1, 20, tzinfo=UTC)
PAST = "2026-01-01T00:00:00Z"
LATE_RECORDED = "2026-01-18T00:00:00Z"
FUTURE = "2026-02-01T00:00:00Z"
HASH = "a" * 64


class FakeProjectionReader:
    def __init__(self, coordinates: Sequence[ProjectionCoordinate]) -> None:
        self.coordinates = {item.canonical_metric_cell_id: item for item in coordinates}

    def read_coordinates(
        self,
        *,
        generation_id: str,
        canonical_metric_cell_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> Mapping[str, ProjectionCoordinate]:
        del cutoff_at
        return {
            key: value
            for key in canonical_metric_cell_ids
            if (value := self.coordinates.get(key)) is not None
            and value.generation_id == generation_id
        }

    def read_coordinate_page(
        self,
        *,
        generation_id: str,
        after_coordinate: str | None,
        limit: int,
        cutoff_at: datetime,
    ) -> Sequence[ProjectionCoordinate]:
        del cutoff_at
        return tuple(
            item
            for key, item in sorted(self.coordinates.items())
            if item.generation_id == generation_id
            and (after_coordinate is None or key > after_coordinate)
        )[:limit]


def _projection(
    coordinate: str,
    **overrides: object,
) -> ProjectionCoordinate:
    values: dict[str, object] = {
        "value": "100",
        "value_kind": "numeric",
        "period_end": "2025-12-31T00:00:00Z",
        "unit": "USD",
        "currency": "USD",
        "change_kind": "upsert",
    }
    values.update(overrides)
    change_kind = values["change_kind"]
    return ProjectionCoordinate.model_validate(
        {
            "generation_id": "generation-1",
            "canonical_metric_cell_id": coordinate,
            "change_kind": change_kind,
            "audit_verified": True,
            "canonical_resolution_revision_id": (
                f"resolution-{coordinate}" if change_kind == "upsert" else None
            ),
            "selected_observation_id": (f"v2-{coordinate}" if change_kind == "upsert" else None),
            "value_kind": (values["value_kind"] if change_kind == "upsert" else None),
            "canonical_value": (values["value"] if change_kind == "upsert" else None),
            "period_end": (values["period_end"] if change_kind == "upsert" else None),
            "unit_key": values["unit"] if change_kind == "upsert" else None,
            "currency": (values["currency"] if change_kind == "upsert" else None),
        }
    )


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "parity.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY,fetched_at TEXT);
        CREATE TABLE financial_facts (
          id INTEGER PRIMARY KEY,ticker TEXT,source_doc_id INTEGER
        );
        CREATE TABLE kpi_facts (
          id INTEGER PRIMARY KEY, computed_from TEXT, formula_id TEXT,
          formula_version TEXT, extracted_by TEXT,ticker TEXT,source_doc_id INTEGER
        );
        CREATE TABLE fact_observation_revisions (
          fact_table TEXT, fact_row_id INTEGER, fact_revision INTEGER,
          observation_id TEXT, logical_key TEXT, captured_at TEXT
        );
        CREATE TABLE observation_resolution_revisions (
          resolution_id TEXT, logical_key TEXT, revision INTEGER,
          selected_observation_id TEXT, knowledge_cutoff TEXT, recorded_at TEXT
        );
        CREATE TABLE fact_resolution_outcomes (
          resolution_id TEXT, resolution_status TEXT
        );
        CREATE TABLE legacy_fact_evidence_match_revisions (
          match_revision_id TEXT, fact_table TEXT, fact_row_id INTEGER,
          issuer_id TEXT, revision INTEGER, fact_payload_json TEXT,
          legacy_binding_revision_id TEXT, legacy_binding_revision INTEGER,
          binding_scope_content_sha256 TEXT, evidence_node_id TEXT,
          matched_entry_sha256 TEXT, candidate_count INTEGER,
          matched_candidate_count INTEGER, outcome TEXT,
          knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE legacy_document_evidence_binding_revisions (
          binding_revision_id TEXT, legacy_document_id INTEGER, revision INTEGER,
          evidence_node_id TEXT, scope_content_sha256 TEXT,
          knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE reporting_entities (
          reporting_entity_id TEXT, issuer_id TEXT
        );
        CREATE TABLE fact_cells_v2 (
          fact_cell_id TEXT, reporting_entity_id TEXT
        );
        CREATE TABLE fact_observations_v2 (
          observation_id TEXT, fact_cell_id TEXT, legacy_match_revision_id TEXT,
          evidence_node_id TEXT, source_entry_sha256 TEXT,
          knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE fact_cell_canonical_binding_revisions (
          binding_revision_id TEXT, source_observation_id TEXT, revision INTEGER,
          binding_status TEXT, canonical_metric_cell_id TEXT,
          knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE canonical_fact_resolution_revisions (
          canonical_resolution_revision_id TEXT,
          canonical_metric_cell_id TEXT, revision INTEGER, status TEXT,
          selected_observation_id TEXT, knowledge_at TEXT, recorded_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO reporting_entities VALUES ('entity-1','issuer-1')")
    conn.commit()
    conn.close()
    return path


def _seed(
    path: Path,
    row_id: int = 1,
    *,
    table: str = "financial_facts",
    coordinate: str | None = None,
    legacy_resolved: bool = True,
    match_outcome: str = "accepted",
    candidate_count: int = 1,
    matched_candidate_count: int = 1,
    bridge: bool = True,
    binding_status: str = "bound",
    canonical_status: str = "resolved",
    knowledge_at: str = PAST,
    recorded_at: str | None = None,
) -> str:
    recorded_at = recorded_at or knowledge_at
    coordinate = coordinate or f"coordinate-{row_id}"
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO documents VALUES (?,?)", (row_id, knowledge_at))
    if table == "financial_facts":
        conn.execute(
            "INSERT INTO financial_facts VALUES (?,'OLD',?)",
            (row_id, row_id),
        )
    else:
        conn.execute(
            "INSERT INTO kpi_facts VALUES (?,NULL,NULL,NULL,'reported','OLD',?)",
            (row_id, row_id),
        )
    logical_key = f"logical-{table}-{row_id}"
    old_observation = f"old-{table}-{row_id}"
    conn.execute(
        "INSERT INTO fact_observation_revisions VALUES (?,?,?,?,?,?)",
        (table, row_id, 1, old_observation, logical_key, knowledge_at),
    )
    if legacy_resolved:
        resolution_id = f"old-resolution-{table}-{row_id}"
        conn.execute(
            "INSERT INTO observation_resolution_revisions VALUES (?,?,?,?,?,?)",
            (
                resolution_id,
                logical_key,
                1,
                old_observation,
                knowledge_at,
                recorded_at,
            ),
        )
        conn.execute(
            "INSERT INTO fact_resolution_outcomes VALUES (?, 'resolved')",
            (resolution_id,),
        )
    match_id = f"match-{table}-{row_id}"
    payload = {
        "value": "100.00",
        "period_end": "2025-12-31",
        "unit": "USD",
        "currency": "USD" if table == "financial_facts" else None,
        "source_doc_id": row_id,
    }
    binding_id = f"document-binding-{table}-{row_id}"
    conn.execute(
        "INSERT INTO legacy_fact_evidence_match_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            match_id,
            table,
            row_id,
            "issuer-1",
            1,
            json.dumps(payload),
            binding_id,
            1,
            HASH,
            f"node-{row_id}",
            f"entry-{row_id}",
            candidate_count,
            matched_candidate_count,
            match_outcome,
            knowledge_at,
            recorded_at,
        ),
    )
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions VALUES (?,?,?,?,?,?,?)",
        (binding_id, row_id, 1, f"node-{row_id}", HASH, knowledge_at, recorded_at),
    )
    observation_id = f"v2-{coordinate}"
    if bridge:
        fact_cell_id = f"source-cell-{table}-{row_id}"
        conn.execute(
            "INSERT INTO fact_cells_v2 VALUES (?, 'entity-1')",
            (fact_cell_id,),
        )
        conn.execute(
            "INSERT INTO fact_observations_v2 VALUES (?,?,?,?,?,?,?)",
            (
                observation_id,
                fact_cell_id,
                match_id,
                f"node-{row_id}",
                f"entry-{row_id}",
                knowledge_at,
                recorded_at,
            ),
        )
        conn.execute(
            "INSERT INTO fact_cell_canonical_binding_revisions VALUES (?,?,?,?,?,?,?)",
            (
                f"ontology-{table}-{row_id}",
                observation_id,
                1,
                binding_status,
                coordinate if binding_status != "quarantined" else None,
                knowledge_at,
                recorded_at,
            ),
        )
        if binding_status == "bound":
            conn.execute(
                "INSERT INTO canonical_fact_resolution_revisions VALUES (?,?,?,?,?,?,?)",
                (
                    f"resolution-{coordinate}",
                    coordinate,
                    1,
                    canonical_status,
                    observation_id if canonical_status == "resolved" else None,
                    knowledge_at,
                    recorded_at,
                ),
            )
    conn.commit()
    conn.close()
    return coordinate


def _run(
    path: Path,
    coordinates: Sequence[ProjectionCoordinate],
    **request_overrides: object,
):
    request = ParityRequest.model_validate(
        {
            "temporal_scope": {
                "knowledge_cutoff": NOW,
                "observed_through": OBSERVED,
            },
            "issuer_id": "issuer-1",
            "projection_generation_id": "generation-1",
            **request_overrides,
        }
    )
    return run_legacy_canonical_parity(path, request, FakeProjectionReader(coordinates))


def test_equal_and_canonical_only_native_are_cutover_ready(tmp_path: Path) -> None:
    path = _database(tmp_path)
    coordinate = _seed(path)
    report = _run(path, [_projection(coordinate), _projection("native-only")])

    assert report.complete
    assert report.cutover_ready
    assert [row.disposition for row in report.rows] == [
        ParityDisposition.EQUAL,
        ParityDisposition.CANONICAL_ONLY_NATIVE,
    ]
    assert report.comparable_rows == 1
    assert report.canonical_coordinates_scanned == 2


@pytest.mark.parametrize(
    ("overrides", "expected", "diff_field"),
    [
        ({"value": "101"}, ParityDisposition.VALUE_MISMATCH, "value"),
        (
            {"period_end": "2025-12-30T00:00:00Z"},
            ParityDisposition.PERIOD_MISMATCH,
            "period_end",
        ),
        ({"unit": "shares"}, ParityDisposition.UNIT_MISMATCH, "unit"),
        ({"currency": "EUR"}, ParityDisposition.CURRENCY_MISMATCH, "currency"),
        (
            {"value_kind": "text", "value": "100"},
            ParityDisposition.VALUE_KIND_MISMATCH,
            "value_kind",
        ),
    ],
)
def test_field_level_exact_mismatches(
    tmp_path: Path,
    overrides: dict[str, object],
    expected: ParityDisposition,
    diff_field: str,
) -> None:
    path = _database(tmp_path)
    coordinate = _seed(path)
    report = _run(path, [_projection(coordinate, **overrides)])

    assert report.rows[0].disposition is expected
    assert diff_field in {diff.field for diff in report.rows[0].field_diffs}
    assert report.mismatch_rows == 1
    assert not report.cutover_ready


def test_unresolved_and_terminal_ambiguous_are_distinct(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _seed(path, 1, legacy_resolved=False)
    _seed(
        path,
        2,
        match_outcome="terminal",
        candidate_count=2,
        matched_candidate_count=2,
    )
    report = _run(path, [])

    assert [row.disposition for row in report.rows] == [
        ParityDisposition.LEGACY_UNRESOLVED_EXCLUDED,
        ParityDisposition.LEGACY_MATCH_TERMINAL_AMBIGUOUS,
    ]


def test_missing_bridge_quarantine_and_tombstone(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _seed(path, 1, bridge=False)
    _seed(path, 2, binding_status="quarantined")
    coordinate = _seed(path, 3)
    report = _run(path, [_projection(coordinate, change_kind="tombstone")])

    assert [row.disposition for row in report.rows] == [
        ParityDisposition.V2_BRIDGE_MISSING,
        ParityDisposition.ONTOLOGY_BINDING_QUARANTINED,
        ParityDisposition.CANONICAL_TOMBSTONED,
    ]


def test_many_legacy_rows_to_one_coordinate_blocks_both_rows(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    coordinate = "shared-coordinate"
    _seed(path, 1, coordinate=coordinate)
    _seed(path, 2, coordinate=coordinate)
    report = _run(path, [_projection(coordinate)])

    assert {row.disposition for row in report.rows} == {
        ParityDisposition.MULTIPLE_LEGACY_ROWS_TO_ONE_COORDINATE
    }
    assert report.blocking_legacy_rows == 2


def test_cutoff_uses_prior_as_known_match_revision(tmp_path: Path) -> None:
    path = _database(tmp_path)
    coordinate = _seed(path)
    conn = sqlite3.connect(path)
    payload = json.dumps(
        {
            "value": "100",
            "period_end": "2025-12-31",
            "unit": "USD",
            "currency": "USD",
            "source_doc_id": 1,
        }
    )
    conn.execute(
        "INSERT INTO legacy_fact_evidence_match_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "future-match",
            "financial_facts",
            1,
            "issuer-1",
            2,
            payload,
            "document-binding-financial_facts-1",
            1,
            HASH,
            "node-1",
            "entry-1",
            2,
            2,
            "terminal",
            FUTURE,
            FUTURE,
        ),
    )
    conn.commit()
    conn.close()

    report = _run(path, [_projection(coordinate)])
    assert report.rows[0].disposition is ParityDisposition.EQUAL
    assert report.rows[0].match_revision_id == "match-financial_facts-1"


def test_issuer_scope_excludes_fact_superseded_by_another_issuer(tmp_path: Path) -> None:
    path = _database(tmp_path)
    coordinate = _seed(path)
    conn = sqlite3.connect(path)
    prior = conn.execute(
        "SELECT fact_payload_json,legacy_binding_revision_id,"
        "legacy_binding_revision,binding_scope_content_sha256,evidence_node_id,"
        "matched_entry_sha256,candidate_count,matched_candidate_count,outcome "
        "FROM legacy_fact_evidence_match_revisions "
        "WHERE match_revision_id='match-financial_facts-1'"
    ).fetchone()
    assert prior is not None
    conn.execute(
        "INSERT INTO legacy_fact_evidence_match_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "newer-other-issuer-match",
            "financial_facts",
            1,
            "issuer-2",
            2,
            *prior,
            PAST,
            PAST,
        ),
    )
    conn.commit()
    conn.close()

    report = _run(
        path,
        [_projection(coordinate)],
        issuer_id="issuer-1",
    )

    assert report.legacy_rows_scanned == 0
    assert report.rows[0].disposition is ParityDisposition.CANONICAL_ONLY_NATIVE


def test_keyset_pagination_and_explicit_truncation(tmp_path: Path) -> None:
    path = _database(tmp_path)
    for row_id in range(1, 4):
        _seed(path, row_id)
    projections = [_projection(f"coordinate-{row_id}") for row_id in range(1, 4)]

    first = _run(path, projections, page_size=1, max_pages=1, max_rows=10)
    assert first.truncated
    assert first.next_cursor == LegacyFactCursor(
        issuer_id="issuer-1",
        temporal_scope=PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=OBSERVED,
        ),
        projection_generation_id="generation-1",
        fact_table_rank=0,
        fact_row_id=1,
    )
    assert first.legacy_rows_scanned == 1
    assert not first.cutover_ready

    second = _run(
        path,
        projections,
        page_size=2,
        max_pages=1,
        max_rows=2,
        after=first.next_cursor,
    )
    assert [row.fact_row_id for row in second.rows] == [2, 3]
    assert second.pages_scanned == 1
    assert not second.complete


def test_timezone_naive_cutoff_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ParityRequest(
            temporal_scope=PopulationTemporalScope(
                knowledge_cutoff=datetime(2026, 1, 1),
                observed_through=OBSERVED,
            ),
            issuer_id="issuer-1",
            projection_generation_id="generation-1",
        )


def test_late_recorded_evidence_is_visible_through_observation_clock(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    coordinate = _seed(
        path,
        knowledge_at=PAST,
        recorded_at=LATE_RECORDED,
    )

    report = _run(path, [_projection(coordinate)])

    assert report.knowledge_cutoff == NOW
    assert report.observed_through == OBSERVED
    assert report.equal_rows == 1
    assert report.cutover_ready


def test_legacy_pages_are_bounded_and_cursor_is_scope_bound(tmp_path: Path) -> None:
    path = _database(tmp_path)
    for row_id in range(1, 6):
        _seed(path, row_id)
    reader = FakeProjectionReader([_projection(f"coordinate-{row_id}") for row_id in range(1, 6)])

    first = run_legacy_canonical_parity(
        path,
        ParityRequest(
            temporal_scope=PopulationTemporalScope(
                knowledge_cutoff=NOW,
                observed_through=OBSERVED,
            ),
            issuer_id="issuer-1",
            projection_generation_id="generation-1",
            page_size=2,
            max_pages=1,
            max_rows=10,
        ),
        reader,
    )

    assert first.legacy_rows_scanned == 2
    assert [row.fact_row_id for row in first.rows] == [1, 2]
    assert first.next_cursor is not None
    assert first.next_cursor == LegacyFactCursor(
        issuer_id="issuer-1",
        temporal_scope=PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=OBSERVED,
        ),
        projection_generation_id="generation-1",
        fact_table_rank=0,
        fact_row_id=2,
    )
    assert (
        LegacyFactCursor.model_validate_json(first.next_cursor.model_dump_json())
        == first.next_cursor
    )
    with pytest.raises(ValueError, match="cursor issuer"):
        ParityRequest(
            temporal_scope=PopulationTemporalScope(
                knowledge_cutoff=NOW,
                observed_through=OBSERVED,
            ),
            issuer_id="issuer-2",
            projection_generation_id="generation-1",
            after=first.next_cursor,
        )
    with pytest.raises(ValueError, match="cursor temporal scope"):
        ParityRequest(
            temporal_scope=PopulationTemporalScope(
                knowledge_cutoff=NOW,
                observed_through=datetime(2026, 1, 21, tzinfo=UTC),
            ),
            issuer_id="issuer-1",
            projection_generation_id="generation-1",
            after=first.next_cursor,
        )

    one_row_pages = _run(
        path,
        list(reader.coordinates.values()),
        page_size=1,
        max_pages=10,
    )
    five_row_page = _run(
        path,
        list(reader.coordinates.values()),
        page_size=5,
        max_pages=2,
    )
    assert one_row_pages.complete
    assert five_row_page.complete
    assert one_row_pages.legacy_fact_universe_sha256 == five_row_page.legacy_fact_universe_sha256
    assert one_row_pages.parity_rows_sha256 == five_row_page.parity_rows_sha256
