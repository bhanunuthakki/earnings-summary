"""The squashed test DB cache ignores obsolete stamp labels."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

_SEED_SQL = (
    "CREATE TABLE seeded_probe (value TEXT NOT NULL); INSERT INTO seeded_probe VALUES ('a');"
)
_SEED_SHA256 = hashlib.sha256(_SEED_SQL.encode()).hexdigest()
_OTHER_SEED_SQL = (
    "CREATE TABLE seeded_probe (value TEXT NOT NULL); INSERT INTO seeded_probe VALUES ('b');"
)
_OTHER_SEED_SHA256 = hashlib.sha256(_OTHER_SEED_SQL.encode()).hexdigest()


def _seed_probe(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(_SEED_SQL)


def _seed_other_probe(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(_OTHER_SEED_SQL)


def test_legacy_stamps_share_one_current_head_template(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # The production fixture owns the one cached chain build. This test only
    # spies on that call; it is not another direct migration-chain builder.
    original = getattr(command, "upgrade")

    def counted_upgrade(config: Config, target: str) -> None:
        calls.append(target)
        original(config, target)

    monkeypatch.setattr(command, "upgrade", counted_upgrade)
    target = "0006_add_ask_proposal_approval"
    first = migrated_db(tmp_path / "first.db", stamp="archived-0100", target=target)
    calls_after_first = len(calls)
    second = migrated_db(tmp_path / "second.db", stamp="archived-0273", target=target)

    # Another test in this xdist worker may already have populated the
    # session cache. The invariant is that two compatibility stamp labels do
    # not trigger a second chain build, regardless of who warmed it first.
    assert calls_after_first in {0, 1}
    assert len(calls) == calls_after_first
    if calls:
        assert calls == [target]
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("archived", [False, True])
def test_seeded_upgrade_is_cached_and_each_copy_is_isolated(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
) -> None:
    calls: list[str] = []
    original = getattr(command, "upgrade")

    def counted_upgrade(config: Config, target: str) -> None:
        calls.append(target)
        original(config, target)

    monkeypatch.setattr(command, "upgrade", counted_upgrade)
    options = {
        "archived": archived,
        "upgrade_from": "base",
        "before_upgrade": _seed_probe,
        "seed_sha256": _SEED_SHA256,
        "target": "0000_baseline",
    }
    first = migrated_db(tmp_path / "seeded-first.db", **options)
    calls_after_first = len(calls)
    second = migrated_db(tmp_path / "seeded-second.db", **options)

    expected_build_calls = 1 if archived else 2
    assert calls_after_first in {0, expected_build_calls}
    assert len(calls) == calls_after_first
    with sqlite3.connect(first) as connection:
        assert connection.execute("SELECT value FROM seeded_probe").fetchone() == ("a",)
        connection.execute("INSERT INTO seeded_probe VALUES ('mutated')")
        connection.commit()
    with sqlite3.connect(second) as connection:
        assert connection.execute("SELECT value FROM seeded_probe").fetchall() == [("a",)]


def test_archived_seeded_upgrade_cache_separates_seed_content(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    first = migrated_db(
        tmp_path / "seed-a.db",
        archived=True,
        upgrade_from="base",
        before_upgrade=_seed_probe,
        seed_sha256=_SEED_SHA256,
        target="0000_baseline",
    )
    second = migrated_db(
        tmp_path / "seed-b.db",
        archived=True,
        upgrade_from="base",
        before_upgrade=_seed_other_probe,
        seed_sha256=_OTHER_SEED_SHA256,
        target="0000_baseline",
    )

    with sqlite3.connect(first) as connection:
        assert connection.execute("SELECT value FROM seeded_probe").fetchone() == ("a",)
    with sqlite3.connect(second) as connection:
        assert connection.execute("SELECT value FROM seeded_probe").fetchone() == ("b",)


@pytest.mark.parametrize("seed_sha256", ["short", "A" * 64, "z" * 64])
def test_seeded_upgrade_rejects_noncanonical_seed_identity(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    seed_sha256: str,
) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        migrated_db(
            tmp_path / "invalid-seed.db",
            archived=True,
            upgrade_from="base",
            before_upgrade=_seed_probe,
            seed_sha256=seed_sha256,
            target="0000_baseline",
        )
