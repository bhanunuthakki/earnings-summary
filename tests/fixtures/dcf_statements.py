"""Publish synthetic DCF statement observations through the real canonical writers."""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast

from dcf.primary_fact_overlay import Statement, statement_field_mappings
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
)
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

STAMP = foundation.STAMP


def seed_dcf_statements(
    conn: sqlite3.Connection,
    ticker: str,
    statements: dict[Statement, list[dict[str, object]]],
    *,
    currency: str,
    canonical_bindings: bool = True,
    transform_cell: Callable[[FactCellV2], FactCellV2] | None = None,
    publication_granularity: Literal["statement_period", "all"] = "statement_period",
    namespace: str = "",
    document_id: int = 1,
    seed_foundation: bool = True,
) -> int:
    """Publish one ticker's statement observations through the real writers.

    The optional ``namespace`` / ``document_id`` / ``seed_foundation``
    parameters exist for the multi-ticker seed (several tickers' facts in ONE
    database copy): every fixed key that collides across tickers (document
    version, legacy document id, extraction run ids, node/observation
    suffixes) is namespaced per ticker, and the shared foundation graph is
    seeded once by the first caller. Defaults reproduce the single-ticker
    layout exactly. Returns the number of facts published.
    """
    # The foundational graph helper has no public fixture seam on the released
    # baseline. Call its typed seed only; the owning migrated_db supplies schema.
    seed = cast("Callable[[sqlite3.Connection], None]", getattr(foundation, "_seed_foundation"))
    if seed_foundation:
        seed(conn)
    document_version_id = f"{namespace}dcf-document"
    issuer_id = f"{namespace}issuer-1"
    reporting_entity_id = f"{namespace}reporting-1"
    subject_binding_id = f"{namespace}binding-1"
    if namespace:
        # Multi-ticker seed: FactCellV2 is keyed by reporting identity, never
        # ticker, so each ticker gets its own issuer + reporting entity. Two
        # tickers reporting the same concept/period stay distinct semantic
        # coordinates in one database copy instead of conflicting envelopes.
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            (issuer_id, f"{namespace}issuer-key-1", "operating_company", STAMP),
        )
        conn.execute(
            "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
            (
                reporting_entity_id,
                f"{namespace}reporting-key-1",
                issuer_id,
                "legal_registrant",
                f"{ticker} Registrant",
                STAMP,
            ),
        )
        # The reported-anchor trigger binds every anchor's subject revision to
        # the cell's reporting entity and the document version's issuer (and
        # binding revisions are unique per recorded issuer), so each ticker
        # needs its own selected binding revision too.
        conn.execute(
            "INSERT INTO recorded_subject_binding_revisions "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                subject_binding_id,
                f"{namespace}binding-key-1",
                issuer_id,
                1,
                issuer_id,
                reporting_entity_id,
                None,
                "selected",
                "deterministic",
                "exact_subject",
                "{}",
                0,
                STAMP,
                STAMP,
                STAMP,
                None,
            ),
        )
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,source_url,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_quality_tier) VALUES (?,?,'sec_xbrl','10-K','https://data.sec.gov/example.json','synthetic.json',?,'2026-07-27T12:00:00+00:00','ok',12,'sec_official')",
        (document_id, ticker, hashlib.sha256(f"filing bytes {ticker}".encode()).hexdigest()),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,legacy_document_id,recorded_at) "
        "SELECT ?,?,1,observation_id,blob_sha256,?,?,'regulatory_filing','10-K','en',?,recorded_at FROM evidence_document_versions WHERE document_version_id='document-1'",
        (
            document_version_id,
            document_version_id,
            issuer_id,
            ticker,
            document_id,
        ),
    )
    facts: list[ReportedSourceFact] = []
    # Routine fixtures select one statement period per extraction config. Each
    # real seal covers every node emitted by that run. The all mode deliberately
    # retains a multi-period publication for positive/corruption boundary tests.
    batches: dict[str, list[ReportedSourceFact]] = {}
    for statement, records in statements.items():
        for record in records:
            year = int(str(record["fiscalYear"]))
            fiscal = str(record["period"])
            run_id = (
                f"{namespace}dcf-run"
                if publication_granularity == "all"
                else f"{namespace}dcf-run-{statement}-{year}-{fiscal}"
            )
            if run_id not in batches:
                batches[run_id] = []
                conn.execute(
                    "INSERT INTO evidence_extraction_runs SELECT ?,?,?,input_sha256,extractor_name,?,extractor_code_version,output_sha256,started_at,completed_at,outcome FROM evidence_extraction_runs WHERE extraction_run_id='run-1'",
                    (
                        run_id,
                        run_id,
                        document_version_id,
                        hashlib.sha256(run_id.encode()).hexdigest(),
                    ),
                )
            quarter = int(fiscal[1])
            half_year = {str(row["period"]) for row in records} == {"Q2", "Q4"}
            month = quarter * 3
            period_end = datetime(year, month, calendar.monthrange(year, month)[1], tzinfo=UTC)
            start_month = month - (5 if half_year else 2)
            period_start = datetime(year, start_month, 1, tzinfo=UTC)
            for mapping in statement_field_mappings(statement):
                raw = record.get(mapping.fmp_field)
                if not isinstance(raw, (int, float)):
                    continue
                suffix = f"{namespace}dcf-{len(facts)}"
                locator = {"path": f"/facts/{len(facts)}/value"}
                locator_json = json.dumps(locator, sort_keys=True, separators=(",", ":"))
                node = f"node-{suffix}"
                conn.execute(
                    "INSERT INTO evidence_nodes VALUES (?,?,?,?,NULL,NULL,'table_cell',?,?,?,?)",
                    (
                        node,
                        node,
                        1,
                        run_id,
                        str(raw),
                        locator_json,
                        hashlib.sha256(locator_json.encode()).hexdigest(),
                        STAMP,
                    ),
                )
                shares = mapping.line_item == "weighted_avg_shares_diluted"
                cell = FactCellV2.model_validate(
                    {
                        **foundation.make_cell(suffix).model_dump(),
                        "reporting_entity_id": reporting_entity_id,
                        "semantic_key_sha256": None,
                        "concept_namespace": "urn:earnings-summary:legacy:financial",
                        "concept_name": mapping.line_item,
                        "taxonomy_name": "earnings-summary-legacy",
                        "taxonomy_version": "2026",
                        "period_kind": "instant" if statement == "balance" else "duration",
                        "period_start": None if statement == "balance" else period_start,
                        "period_end": period_end,
                        "fiscal_year": year,
                        "fiscal_period": fiscal,
                        "dimensions": (),
                        "unit_key": "shares" if shares else currency,
                        "currency": None if shares else currency,
                    }
                )
                if transform_cell is not None:
                    cell = transform_cell(cell)
                observation = foundation.make_report(
                    cell, suffix, numeric_value=str(raw)
                ).model_copy(
                    update={
                        "evidence_node_id": node,
                        "document_version_id": document_version_id,
                        "subject_binding_revision_id": subject_binding_id,
                        "source_locator": CanonicalJSONObject.model_validate(locator),
                        "source_locator_sha256": hashlib.sha256(locator_json.encode()).hexdigest(),
                    }
                )
                fact = ReportedSourceFact(cell=cell, observation=observation)
                facts.append(fact)
                batches[run_id].append(fact)
    conn.commit()
    for run_id, batch in batches.items():
        if not batch:
            continue
        SourceFactRepository(conn).publish(
            SourceFactPublication(
                publication_id=f"{run_id}-publication",
                idempotency_key=f"{run_id}-publication",
                reported_facts=tuple(batch),
                extraction_seals=(
                    ExtractionRunCompletenessSealV2(
                        extraction_seal_id=f"{run_id}-seal",
                        idempotency_key=f"{run_id}-seal",
                        extraction_run_id=run_id,
                        expected_node_count=len(batch),
                        completeness_policy_name="all-run-nodes",
                        completeness_policy_version="v1",
                        completeness_policy_sha256=hashlib.sha256(b"dcf-completeness").hexdigest(),
                        knowledge_at=STAMP,
                        recorded_at=STAMP,
                    ),
                ),
            )
        )
    if not canonical_bindings:
        conn.commit()
        return len(facts)
    ontology = populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    assert ontology.snapshot_id is not None
    result = populate_canonical_resolution(
        conn,
        CanonicalResolutionPopulationRequest(
            cutoff_at=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    assert result.resolved_cell_count == len(facts)
    conn.commit()
    return len(facts)
