"""Retire unused selection writes while preserving canonical lineage and replay.

The historical migration remains valid. Canonical reads use the immutable
resolution ledger, and legacy backfill keeps its bounded checkpoint contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.financial_fact_resolution import (
    FactCutoverRequest,
    canonical_fact_relation,
    execute_fact_cutover,
    resolve_fact_row,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, 0)
SELECTION_LOGICAL_KEY = "financial_facts:ACME:revenue:2026-06-30:Q2"

# Exactly the population the resolved views serve: the latest resolution
# revision per logical key whose outcome is resolved, anchored through the
# selected observation's latest capture link.
_VIEW_DERIVED_SELECTION_SQL = (
    "SELECT link.logical_key, link.fact_table, link.fact_row_id, "
    "link.fact_revision, link.observation_id, resolution.resolution_id, "
    "resolution.revision AS resolution_revision "
    "FROM fact_observation_revisions AS link "
    "JOIN v_observation_resolution_current AS resolution "
    "ON resolution.selected_observation_id = link.observation_id "
    "JOIN fact_resolution_outcomes AS outcome "
    "ON outcome.resolution_id = resolution.resolution_id "
    "WHERE outcome.resolution_status = 'resolved' "
    "AND link.fact_revision = (SELECT MAX(latest.fact_revision) "
    "FROM fact_observation_revisions AS latest "
    "WHERE latest.fact_table = link.fact_table "
    "AND latest.fact_row_id = link.fact_row_id)"
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_document(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    source_tier: str,
    value_suffix: str,
    source_type: str | None = None,
) -> None:
    blob_sha = hashlib.sha256(f"document-{document_id}".encode()).hexdigest()
    config_sha = hashlib.sha256(b"config").hexdigest()
    output_sha = hashlib.sha256(f"output-{document_id}".encode()).hexdigest()
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, period_start, period_end, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, source_url, source_quality_tier, "
        "accession_number, filing_date) "
        "VALUES (?, 'ACME', ?, 'sec_10q', '2026-04-01', '2026-06-30', ?, ?, ?, 'ok', 42, "
        "?, ?, ?, '2026-07-20')",
        (
            document_id,
            source_type or ("sec_xbrl" if source_tier == "sec_official" else "fmp"),
            f"data/document-{document_id}.json",
            blob_sha,
            STAMP,
            f"https://example.test/{document_id}",
            source_tier,
            f"0000000000-26-{document_id:06d}",
        ),
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=42,
            media_type="application/json",
            storage_uri=f"file:///evidence/{document_id}.json",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"source-{document_id}",
            idempotency_key=f"source-{document_id}",
            source_kind="sec_filing",
            source_url=f"https://example.test/{document_id}",
            blob_sha256=blob_sha,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=config_sha,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=f"document-version-{document_id}",
            document_key=f"ACME:2026Q2:{document_id}",
            version_sequence=1,
            observation_id=f"source-{document_id}",
            blob_sha256=blob_sha,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="10-Q",
            form_type="10-Q",
            accession_number=f"0000000000-26-{document_id:06d}",
            exhibit_id=None,
            period_start=datetime(2026, 4, 1),
            period_end=datetime(2026, 6, 30),
            as_of_at=datetime(2026, 6, 30),
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=document_id,
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=f"run-{document_id}",
            idempotency_key=f"run-{document_id}",
            document_version_id=f"document-version-{document_id}",
            input_sha256=blob_sha,
            extractor_name="test-parser",
            extractor_config_sha256=config_sha,
            extractor_code_version="test@1",
            output_sha256=output_sha,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id=f"node-document-{document_id}",
            evidence_key=f"document-{document_id}",
            revision=1,
            extraction_run_id=f"run-{document_id}",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text=f"Document {document_id} {value_suffix}",
            locator=None,
            recorded_at=STAMP,
        )
    )


def _insert_financial_fact(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    value: str,
    line_item: str = "revenue",
) -> int:
    cursor = conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, unit, "
        "source_doc_id, confidence, extracted_by, locator) "
        "VALUES ('ACME', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', ?, 0.99, "
        "'deterministic:test', '{\"json_path\":\"[0].revenue\"}')",
        (line_item, value, document_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _seed_resolved_pair(conn: sqlite3.Connection) -> list[str]:
    """Seed and resolve two independent logical keys; return them in order."""

    _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
    _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
    conn.commit()
    logical_keys: list[str] = []
    for document_id, line_item, value in ((1, "cogs", "50"), (2, "revenue", "100")):
        fact_id = _insert_financial_fact(
            conn,
            document_id=document_id,
            value=value,
            line_item=line_item,
        )
        conn.commit()
        resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_id,
            knowledge_cutoff=STAMP,
        )
        conn.commit()
        logical_keys.append(f"financial_facts:ACME:{line_item}:2026-06-30:Q2")
    return sorted(logical_keys)


def _canonical_tuples(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    relation = canonical_fact_relation(conn, "financial_facts")
    assert relation.selection_mode == "resolved_view"
    return {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            f"SELECT id, reported_observation_id, resolution_id FROM {relation.sql}"  # nosec B608 -- closed canonical relation
        )
    }


def _assert_ledger_parity(conn: sqlite3.Connection) -> None:
    expected = {
        (str(row[2]), str(row[4]), str(row[5])) for row in conn.execute(_VIEW_DERIVED_SELECTION_SQL)
    }
    assert _canonical_tuples(conn) == expected


def test_historical_selection_schema_remains_upgrade_compatible(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
) -> None:
    conn = _connect(migrated_db(tmp_path / "selection-shape.db"))
    try:
        kind = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'current_fact_selection'"
        ).fetchone()
        assert kind is not None and kind[0] == "table"
        primary_key_columns = [
            str(row[1])
            for row in conn.execute("PRAGMA table_info(current_fact_selection)")
            if int(row[5]) > 0
        ]
        assert primary_key_columns == ["logical_key"]
        reader_index = [
            str(row[2])
            for row in conn.execute("PRAGMA index_info(ix_current_fact_selection_reader)")
        ]
        assert reader_index == ["ticker", "concept_key", "period_end", "fiscal_period_type"]
        constraints = {
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'current_fact_selection'"
            )
        }
        assert any("ck_current_fact_selection_table" in text for text in constraints)
        # Migration 0049 must not duplicate the fact_observation_revisions
        # (fact_table, fact_row_id) anchor: the composite primary-key autoindex
        # already covers it.
        for index in conn.execute("PRAGMA index_list('fact_observation_revisions')").fetchall():
            columns = [
                str(row[2])
                for row in conn.execute(f"PRAGMA index_info({index[1]})")  # nosec B608
            ]
            assert columns != ["fact_table", "fact_row_id"]
    finally:
        conn.close()


@pytest.mark.parametrize("preparation_table", ["missing", "empty", "stale"])
def test_canonical_reads_ignore_unused_preparation_table(
    migrated_db: Callable[..., Path], tmp_path: Path, preparation_table: str
) -> None:
    conn = _connect(migrated_db(tmp_path / "retired-selection-read.db"))
    try:
        _seed_resolved_pair(conn)
        baseline = _canonical_tuples(conn)
        assert len(baseline) == 2
        assert conn.execute("SELECT COUNT(*) FROM current_fact_selection").fetchone()[0] == 0
        if preparation_table == "missing":
            conn.execute("DROP TABLE current_fact_selection")
        elif preparation_table == "stale":
            conn.execute(
                "INSERT INTO current_fact_selection "
                "(logical_key,fact_table,fact_row_id,fact_revision,observation_id,"
                "resolution_id,resolution_revision,ticker,concept_key,period_end,fiscal_period_type) "
                "SELECT link.logical_key,link.fact_table,link.fact_row_id+1000,"
                "link.fact_revision,link.observation_id,resolution.resolution_id,"
                "resolution.revision,'ACME','wrong-concept','1900-01-01','FY' "
                "FROM fact_observation_revisions link "
                "JOIN v_observation_resolution_current resolution "
                "ON resolution.selected_observation_id=link.observation_id"
            )
        conn.commit()
        before = conn.total_changes
        assert _canonical_tuples(conn) == baseline
        _assert_ledger_parity(conn)
        assert conn.total_changes == before
    finally:
        conn.close()


def test_canonical_head_changes_and_rollback_remain_transactional(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = _connect(migrated_db(tmp_path / "canonical-transaction.db"))
    statements: list[str] = []
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        fact_a = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        conn.set_trace_callback(statements.append)
        first = resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_a, knowledge_cutoff=STAMP
        )
        conn.commit()
        assert first is not None
        assert _canonical_tuples(conn) == {
            (str(fact_a), f"financial_facts:{fact_a}:r1", first.resolution_id)
        }
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        fact_b = _insert_financial_fact(conn, document_id=2, value="110")
        second = resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_b, knowledge_cutoff=STAMP
        )
        assert second is not None
        assert _canonical_tuples(conn) == {
            (str(fact_b), f"financial_facts:{fact_b}:r1", second.resolution_id)
        }
        _assert_ledger_parity(conn)
        conn.rollback()
        assert _canonical_tuples(conn) == {
            (str(fact_a), f"financial_facts:{fact_a}:r1", first.resolution_id)
        }
        _assert_ledger_parity(conn)
        assert not any("current_fact_selection" in statement.lower() for statement in statements)
    finally:
        conn.close()


def test_unresolved_current_head_remains_excluded_and_rollback_restores_it(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = _connect(migrated_db(tmp_path / "canonical-unresolved.db"))
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        _seed_document(conn, document_id=3, source_tier="fmp_normalized", value_suffix="CONFLICT")
        fact_a = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_a, knowledge_cutoff=STAMP
        )
        conn.commit()
        baseline = _canonical_tuples(conn)
        conn.execute("BEGIN IMMEDIATE")
        fact_c = _insert_financial_fact(conn, document_id=3, value="130")
        result = resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_c, knowledge_cutoff=STAMP
        )
        assert result is not None
        assert result.resolution_status == "unresolved_material"
        assert _canonical_tuples(conn) == set()
        _assert_ledger_parity(conn)
        conn.rollback()
        assert _canonical_tuples(conn) == baseline
    finally:
        conn.close()


def test_new_capture_without_resolution_does_not_reuse_old_selection(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = _connect(migrated_db(tmp_path / "canonical-freshness.db"))
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        fact_id = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_id, knowledge_cutoff=STAMP
        )
        conn.commit()
        assert len(_canonical_tuples(conn)) == 1
        conn.execute("UPDATE financial_facts SET value=101 WHERE id=?", (fact_id,))
        conn.commit()
        assert _canonical_tuples(conn) == set()
        _assert_ledger_parity(conn)
        resolve_fact_row(
            conn, fact_table="financial_facts", fact_row_id=fact_id, knowledge_cutoff=STAMP
        )
        assert len(_canonical_tuples(conn)) == 1
        _assert_ledger_parity(conn)
    finally:
        conn.close()


def test_backfill_remains_bounded_and_resumes_old_checkpoint_without_cache_work(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = _connect(migrated_db(tmp_path / "canonical-backfill.db"))
    checkpoint = tmp_path / "state.json"
    statements: list[str] = []
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        for line_item, value in (("cogs", "50"), ("revenue", "100"), ("eps", "2")):
            _insert_financial_fact(conn, document_id=1, value=value, line_item=line_item)
        conn.commit()
        checkpoint.write_text(
            json.dumps(
                {
                    "table_index": 0,
                    "last_row_id": 0,
                    "updated_at": STAMP.isoformat(),
                    "selection_cursor": SELECTION_LOGICAL_KEY,
                }
            ),
            encoding="utf-8",
        )
        original_checkpoint = checkpoint.read_bytes()
        changes_before = conn.total_changes
        conn.set_trace_callback(statements.append)
        dry = execute_fact_cutover(
            conn,
            FactCutoverRequest(batch_size=2, checkpoint_path=checkpoint, knowledge_cutoff=STAMP),
        )
        assert dry.rows_considered == dry.rows_planned == 2
        assert conn.total_changes == changes_before
        assert checkpoint.read_bytes() == original_checkpoint
        assert _canonical_tuples(conn) == set()
        request = FactCutoverRequest(
            apply=True, batch_size=2, checkpoint_path=checkpoint, knowledge_cutoff=STAMP
        )
        first = execute_fact_cutover(conn, request)
        assert first.rows_captured == first.resolutions_created == 2
        assert first.checkpoint_complete is False
        state_one = json.loads(checkpoint.read_text(encoding="utf-8"))
        second = execute_fact_cutover(conn, request)
        assert second.rows_captured == second.resolutions_created == 1
        assert second.checkpoint_complete is True
        state_two = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert state_two["table_index"] >= state_one["table_index"]
        assert state_two["selection_cursor"] == SELECTION_LOGICAL_KEY
        settled = execute_fact_cutover(conn, request)
        assert settled.rows_captured == settled.resolutions_created == 0
        assert settled.checkpoint_complete is True
        assert (
            settled.selection_keys_examined
            == settled.selection_rows_written
            == settled.selection_rows_deleted
            == 0
        )
        assert len(_canonical_tuples(conn)) == 3
        _assert_ledger_parity(conn)
        assert not any("current_fact_selection" in statement.lower() for statement in statements)
        assert conn.execute("SELECT COUNT(*) FROM current_fact_selection").fetchone()[0] == 0
    finally:
        conn.close()


def test_0049_upgrade_downgrade_reupgrade_round_trip(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
) -> None:
    path = migrated_db(tmp_path / "selection-roundtrip.db")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")

    conn = _connect(path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'current_fact_selection' "
            "AND type = 'table'"
        ).fetchone()[0]
        == 1
    )
    conn.close()

    command.downgrade(config, "0048_metric_computation_output_observation")
    conn = _connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'current_fact_selection'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = "
                "'v_financial_facts_resolved_current' AND type = 'view'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = "
                "'v_kpi_facts_resolved_current' AND type = 'view'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    migrated_db(path, target="head", upgrade_existing=True)
    conn = _connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'current_fact_selection' "
                "AND type = 'table'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_no_runtime_writer_remains_for_unused_preparation_table() -> None:
    """Keep unused preparation state out of fact writes and scheduled backfill."""

    mutation = re.compile(
        r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
        r"\s+current_fact_selection\b",
        re.IGNORECASE,
    )
    writers = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for production_root in (PROJECT_ROOT / "src", PROJECT_ROOT / "execution")
        for path in production_root.rglob("*.py")
        if mutation.search(path.read_text(encoding="utf-8"))
    }
    assert writers == set()
