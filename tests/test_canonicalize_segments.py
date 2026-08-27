"""Unit-of-work tests for the segment canonicalizer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from execution import canonicalize_segments


class _TrackingConnection(sqlite3.Connection):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.commit_calls = 0
        self.close_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        super().commit()

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            canonical_name TEXT NOT NULL UNIQUE,
            display_name TEXT,
            external_ids TEXT,
            parent_entity_id INTEGER,
            meta_json TEXT,
            effective_from TEXT,
            created_at TEXT NOT NULL,
            last_observed_at TEXT
        );
        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias_text TEXT NOT NULL,
            alias_kind TEXT NOT NULL,
            first_observed_at TEXT,
            last_observed_at TEXT,
            observation_count INTEGER NOT NULL DEFAULT 1,
            confidence REAL NOT NULL DEFAULT 1.0,
            exemplar_source_doc_id INTEGER,
            exemplar_excerpt TEXT,
            UNIQUE(entity_id, alias_text)
        );
        CREATE TABLE entity_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity_id INTEGER NOT NULL,
            relationship_kind TEXT NOT NULL,
            to_entity_id INTEGER NOT NULL,
            effective_from TEXT,
            effective_to TEXT,
            evidence_doc_id INTEGER,
            evidence_excerpt TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            meta_json TEXT
        );
        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL
        );
        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL,
            dim_name TEXT NOT NULL,
            segment_entity_id INTEGER
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "portfolio.db"
    conn = sqlite3.connect(path)
    try:
        _schema(conn)
        conn.execute(
            "INSERT INTO entities (kind, canonical_name, created_at) VALUES ('company', 'NU', 'now')"
        )
        conn.execute("INSERT INTO segment_periods (ticker) VALUES ('NU')")
        period_id = conn.execute("SELECT id FROM segment_periods").fetchone()[0]
        conn.executemany(
            "INSERT INTO segment_dimensions (period_id, dim_name) VALUES (?, ?)",
            [(period_id, "Cloud"), (period_id, "Ads"), (period_id, "Ignored")],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _patch_writer(monkeypatch: pytest.MonkeyPatch, opened: list[_TrackingConnection]) -> None:
    def connect(path: str, *, role: object, schema_preflight: bool = False) -> _TrackingConnection:
        del role, schema_preflight
        conn = sqlite3.connect(path, factory=_TrackingConnection)
        opened.append(conn)
        return conn

    monkeypatch.setattr(canonicalize_segments, "connect_sqlite", connect)


def _parsed() -> dict[str, object]:
    return {
        "groups": [
            {"canonical_name": "Cloud", "kind": "segment", "aliases": ["Cloud"]},
            {"canonical_name": "Advertising", "kind": "segment", "aliases": ["Ads"]},
        ]
    }


def test_apply_groups_one_connection_and_commit_for_multiple_groups(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[_TrackingConnection] = []
    _patch_writer(monkeypatch, opened)

    assert (
        canonicalize_segments._apply_groups(
            ticker="NU",
            company_entity_id=1,
            parsed=_parsed(),
            repo_root=db.parent.parent,
            dry_run=False,
        )
        == 2
    )

    assert len(opened) == 1
    assert opened[0].commit_calls == 1
    assert opened[0].close_calls == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM segment_dimensions WHERE segment_entity_id IS NOT NULL"
            ).fetchone()[0]
            == 2
        )


def test_apply_groups_is_idempotent(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[_TrackingConnection] = []
    _patch_writer(monkeypatch, opened)
    kwargs = dict(
        ticker="NU",
        company_entity_id=1,
        parsed=_parsed(),
        repo_root=db.parent.parent,
        dry_run=False,
    )

    assert canonicalize_segments._apply_groups(**kwargs) == 2
    assert canonicalize_segments._apply_groups(**kwargs) == 0
    assert len(opened) == 2
    assert [conn.commit_calls for conn in opened] == [1, 1]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0] == 2


def test_apply_groups_rolls_back_entire_ticker_on_helper_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[_TrackingConnection] = []
    _patch_writer(monkeypatch, opened)
    original = canonicalize_segments.record_alias

    def fail_on_bad(*, alias_text: str, **kwargs: object) -> int | None:
        if alias_text == "bad":
            return None
        return original(alias_text=alias_text, **kwargs)

    monkeypatch.setattr(canonicalize_segments, "record_alias", fail_on_bad)
    parsed = {
        "groups": [
            {"canonical_name": "Cloud", "kind": "segment", "aliases": ["Cloud"]},
            {"canonical_name": "Bad", "kind": "segment", "aliases": ["bad"]},
        ]
    }
    with pytest.raises(RuntimeError, match="alias upsert failed"):
        canonicalize_segments._apply_groups(
            ticker="NU",
            company_entity_id=1,
            parsed=parsed,
            repo_root=db.parent.parent,
            dry_run=False,
        )

    assert opened[0].commit_calls == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM segment_dimensions WHERE segment_entity_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_apply_groups_dry_run_does_not_open_writer(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run must not open a writer")

    monkeypatch.setattr(canonicalize_segments, "connect_sqlite", fail_connect)
    assert (
        canonicalize_segments._apply_groups(
            ticker="NU",
            company_entity_id=1,
            parsed=_parsed(),
            repo_root=db.parent.parent,
            dry_run=True,
        )
        == 0
    )
