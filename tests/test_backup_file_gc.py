"""Retention policy for backup FILES: what survives, and what must never be touched."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution.backup_file_gc import build_plan, main


def _touch(path: Path, *, size: int = 32, when: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    if when is not None:
        ts = when.timestamp()
        os.utime(path, (ts, ts))
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    _touch(tmp_path / "data" / "portfolio.db", size=64)
    _touch(tmp_path / "data" / "portfolio.db-wal", size=64)
    _touch(tmp_path / "data" / "portfolio.db-shm", size=64)
    return tmp_path


def test_live_db_and_sidecars_are_never_discovered(tree: Path, monkeypatch) -> None:
    """The live DB is excluded structurally. A retention bug must not be able to
    reach it, so this is asserted at discovery, not at the delete step."""
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    _touch(tree / "data" / "portfolio.db.bak_old_20260101", when=datetime(2026, 1, 1))

    found = {f.path.name for f in gc.discover(tree)}
    assert "portfolio.db" not in found
    assert "portfolio.db-wal" not in found
    assert "portfolio.db-shm" not in found
    assert "portfolio.db.bak_old_20260101" in found


def test_json_receipts_are_never_discovered(tree: Path, monkeypatch) -> None:
    """Cutover receipts document what the backups were for and cost ~1 MB against
    the tens of GB they describe — losing them makes a surviving backup unreadable."""
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    _touch(tree / "data" / "backups" / "portfolio-0219.snapshot.db.manifest.json")
    _touch(tree / "data" / "backups" / "portfolio-0219.snapshot.db")

    found = {f.path.name for f in gc.discover(tree)}
    assert found == {"portfolio-0219.snapshot.db"}


@pytest.mark.parametrize(
    "name",
    [
        # The repo's dominant convention. A literal "pre_" marker list missed every
        # one of these, so a real run scanned 0 files with backups sitting in data/.
        "portfolio_pre0263_20260802.db.gz",
        "portfolio_pre0151_20260716.db.gz",
        "portfolio.db.pre0262.bak",
        "portfolio.db.pre0069.20260601_202258.bak",
        "pre_gc_20260731_portfolio.db",
        "portfolio.db.bak_tenq_ingest_20260725T134626",
        "portfolio-0259-atomic-rollback.db",
        "portfolio-0258-additive-candidate-rehearsal.db",
    ],
)
def test_recognises_every_naming_convention_in_use(name: str) -> None:
    from execution.backup_file_gc import _is_backup_file

    assert _is_backup_file(Path(name)), f"{name} not recognised as a backup"


def test_gc_archive_is_protected(tmp_path: Path, monkeypatch) -> None:
    """portfolio_gc_archive.db holds the rows db_gc pruned. Deleting it turns every
    past reversible prune into permanent loss, so it must never be discovered."""
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tmp_path / "data" / "portfolio.db")
    _touch(tmp_path / "data" / "archive" / "portfolio_gc_archive.db")
    _touch(
        tmp_path / "data" / "archive" / "pre_gc_20260731_portfolio.db",
        when=datetime(2026, 7, 31),
    )

    found = {f.path.name for f in gc.discover(tmp_path)}
    assert "portfolio_gc_archive.db" not in found
    assert "pre_gc_20260731_portfolio.db" in found


def test_keeps_newest_per_month_plus_n_most_recent(tree: Path, monkeypatch) -> None:
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    stamps = {
        "portfolio.db.bak_a": datetime(2026, 6, 1),
        "portfolio.db.bak_b": datetime(2026, 6, 29),  # June survivor
        "portfolio.db.bak_c": datetime(2026, 7, 2),
        "portfolio.db.bak_d": datetime(2026, 7, 25),  # 2nd most recent
        "portfolio.db.bak_e": datetime(2026, 7, 31),  # July survivor + most recent
    }
    for name, when in stamps.items():
        _touch(tree / "data" / name, when=when)

    plan = build_plan(
        gc.discover(tree),
        keep_monthly=1,
        keep_recent=2,
        gzip_after_days=10_000,  # disable gzip for this assertion
        now=datetime(2026, 8, 2),
    )
    assert {f.path.name for f in plan.keep} == {
        "portfolio.db.bak_b",
        "portfolio.db.bak_d",
        "portfolio.db.bak_e",
    }
    assert {f.path.name for f in plan.prune} == {
        "portfolio.db.bak_a",
        "portfolio.db.bak_c",
    }


def test_survivors_older_than_threshold_are_gzipped_not_pruned(tree: Path, monkeypatch) -> None:
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    now = datetime(2026, 8, 2)
    _touch(tree / "data" / "portfolio.db.bak_old", when=now - timedelta(days=30))
    _touch(tree / "data" / "portfolio.db.bak_fresh", when=now - timedelta(days=1))

    plan = build_plan(
        gc.discover(tree),
        keep_monthly=1,
        keep_recent=2,
        gzip_after_days=7,
        now=now,
    )
    assert not plan.prune  # both survive: one per month, both in the 2 most recent
    assert {f.path.name for f in plan.gzip} == {"portfolio.db.bak_old"}


def test_already_gzipped_survivors_are_not_regzipped(tree: Path, monkeypatch) -> None:
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    now = datetime(2026, 8, 2)
    _touch(tree / "data" / "portfolio.db.bak_old.gz", when=now - timedelta(days=30))

    plan = build_plan(gc.discover(tree), keep_monthly=1, keep_recent=2, gzip_after_days=7, now=now)
    assert not plan.gzip


def test_sidecars_follow_their_base_and_are_never_standalone(tree: Path, monkeypatch) -> None:
    """A -wal/-shm must never be planned independently of its base: a real run
    planned to delete a 0-byte -wal while keeping the 1.77 GB .db it belonged to."""
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    old = _touch(tree / "data" / "portfolio.db.bak_old", when=datetime(2026, 5, 1))
    _touch(tree / "data" / "portfolio.db.bak_old-wal", when=datetime(2026, 5, 1))
    _touch(tree / "data" / "portfolio.db.bak_old-shm", when=datetime(2026, 5, 1))
    # a later May file, so May's survivor is NOT bak_old and it is genuinely pruned
    _touch(tree / "data" / "portfolio.db.bak_may2", when=datetime(2026, 5, 15))
    _touch(tree / "data" / "portfolio.db.bak_new1", when=datetime(2026, 6, 1))
    _touch(tree / "data" / "portfolio.db.bak_new2", when=datetime(2026, 7, 1))

    assert not any(f.path.name.endswith(("-wal", "-shm")) for f in gc.discover(tree))

    assert main(["--root", str(tree), "--apply", "--gzip-after-days", "10000"]) == 0
    assert not old.exists()
    assert not (tree / "data" / "portfolio.db.bak_old-wal").exists()
    assert not (tree / "data" / "portfolio.db.bak_old-shm").exists()


def test_dry_run_deletes_nothing(tree: Path, monkeypatch, capsys) -> None:
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    for i, when in enumerate([datetime(2026, 6, 1), datetime(2026, 6, 2), datetime(2026, 6, 3)]):
        _touch(tree / "data" / f"portfolio.db.bak_{i}", when=when)

    rc = main(["--root", str(tree)])
    assert rc == 0
    assert len(list((tree / "data").glob("portfolio.db.bak_*"))) == 3


def test_apply_is_idempotent(tree: Path, monkeypatch) -> None:
    """A second run over an already-pruned tree must plan nothing."""
    import execution.backup_file_gc as gc

    monkeypatch.setattr(gc, "LIVE_DB", tree / "data" / "portfolio.db")
    for i, when in enumerate([datetime(2026, 5, 1), datetime(2026, 6, 1), datetime(2026, 6, 2)]):
        _touch(tree / "data" / f"portfolio.db.bak_{i}", when=when)

    assert main(["--root", str(tree), "--apply", "--gzip-after-days", "10000"]) == 0
    survivors = sorted(p.name for p in (tree / "data").glob("portfolio.db.bak_*"))

    assert main(["--root", str(tree), "--apply", "--gzip-after-days", "10000"]) == 0
    assert sorted(p.name for p in (tree / "data").glob("portfolio.db.bak_*")) == survivors
