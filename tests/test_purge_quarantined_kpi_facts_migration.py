"""Tests for Alembic migration 0076_purge_quarantined_kpi_facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from tests.test_data_infrastructure_migration_chain import ROOT

_PRIOR_REVISION = "0075_admit_google_finance_beta_transcript_provider"
_TARGET_REVISION = "0076_purge_quarantined_kpi_facts"


def _config(path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def _insert_context(
    conn: sqlite3.Connection,
    *,
    ctx_id: int,
    kpi_fact_id: int,
    revision: int,
    supersedes_context_id: int | None,
    status: str,
    reason_code: str | None,
) -> None:
    is_admitted = status == "admitted"
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts ("
        "id,kpi_fact_id,revision,supersedes_context_id,metric_name_as_reported,"
        "reported_period_end,period_role,publication_lane,accounting_basis,"
        "consolidation_scope,dimensions_json,unit_scale,source_row_label,"
        "source_column_header,source_value_text,status,reason_code,reviewed_by,knowledge_at"
        ") VALUES (?,?,?,?,?,'2025-12-31','current',?,?,?,'{}','none','Row','Q4 FY25','100',?,?,?,?)",
        (
            ctx_id,
            kpi_fact_id,
            revision,
            supersedes_context_id,
            "Revenue",
            "current_actual" if is_admitted else "unclassified",
            "gaap" if is_admitted else "unknown",
            "consolidated" if is_admitted else "unknown",
            status,
            reason_code,
            "owner",
            "2026-10-03T00:00:00Z",
        ),
    )


def _insert_capture(
    conn: sqlite3.Connection, *, kpi_fact_id: int, quarantine_context_id: int
) -> None:
    payload = json.dumps({"kpi_fact_id": kpi_fact_id}, separators=(",", ":"))
    payload_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO kpi_legacy_disposition_captures ("
        "kpi_fact_id,quarantine_context_id,user_id,payload_json,payload_sha256,"
        "manifest_sha256,recorded_at"
        ") VALUES (?,?,'owner',?,?,?,'2026-10-03T00:00:00Z')",
        (kpi_fact_id, quarantine_context_id, payload, payload_sha, "a" * 64),
    )


def test_0076_purges_only_quarantined_facts_and_restores_triggers(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    cfg = _config(db_path)
    command.downgrade(cfg, _PRIOR_REVISION)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size,source_quality_tier) VALUES "
            "(99010,'TESTQ','llm_extracted','llm_summary','2025-12-31','.tmp/s.txt',?,?, 'ok',1,'fmp_normalized')",
            ("a" * 64, "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO kpi_definitions(id,ticker,name,unit,primary_source) "
            "VALUES (99001,'TESTQ','Customers','count','ir_doc')"
        )
        insert_trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_kpi_facts_observation_insert'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
        conn.executemany(
            "INSERT INTO kpi_facts("
            "id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
            "source_doc_id,supersedes_id"
            ") VALUES (?,'TESTQ',?,'Q4',99001,?,'count',99010,?)",
            [
                (99100, "2025-09-30", 10.0, None),  # quarantined-only -> purged
                (99101, "2025-12-31", 11.0, 99100),  # admitted, supersedes 99100 -> kept cleanly
                (99102, "2025-06-30", 20.0, None),  # mixed (rev1 quarantined -> rev2 admitted)
                (99103, "2025-03-31", 30.0, None),  # legacy_unknown -> kept intact
            ],
        )
        conn.execute(str(insert_trigger))

        _insert_context(
            conn,
            ctx_id=991000,
            kpi_fact_id=99100,
            revision=1,
            supersedes_context_id=None,
            status="quarantined",
            reason_code="exact_numeric_evidence_candidates",
        )
        _insert_context(
            conn,
            ctx_id=991001,
            kpi_fact_id=99101,
            revision=1,
            supersedes_context_id=None,
            status="admitted",
            reason_code=None,
        )
        _insert_context(
            conn,
            ctx_id=991002,
            kpi_fact_id=99102,
            revision=1,
            supersedes_context_id=None,
            status="quarantined",
            reason_code="exact_numeric_evidence_candidates",
        )
        _insert_context(
            conn,
            ctx_id=991003,
            kpi_fact_id=99102,
            revision=2,
            supersedes_context_id=991002,
            status="admitted",
            reason_code=None,
        )
        _insert_context(
            conn,
            ctx_id=991004,
            kpi_fact_id=99103,
            revision=1,
            supersedes_context_id=None,
            status="legacy_unknown",
            reason_code="legacy_unreviewed",
        )

        _insert_capture(conn, kpi_fact_id=99100, quarantine_context_id=991000)
        _insert_capture(conn, kpi_fact_id=99102, quarantine_context_id=991002)

        conn.execute(
            "INSERT INTO metric_computation_attempts("
            "id,ticker,period_end,fiscal_period_type,formula_id,status,"
            "kpi_fact_id,input_fingerprint,engine_version,computed_at"
            ") VALUES (99500,'TESTQ','2025-12-31','Q4',1,'ok',99100,'fp','v1','2026-10-03T00:00:00Z')"
        )
        conn.commit()

    command.upgrade(cfg, _TARGET_REVISION)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )
        facts = {
            int(row[0]): row[1]
            for row in conn.execute(
                "SELECT id, supersedes_id FROM kpi_facts WHERE ticker = 'TESTQ' ORDER BY id"
            )
        }
        assert facts == {99101: None, 99102: None, 99103: None}

        contexts = {
            int(row[0]): (int(row[1]), int(row[2]), str(row[3]))
            for row in conn.execute(
                "SELECT id, kpi_fact_id, revision, status "
                "FROM kpi_fact_semantic_contexts WHERE id >= 991000 ORDER BY id"
            )
        }
        assert contexts == {
            991001: (99101, 1, "admitted"),
            991002: (99102, 1, "quarantined"),
            991003: (99102, 2, "admitted"),
            991004: (99103, 1, "legacy_unknown"),
        }

        captures = [
            int(row[0])
            for row in conn.execute(
                "SELECT kpi_fact_id FROM kpi_legacy_disposition_captures "
                "WHERE kpi_fact_id >= 99100 ORDER BY kpi_fact_id"
            )
        ]
        assert captures == [99102]

        assert conn.execute(
            "SELECT kpi_fact_id FROM metric_computation_attempts WHERE id = 99500"
        ).fetchone() == (None,)

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM kpi_facts WHERE id = 99101")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM kpi_fact_semantic_contexts WHERE id = 991001")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM kpi_legacy_disposition_captures WHERE kpi_fact_id = 99102")

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

    command.downgrade(cfg, _PRIOR_REVISION)
    command.upgrade(cfg, _TARGET_REVISION)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0076_clears_supersedes_id_on_same_doc_replacement_without_unique_collision(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    """Surviving replacement on the same provenance key must not collide on uq_kpi_facts_provenance."""
    db_path = migrated_db(tmp_path / "same_doc_supersession.db")
    cfg = _config(db_path)
    command.downgrade(cfg, _PRIOR_REVISION)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size,source_quality_tier) VALUES "
            "(99020,'BKNG','ir_doc','ir_press_release','2025-03-31','ir_documents/BKNG/pr.pdf',?,?,'ok',1,'ir_primary')",
            ("b" * 64, "2026-10-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO kpi_definitions(id,ticker,name,unit,primary_source) "
            "VALUES (99002,'BKNG','Room Nights','millions','ir_doc')"
        )
        insert_trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_kpi_facts_observation_insert'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
        # Row A (quarantined, supersedes_id=NULL) and Row B (admitted, supersedes_id=A)
        # share the exact (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id).
        conn.executemany(
            "INSERT INTO kpi_facts("
            "id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,"
            "source_doc_id,supersedes_id"
            ") VALUES (?,'BKNG','2025-03-31','Q1',99002,?,'millions',99020,?)",
            [
                (99200, 310.0, None),
                (99201, 319.0, 99200),
            ],
        )
        conn.execute(str(insert_trigger))

        _insert_context(
            conn,
            ctx_id=992000,
            kpi_fact_id=99200,
            revision=1,
            supersedes_context_id=None,
            status="quarantined",
            reason_code="exact_numeric_evidence_candidates",
        )
        _insert_context(
            conn,
            ctx_id=992001,
            kpi_fact_id=99201,
            revision=1,
            supersedes_context_id=None,
            status="admitted",
            reason_code=None,
        )
        conn.commit()

    command.upgrade(cfg, _TARGET_REVISION)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )
        rows = conn.execute(
            "SELECT id, value, supersedes_id FROM kpi_facts WHERE ticker = 'BKNG' ORDER BY id"
        ).fetchall()
        assert rows == [(99201, 319.0, None)]
        assert conn.execute("PRAGMA foreign_key_check(kpi_facts)").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
