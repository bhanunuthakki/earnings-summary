# pyright: reportPrivateUsage=false
"""Tests for execution/restore_drill.py — the monthly DB restore drill.

The drill restores the newest backup snapshot to a throwaway temp file and
verifies it (integrity via restore_db + core-table row counts + a soft schema
match). These tests build synthetic .gz snapshots in tmp_path and never touch a
real DB; ingestion_runs bookkeeping is skipped by pointing --db at a path that
does not exist.
"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from execution import restore_drill
from runtime.backup_crypto import encrypt_file, load_or_create_key


@pytest.fixture(autouse=True)
def _isolated_backup_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(tmp_path / "secrets"))


def _make_snapshot(
    backup_dir: Path,
    *,
    populated: bool = True,
    version: str = "0113_x",
    name: str = "portfolio.db.20260715T090000Z.gz.enc",
) -> Path:
    """Write a gzipped SQLite snapshot restore_db.list_snapshots will discover."""
    raw = backup_dir / "_raw.db"
    conn = sqlite3.connect(str(raw))
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (version,))
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT)")
    conn.execute("CREATE TABLE financial_facts (id INTEGER)")
    if populated:
        conn.execute("INSERT INTO tracked_companies VALUES ('NU')")
        conn.execute("INSERT INTO financial_facts VALUES (1)")
    conn.commit()
    conn.close()
    plain = backup_dir / "_snapshot.gz"
    with open(raw, "rb") as f, gzip.open(plain, "wb") as g:
        g.write(f.read())
    raw.unlink()
    encrypted = backup_dir / name
    encrypt_file(plain, encrypted, key=load_or_create_key())
    plain.unlink()
    return encrypted


def test_drill_passes_on_healthy_snapshot(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir, populated=True)

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is True
    assert summary["status"] == "ok"
    assert summary["row_counts"] == {"tracked_companies": 1, "financial_facts": 1}
    assert summary["schema_match"] is True  # live absent → soft check passes


def test_drill_fails_on_empty_core_tables(tmp_path: Path) -> None:
    """A snapshot that passes integrity_check but has empty core tables (e.g. a
    truncated/half-written backup) must be caught."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir, populated=False)

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is False
    assert summary["status"] == "empty_core_tables"


