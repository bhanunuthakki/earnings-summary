"""Novel management indicators stay source-bound staging records, never KPI facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from compute.management_indicators import (
    IndicatorPromotionStatus,
    IndicatorRecurrence,
    IndicatorScope,
    ManagementIndicatorInput,
    ManagementIndicatorSchemaError,
    mark_indicator_reviewed,
    persist_indicator,
)
from models.facts import Unit


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, ticker TEXT NOT NULL
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY, transcript_id INTEGER NOT NULL, seq INTEGER NOT NULL,
            speaker TEXT, time_code_start TEXT, text TEXT NOT NULL
        );
        CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, value NUMERIC);
        CREATE TABLE management_indicator_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            transcript_segment_id INTEGER NOT NULL,
            source_doc_id INTEGER NOT NULL,
            raw_label TEXT NOT NULL,
            value TEXT NOT NULL,
            unit TEXT NOT NULL,
            scope TEXT NOT NULL,
            speaker TEXT,
            source_excerpt TEXT NOT NULL,
            source_locator_json TEXT NOT NULL,
            recurrence TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            created_at TEXT,
            reviewed_at TEXT,
            reviewed_by TEXT
        );
        INSERT INTO documents VALUES (7, 'ACME');
        INSERT INTO transcripts VALUES (4, 7, 'ACME');
        INSERT INTO transcript_segments VALUES (
            11, 4, 3, 'CFO', '00:14:02',
            'We completed 42 AI-assisted onboarding pilots this quarter.'
        );
        """
    )
    return conn


def _indicator() -> ManagementIndicatorInput:
    return ManagementIndicatorInput(
        ticker="acme",
        transcript_segment_id=11,
        raw_label="AI-assisted onboarding completions",
        value=Decimal("42"),
        unit=Unit.COUNT,
        scope=IndicatorScope.PRODUCT,
        recurrence=IndicatorRecurrence.ONE_OFF,
        source_excerpt="We completed 42 AI-assisted onboarding pilots this quarter.",
    )


def test_missing_staging_table_fails_closed_instead_of_dropping_indicator() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(
            ManagementIndicatorSchemaError, match="0031_add_management_indicator_observations"
        ):
            persist_indicator(conn, indicator=_indicator())
    finally:
        conn.close()


def test_persisted_indicator_is_idempotent_source_bound_and_pending_review() -> None:
    conn = _conn()
    try:
        first = persist_indicator(conn, indicator=_indicator())
        second = persist_indicator(conn, indicator=_indicator())
        row = conn.execute("SELECT * FROM management_indicator_observations").fetchone()

        assert first == second == 1
        assert row is not None
        assert row["source_doc_id"] == 7
        assert row["speaker"] == "CFO"
        assert row["promotion_status"] == "pending_review"
        assert row["recurrence"] == "one_off"
        assert json.loads(row["source_locator_json"]) == {
            "document_id": 7,
            "segment_sequence": 3,
            "time_code_start": "00:14:02",
            "transcript_id": 4,
            "transcript_segment_id": 11,
        }
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    finally:
        conn.close()


def test_review_status_does_not_write_a_canonical_kpi_fact() -> None:
    conn = _conn()
    try:
        indicator_id = persist_indicator(conn, indicator=_indicator())
        assert indicator_id is not None
        mark_indicator_reviewed(
            conn,
            indicator_id=indicator_id,
            status=IndicatorPromotionStatus.PROMOTED,
            reviewed_by="owner",
        )
        row = conn.execute(
            "SELECT promotion_status, reviewed_at, reviewed_by "
            "FROM management_indicator_observations WHERE id=?",
            (indicator_id,),
        ).fetchone()
        assert row is not None
        assert row["promotion_status"] == "promoted"
        assert row["reviewed_at"] is not None
        assert row["reviewed_by"] == "owner"
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    finally:
        conn.close()


def test_active_migration_head_installs_indicator_staging_table(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")

    conn = sqlite3.connect(db_path)
    try:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(management_indicator_observations)")
        }
        assert {
            "raw_label",
            "value",
            "unit",
            "scope",
            "speaker",
            "source_locator_json",
            "recurrence",
            "promotion_status",
            "reviewed_by",
        } <= columns
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_management_indicator_%'"
            )
        }
        assert {
            "trg_management_indicator_source_immutable",
            "trg_management_indicator_review_once",
            "trg_management_indicator_no_delete",
        } <= triggers
    finally:
        conn.close()


def test_indicator_requires_matching_issuer_and_verbatim_segment() -> None:
    conn = _conn()
    try:
        with pytest.raises(ValueError, match="does not match transcript/document ticker"):
            persist_indicator(conn, indicator=_indicator().model_copy(update={"ticker": "OTHER"}))
        with pytest.raises(ValueError, match="must match exactly one transcript segment"):
            persist_indicator(
                conn,
                indicator=_indicator().model_copy(
                    update={"source_excerpt": "Invented metric claim."}
                ),
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM management_indicator_observations").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_indicator_resolves_the_exact_supporting_segment() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO transcript_segments VALUES (12, 4, 4, 'CEO', '00:15:00', ?)",
        ("We launched 17 enterprise pilots in Brazil.",),
    )
    try:
        indicator = _indicator().model_copy(
            update={
                "raw_label": "Brazil enterprise pilots",
                "value": Decimal("17"),
                "source_excerpt": "We launched 17 enterprise pilots in Brazil.",
                "speaker": None,
            }
        )
        indicator_id = persist_indicator(conn, indicator=indicator)
        row = conn.execute(
            "SELECT transcript_segment_id,speaker,source_locator_json "
            "FROM management_indicator_observations WHERE id=?",
            (indicator_id,),
        ).fetchone()
        assert row is not None
        assert row["transcript_segment_id"] == 12
        assert row["speaker"] == "CEO"
        assert json.loads(row["source_locator_json"])["segment_sequence"] == 4
    finally:
        conn.close()
