"""Physical current_fact_selection materialization contracts (migration 0049).

Pins the table's physical shape and unique key, the resolution layer's
single-writer in-transaction mirror semantics, the bounded/checkpointed/
idempotent convergence of the scheduled backfill entrypoint, and the
migration round trip that the scheduled job depends on.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from execution import backfill_financial_fact_resolutions as cutover_cli
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
    execute_fact_cutover,
    parse_fact_logical_key,
    refresh_current_fact_selection,
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


def _selection_tuples(conn: sqlite3.Connection) -> set[tuple[str, ...]]:
    return {
        tuple(str(value) for value in row)
        for row in conn.execute(
            "SELECT logical_key, fact_table, fact_row_id, fact_revision, observation_id, "
            "resolution_id, resolution_revision FROM current_fact_selection "
            "ORDER BY logical_key"
        ).fetchall()
    }


def _view_derived_tuples(conn: sqlite3.Connection) -> set[tuple[str, ...]]:
    return {
        tuple(str(value) for value in row)
        for row in conn.execute(
            _VIEW_DERIVED_SELECTION_SQL + " ORDER BY link.logical_key"
        ).fetchall()
    }


def test_parse_fact_logical_key_round_trips_both_table_kinds() -> None:
    financial = parse_fact_logical_key("financial_facts:ACME:revenue:2026-06-30:Q2")
    assert (financial.fact_table, financial.ticker, financial.concept_key) == (
        "financial_facts",
        "ACME",
        "revenue",
    )
    assert (financial.period_end, financial.fiscal_period_type) == ("2026-06-30", "Q2")
    kpi = parse_fact_logical_key("kpi_facts:ACME:kpi_definition:123:2026-06-30:Q2")
    assert kpi.concept_key == "kpi_definition:123"
    assert parse_fact_logical_key(
        "financial_facts:ACME:operating:margin:2026-06-30:Q2"
    ).concept_key == ("operating:margin")
    with pytest.raises(ValueError, match="not a fact logical key"):
        parse_fact_logical_key("transcripts:ACME:something:2026-06-30:Q2")
    with pytest.raises(ValueError, match="not a fact logical key"):
        parse_fact_logical_key("financial_facts:ACME:revenue:Q2")


def test_selection_table_is_physical_unique_and_reader_indexed(
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


def test_resolution_mirror_flips_and_reverts_with_the_transaction(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
) -> None:
    conn = _connect(migrated_db(tmp_path / "selection-flip.db"))
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        conn.commit()
        fact_a = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_a,
            knowledge_cutoff=STAMP,
        )
        conn.commit()
        mirrored = conn.execute(
            "SELECT * FROM current_fact_selection WHERE logical_key = ?",
            (SELECTION_LOGICAL_KEY,),
        ).fetchone()
        assert mirrored is not None
        assert mirrored["fact_row_id"] == fact_a
        assert mirrored["fact_table"] == "financial_facts"
        assert mirrored["ticker"] == "ACME"
        assert mirrored["concept_key"] == "revenue"
        assert mirrored["period_end"] == "2026-06-30"
        assert mirrored["fiscal_period_type"] == "Q2"
        assert mirrored["observation_id"] == f"financial_facts:{fact_a}:r1"
        assert _selection_tuples(conn) == _view_derived_tuples(conn)

        # A higher-tier successor resolves inside the caller's transaction and
        # the mirror flips with it; a rollback reverts both together.
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        fact_b = _insert_financial_fact(conn, document_id=2, value="110")
        resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_b,
            knowledge_cutoff=STAMP,
        )
        flipped = conn.execute(
            "SELECT fact_row_id, observation_id, resolution_revision "
            "FROM current_fact_selection WHERE logical_key = ?",
            (SELECTION_LOGICAL_KEY,),
        ).fetchone()
        assert flipped is not None
        assert flipped["fact_row_id"] == fact_b
        assert flipped["observation_id"] == f"financial_facts:{fact_b}:r1"
        assert flipped["resolution_revision"] == 2
        conn.rollback()
        reverted = conn.execute(
            "SELECT fact_row_id, resolution_revision FROM current_fact_selection "
            "WHERE logical_key = ?",
            (SELECTION_LOGICAL_KEY,),
        ).fetchone()
        assert reverted is not None
        assert reverted["fact_row_id"] == fact_a
        assert reverted["resolution_revision"] == 1
        assert _selection_tuples(conn) == _view_derived_tuples(conn)
    finally:
        conn.close()


def test_unresolved_material_outcome_removes_the_selection_row(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
) -> None:
    conn = _connect(migrated_db(tmp_path / "selection-unresolved.db"))
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        _seed_document(conn, document_id=3, source_tier="fmp_normalized", value_suffix="CONFLICT")
        conn.commit()
        fact_a = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_a,
            knowledge_cutoff=STAMP,
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM current_fact_selection WHERE logical_key = ?",
                (SELECTION_LOGICAL_KEY,),
            ).fetchone()[0]
            == 1
        )
        # A same-tier, materially conflicting successor leaves the key with no
        # canonical row: the materialized selection must drop it in the same
        # transaction, never surface the unresolved key as selected.
        conn.execute("BEGIN IMMEDIATE")
        fact_c = _insert_financial_fact(conn, document_id=3, value="130")
        unresolved = resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=fact_c,
            knowledge_cutoff=STAMP,
        )
        assert unresolved is not None
        assert unresolved.resolution_status == "unresolved_material"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM current_fact_selection WHERE logical_key = ?",
                (SELECTION_LOGICAL_KEY,),
            ).fetchone()[0]
            == 0
        )
        conn.rollback()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM current_fact_selection WHERE logical_key = ?",
                (SELECTION_LOGICAL_KEY,),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_selection_sync_repairs_drift_and_converges_idempotently(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
) -> None:
    conn = _connect(migrated_db(tmp_path / "selection-sync.db"))
    try:
        logical_keys = _seed_resolved_pair(conn)
        assert _selection_tuples(conn) == _view_derived_tuples(conn)

        # Simulate drift the per-key mirror cannot have produced: a deleted
        # row, a corrupted row, and a foreign-key-valid stale row anchored at
        # a key with no ledger state at all.
        conn.execute("DELETE FROM current_fact_selection WHERE logical_key = ?", (logical_keys[0],))
        conn.execute(
            "UPDATE current_fact_selection SET fact_row_id = fact_row_id + 1000 "
            "WHERE logical_key = ?",
            (logical_keys[1],),
        )
        stale_key = "financial_facts:ACME:stale:2026-06-30:Q2"
        conn.execute(
            "INSERT INTO current_fact_selection (logical_key, fact_table, fact_row_id, "
            "fact_revision, observation_id, resolution_id, resolution_revision, ticker, "
            "concept_key, period_end, fiscal_period_type) "
            "SELECT ?, fact_table, fact_row_id, fact_revision, observation_id, "
            "resolution_id, resolution_revision, ticker, concept_key, period_end, "
            "fiscal_period_type FROM current_fact_selection WHERE logical_key = ?",
            (stale_key, logical_keys[1]),
        )
        conn.commit()
        assert _selection_tuples(conn) != _view_derived_tuples(conn)

        # Bounded window beyond the corrupted key: only that key is examined
        # and its row is rewritten.
        first = refresh_current_fact_selection(conn, batch_size=1, cursor=logical_keys[0])
        assert first.keys_examined == 1
        assert first.next_cursor == logical_keys[1]
        assert first.rows_written == 1
        assert first.rows_deleted == 0
        assert first.wrapped is False

        # The pass runs past the last captured key: the unanchored stale row
        # beyond it is deleted, the pass wraps, and the wrapped window
        # restores the deleted first key.
        second = refresh_current_fact_selection(conn, batch_size=5, cursor=first.next_cursor)
        assert second.wrapped is True
        assert second.keys_examined == 2
        assert second.rows_written == 1
        assert second.rows_deleted == 1
        assert _selection_tuples(conn) == _view_derived_tuples(conn)

        # Further passes verify the whole key space and issue no writes.
        third = refresh_current_fact_selection(conn, batch_size=5, cursor=second.next_cursor)
        assert third.wrapped is True
        assert third.keys_examined == 2
        assert third.rows_written == 0
        assert third.rows_deleted == 0
        fourth = refresh_current_fact_selection(conn, batch_size=5, cursor=third.next_cursor)
        assert fourth.keys_examined == 2
        assert fourth.rows_written == 0
        assert fourth.rows_deleted == 0
        assert _selection_tuples(conn) == _view_derived_tuples(conn)
    finally:
        conn.close()


def test_cli_backfill_is_dry_run_default_bounded_checkpointed_and_idempotent(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = migrated_db(tmp_path / "selection-cli.db")
    conn = _connect(path)
    _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
    # Model rows that predate migration 0225: no automatic capture on insert.
    conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
    for line_item, value in (("cogs", "50"), ("revenue", "100"), ("eps", "2")):
        _insert_financial_fact(conn, document_id=1, value=value, line_item=line_item)
    conn.commit()
    conn.close()

    task_dir = PROJECT_ROOT / ".tmp" / "selection-cli-test-state"
    shutil.rmtree(task_dir, ignore_errors=True)
    argv = ["--db", str(path), "--task-id", "selection-cli-test-state", "--batch-size", "2"]
    try:
        # Dry-run is the default and performs zero writes.
        assert cutover_cli.main([*argv]) == 0
        dry_run = json.loads(capsys.readouterr().out)
        assert dry_run["rows_planned"] == 2
        assert dry_run["rows_considered"] == 2
        assert dry_run["apply"] is False
        assert not task_dir.exists()
        conn = _connect(path)
        assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM current_fact_selection").fetchone()[0] == 0
        conn.close()

        # Apply one bounded batch of two rows; the checkpoint advances only
        # after the commit. The per-key mirror writes each resolved key inside
        # the same transaction, so the bounded convergence window verifies the
        # captured keys without writing again.
        assert cutover_cli.main([*argv, "--apply"]) == 0
        applied = json.loads(capsys.readouterr().out)
        assert applied["rows_captured"] == 2
        assert applied["rows_considered"] == 2
        assert applied["resolutions_created"] == 2
        assert applied["selection_keys_examined"] == 2
        assert applied["selection_rows_written"] == 0
        assert applied["checkpoint_complete"] is False
        state_one = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        assert state_one["table_index"] == 0
        conn = _connect(path)
        assert conn.execute("SELECT COUNT(*) FROM current_fact_selection").fetchone()[0] == 2
        conn.close()

        # Apply the remaining bounded batch; the already-resolved keys replay
        # without new revisions.
        assert cutover_cli.main([*argv, "--apply"]) == 0
        resumed = json.loads(capsys.readouterr().out)
        assert resumed["rows_captured"] == 1
        assert resumed["resolutions_created"] == 1
        assert resumed["resolutions_replayed"] == 0
        state_two = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

        # Third apply: nothing left to capture or resolve, the capture cursor
        # only advances, and the selection pass re-verifies the remaining
        # bounded window with zero writes. Sorted keys run cogs < eps <
        # revenue, so this pass sees just the last key.
        assert cutover_cli.main([*argv, "--apply"]) == 0
        settled = json.loads(capsys.readouterr().out)
        assert settled["rows_considered"] == 0
        assert settled["rows_captured"] == 0
        assert settled["resolutions_created"] == 0
        assert settled["selection_keys_examined"] == 1
        assert settled["selection_pass_wrapped"] is False
        assert settled["selection_rows_written"] == 0
        assert settled["selection_rows_deleted"] == 0
        assert settled["checkpoint_complete"] is True
        state_three = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        assert state_three["table_index"] >= state_two["table_index"]
        assert state_three["selection_cursor"] == SELECTION_LOGICAL_KEY

        # Fourth apply: converged steady state — the pass past the last key
        # wraps to the start of the key space and re-verifies the first
        # window, still with zero writes.
        assert cutover_cli.main([*argv, "--apply"]) == 0
        steady = json.loads(capsys.readouterr().out)
        assert steady["rows_considered"] == 0
        assert steady["selection_keys_examined"] == 2
        assert steady["selection_pass_wrapped"] is True
        assert steady["selection_rows_written"] == 0
        assert steady["selection_rows_deleted"] == 0
        state_four = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        assert state_four["table_index"] == state_three["table_index"]
        assert state_four["last_row_id"] == state_three["last_row_id"]
        assert state_four["selection_cursor"] == state_two["selection_cursor"]

        # Fifth apply: the cycle returns to the third apply's cursor with
        # zero writes — the bounded pass repeats without changing anything,
        # which is the idempotent steady state.
        assert cutover_cli.main([*argv, "--apply"]) == 0
        cycled = json.loads(capsys.readouterr().out)
        assert cycled["selection_pass_wrapped"] is False
        assert cycled["selection_keys_examined"] == 1
        assert cycled["selection_rows_written"] == 0
        assert cycled["selection_rows_deleted"] == 0
        state_five = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        assert state_five["table_index"] == state_three["table_index"]
        assert state_five["last_row_id"] == state_three["last_row_id"]
        assert state_five["selection_cursor"] == state_three["selection_cursor"]

        conn = _connect(path)
        try:
            assert _selection_tuples(conn) == _view_derived_tuples(conn)
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def test_execute_fact_cutover_reconciles_selection_without_separate_cursor(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
) -> None:
    conn = _connect(migrated_db(tmp_path / "selection-cutover.db"))
    try:
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        # Resolve one key first so its mirrored row exists.
        resolved_fact_id = _insert_financial_fact(conn, document_id=1, value="100")
        conn.commit()
        resolve_fact_row(
            conn,
            fact_table="financial_facts",
            fact_row_id=resolved_fact_id,
            knowledge_cutoff=STAMP,
        )
        conn.commit()
        # Model one row that predates migration 0225 for the same batch, and
        # drift the already-mirrored key so only the batch's bounded
        # convergence window can restore it.
        conn.execute("DROP TRIGGER trg_financial_facts_observation_insert")
        _insert_financial_fact(conn, document_id=1, value="50", line_item="cogs")
        conn.execute(
            "DELETE FROM current_fact_selection WHERE logical_key = ?",
            (SELECTION_LOGICAL_KEY,),
        )
        conn.commit()
        checkpoint = tmp_path / "selection-cutover-state.json"
        summary = execute_fact_cutover(
            conn,
            FactCutoverRequest(
                apply=True,
                batch_size=10,
                checkpoint_path=checkpoint,
                knowledge_cutoff=STAMP,
            ),
        )
        assert summary.rows_captured == 1
        assert summary.selection_keys_examined == 2
        assert summary.selection_rows_written == 1
        assert _selection_tuples(conn) == _view_derived_tuples(conn)
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert state["selection_cursor"] == SELECTION_LOGICAL_KEY
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

    command.upgrade(config, "head")
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


def test_current_fact_selection_writes_are_confined_to_the_resolution_layer() -> None:
    """Only the resolution layer may mutate the materialized selection (F1)."""

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
    assert writers == {"src/provenance/financial_fact_resolution.py"}
