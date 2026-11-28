"""Tests for Alembic migration 0077_repair_google_finance_non_dec_fye_transcript_periods."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from tests.test_data_infrastructure_migration_chain import ROOT

_PRIOR_REVISION = "0076_purge_quarantined_kpi_facts"
_TARGET_REVISION = "0077_repair_google_finance_non_dec_fye_transcript_periods"

_REPAIRS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "BIRK",
        "Q3",
        "db07a4baa7296844773e934c230a292b1d7f5852cd29ec00ca1f3bd326034222",  # pragma: allowlist secret
        "2026-09-30 00:00:00",
        "2026-06-30 00:00:00",
    ),
    (
        "TJX",
        "Q3",
        "880077dbc9f5d79dce93db440677517e356b76899bef9239d7463b2abeb78dfe",  # pragma: allowlist secret
        "2027-09-30 00:00:00",
        "2026-10-31 00:00:00",
    ),
    (
        "TWST",
        "Q4",
        "510c70fbf15ecfefa844d2259e929a80bc65fbd844ce8c6a4cdf01121281be0e",  # pragma: allowlist secret
        "2026-12-31 00:00:00",
        "2026-09-30 00:00:00",
    ),
    (
        "VFC",
        "Q2",
        "157b4d871e944d437a35638d255f86ddd14ac5779a12ec5b47664b5e1bb03dda",  # pragma: allowlist secret
        "2027-06-30 00:00:00",
        "2026-09-30 00:00:00",
    ),
)


def _config(path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def _seed_transcript(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    transcript_id: int,
    ticker: str,
    fiscal_period_type: str,
    sha256: str,
    period_end: str,
    source: str = "aggregator_google_finance_beta",
) -> None:
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, ?, 'ir_doc', 'earnings_call_transcript', ?, ?, ?, "
        "'2026-11-27T00:00:00Z', 'ok', 100)",
        (
            doc_id,
            ticker,
            period_end,
            f"transcripts/raw/{ticker}_{fiscal_period_type}__{sha256[:8]}.txt",
            sha256,
        ),
    )
    conn.execute(
        "INSERT INTO transcripts (id, document_id, ticker, fiscal_period_type, period_end, "
        "source, is_active, is_current, version_number) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1)",
        (transcript_id, doc_id, ticker, fiscal_period_type, period_end, source),
    )


def _trigger_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='trg_transcripts_immutable_evidence'"
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def test_0077_repairs_four_google_finance_fye_transcripts_and_restores_trigger(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db", target=_PRIOR_REVISION)
    cfg = _config(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        trigger_before = _trigger_sql(conn)
        assert trigger_before is not None

        for idx, (ticker, fp, sha256, old_pe, _new_pe) in enumerate(_REPAIRS, start=1):
            _seed_transcript(
                conn,
                doc_id=100 + idx,
                transcript_id=200 + idx,
                ticker=ticker,
                fiscal_period_type=fp,
                sha256=sha256,
                period_end=old_pe,
            )

        # Unrelated rows: different sha256 or different source must remain untouched.
        _seed_transcript(
            conn,
            doc_id=190,
            transcript_id=290,
            ticker="BIRK",
            fiscal_period_type="Q2",
            sha256="f" * 64,
            period_end="2026-03-31 00:00:00",
        )
        _seed_transcript(
            conn,
            doc_id=191,
            transcript_id=291,
            ticker="TWST",
            fiscal_period_type="Q3",
            sha256="e" * 64,
            period_end="2026-06-30 00:00:00",
            source="issuer_ir",
        )
        conn.commit()

        # Verify trigger actively blocks direct period_end updates before migration.
        with pytest.raises(
            sqlite3.IntegrityError, match="transcript evidence versions are immutable"
        ):
            conn.execute("UPDATE transcripts SET period_end='2026-06-30 00:00:00' WHERE id=201")

    command.upgrade(cfg, _TARGET_REVISION)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )
        for idx, (_ticker, _fp, _sha256, _old_pe, new_pe) in enumerate(_REPAIRS, start=1):
            assert conn.execute(
                "SELECT period_end FROM transcripts WHERE id=?", (200 + idx,)
            ).fetchone() == (new_pe,)
            assert conn.execute(
                "SELECT period_end FROM documents WHERE id=?", (100 + idx,)
            ).fetchone() == (new_pe,)

        # Unrelated rows stay intact.
        assert conn.execute("SELECT period_end FROM transcripts WHERE id=290").fetchone() == (
            "2026-03-31 00:00:00",
        )
        assert conn.execute("SELECT period_end FROM transcripts WHERE id=291").fetchone() == (
            "2026-06-30 00:00:00",
        )

        # Trigger is restored verbatim and still blocks post-migration updates.
        assert _trigger_sql(conn) == trigger_before
        with pytest.raises(
            sqlite3.IntegrityError, match="transcript evidence versions are immutable"
        ):
            conn.execute("UPDATE transcripts SET period_end='2026-09-30 00:00:00' WHERE id=201")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

    # Idempotency on already-repaired DB (stamp back to 0076 and re-run upgrade).
    command.stamp(cfg, _PRIOR_REVISION)
    command.upgrade(cfg, _TARGET_REVISION)
    with sqlite3.connect(db_path) as conn:
        for idx, (_ticker, _fp, _sha256, _old_pe, new_pe) in enumerate(_REPAIRS, start=1):
            assert conn.execute(
                "SELECT period_end FROM transcripts WHERE id=?", (200 + idx,)
            ).fetchone() == (new_pe,)
            assert conn.execute(
                "SELECT period_end FROM documents WHERE id=?", (100 + idx,)
            ).fetchone() == (new_pe,)
        assert _trigger_sql(conn) == trigger_before

    # Clean downgrade reverses all 4 rows and preserves the trigger.
    command.downgrade(cfg, _PRIOR_REVISION)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _PRIOR_REVISION,
        )
        for idx, (_ticker, _fp, _sha256, old_pe, _new_pe) in enumerate(_REPAIRS, start=1):
            assert conn.execute(
                "SELECT period_end FROM transcripts WHERE id=?", (200 + idx,)
            ).fetchone() == (old_pe,)
            assert conn.execute(
                "SELECT period_end FROM documents WHERE id=?", (100 + idx,)
            ).fetchone() == (old_pe,)
        assert _trigger_sql(conn) == trigger_before
        with pytest.raises(
            sqlite3.IntegrityError, match="transcript evidence versions are immutable"
        ):
            conn.execute("UPDATE transcripts SET period_end='2026-06-30 00:00:00' WHERE id=201")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # Re-upgrade cleanly repairs all 4 rows again.
    command.upgrade(cfg, _TARGET_REVISION)
    with sqlite3.connect(db_path) as conn:
        for idx, (_ticker, _fp, _sha256, _old_pe, new_pe) in enumerate(_REPAIRS, start=1):
            assert conn.execute(
                "SELECT period_end FROM transcripts WHERE id=?", (200 + idx,)
            ).fetchone() == (new_pe,)


def test_0077_empty_db_is_noop_and_preserves_trigger(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "empty.db", target=_PRIOR_REVISION)
    cfg = _config(db_path)
    with sqlite3.connect(db_path) as conn:
        trigger_before = _trigger_sql(conn)
        assert trigger_before is not None

    command.upgrade(cfg, _TARGET_REVISION)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _TARGET_REVISION,
        )
        assert _trigger_sql(conn) == trigger_before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_0077_collision_guard_skips_conflicting_row_without_aborting(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "collision.db", target=_PRIOR_REVISION)
    cfg = _config(db_path)

    birk_ticker, birk_fp, birk_sha, birk_old, birk_new = _REPAIRS[0]
    twst_ticker, twst_fp, twst_sha, twst_old, twst_new = _REPAIRS[2]

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        trigger_before = _trigger_sql(conn)
        assert trigger_before is not None
        # Seed the target BIRK row PLUS a pre-existing BIRK Q3 transcript at the destination period.
        _seed_transcript(
            conn,
            doc_id=301,
            transcript_id=401,
            ticker=birk_ticker,
            fiscal_period_type=birk_fp,
            sha256=birk_sha,
            period_end=birk_old,
        )
        _seed_transcript(
            conn,
            doc_id=302,
            transcript_id=402,
            ticker=birk_ticker,
            fiscal_period_type=birk_fp,
            sha256="9" * 64,
            period_end=birk_new,
            source="issuer_ir",
        )
        # Seed TWST with no collision; it must still be repaired.
        _seed_transcript(
            conn,
            doc_id=303,
            transcript_id=403,
            ticker=twst_ticker,
            fiscal_period_type=twst_fp,
            sha256=twst_sha,
            period_end=twst_old,
        )
        conn.commit()

    command.upgrade(cfg, _TARGET_REVISION)

    with sqlite3.connect(db_path) as conn:
        # Conflicting BIRK row is safely skipped; existing destination row is intact.
        assert conn.execute("SELECT period_end FROM transcripts WHERE id=401").fetchone() == (
            birk_old,
        )
        assert conn.execute("SELECT period_end FROM documents WHERE id=301").fetchone() == (
            birk_old,
        )
        assert conn.execute("SELECT period_end FROM transcripts WHERE id=402").fetchone() == (
            birk_new,
        )
        # Non-conflicting TWST row is repaired cleanly.
        assert conn.execute("SELECT period_end FROM transcripts WHERE id=403").fetchone() == (
            twst_new,
        )
        assert conn.execute("SELECT period_end FROM documents WHERE id=303").fetchone() == (
            twst_new,
        )
        assert _trigger_sql(conn) == trigger_before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
