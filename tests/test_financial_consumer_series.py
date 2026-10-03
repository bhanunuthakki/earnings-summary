"""Consumer-path financial evidence and explicit cadence regressions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ask import turn_cache
from ask.grounding import gather_evidence
from ask.grounding_trace import view_trace_items
from decision_conditions import DecisionCondition, parse_condition, stamp_condition_baselines
from provenance.metric_ontology import MetricOntology
from sources.canonical_financial_series import (
    CanonicalFinancialSeriesReader,
    FinancialCadence,
    FinancialConsumerPoint,
    SeriesContinuity,
    read_financial_consumer_series,
)
from sources.report_financials import (
    FinancialEvidenceReference,
    financial_series_reference,
    read_financial_evidence,
    read_financial_table,
)
from tests.test_canonical_financial_peek import content_client
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database
from ui.source_chip import financial_consumer_source, source_chip_html, viewer_href
from viewspec.engine import execute_view
from viewspec.spec import ViewSpec


def test_ask_cites_each_canonical_period_document(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    facts_a = seed_table(
        database,
        [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")],
        publication_prefix="first",
        populate=False,
    )
    facts_b = seed_table(
        database,
        [("revenue", "2025-04-01", "2025-06-30", "Q2", "120", "USD")],
        publication_prefix="second",
    )
    database.commit()
    items = gather_evidence(
        "fin:SYNTH:revenue:quarterly",
        repo_root=tmp_path,
        db_path=tmp_path / "source-fact-repository.db",
        scope_tickers=["SYNTH"],
    )
    points = [
        item
        for item in items
        if item.kind == "fact" and item.fact_ref == "fin:SYNTH:revenue:quarterly"
    ]
    assert len(points) == 2
    for item, fact in zip(
        sorted(points, key=lambda item: item.period or ""), (facts_a[0], facts_b[0]), strict=True
    ):
        assert item.href is not None
        assert fact.observation.observation_id in item.href
        assert fact.observation.document_version_id in item.text


def test_ask_memo_retains_cutoff_and_invalidates_on_canonical_capture(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    database.commit()

    def gather() -> list[object]:
        return list(
            gather_evidence(
                "fin:SYNTH:revenue:quarterly",
                repo_root=tmp_path,
                db_path=tmp_path / "source-fact-repository.db",
                scope_tickers=["SYNTH"],
                cache_key="financial-evidence-session",
            )
        )

    first = gather()
    assert first
    assert gather() == first
    assert turn_cache.stats()["gather"] == {"hits": 1, "misses": 1}
    seed_table(
        database,
        [("revenue", "2025-04-01", "2025-06-30", "Q2", "120", "USD")],
        publication_prefix="later",
        populate=False,
    )
    database.commit()
    gather()
    assert turn_cache.stats()["gather"] == {"hits": 1, "misses": 2}


def test_qualification_note_keeps_original_financial_identity(
    database: sqlite3.Connection,
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    result = read_financial_consumer_series(
        database, "SYNTH", "revenue", cutoff=STAMP, cadence=FinancialCadence.QUARTERLY
    )
    original = financial_consumer_source(result.points[0], result)
    point = result.points[0].model_copy(update={"qualifications": ("Scope needs review",)})
    qualified = financial_consumer_source(point, result)
    rendered = source_chip_html(qualified)
    assert "Scope needs review" in rendered
    assert "overridden by" not in rendered
    assert qualified.override is None
    assert qualified.canonical_reference == original.canonical_reference
    assert viewer_href(qualified) == viewer_href(original)


def test_condition_baseline_uses_canonical_winner_not_latest_legacy(
    database: sqlite3.Connection,
) -> None:
    database.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) VALUES (999,'SYNTH','fmp','fmp_income','synthetic','synthetic','2025-04-01','ok',1)"
    )
    seed_table(
        database,
        [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")],
        legacy_document_id=999,
    )
    database.execute(
        "INSERT INTO financial_facts(ticker,period_end,fiscal_period_type,line_item,value,unit,source_doc_id) VALUES ('SYNTH','2025-03-31','Q1','revenue',999,'actual',999)"
    )
    database.row_factory = sqlite3.Row
    condition = parse_condition(
        {
            "metric": "revenue",
            "metric_source": "financial",
            "op": "gt",
            "threshold": 200,
            "unit": "actual",
            "for_periods": 1,
            "financial_cadence": "quarterly",
        }
    )
    assert condition is not None
    stamped = stamp_condition_baselines(database, "SYNTH", [condition])[0]
    assert stamped.baseline_period_end == "2025-03-31"
    assert not stamped.breached_at_attach
    assert stamped.as_json_obj().get("baseline_source_reference") is not None


def test_annual_series_reference_is_strict_and_distinct_from_report() -> None:
    payload = {
        "ticker": "SYNTH",
        "concept": "revenue",
        "reader_kind": "series",
        "cadence": "annual",
        "continuity": "windowed",
        "canonical_metric_cell_id": "cell",
        "observation_id": "observation",
        "canonical_resolution_revision_id": "resolution",
        "metric_definition_revision_id": "definition",
        "as_of": STAMP.isoformat(),
    }
    reference = FinancialEvidenceReference.model_validate_json(json.dumps(payload))
    assert reference.model_dump(mode="json")["reader_kind"] == "series"
    assert reference.model_dump(mode="json")["cadence"] == "annual"


@pytest.mark.parametrize(
    "cadence, fiscal, start, end, metric, unit, value",
    [
        (FinancialCadence.ANNUAL, "FY", "2025-01-01", "2025-12-31", "revenue", "USD", "100"),
        (
            FinancialCadence.QUARTERLY,
            "Q1",
            "2025-01-01",
            "2025-03-31",
            "eps_diluted",
            "USD/shares",
            "2.50",
        ),
    ],
)
def test_supported_annual_and_per_share_evidence_is_exact(
    database: sqlite3.Connection,
    tmp_path: Path,
    cadence: FinancialCadence,
    fiscal: str,
    start: str,
    end: str,
    metric: str,
    unit: str,
    value: str,
) -> None:
    facts = seed_table(database, [(metric, start, end, fiscal, value, unit)])
    result = read_financial_consumer_series(
        database, "SYNTH", metric, cutoff=STAMP, cadence=cadence
    )
    assert result.series.status == "available", result.series.reason_code
    source = financial_consumer_source(result.points[0], result)
    assert source.canonical_reference is not None
    assert source.canonical_reference.reader_kind == "series"
    assert source.canonical_reference.observation_id == facts[0].observation.observation_id
    href = viewer_href(source)
    assert href is not None
    client, _reads = content_client(database, tmp_path)
    response = client.get(href)
    assert response.status_code == 200
    assert f'<span class="sv-sec-val">{value}</span>' in response.text
    assert unit in response.text
    assert fiscal in response.text
    assert facts[0].observation.observation_id in response.text


def test_report_rejection_cannot_be_bypassed_by_series_reference_mode(
    database: sqlite3.Connection,
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD"),
            ("net_income", "2025-01-01", "2025-03-31", "Q1", "20", "EUR"),
        ],
        currencies={1: "EUR"},
    )
    table = read_financial_table(database, "SYNTH", as_of=STAMP)
    assert all(not cell.available for cell in table.cells)
    result = read_financial_consumer_series(
        database, "SYNTH", "revenue", cutoff=STAMP, cadence=FinancialCadence.QUARTERLY
    )
    assert result.series.status == "available"
    ref = financial_series_reference(
        result.points[0],
        ticker="SYNTH",
        cutoff=STAMP,
        cadence=FinancialCadence.QUARTERLY,
        continuity=SeriesContinuity.WINDOWED,
    )
    assert isinstance(read_financial_evidence(database, ref), FinancialConsumerPoint)
    report_ref = ref.model_copy(
        update={"reader_kind": "report_table", "cadence": None, "continuity": None}
    )
    assert read_financial_evidence(database, report_ref) is None


@pytest.mark.parametrize("cutoff", [datetime(2025, 1, 1), datetime.now(UTC) + timedelta(days=1)])
def test_invalid_consumer_cutoff_fails_before_reads(
    database: sqlite3.Connection, cutoff: datetime
) -> None:
    statements: list[str] = []
    database.set_trace_callback(statements.append)
    try:
        with pytest.raises(ValueError):
            read_financial_consumer_series(
                database, "SYNTH", "revenue", cutoff=cutoff, cadence=FinancialCadence.QUARTERLY
            )
        assert not statements
    finally:
        database.set_trace_callback(None)


def test_series_reference_rejects_inconsistent_mode_before_database(tmp_path: Path) -> None:
    payload = {
        "ticker": "SYNTH",
        "concept": "revenue",
        "canonical_metric_cell_id": "cell",
        "observation_id": "observation",
        "canonical_resolution_revision_id": "resolution",
        "metric_definition_revision_id": "definition",
        "as_of": STAMP.isoformat(),
        "cadence": "annual",
    }
    with pytest.raises(ValidationError):
        FinancialEvidenceReference.model_validate_json(json.dumps(payload))
    client, reads = content_client(None, tmp_path)
    response = client.get(
        "/api/peek/canonical-financial", query_string={"reference": json.dumps(payload)}
    )
    assert response.status_code == 400
    assert not reads


def test_later_definition_does_not_reselect_old_series_evidence(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    result = read_financial_consumer_series(
        database, "SYNTH", "revenue", cutoff=STAMP, cadence=FinancialCadence.QUARTERLY
    )
    point = result.points[0]
    ref = financial_series_reference(
        point,
        ticker="SYNTH",
        cutoff=STAMP,
        cadence=result.series.cadence,
        continuity=result.series.continuity,
    )
    ontology = MetricOntology(database)
    definition = ontology.metric_definition_as_known(point.observation.metric_id, STAMP)
    assert definition is not None
    later = STAMP + timedelta(days=1)
    ontology.persist_metric_definition(
        definition.model_copy(
            update={
                "metric_definition_revision_id": "changed-series-definition",
                "idempotency_key": "changed-series-definition",
                "revision": 2,
                "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                "definition_text": "Changed scope",
                "effective_at": later,
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    database.commit()
    client, _reads = content_client(database, tmp_path)
    old = client.get(
        "/api/peek/canonical-financial", query_string={"reference": ref.model_dump_json()}
    )
    assert old.status_code == 200 and point.observation.observation_id in old.text
    current = client.get(
        "/api/peek/canonical-financial",
        query_string={"reference": ref.model_copy(update={"as_of": later}).model_dump_json()},
    )
    assert current.status_code == 404


def test_consumer_preserves_caller_transaction_and_evidence(database: sqlite3.Connection) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    database.execute("BEGIN")
    result = read_financial_consumer_series(
        database, "SYNTH", "revenue", cutoff=STAMP, cadence=FinancialCadence.QUARTERLY
    )
    assert database.in_transaction
    ref = financial_series_reference(
        result.points[0],
        ticker="SYNTH",
        cutoff=STAMP,
        cadence=result.series.cadence,
        continuity=result.series.continuity,
    )
    evidence = read_financial_evidence(database, ref)
    assert isinstance(evidence, FinancialConsumerPoint)
    assert evidence == result.points[0]
    assert database.in_transaction
    database.rollback()


def test_concurrent_commit_does_not_mix_series_snapshot(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    database.execute("PRAGMA journal_mode=WAL")
    original = CanonicalFinancialSeriesReader.read
    later = STAMP + timedelta(days=1)
    committed: list[str] = []

    def commit_definition(
        reader: CanonicalFinancialSeriesReader,
        metric: str,
        *,
        cadence: FinancialCadence = FinancialCadence.QUARTERLY,
        continuity: SeriesContinuity = SeriesContinuity.STRICT_CONTIGUOUS,
    ):
        series = original(reader, metric, cadence=cadence, continuity=continuity)
        if not committed:
            with sqlite3.connect(tmp_path / "source-fact-repository.db") as writer:
                ontology = MetricOntology(writer)
                definition = ontology.metric_definition_as_known(
                    series.observations[0].metric_id, STAMP
                )
                assert definition is not None
                ontology.persist_metric_definition(
                    definition.model_copy(
                        update={
                            "metric_definition_revision_id": "concurrent-series-definition",
                            "idempotency_key": "concurrent-series-definition",
                            "revision": 2,
                            "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                            "definition_text": "Changed scope",
                            "effective_at": later,
                            "knowledge_at": later,
                            "recorded_at": later,
                        }
                    )
                )
            committed.append("changed")
        return series

    monkeypatch.setattr(CanonicalFinancialSeriesReader, "read", commit_definition)
    result = read_financial_consumer_series(
        database,
        "SYNTH",
        "revenue",
        cutoff=later + timedelta(days=1),
        cadence=FinancialCadence.QUARTERLY,
    )
    assert committed == ["changed"]
    assert result.series.status == "available" and result.points[0].observation.value == 100
    assert not database.in_transaction


def test_explore_canonical_cell_and_trace_retain_exact_source(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    facts = seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    view = execute_view(
        ViewSpec.from_dict(
            {"tickers": ["SYNTH"], "metrics": ["fin:revenue"], "cadence": "quarterly", "periods": 4}
        ),
        db_path=tmp_path / "source-fact-repository.db",
    )
    assert view.rows, view.warnings
    cell = view.rows[0].cells[0]
    assert cell.raw == 100 and cell.source is not None
    assert cell.source.canonical_reference is not None
    assert cell.source.canonical_reference.observation_id == facts[0].observation.observation_id
    trace = view_trace_items(view)
    assert trace[0].canonical_reference == cell.source.canonical_reference
    assert trace[0].href == viewer_href(cell.source)


def test_ambiguous_condition_cadence_is_visible_and_never_assumed(
    database: sqlite3.Connection,
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    condition = parse_condition(
        {
            "metric": "revenue",
            "metric_source": "financial",
            "op": "gt",
            "threshold": 50,
            "unit": "actual",
            "for_periods": 1,
        }
    )
    assert condition is not None
    stamped = stamp_condition_baselines(database, "SYNTH", [condition])[0]
    assert stamped.financial_cadence is None
    assert stamped.baseline_unavailable_reason == "financial_cadence_unresolved"
    assert stamped.baseline_source_reference is None and stamped.baseline_period_end is None


def test_canonical_names_reach_condition_vocabulary(database: sqlite3.Connection) -> None:
    from decision_conditions import metric_vocabulary

    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    assert metric_vocabulary(database, "SYNTH")[1] == ["revenue"]


def test_ask_followup_retains_each_point_and_deduplicates_read_clocks(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    from ask.grounding import EvidenceItem, EvidenceNeed, gather_requested_evidence
    from ask.grounding_trace import narrative_trace_items

    facts = seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD"),
            ("revenue", "2025-04-01", "2025-06-30", "Q2", "120", "USD"),
        ],
    )
    need = EvidenceNeed(kind="fact", ticker="SYNTH", query="revenue")

    def retrieve(existing: list[EvidenceItem]) -> list[EvidenceItem]:
        return gather_requested_evidence(
            [need],
            existing=existing,
            question="SYNTH revenue",
            repo_root=tmp_path,
            db_path=tmp_path / "source-fact-repository.db",
            scope_tickers=["SYNTH"],
        )

    items = retrieve([])
    assert len(items) == 2
    assert {
        item.canonical_reference.observation_id for item in items if item.canonical_reference
    } == {fact.observation.observation_id for fact in facts}
    assert all(item.source_manifest for item in narrative_trace_items(items))
    assert retrieve(items) == []


def test_condition_projection_uses_cadence_and_shows_unavailable_reason(
    database: sqlite3.Connection,
) -> None:
    from pipeline.work_os_decisions import project_decision_condition

    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    common = {
        "metric": "revenue",
        "metric_source": "financial",
        "op": "gt",
        "threshold": 50,
        "unit": "actual",
        "for_periods": 1,
        "baseline_period_end": "2024-12-31",
    }

    def project(condition: DecisionCondition | None):
        assert condition is not None
        return project_decision_condition(
            decision_id=1,
            revision="v1",
            index=0,
            condition=condition,
            origin="owner",
            ticker="SYNTH",
            conn=database,
            as_of=STAMP,
        )

    supported = project(parse_condition({**common, "financial_cadence": "quarterly"}))
    assert supported.status == "BREACH" and supported.latest_value == 100
    assert supported.financial_source_manifest is not None
    unknown = project(parse_condition(common))
    assert unknown.status == "PENDING DATA"
    assert unknown.status_detail == "financial_cadence_unresolved"


@pytest.mark.parametrize("baseline_end,expected_count", [("2025-03-31", 1), ("2025-06-30", 0)])
def test_financial_trigger_freshness_and_signature_keep_exact_source(
    database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    baseline_end: str,
    expected_count: int,
) -> None:
    from dataclasses import replace

    from decision_conditions import OpenDecision, conditions_from_json
    from triggers import decision_condition as trigger_module

    seed_table(
        database,
        [
            ("free_cash_flow", "2025-01-01", "2025-03-31", "Q1", "1800000000", "USD"),
            ("free_cash_flow", "2025-04-01", "2025-06-30", "Q2", "1200000000", "USD"),
        ],
    )
    result = read_financial_consumer_series(
        database,
        "SYNTH",
        "free_cash_flow",
        cutoff=STAMP,
        cadence=FinancialCadence.QUARTERLY,
        continuity=SeriesContinuity.STRICT_CONTIGUOUS,
    )
    baseline = next(
        point
        for point in result.points
        if point.observation.period_end.date().isoformat() == baseline_end
    )
    condition = parse_condition(
        {
            "metric": "free_cash_flow",
            "metric_source": "financial",
            "op": "lt",
            "threshold": 1.5,
            "unit": "billions",
            "for_periods": 1,
            "financial_cadence": "quarterly",
        }
    )
    assert condition is not None
    condition = replace(
        condition,
        baseline_period_end=baseline_end,
        baseline_source_context=baseline.observation,
        baseline_source_reference=financial_series_reference(
            baseline,
            ticker="SYNTH",
            cutoff=STAMP,
            cadence=result.series.cadence,
            continuity=result.series.continuity,
        ),
    )
    saved = conditions_from_json(json.dumps([condition.as_json_obj()]))
    assert saved == (condition,)
    decision = OpenDecision(
        decision_id=7,
        ticker="SYNTH",
        recommendation_kind="trim",
        recommendation_value=20,
        source_lens="test",
        made_at=STAMP.isoformat(),
        conditions=saved,
        decided_by="owner",
    )

    def load_decisions(_conn: sqlite3.Connection, _ticker: str) -> list[OpenDecision]:
        return [decision]

    monkeypatch.setattr(trigger_module, "load_open_decisions", load_decisions)
    trigger = trigger_module.DecisionConditionTrigger()
    first = trigger.scan("SYNTH", database)
    assert len(first) == expected_count
    if first:
        evidence = first[0].evidence
        assert evidence["latest_value"] == 1.2
        assert condition.baseline_source_reference is not None
        assert evidence[
            "baseline_source_reference"
        ] == condition.baseline_source_reference.model_dump(mode="json")
        assert result.points[-1].observation.observation_id in json.dumps(
            evidence["financial_source_manifest"]
        )
        second = trigger.scan("SYNTH", database)
        assert trigger.signature_key_evidence(first[0]) == trigger.signature_key_evidence(second[0])
        assert trigger.signature_key_evidence(first[0]) == {
            "decision_id": 7,
            "condition_index": 0,
            "period_end": "2025-06-30",
        }
