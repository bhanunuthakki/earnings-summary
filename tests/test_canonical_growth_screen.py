"""Migrated growth screen uses published/resolved synthetic facts, never raw fallback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from discovery.screens import run_screens
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from sources.discovery_financials import GrowthFinancials, read_growth_financials
from sources.foreign_filers import ForeignFilingForm
from sources.foreign_normalization_run import (
    ForeignDocumentInput,
    ForeignNormalizationManifest,
    ForeignPeriodAssertion,
    normalize_foreign_sources,
)

STAMP = datetime(2026, 9, 18, 12, tzinfo=UTC)


def seed_growth_graph(
    conn: sqlite3.Connection,
    root: Path,
    *,
    published_at: datetime | None = None,
    include_screen_financials: bool = False,
    include_peer_financials: bool = False,
) -> None:
    stamp = STAMP.strftime("%Y-%m-%d %H:%M:%S")
    ends = [
        "2026-03-31",
        "2025-12-31",
        "2025-09-30",
        "2025-06-30",
        "2025-03-31",
        "2024-12-31",
        "2024-09-30",
        "2024-06-30",
        "2024-03-31",
    ]
    revenues = [130, 120, 115, 110, 100, 95, 90, 88, 84]
    records = [
        {"concept": "revenue", "end": end, "value": value}
        for end, value in zip(ends, revenues, strict=True)
    ]
    records += [
        {"concept": "gross_profit", "end": end, "value": value}
        for end, value in zip(ends[:4], [65, 50, 52, 50], strict=True)
    ]
    if include_screen_financials:
        records += [
            {"concept": concept, "end": end, "value": amount}
            for concept, amount in (("operating_income", 20), ("free_cash_flow", 10))
            for end in ends[:4]
        ]
    if include_peer_financials:
        records += [{"concept": "net_income", "end": end, "value": 15} for end in ends[:4]]
    payload = json.dumps(records, sort_keys=True).encode()
    source = root / "synthetic-quarterly-source.json"
    source.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    conn.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type,instrument_type) VALUES ('WIX','Synthetic growth fixture','portfolio','equity')"
    )
    conn.execute(
        "INSERT INTO documents(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) VALUES (1,'WIX','fmp','fmp_income_statement',?,?,?,'ok',?)",
        (str(source), sha, stamp, len(payload)),
    )
    conn.execute(
        "INSERT INTO issuer_entities VALUES ('issuer-1','issuer-1','operating_company',?)", (stamp,)
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES ('reporting-1','reporting-1','issuer-1','legal_registrant','Synthetic growth fixture',?)",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (sha, len(payload), "application/json", source.as_uri(), stamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,retrieved_at,retrieval_config_sha256,collector_code_version,source_published_at) VALUES ('source-1','source-1','fmp','https://example.invalid/synthetic',?,?,?,?,'test',?)",
        (sha, stamp, stamp, "1" * 64, published_at.isoformat() if published_at else None),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,legacy_document_id,recorded_at) VALUES ('document-1','document-1',1,'source-1',?,'issuer-1','WIX','fmp_income_statement','statement','en',1,?)",
        (sha, stamp),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-1",
            "binding-1",
            "issuer-1",
            1,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "fixture",
            "{}",
            0,
            stamp,
            stamp,
            stamp,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES ('run-1','run-1','document-1',?,'fixture-parser',?,'fixture-v1',?,?,?,'succeeded')",
        (sha, "2" * 64, "3" * 64, stamp, stamp),
    )
    periods: list[ForeignPeriodAssertion] = []
    for index, row in enumerate(records):
        end = date.fromisoformat(str(row["end"]))
        start = date(end.year, ((end.month - 1) // 3) * 3 + 1, 1)
        period = ForeignPeriodAssertion(start=start, end=end, fiscal_period="quarter")
        if period not in periods:
            periods.append(period)
        locator = json.dumps({"path": f"/{index}/value"}, sort_keys=True, separators=(",", ":"))
        node = f"node-{index}"
        observation = f"observation-{index}"
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,'run-1',NULL,NULL,'table_cell',?,?,?,?)",
            (
                node,
                node,
                1,
                str(row["value"]),
                locator,
                hashlib.sha256(locator.encode()).hexdigest(),
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO reported_observations(observation_id,idempotency_key,issuer_id,ticker,concept_key,period_start,period_end,fiscal_period_type,dimensions_json,numeric_value,currency,unit,observation_status,evidence_node_id,available_at,recorded_at,method,method_version,confidence) VALUES (?,?,'issuer-1','WIX',?,?,?,'quarter','[]',?,'USD','USD','reported',?,?,?,'fixture-parser','1',1)",
            (
                observation,
                observation,
                row["concept"],
                start.isoformat(),
                end.isoformat(),
                str(row["value"]),
                node,
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO fact_observation_revisions VALUES ('financial_facts',?,1,?,?,1,'primary',?,?)",
            (index + 1, observation, f"financial-{index}", locator, stamp),
        )
    conn.commit()
    manifest = ForeignNormalizationManifest(
        data_cutoff_at=STAMP,
        recorded_at=STAMP,
        documents=(
            ForeignDocumentInput(
                ticker="WIX",
                issuer_id="issuer-1",
                document_version_id="document-1",
                document_sha256=sha,
                form=ForeignFilingForm.ISSUER_STATEMENT_CACHE,
                currencies=("USD",),
                units=("USD",),
                periods=tuple(periods),
            ),
        ),
    )
    result = normalize_foreign_sources(conn, manifest, input_manifest_sha256="4" * 64, apply=True)
    assert result.receipts[0].observations == len(records)
    ontology = populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    assert ontology.snapshot_id is not None
    resolution = populate_canonical_resolution(
        conn,
        CanonicalResolutionPopulationRequest(
            cutoff_at=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    assert resolution.resolved_cell_count == len(records)
    conn.execute("UPDATE tracked_companies SET list_type='index_member' WHERE ticker='WIX'")
    conn.commit()


def test_growth_screen_real_published_resolved_path_and_no_raw_fallback(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        seed_growth_graph(conn, tmp_path)
        resolved = read_growth_financials(conn, "WIX", as_of=STAMP.date())
        assert resolved.status == "available"
        assert str(resolved.revenue_yoy) == "0.3"
        assert len(resolved.references) == 13
        assert all(
            item.canonical_resolution_revision_id and item.observation_payload_sha256
            for item in resolved.references
        )
    # No legacy cache is needed to screen the canonical admitted source graph.
    hits = run_screens(database, tmp_path / "absent-legacy-cache", as_of=STAMP.date())
    assert len(hits) == 1
    assert hits[0].screen == "growth_inflection"
    assert hits[0].evidence is not None
    assert hits[0].evidence["decision_grade"] is False


def test_calculation_rejects_mixed_currency_missing_quarters_and_semiannual_windows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from sources.discovery_financials import calculate_growth_financials

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        resolved = read_growth_financials(conn, "WIX", as_of=STAMP.date())
    mixed = (
        resolved.references[0].model_copy(update={"currency": "DKK"}),
        *resolved.references[1:],
    )
    assert calculate_growth_financials("WIX", STAMP.date(), mixed).reason_codes == (
        "incomparable_source_coordinates",
    )
    assert (
        calculate_growth_financials("WIX", STAMP.date(), resolved.references[1:]).status
        == "degraded"
    )
    half = (
        resolved.references[0].model_copy(update={"period_start": date(2025, 10, 1)}),
        *resolved.references[1:],
    )
    assert calculate_growth_financials("WIX", STAMP.date(), half).reason_codes == (
        "nonquarterly_or_unsupported_duration",
    )


@pytest.mark.parametrize("offset", [-5, 5])
def test_growth_calculation_rejects_quarter_duration_overlap_and_gap(
    tmp_path: Path, migrated_db: Callable[..., Path], offset: int
) -> None:
    from sources.discovery_financials import calculate_growth_financials

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        resolved = read_growth_financials(conn, "WIX", as_of=STAMP.date())
    # Shift both revenue and gross-profit start to preserve their exact pairing;
    # end-date spacing and broad quarter duration remain otherwise valid.
    latest = resolved.latest_period_end
    shifted = tuple(
        item.model_copy(update={"period_start": item.period_start + timedelta(days=offset)})
        if item.period_end == latest
        else item
        for item in resolved.references
    )
    assert calculate_growth_financials("WIX", STAMP.date(), shifted).reason_codes == (
        "quarterly_duration_gap_or_overlap",
    )


def test_discover_persists_canonical_calculation_and_resolves_configured_database(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from discovery.store import list_candidates, upsert_candidate
    from execution import run_discovery

    database = migrated_db(tmp_path / "configured-fixture.db")
    with sqlite3.connect(database) as conn:
        seed_growth_graph(conn, tmp_path)
    upsert_candidate(
        ticker="WIX", name="Synthetic growth fixture", score=1, evidence=[], db_path=database
    )
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))

    def no_external_ranking(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(run_discovery, "_compute_need_ranks", no_external_ranking)
    coverage: list[GrowthFinancials] = []
    run_discovery.discover(tmp_path, include_adjacency=False, growth_coverage_sink=coverage.append)
    assert coverage[0].status == "available"
    candidate = next(item for item in list_candidates(db_path=database) if item.ticker == "WIX")
    evidence = next(
        item for item in candidate.evidence if item["source"] == "screen:growth_inflection"
    )
    calculation = evidence["calculation"]
    assert isinstance(calculation, dict)
    assert calculation["calculation_version"] == "growth-inflection-canonical/v1"
    calculation_data = cast(dict[str, object], calculation)
    parsed = GrowthFinancials.model_validate(
        {key: value for key, value in calculation_data.items() if key != "dual_read_parity"}
    )
    assert len(parsed.references) == 13
    assert not (tmp_path / "data" / "portfolio.db").exists()


def test_production_shadow_compares_hash_bound_legacy_calculations(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        seed_growth_graph(conn, tmp_path)
        resolved = read_growth_financials(conn, "WIX", as_of=STAMP.date())
    revenues = sorted(
        (item for item in resolved.references if item.concept == "revenue"),
        key=lambda item: item.period_end,
        reverse=True,
    )
    profits = {
        item.period_end: item.value
        for item in resolved.references
        if item.concept == "gross_profit"
    }
    rows = [
        {
            "date": item.period_end.isoformat(),
            "revenue": float(item.value),
            "grossProfit": float(profits[item.period_end]) if item.period_end in profits else None,
        }
        for item in revenues
    ]
    cache = tmp_path / "legacy"
    cache.mkdir()
    source = cache / "WIX_income_statement_quarterly.json"
    source.write_text(json.dumps(rows))
    hits = run_screens(database, cache, as_of=STAMP.date())
    assert len(hits) == 1
    assert hits[0].evidence is not None
    parity = hits[0].evidence["dual_read_parity"]
    assert isinstance(parity, dict)
    assert parity["status"] == "VERIFIED_MATCH"
    assert parity["legacy_source_hashes"] == {
        source.name: hashlib.sha256(source.read_bytes()).hexdigest()
    }
    rows[0]["revenue"] = 131.0
    source.write_text(json.dumps(rows))
    changed = run_screens(database, cache, as_of=STAMP.date())
    assert changed[0].evidence is not None
    divergent = changed[0].evidence["dual_read_parity"]
    assert isinstance(divergent, dict)
    assert divergent["status"] == "VERIFIED_DIVERGENCE"
    assert changed[0].detail == hits[0].detail


def test_parity_unavailable_values_are_json_safe() -> None:
    from sources.discovery_financials import growth_parity_receipt

    receipt = growth_parity_receipt(
        GrowthFinancials(
            ticker="WIX", as_of=STAMP.date(), status="unavailable", reason_codes=("fixture",)
        ),
        legacy_values=(float("nan"), float("inf"), None),
        legacy_source_hashes={},
    )
    assert receipt["status"] == "INDETERMINATE_UNAVAILABLE"
    json.dumps(receipt, allow_nan=False)


def test_discover_without_configuration_refuses_checkout_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import run_discovery

    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    with pytest.raises(RuntimeError, match="checkout database is prohibited"):
        run_discovery.discover(tmp_path, include_adjacency=False)
    assert not (tmp_path / "data" / "portfolio.db").exists()
