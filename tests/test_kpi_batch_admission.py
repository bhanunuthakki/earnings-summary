"""Tests for auditable, append-only batch KPI semantic admission."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from compute.kpi_resolver import semantic_series_identity_sql
from compute.thesis_evaluator import evaluate_ticker_thesis
from execution import admit_verified_kpis
from models.kpis import BreachStatus
from pipeline.kpi_batch_admission import run_batch_kpi_admission
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
    semantic_admission_sql,
    unclassified_kpi_context,
)
from pipeline.research_cockpit import tier1_kpi_deltas
from provenance.financial_fact_resolution import resolve_fact_row
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

NOW = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def _seed_evidence_doc(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    ticker: str,
    issuer_id: str,
    period_end: str,
    file_path: str,
    sha_char: str,
) -> None:
    stamp = NOW.isoformat()
    sha = sha_char * 64
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_quality_tier) VALUES "
        "(?, ?, 'ir_doc', 'ir_press_release', ?, ?, ?, ?, 'ok', 100, 'fmp_normalized')",
        (doc_id, ticker, period_end, file_path, sha, stamp),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs (sha256, byte_size, media_type, storage_uri, recorded_at) "
        "VALUES (?, 100, 'text/html', ?, ?)",
        (sha, f"https://example.invalid/{ticker.lower()}/{doc_id}", stamp),
    )
    obs_id = f"source-{doc_id}"
    ver_id = f"docver-{doc_id}"
    run_id = f"run-{doc_id}"
    node_id = f"root-{doc_id}"
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, observed_at, "
        "retrieved_at, retrieval_config_sha256, collector_code_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            obs_id,
            f"source:{doc_id}",
            "ir_document",
            f"https://example.invalid/{ticker.lower()}/{doc_id}",
            sha,
            stamp,
            stamp,
            "d" * 64,
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, period_end, language, legacy_document_id, "
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ver_id,
            f"document:{doc_id}",
            1,
            obs_id,
            sha,
            issuer_id,
            ticker,
            "ir_press_release",
            "ir_press_release",
            period_end,
            "en",
            doc_id,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
        "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
        "started_at, completed_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            f"run:{doc_id}",
            ver_id,
            sha,
            "test-extractor",
            "e" * 64,
            "test-v1",
            "f" * 64,
            stamp,
            stamp,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, supersedes_node_id, "
        "node_kind, text, locator_json, locator_sha256, recorded_at) "
        "VALUES (?, ?, 1, ?, NULL, NULL, 'document', 'Earnings release.', NULL, NULL, ?)",
        (node_id, f"root:{doc_id}", run_id, stamp),
    )


def _seed_base_db(db_path: Path, migrated_db: Callable[..., Path]) -> None:
    migrated_db(db_path)
    stamp = NOW.isoformat()
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)
    try:
        conn.execute("INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('bhanu', ?)", (stamp,))
        conn.execute(
            "INSERT INTO tracked_companies (id, user_id, ticker, name, list_type, instrument_type) "
            "VALUES (1, 'bhanu', 'NU', 'Nu Holdings', 'portfolio', 'equity'), "
            "(2, 'bhanu', 'BKNG', 'Booking Holdings', 'portfolio', 'equity')"
        )
        conn.execute(
            "INSERT INTO issuer_entities (issuer_id, idempotency_key, entity_kind, created_at) "
            "VALUES ('issuer-nu', 'issuer:nu', 'operating_company', ?), "
            "('issuer-bkng', 'issuer:bkng', 'operating_company', ?)",
            (stamp, stamp),
        )
        _seed_evidence_doc(
            conn,
            doc_id=10,
            ticker="NU",
            issuer_id="issuer-nu",
            period_end="2025-09-30",
            file_path="data/nu_q3.pdf",
            sha_char="a",
        )
        _seed_evidence_doc(
            conn,
            doc_id=11,
            ticker="NU",
            issuer_id="issuer-nu",
            period_end="2025-12-31",
            file_path="data/nu_q4.pdf",
            sha_char="b",
        )
        _seed_evidence_doc(
            conn,
            doc_id=12,
            ticker="BKNG",
            issuer_id="issuer-bkng",
            period_end="2025-12-31",
            file_path="data/bkng_q4.pdf",
            sha_char="c",
        )
        conn.execute(
            "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source, threshold_tier) "
            "VALUES "
            "(101, 'NU', 'Net margin', 'percent', 'ir_doc', 'tier_1_break'), "
            "(102, 'NU', 'Adjusted EBITDA margin', 'percent', 'ir_doc', NULL), "
            "(103, 'BKNG', 'Alternative accommodations listings', 'count', 'ir_doc', 'tier_1_break'), "
            "(104, 'NU', 'Unverified count', 'count', 'ir_doc', NULL)"
        )
        conn.execute(
            "INSERT INTO kpi_facts ("
            "id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "source_doc_id, source_excerpt, extracted_by"
            ") VALUES "
            # 1: deterministic Q3 -> resolved & eligible (GAAP)
            "(1, 'NU', '2025-09-30', 'Q3', 101, '20.0', 'percent', 10, '20.0%', 'metrics_engine'), "
            # 2: deterministic Q4 with legacy_unknown context -> resolved & upgraded to admitted
            "(2, 'NU', '2025-12-31', 'Q4', 101, '24.0', 'percent', 11, '24.0%', 'issuer_manifest_v1'), "
            # 3: future dated (> as_of=2026-11-18) -> skipped future_period_end
            "(3, 'NU', '2027-03-31', 'Q1', 101, '99.0', 'percent', 11, '99.0%', 'metrics_engine'), "
            # 4: unlinked to v_kpi_facts_resolved_current (never passed to resolve_fact_row)
            "(4, 'NU', '2025-06-30', 'Q2', 101, '18.0', 'percent', 10, '18.0%', 'metrics_engine'), "
            # 5: quarantined deterministic row -> preserved (never overridden)
            "(5, 'NU', '2025-03-31', 'Q1', 101, '15.0', 'percent', 10, '15.0%', 'metrics_engine'), "
            # 6: LLM-extracted row -> skipped by default
            "(6, 'NU', '2024-12-31', 'Q4', 101, '19.0', 'percent', 10, 'Net margin reached 19.0%', 'llm_extracted'), "
            # 7: existing admitted non-gaap row on def 102
            "(7, 'NU', '2025-09-30', 'Q3', 102, '31.0', 'percent', 10, '31.0%', 'fmp_derived'), "
            # 8: unreviewed deterministic row on def 102 -> inherits NON_GAAP signature from id=7
            "(8, 'NU', '2025-12-31', 'Q4', 102, '33.5', 'percent', 11, '33.5%', 'ir_spreadsheet'), "
            # 9: BKNG count row with 8.6M in source_excerpt -> matched ("8.6", MILLIONS)
            "(9, 'BKNG', '2025-12-31', 'Q4', 103, '8600000', 'count', 12, 'Alternative accommodations reached 8.6M listings (+8% YoY)', 'ir_press_release_readout'), "
            # 10: count row whose excerpt lacks matching token -> count_source_value_unverified
            "(10, 'NU', '2025-12-31', 'Q4', 104, '4200000', 'count', 11, 'Strong customer growth', 'ir_spreadsheet')"
        )
        # Resolve every row EXCEPT id=4 (stays unlinked from v_kpi_facts_resolved_current).
        for fact_id in (1, 2, 3, 5, 6, 7, 8, 9, 10):
            resolved = resolve_fact_row(
                conn, fact_table="kpi_facts", fact_row_id=fact_id, knowledge_cutoff=NOW
            )
            assert resolved is not None and resolved.resolution_status == "resolved"

        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=2,
            context=unclassified_kpi_context(
                metric_name_as_reported="Net margin",
                reported_period_end=date(2025, 12, 31),
            ),
            reviewed_by="issuer_manifest_v1",
            knowledge_at=NOW,
        )
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=5,
            context=KpiSemanticContext(
                metric_name_as_reported="Net margin",
                reported_period_end=date(2025, 3, 31),
                period_role=KpiPeriodRole.CURRENT,
                publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
                accounting_basis=KpiAccountingBasis.GAAP,
                consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
                unit_scale=KpiUnitScale.NONE,
                status=KpiSemanticStatus.QUARANTINED,
                reason_code="disputed_definition",
            ),
            reviewed_by="source_review:owner",
            knowledge_at=NOW,
        )
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=7,
            context=KpiSemanticContext(
                metric_name_as_reported="Adjusted EBITDA Margin (Custom)",
                reported_period_end=date(2025, 9, 30),
                period_role=KpiPeriodRole.CURRENT,
                publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
                accounting_basis=KpiAccountingBasis.NON_GAAP,
                consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
                unit_scale=KpiUnitScale.NONE,
                status=KpiSemanticStatus.ADMITTED,
                reason_code=None,
            ),
            reviewed_by="source_review:owner",
            knowledge_at=NOW,
        )
        conn.commit()
    finally:
        conn.close()


def test_dry_run_on_read_only_db_never_writes(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_base_db(db_path, migrated_db)

    rc = admit_verified_kpis.main(
        ["--db-path", str(db_path), "--tickers", "NU,BKNG", "--as-of", "2026-11-18"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert payload["total_already_admitted"] == 1
    assert payload["total_eligible"] == 4  # ids 1, 2, 8, 9
    assert payload["total_admitted_applied"] == 0
    assert payload["skipped_counts"] == {
        "count_source_value_unverified": 1,
        "future_period_end": 1,
        "quarantined": 1,
        "unlinked_observation_resolution": 1,
        "unverified_llm_extractor": 1,
    }

    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        ro.execute("PRAGMA query_only=1")
        ctx_count = ro.execute("SELECT COUNT(*) FROM kpi_fact_semantic_contexts").fetchone()[0]
        assert ctx_count == 3
    finally:
        ro.close()


def test_apply_is_append_only_idempotent_and_passes_strict_readers_and_thesis_eval(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_base_db(db_path, migrated_db)

    rc = admit_verified_kpis.main(
        [
            "--db-path",
            str(db_path),
            "--portfolio",
            "--as-of",
            "2026-11-18",
            "--apply",
            "--reviewed-by",
            "batch_admission:test_v1",
        ]
    )
    assert rc == 0
    first = json.loads(capsys.readouterr().out)
    assert first["apply"] is True
    assert first["total_admitted_applied"] == 4
    assert first["total_already_admitted"] == 1

    # Replay is strictly idempotent.
    rc2 = admit_verified_kpis.main(
        ["--db-path", str(db_path), "--portfolio", "--as-of", "2026-11-18", "--apply"]
    )
    assert rc2 == 0
    second = json.loads(capsys.readouterr().out)
    assert second["total_admitted_applied"] == 0
    assert second["total_eligible"] == 0
    assert second["total_already_admitted"] == 5

    ro = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        head_2 = current_kpi_semantic_context(ro, kpi_fact_id=2)
        assert head_2 is not None
        assert head_2.revision == 2
        assert head_2.context.status is KpiSemanticStatus.ADMITTED
        assert head_2.context.accounting_basis is KpiAccountingBasis.GAAP

        head_5 = current_kpi_semantic_context(ro, kpi_fact_id=5)
        assert head_5 is not None and head_5.context.status is KpiSemanticStatus.QUARANTINED

        head_8 = current_kpi_semantic_context(ro, kpi_fact_id=8)
        assert head_8 is not None
        assert head_8.context.metric_name_as_reported == "Adjusted EBITDA Margin (Custom)"
        assert head_8.context.accounting_basis is KpiAccountingBasis.NON_GAAP

        head_9 = current_kpi_semantic_context(ro, kpi_fact_id=9)
        assert head_9 is not None
        assert head_9.context.source_value_text == "8.6"
        assert head_9.context.unit_scale is KpiUnitScale.MILLIONS

        # Unlinked id=4 received NO semantic context row.
        assert current_kpi_semantic_context(ro, kpi_fact_id=4) is None

        # Strict semantic admission SQL + series identity SQL read back the admitted NU facts.
        sem_join, sem_where = semantic_admission_sql(ro, fact_alias="kf", fail_closed=True)
        sem_id = semantic_series_identity_sql(ro, fact_alias="kf")
        admitted_ids = [
            r[0]
            for r in ro.execute(
                f"SELECT kf.id FROM kpi_facts kf {sem_join} "
                f"WHERE kf.ticker='NU' AND kf.kpi_definition_id=101 "
                f"AND {sem_where} AND {sem_id} ORDER BY kf.period_end"
            ).fetchall()
        ]
        assert admitted_ids == [1, 2]

        # Cockpit strict tier-1 deltas consume admitted facts without fallback.
        deltas = tier1_kpi_deltas(ro, {"NU"}, as_of=date(2026, 11, 18))["NU"]
        assert len(deltas) == 1
        assert deltas[0].name == "Net margin"
        assert deltas[0].latest_value == pytest.approx(24.0)
        assert deltas[0].prior_value == pytest.approx(20.0)
        assert deltas[0].unreviewed_fallback is False

        # End-to-end thesis evaluation resolves Net margin cleanly via admitted facts.
        holdings_dir = tmp_path / "holdings"
        holdings_dir.mkdir()
        (holdings_dir / "NU.json").write_text(
            json.dumps(
                {
                    "ticker": "NU",
                    "thesis": "Net margin stays above floor.",
                    "break_rules": [
                        {
                            "rule_id": "net_margin_floor",
                            "kpi_name": "Net margin",
                            "comparator": "lt",
                            "threshold": 15.0,
                            "unit": "percent",
                            "consecutive_periods": 1,
                            "narrative": "Net margin < 15%",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        verdict = evaluate_ticker_thesis(ro, ticker="NU", holdings_dir=holdings_dir)
        assert verdict.overall_status is BreachStatus.OK
        assert len(verdict.rule_evaluations) == 1
        assert verdict.rule_evaluations[0].status is BreachStatus.OK
    finally:
        ro.close()


def test_include_llm_with_excerpt_is_opt_in_and_missing_doc_rejected_on_legacy_schema(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_base_db(db_path, migrated_db)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)
    try:
        report = run_batch_kpi_admission(
            conn,
            tickers=["NU"],
            as_of=date(2026, 11, 18),
            apply=True,
            include_llm_with_excerpt=True,
        )
    finally:
        conn.close()
    assert report.per_ticker[0].admitted_applied == 4  # ids 1, 2, 6, 8
    assert (
        admit_verified_kpis.OPERATIONS_GOVERNANCE_DISPOSITION
        == "no_surface_change_internal_append_only_kpi_batch_admission"
    )
    assert admit_verified_kpis.OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT == (
        "src/operations/registry.py:OperationsRegistry",
        "src/pipeline/operations_panel.py:visible_surface_dispositions",
        "src/operations/review_bundle.py:ReviewKpiCensus",
    )

    legacy = sqlite3.connect(":memory:")
    try:
        legacy.executescript(
            """
            CREATE TABLE documents (id INTEGER PRIMARY KEY);
            CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY, ticker TEXT, name TEXT);
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
                fiscal_period_type TEXT, kpi_definition_id INTEGER,
                value REAL, unit TEXT, source_doc_id INTEGER,
                source_excerpt TEXT, extracted_by TEXT
            );
            CREATE TABLE kpi_fact_semantic_contexts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kpi_fact_id INTEGER,
                revision INTEGER, supersedes_context_id INTEGER,
                metric_name_as_reported TEXT, reported_period_end TEXT,
                period_role TEXT, publication_lane TEXT,
                accounting_basis TEXT, consolidation_scope TEXT,
                dimensions_json TEXT, unit_scale TEXT,
                source_row_label TEXT, source_column_header TEXT,
                source_value_text TEXT, status TEXT, reason_code TEXT,
                reviewed_by TEXT, knowledge_at TEXT, created_at TEXT
            );
            INSERT INTO kpi_definitions VALUES (1, 'NU', 'Net margin');
            INSERT INTO kpi_facts VALUES
                (1, 'NU', '2025-12-31', 'Q4', 1, 20.0, 'percent', NULL, '20%', 'metrics_engine');
            """
        )
        res = run_batch_kpi_admission(legacy, tickers=["NU"], as_of=date(2026, 11, 18))
        assert res.skipped_counts == {"missing_source_document": 1}
    finally:
        legacy.close()