def test_no_snapshot_reports_status(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is False
    assert summary["status"] == "no_snapshot"


def test_corrupt_snapshot_fails(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "portfolio.db.20260715T090000Z.gz.enc").write_bytes(b"this is not encrypted")

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is False
    assert summary["status"] == "restore_failed"


def test_schema_mismatch_warns_but_passes(tmp_path: Path) -> None:
    """A snapshot whose schema differs from the live DB warns (a migration after
    the last backup is legitimate) but does not fail the drill."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir, version="0100_old")

    live = tmp_path / "live.db"
    conn = sqlite3.connect(str(live))
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES ('0200_new')")
    conn.commit()
    conn.close()

    ok, summary = restore_drill.run_drill(bdir, live)

    assert ok is True
    assert summary["schema_match"] is False
    assert "warning" in summary


def test_temp_restore_is_cleaned_up(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir)
    before = set(Path(tmp_path).rglob("drill.db"))
    restore_drill.run_drill(bdir, tmp_path / "absent.db")
    after = set(Path(tmp_path).rglob("drill.db"))
    assert before == after == set()  # no leftover temp restore


def test_main_exit_codes(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    absent = tmp_path / "absent.db"

    # no snapshot → exit 2
    assert restore_drill.main(["--backup-dir", str(bdir), "--db", str(absent)]) == 2

    # healthy snapshot → exit 0
    _make_snapshot(bdir)
    assert restore_drill.main(["--backup-dir", str(bdir), "--db", str(absent)]) == 0


def test_main_defaults_to_canonical_env_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / "canonical.db"
    observed: list[Path] = []

    def _fake_drill(
        backup_dir: Path,
        live_db: Path,
        *,
        keep: bool = False,
        **_kwargs: object,
    ) -> tuple[bool, dict[str, object]]:
        del backup_dir, keep
        observed.append(live_db)
        return True, {"status": "ok"}

    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(canonical))
    monkeypatch.setattr(restore_drill, "run_drill", _fake_drill)

    def no_snapshot(_backup_dir: Path) -> None:
        return None

    def no_accounting(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(restore_drill, "_latest_snapshot", no_snapshot)
    monkeypatch.setattr(restore_drill, "_start_accounting", no_accounting)

    assert restore_drill.main([]) == 0
    assert observed == [canonical]


class TestArchiveDrillLeg:
    """restore_drill._drill_archive — the archive half of the monthly drill."""

    @staticmethod
    def _live_with_archive(tmp_path: Path) -> Path:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))
        import db_gc

        live = tmp_path / "portfolio.db"
        conn = sqlite3.connect(live)
        conn.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
                fiscal_period_type TEXT, line_item TEXT, value NUMERIC,
                extracted_by TEXT, supersedes_id INTEGER REFERENCES financial_facts(id)
            );
            CREATE INDEX ix_0270_financial_facts_supersedes_id
                ON financial_facts(supersedes_id);
            """
        )
        # Archive one fact under run r1, then delete it (a normal prune).
        db_gc.attach_archive(conn, live.parent / "archive" / db_gc.ARCHIVE_NAME)
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES (1, 'NU', '2020-12-31', 'Q4', 'revenue', 9, 'fmp')"
        )
        db_gc._reset_doomed(conn)
        conn.execute("INSERT INTO _gc_doomed (id) VALUES (1)")
        db_gc._archive_doomed(conn, table="financial_facts", run_at="r1", policy="facts-depth")
        conn.execute("DELETE FROM financial_facts WHERE id = 1")
        conn.commit()
        conn.close()
        return live

    def test_no_archive_is_vacuously_ok(self, tmp_path: Path) -> None:
        live = tmp_path / "portfolio.db"
        sqlite3.connect(live).close()
        ok, summary = restore_drill._drill_archive(live)
        assert ok is True
        assert summary["status"] == "no_archive"

    def test_healthy_archive_verifies(self, tmp_path: Path) -> None:
        live = self._live_with_archive(tmp_path)
        ok, summary = restore_drill._drill_archive(live)
        assert ok is True
        assert summary["status"] == "ok"
        verified = summary["tables_verified"]
        total = summary["tables_total"]
        assert isinstance(verified, int) and isinstance(total, int)
        assert verified == total >= 1

    def test_conflict_is_surfaced_but_not_a_failure(self, tmp_path: Path) -> None:
        live = self._live_with_archive(tmp_path)
        # Bring id 1 back with a DIFFERENT payload -> a restore-time conflict.
        conn = sqlite3.connect(live)
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by) VALUES (1, 'OTHER', '2020-12-31', 'Q4', 'revenue', 99, 'x')"
        )
        conn.commit()
        conn.close()
        ok, summary = restore_drill._drill_archive(live)
        assert ok is True  # a conflict is a human decision, not archive rot
        conflicts = summary["conflict_tables"]
        assert isinstance(conflicts, dict)
        assert conflicts.get("financial_facts") == 1

    def test_main_folds_archive_verdict_into_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = self._live_with_archive(tmp_path)

        # Force the snapshot half to pass so only the archive leg decides.
        def _pass_drill(*_a: object, **_k: object) -> tuple[bool, dict[str, object]]:
            return True, {"status": "ok"}

        def _no_snapshot(_d: object) -> None:
            return None

        def _no_sha(_s: object) -> None:
            return None

        def _no_accounting(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(restore_drill, "run_drill", _pass_drill)
        monkeypatch.setattr(restore_drill, "_latest_snapshot", _no_snapshot)
        monkeypatch.setattr(restore_drill, "_snapshot_sha256", _no_sha)
        monkeypatch.setattr(restore_drill, "_start_accounting", _no_accounting)
        # Healthy archive -> exit 0.
        assert restore_drill.main(["--db", str(live), "--backup-dir", str(tmp_path)]) == 0

        # Break the archive so the drill cannot verify it -> exit 1.
        def _unverified(_live: Path) -> tuple[bool, dict[str, object]]:
            return False, {"status": "archive_unverified", "unverified_tables": ["financial_facts"]}

        monkeypatch.setattr(restore_drill, "_drill_archive", _unverified)
        assert restore_drill.main(["--db", str(live), "--backup-dir", str(tmp_path)]) == 1
