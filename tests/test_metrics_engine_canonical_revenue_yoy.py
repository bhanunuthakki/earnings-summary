"""Public canonical revenue-YoY computation and read-path coverage."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from compute.metrics_engine.io import compute_for_ticker, read_attempt_result
from compute.metrics_engine.registry import ReasonCode
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivedFactObservationV2,
    ExtractionRunCompletenessSealV2,
    FactPlaneV2,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from tests import test_source_fact_repository as foundation
from tests.test_report_canonical_financials import STAMP, seed_table
from timeseries.loaders import load_kpi_series_with_provenance


@pytest.fixture(scope="module")
def head_template(
    tmp_path_factory: pytest.TempPathFactory,
    migrated_db: Callable[..., Path],
) -> Path:
    path = tmp_path_factory.mktemp("metrics-yoy-head") / "head.db"
    return migrated_db(path)


@pytest.fixture
def conn(head_template: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "portfolio.db"
    shutil.copy(head_template, path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    foundation.seed_foundation(connection)
    yield connection
    connection.close()


def _revenue_rows(
    values: tuple[str, ...] = ("100", "100", "100", "100", "110"),
) -> list[tuple[str, str, str, str, str, str]]:
    periods = (
        ("2025-01-01", "2025-03-31", "Q1"),
        ("2025-04-01", "2025-06-30", "Q2"),
        ("2025-07-01", "2025-09-30", "Q3"),
        ("2025-10-01", "2025-12-31", "Q4"),
        ("2026-01-01", "2026-03-31", "Q1"),
    )
    return [
        ("revenue", start, end, fiscal, value, "USD")
        for (start, end, fiscal), value in zip(periods, values, strict=True)
    ]


def _attempts(conn: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(
        conn.execute(
            "SELECT attempt.* FROM metric_computation_attempts AS attempt "
            "JOIN formula_definitions AS definition ON definition.id=attempt.formula_id "
            "WHERE attempt.ticker='SYNTH' AND definition.formula_key='revenue_yoy' "
            "ORDER BY attempt.period_end"
        ).fetchall()
    )


def _seed_filing_bridge(
    conn: sqlite3.Connection,
    *,
    rows: list[tuple[str, str, str, str, str, str]],
    publication_prefix: str,
) -> int:
    document_id = conn.execute(
        "INSERT INTO documents(ticker,source_type,doc_type,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size,source_quality_tier) VALUES "
        "('SYNTH','sec_xbrl','10-q',?, ?,?,'ok',1,'sec_official')",
        (f"{publication_prefix}.html", publication_prefix[0] * 64, STAMP),
    ).lastrowid
    assert document_id is not None
    source_id = f"{publication_prefix}-source"
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "SELECT ?,?,'sec_filing',source_url,blob_sha256,source_published_at,filing_at,"
        "accepted_at,observed_at,retrieved_at,retrieval_config_sha256,collector_code_version "
        "FROM evidence_source_observations WHERE observation_id='source-1'",
        (source_id, source_id),
    )
    seed_table(
        conn,
        rows,
        publication_prefix=publication_prefix,
        legacy_document_id=int(document_id),
        source_observation_id=source_id,
        legacy_scope_node=True,
    )
    document_version_id = f"{publication_prefix}-document"
    node_id = f"node-{publication_prefix}-0"
    locator = '{"path":"/facts/0/value"}'
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions "
        "(binding_revision_id,idempotency_key,legacy_document_id,revision,"
        "document_version_id,evidence_node_id,scope_locator_json,scope_locator_sha256,"
        "scope_content_sha256,effective_at,knowledge_at,recorded_at,"
        "supersedes_binding_revision_id) VALUES (?,?,?,1,?,?,?,?,?,?,?,?,NULL)",
        (
            f"{publication_prefix}-legacy-binding",
            f"{publication_prefix}-legacy-binding",
            int(document_id),
            document_version_id,
            node_id,
            locator,
            foundation.sha256(locator),
            foundation.sha256(f"{publication_prefix}-scope"),
            STAMP,
            STAMP,
            STAMP,
        ),
    )
    return int(document_id)


def test_public_compute_publishes_and_reads_canonical_revenue_yoy(
    conn: sqlite3.Connection,
) -> None:
    seed_table(conn, _revenue_rows())

    summary = compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)

    assert summary.computed_ok == 1
    attempt = conn.execute(
        "SELECT attempt.* FROM metric_computation_attempts AS attempt "
        "JOIN formula_definitions AS definition ON definition.id=attempt.formula_id "
        "WHERE attempt.ticker='SYNTH' AND definition.formula_key='revenue_yoy' "
        "AND attempt.status='ok'"
    ).fetchone()
    assert attempt is not None
    assert attempt["output_observation_id"] is not None
    assert attempt["kpi_fact_id"] is None

    result = read_attempt_result(
        conn,
        "SYNTH",
        "revenue_yoy",
        read_cutoff=datetime.now(UTC),
    )
    assert result is not None
    assert result.status == "ok"
    assert result.value == Decimal("10.0")
    assert result.provenance is not None
    assert result.provenance.cell.concept_namespace == (
        "urn:earnings-summary:derived:metrics-engine"
    )
    assert result.provenance.cell.concept_name == "revenue_yoy"
    assert result.provenance.cell.taxonomy_version == "formula-v1"
    assert result.provenance.observation.observation_kind == "derived"
    assert result.provenance.derivation is not None
    assert result.provenance.derivation.input_observation_ids == (
        "observation-report-4",
        "observation-report-0",
    )
    assert result.provenance.derivation.input_resolution_revision_ids == (None, None)
    assert all(result.provenance.derivation.input_canonical_resolution_revision_ids)
    assert result.provenance.derivation.knowledge_cutoff == STAMP
    assert result.provenance.derivation.recorded_at > STAMP
    findings = audit_connection(conn, AuditOptions(deep_sqlite_checks=False)).findings
    assert not any(
        finding.code
        in {
            "FACT_PLANE_V2_DERIVATION_INPUT_DIGEST_MISMATCH",
            "FACT_PLANE_V2_DERIVATION_NO_LOOKAHEAD",
        }
        and finding.count
        for finding in findings
    )


def test_no_canonical_candidate_ignores_tempting_legacy_revenue(
    conn: sqlite3.Connection,
) -> None:
    document_id = _seed_filing_bridge(
        conn,
        rows=[("operating_cash_flow", "2026-01-01", "2026-03-31", "Q1", "10", "USD")],
        publication_prefix="tempting",
    )
    conn.execute(
        "INSERT INTO financial_facts(ticker,period_end,fiscal_period_type,line_item,"
        "value,currency,unit,source_doc_id) VALUES "
        "('SYNTH','2026-03-31','Q1','revenue',999,'USD','USD',?)",
        (document_id,),
    )

    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)

    assert _attempts(conn) == ()


def test_rejected_current_coordinate_records_exact_missing_input(
    conn: sqlite3.Connection,
) -> None:
    rows = _revenue_rows()
    rows[-1] = ("revenue", "2026-03-01", "2026-03-31", "Q1", "110", "USD")
    seed_table(conn, rows)

    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)

    current = _attempts(conn)[-1]
    assert current["status"] == "not_computable"
    assert current["reason_code"] == ReasonCode.MISSING_INPUT.value
    assert current["reason_detail"] == (
        "canonical_current_rejected:unsupported_financial_cadence_or_duration"
    )
    assert current["output_observation_id"] is None


@pytest.mark.parametrize("prior", ["0", "-1"])
def test_nonpositive_prior_retains_denominator_reason(
    conn: sqlite3.Connection,
    prior: str,
) -> None:
    seed_table(conn, _revenue_rows((prior, "100", "100", "100", "110")))

    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)

    current = _attempts(conn)[-1]
    assert current["status"] == "not_computable"
    assert current["reason_code"] == ReasonCode.DENOMINATOR_LE_ZERO.value


def test_mixed_currency_fails_before_formula_math(conn: sqlite3.Connection) -> None:
    seed_table(conn, _revenue_rows(), currencies={4: "EUR"})

    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)

    current = _attempts(conn)[-1]
    assert current["status"] == "not_computable"
    assert current["reason_code"] == ReasonCode.MISSING_INPUT.value
    assert current["reason_detail"] == "canonical_prior_coordinate_incomparable"


def test_same_cutoff_replay_and_force_reuse_exact_sealed_output(
    conn: sqlite3.Connection,
) -> None:
    seed_table(conn, _revenue_rows())
    first = compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)
    first_output = str(_attempts(conn)[-1]["output_observation_id"])
    first_recorded_at = conn.execute(
        "SELECT recorded_at FROM fact_observations_v2 WHERE observation_id=?",
        (first_output,),
    ).fetchone()[0]

    replay = compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)
    forced = compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP, force=True)

    assert first.computed_ok == 1
    assert replay.attempts_written == 0 and replay.skipped_unchanged == 5
    assert forced.computed_ok == 1
    assert str(_attempts(conn)[-1]["output_observation_id"]) == first_output
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM fact_observations_v2 WHERE observation_id LIKE "
            "'metrics-engine-observation-%'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT recorded_at FROM fact_observations_v2 WHERE observation_id=?",
            (first_output,),
        ).fetchone()[0]
        == first_recorded_at
    )


def test_later_restatement_creates_new_output_and_preserves_prior_publication(
    conn: sqlite3.Connection,
) -> None:
    facts = seed_table(conn, _revenue_rows())
    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)
    first_output = str(_attempts(conn)[-1]["output_observation_id"])
    first = read_attempt_result(conn, "SYNTH", "revenue_yoy", read_cutoff=datetime.now(UTC))
    assert first is not None and first.value == Decimal("10.0")

    revised_at = STAMP + timedelta(days=1)
    locator = CanonicalJSONObject.model_validate({"path": "/restated/current-revenue"})
    locator_json = locator.model_dump_json()
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "SELECT 'metrics-restated-run','metrics-restated-run','report-document',"
        "input_sha256,extractor_name,extractor_config_sha256,'restatement-v2',"
        "output_sha256,?,?,outcome FROM evidence_extraction_runs "
        "WHERE extraction_run_id='run-1'",
        (revised_at, revised_at),
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES "
        "('metrics-restated-node','metrics-restated-node',1,'metrics-restated-run',"
        "NULL,NULL,'table_cell','120',?,?,?)",
        (locator_json, foundation.sha256(locator_json), revised_at),
    )
    original = facts[-1]
    restated = foundation.make_report(
        original.cell,
        "metrics-restated",
        numeric_value="120",
        at=revised_at,
    ).model_copy(
        update={
            "revision_kind": "restatement",
            "supersedes_observation_id": original.observation.observation_id,
            "document_version_id": "report-document",
            "evidence_node_id": "metrics-restated-node",
            "source_locator": locator,
            "source_locator_sha256": foundation.sha256(locator_json),
        }
    )
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="metrics-restated-publication",
            idempotency_key="metrics-restated-publication",
            reported_facts=(ReportedSourceFact(cell=original.cell, observation=restated),),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="metrics-restated-seal",
                    idempotency_key="metrics-restated-seal",
                    extraction_run_id="metrics-restated-run",
                    expected_node_count=1,
                    completeness_policy_name="all-run-nodes",
                    completeness_policy_version="v1",
                    completeness_policy_sha256=foundation.sha256("report-completeness"),
                    knowledge_at=revised_at,
                    recorded_at=revised_at,
                ),
            ),
        )
    )
    populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=revised_at,
            operation_recorded_at=revised_at,
            apply=True,
        ),
    )
    populate_canonical_resolution(
        conn,
        CanonicalResolutionPopulationRequest(
            cutoff_at=revised_at,
            operation_recorded_at=revised_at,
            apply=True,
        ),
    )

    compute_for_ticker(conn, "SYNTH", source_cutoff=revised_at)

    second_output = str(_attempts(conn)[-1]["output_observation_id"])
    second = read_attempt_result(conn, "SYNTH", "revenue_yoy", read_cutoff=datetime.now(UTC))
    assert second is not None and second.value == Decimal("20.0")
    assert second_output != first_output
    assert (
        conn.execute(
            "SELECT supersedes_observation_id FROM fact_observations_v2 WHERE observation_id=?",
            (second_output,),
        ).fetchone()[0]
        == first_output
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM source_fact_publications "
            "WHERE publication_id LIKE 'metrics-engine-publication-%'"
        ).fetchone()[0]
        == 2
    )


def test_bridge_projects_kpi_and_timeseries_with_full_canonical_locator(
    conn: sqlite3.Connection,
) -> None:
    _seed_filing_bridge(
        conn,
        rows=_revenue_rows(),
        publication_prefix="bridge",
    )

    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)

    attempt = _attempts(conn)[-1]
    assert attempt["output_observation_id"] is not None
    assert attempt["kpi_fact_id"] is not None
    locator = conn.execute(
        "SELECT locator FROM kpi_facts WHERE id=?", (attempt["kpi_fact_id"],)
    ).fetchone()[0]
    assert "resolution_revision_id" in str(locator)
    conn.commit()
    database_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    series = load_kpi_series_with_provenance("SYNTH", "revenue_yoy", db_path=database_path)
    assert series[-1].value == 10.0


def test_invalid_output_id_never_falls_back_to_legacy_kpi(
    conn: sqlite3.Connection,
) -> None:
    _seed_filing_bridge(
        conn,
        rows=_revenue_rows(),
        publication_prefix="fallback",
    )
    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)
    attempt = _attempts(conn)[-1]
    assert attempt["kpi_fact_id"] is not None
    conn.execute(
        "UPDATE metric_computation_attempts SET output_observation_id=? WHERE id=?",
        ("observation-report-4", attempt["id"]),
    )

    result = read_attempt_result(conn, "SYNTH", "revenue_yoy", read_cutoff=datetime.now(UTC))

    assert result is not None and result.status == "unavailable"
    assert result.reason_code == "canonical_output_unavailable"


def test_direct_unsealed_output_never_satisfies_public_attempt_reader(
    conn: sqlite3.Connection,
) -> None:
    seed_table(conn, _revenue_rows())
    compute_for_ticker(conn, "SYNTH", source_cutoff=STAMP)
    admitted = read_attempt_result(conn, "SYNTH", "revenue_yoy", read_cutoff=datetime.now(UTC))
    assert admitted is not None and admitted.provenance is not None
    source = admitted.provenance.observation
    assert admitted.provenance.derivation is not None
    direct = DerivedFactObservationV2(
        observation_id="direct-unsealed-revenue-yoy",
        idempotency_key="direct-unsealed-revenue-yoy",
        fact_cell_id=source.fact_cell_id,
        observation_kind="derived",
        value_kind=source.value_kind,
        numeric_value=str(source.decimal_value),
        text_value=source.text_value,
        is_nil=source.is_nil,
        raw_lexical_value=source.raw_lexical_value,
        method_name=source.method_name,
        method_version=source.method_version,
        method_config_sha256=source.method_config_sha256,
        revision_kind="initial",
        effective_at=source.effective_at,
        knowledge_at=source.knowledge_at,
        recorded_at=source.recorded_at,
        formula_id=admitted.provenance.derivation.formula_id,
        formula_version=admitted.provenance.derivation.formula_version,
    )
    FactPlaneV2(conn).persist_observation(direct)
    attempt = _attempts(conn)[-1]
    conn.execute(
        "UPDATE metric_computation_attempts SET output_observation_id=? WHERE id=?",
        (direct.observation_id, attempt["id"]),
    )

    result = read_attempt_result(conn, "SYNTH", "revenue_yoy", read_cutoff=datetime.now(UTC))

    assert result is not None and result.status == "unavailable"
    assert result.reason_code == "canonical_output_unavailable"
