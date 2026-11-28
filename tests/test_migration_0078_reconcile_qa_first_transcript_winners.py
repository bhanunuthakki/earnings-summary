"""Tests for Alembic migration 0078_reconcile_qa_first_transcript_winners."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from tests.test_data_infrastructure_migration_chain import ROOT

_PRIOR_REVISION = "0077_repair_google_finance_non_dec_fye_transcript_periods"
_TARGET_REVISION = "0078_reconcile_qa_first_transcript_winners"


def _config(path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def _trigger_sqls(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name IN ('trg_transcripts_lifecycle_update', 'trg_transcripts_immutable_evidence')"
    ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _seed_transcript(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    transcript_id: int,
    ticker: str,
    fiscal_period_type: str,
    period_end: str,
    source: str,
    has_qa_section: int | None,
    segment_count: int,
    is_current: int = 1,
    superseded_by_id: int | None = None,
    recorded_at: str = "2026-11-20 00:00:00",
    version_number: int = 1,
) -> None:
    sha = f"{doc_id:064x}"
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, ?, 'transcript_audio', 'earnings_call_transcript', ?, ?, ?, ?, 'ok', 100)",
        (
            doc_id,
            ticker,
            period_end,
            f"transcripts/raw/{ticker}_{fiscal_period_type}_{doc_id}.txt",
            sha,
            recorded_at,
        ),
    )
    superseded_at = None if is_current == 1 else "2026-11-21 00:00:00"
    conn.execute(
        "INSERT INTO transcripts (id, document_id, ticker, fiscal_period_type, period_end, "
        "source, has_qa_section, version_number, is_current, is_active, recorded_at, "
        "superseded_at, superseded_by_id, superseded_by_transcript_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            transcript_id,
            doc_id,
            ticker,
            fiscal_period_type,
            period_end,
            source,
            has_qa_section,
            version_number,
            is_current,
            is_current,
            recorded_at,
            superseded_at,
            superseded_by_id,
            superseded_by_id,
        ),
    )
    for seq in range(segment_count):
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) "
            "VALUES (?, ?, 'Speaker', 'turn text')",
            (transcript_id, seq),
        )


def test_0078_reconciles_qa_first_winners_while_keeping_triggers_active(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    cfg = _config(db_path)
    command.downgrade(cfg, _PRIOR_REVISION)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        triggers_before = _trigger_sqls(conn)
        assert set(triggers_before) == {
            "trg_transcripts_lifecycle_update",
            "trg_transcripts_immutable_evidence",
        }

        # Group 1 (UBER Q3 2026): ir_transcript_doc with has_qa_section=0 vs
        # aggregator_google_finance_beta with has_qa_section=1 -> GFB wins.
        _seed_transcript(
            conn,
            doc_id=101,
            transcript_id=101,
            ticker="UBER",
            fiscal_period_type="Q3",
            period_end="2026-09-30 00:00:00",
            source="ir_transcript_doc",
            has_qa_section=0,
            segment_count=40,
            is_current=1,
            version_number=1,
        )
        _seed_transcript(
            conn,
            doc_id=102,
            transcript_id=102,
            ticker="UBER",
            fiscal_period_type="Q3",
            period_end="2026-09-30 00:00:00",
            source="aggregator_google_finance_beta",
            has_qa_section=1,
            segment_count=18,
            is_current=0,
            superseded_by_id=101,
            version_number=2,
        )

        # Group 2 (META Q4 2025): ir_transcript_doc with NULL has_qa_section vs
        # two aggregator_roic Q&A candidates with different segment counts ->
        # richer segment-count roic row (203) wins, and both old winner (201)
        # and thinner roic loser (202) point to 203.
        _seed_transcript(
            conn,
            doc_id=201,
            transcript_id=201,
            ticker="META",
            fiscal_period_type="Q4",
            period_end="2025-12-31 00:00:00",
            source="ir_transcript_doc",
            has_qa_section=None,
            segment_count=50,
            is_current=1,
            version_number=1,
        )
        _seed_transcript(
            conn,
            doc_id=202,
            transcript_id=202,
            ticker="META",
            fiscal_period_type="Q4",
            period_end="2025-12-31 00:00:00",
            source="aggregator_roic",
            has_qa_section=1,
            segment_count=8,
            is_current=0,
            superseded_by_id=201,
            version_number=2,
        )
        _seed_transcript(
            conn,
            doc_id=203,
            transcript_id=203,
            ticker="META",
            fiscal_period_type="Q4",
            period_end="2025-12-31 00:00:00",
            source="aggregator_roic",
            has_qa_section=1,
            segment_count=24,
            is_current=0,
            superseded_by_id=201,
            version_number=3,
        )

        # Group 3 (SNOW Q1 2026): manual_pdf no-QA winner vs two Q&A candidates
        # of different reliability tiers -> GFB (302) beats higher-segment roic (303).
        _seed_transcript(
            conn,
            doc_id=301,
            transcript_id=301,
            ticker="SNOW",
            fiscal_period_type="Q1",
            period_end="2026-04-30 00:00:00",
            source="manual_pdf",
            has_qa_section=0,
            segment_count=30,
            is_current=1,
            version_number=1,
        )
        _seed_transcript(
            conn,
            doc_id=302,
            transcript_id=302,
            ticker="SNOW",
            fiscal_period_type="Q1",
            period_end="2026-04-30 00:00:00",
            source="aggregator_google_finance_beta",
            has_qa_section=1,
            segment_count=12,
            is_current=0,
            superseded_by_id=301,
            version_number=2,
        )
        _seed_transcript(
            conn,
            doc_id=303,
            transcript_id=303,
            ticker="SNOW",
            fiscal_period_type="Q1",
            period_end="2026-04-30 00:00:00",
            source="aggregator_roic",
            has_qa_section=1,
            segment_count=45,
            is_current=0,
            superseded_by_id=301,
            version_number=3,
        )

        # Group 4 (NVDA Q1 2026): already-QA ir_transcript_doc winner must NOT be
        # demoted by a lower-tier Q&A candidate with more segments.
        _seed_transcript(
            conn,
            doc_id=401,
            transcript_id=401,
            ticker="NVDA",
            fiscal_period_type="Q1",
            period_end="2026-04-30 00:00:00",
            source="ir_transcript_doc",
            has_qa_section=1,
            segment_count=10,
            is_current=1,
            version_number=1,
        )
        _seed_transcript(
            conn,
            doc_id=402,
            transcript_id=402,
            ticker="NVDA",
            fiscal_period_type="Q1",
            period_end="2026-04-30 00:00:00",
            source="aggregator_google_finance_beta",
            has_qa_section=1,
            segment_count=60,
            is_current=0,
            superseded_by_id=401,
            version_number=2,
        )

        # Group 5 (WIX Q1 2025): no Q&A candidate at all -> untouched.
        _seed_transcript(
            conn,
            doc_id=501,
            transcript_id=501,
            ticker="WIX",
            fiscal_period_type="Q1",
            period_end="2025-03-31 00:00:00",
            source="ir_transcript_doc",
            has_qa_section=0,
            segment_count=20,
            is_current=1,
            version_number=1,
        )
        _seed_transcript(
            conn,
            doc_id=502,
            transcript_id=502,
            ticker="WIX",
            fiscal_period_type="Q1",
            period_end="2025-03-31 00:00:00",
            source="aggregator_roic",
            has_qa_section=0,
            segment_count=25,
            is_current=0,
            superseded_by_id=501,
            version_number=2,
        )
        conn.commit()

    command.upgrade(cfg, _TARGET_REVISION)

    def _row(conn: sqlite3.Connection, tid: int) -> tuple[object, ...]:
        res = conn.execute(
            "SELECT is_current, is_active, superseded_by_id, superseded_by_transcript_id, "
            "superseded_at IS NOT NULL, selection_reason FROM transcripts WHERE id = ?",
            (tid,),
        ).fetchone()
        assert res is not None
        return tuple(res)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )

        # Group 1 (UBER): 102 promoted, 101 demoted.
        assert _row(conn, 102) == (1, 1, None, None, 0, None)
        assert _row(conn, 101) == (0, 0, 102, 102, 1, "superseded_by_qa_first_policy")

        # Group 2 (META): 203 promoted, 201 demoted, 202 re-pointed to 203.
        assert _row(conn, 203) == (1, 1, None, None, 0, None)
        assert _row(conn, 201) == (0, 0, 203, 203, 1, "superseded_by_qa_first_policy")
        assert _row(conn, 202) == (0, 0, 203, 203, 1, None)

        # Group 3 (SNOW): 302 (GFB) promoted over 303 (roic); 301 & 303 point to 302.
        assert _row(conn, 302) == (1, 1, None, None, 0, None)
        assert _row(conn, 301) == (0, 0, 302, 302, 1, "superseded_by_qa_first_policy")
        assert _row(conn, 303) == (0, 0, 302, 302, 1, None)

        # Group 4 (NVDA): 401 already had Q&A -> untouched.
        assert _row(conn, 401) == (1, 1, None, None, 0, None)
        assert _row(conn, 402) == (0, 0, 401, 401, 1, None)

        # Group 5 (WIX): no Q&A candidate -> untouched.
        assert _row(conn, 501) == (1, 1, None, None, 0, None)
        assert _row(conn, 502) == (0, 0, 501, 501, 1, None)

        # Triggers remained verbatim and active.
        assert _trigger_sqls(conn) == triggers_before
        with pytest.raises(
            sqlite3.IntegrityError, match="transcript evidence versions are immutable"
        ):
            conn.execute("UPDATE transcripts SET has_qa_section = 1 WHERE id = 501")
        with pytest.raises(sqlite3.IntegrityError, match="invalid transcript lifecycle"):
            conn.execute("UPDATE transcripts SET is_current = 0 WHERE id = 401")

        assert conn.execute("PRAGMA foreign_key_check(transcripts)").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

        snapshot = conn.execute(
            "SELECT id, is_current, is_active, superseded_by_id, superseded_by_transcript_id, "
            "superseded_at, selection_reason FROM transcripts ORDER BY id"
        ).fetchall()

    # Idempotency: re-running upgrade after stamping back to 0077 is a 0-row no-op.
    command.stamp(cfg, _PRIOR_REVISION)
    command.upgrade(cfg, _TARGET_REVISION)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT id, is_current, is_active, superseded_by_id, superseded_by_transcript_id, "
                "superseded_at, selection_reason FROM transcripts ORDER BY id"
            ).fetchall()
            == snapshot
        )
        assert _trigger_sqls(conn) == triggers_before

    # Clean downgrade succeeds without error and preserves triggers.
    command.downgrade(cfg, _PRIOR_REVISION)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _PRIOR_REVISION,
        )
        assert _trigger_sqls(conn) == triggers_before


def test_0078_empty_db_is_noop_and_preserves_triggers(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "empty.db")
    cfg = _config(db_path)
    command.downgrade(cfg, _PRIOR_REVISION)
    with sqlite3.connect(db_path) as conn:
        triggers_before = _trigger_sqls(conn)

    command.upgrade(cfg, _TARGET_REVISION)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )
        assert _trigger_sqls(conn) == triggers_before
        assert conn.execute("PRAGMA foreign_key_check(transcripts)").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
