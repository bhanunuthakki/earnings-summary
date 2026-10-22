"""Native SQLite boundary controls; scope semantics remain owned by analysis_scope."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from tests import test_research_snapshot as foundation

PRIOR = "0058_sealed_snapshot_admission_fences"
CURRENT = "0059_scoped_research_universe_commitments"
_ABSENT = object()
_seed_foundation: Callable[[sqlite3.Connection], None] = getattr(foundation, "_insert_foundation")
_fixture_config: Callable[[Path], Config] = getattr(foundation, "_config")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.create_function("fact_sha256", 1, _sha)
    return conn


def _seed(path: Path) -> None:
    with _connect(path) as conn:
        _seed_foundation(conn)
    conn.close()


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    path = migrated_db(tmp_path / "scope-trigger.db")
    _seed(path)
    database = _connect(path)
    yield database
    database.close()


def _scope() -> dict[str, object]:
    # These nested values exercise exact serialized scope binding, not native
    # qualification. The SQL boundary must not interpret or omit any scope key.
    return {
        "schema_version": "analysis-evidence-scope/v6",
        "scope_id": "analysis-scope:" + "a" * 64,
        "scope_sha256": "a" * 64,
        "request": {
            "readiness_profile": "complete_report_evidence/v1",
            "issuer_documents": [{"document_version_id": "document-1"}],
            "historical_reconstruction": {"analysis_at": "2026-08-12T23:59:59Z"},
        },
        "public_witnesses": [{"policy": "retained-policy", "raw_sha256": "b" * 64}],
    }


def _universe(scope: object = _ABSENT) -> dict[str, object]:
    result: dict[str, object] = {
        "document_version_ids": ["document-1"],
        "issuer_id": "issuer-1",
        "reporting_entity_ids": ["reporting-1"],
        "source_obligation_revision_ids": ["obligation-a:v1"],
    }
    if scope is not _ABSENT:
        result["analysis_scope"] = scope
    return result


def _header(conn: sqlite3.Connection, universe: dict[str, object]) -> None:
    request = _json({"research_universe": universe})
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
        ("snapshot-1", "snapshot-1", request, _sha(request), foundation.T1, foundation.T1),
    )


def _insert(
    conn: sqlite3.Connection, universe: dict[str, object], *, digest: str | None = None
) -> str:
    canonical = _json(universe)
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "snapshot-1",
            universe["issuer_id"],
            _json(universe["reporting_entity_ids"]),
            _json(universe["document_version_ids"]),
            _json(universe["source_obligation_revision_ids"]),
            canonical,
            digest or _sha(canonical),
            foundation.T1,
            foundation.T1,
        ),
    )
    return canonical


def test_0059_reproduces_old_failure_then_admits_exact_scoped_payload(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "upgrade.db", target=PRIOR)
    _seed(path)
    conn = _connect(path)
    universe = _universe(_scope())
    _header(conn, universe)
    with pytest.raises(sqlite3.IntegrityError, match="research universe commitment mismatch"):
        _insert(conn, universe)
    conn.commit()
    before = tuple(conn.execute("SELECT * FROM research_snapshot_headers").fetchone())
    old_sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_research_snapshot_universe_exact'"
        ).fetchone()[0]
    )
    conn.close()
    command.upgrade(_fixture_config(path), CURRENT)
    conn = _connect(path)
    canonical = _insert(conn, universe)
    assert tuple(conn.execute("SELECT * FROM research_snapshot_headers").fetchone()) == before
    assert (
        conn.execute(
            "SELECT canonical_universe_json FROM research_snapshot_universe_commitments"
        ).fetchone()[0]
        == canonical
    )
    new_sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_research_snapshot_universe_exact'"
        ).fetchone()[0]
    )
    # All source/entity/hash guards after canonical construction retain exact SQL.
    suffix = "OR NEW.universe_sha256<>fact_sha256(NEW.canonical_universe_json)"
    assert old_sql.split(suffix, 1)[1] == new_sql.split(suffix, 1)[1]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="downgrade refused: scoped research"):
        command.downgrade(_fixture_config(path), PRIOR)


def test_legacy_four_field_bytes_and_downgrade_round_trip(
    conn: sqlite3.Connection,
) -> None:
    universe = _universe()
    _header(conn, universe)
    canonical = _insert(conn, universe)
    assert canonical == (
        '{"document_version_ids":["document-1"],"issuer_id":"issuer-1",'
        '"reporting_entity_ids":["reporting-1"],'
        '"source_obligation_revision_ids":["obligation-a:v1"]}'
    )
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    command.downgrade(_fixture_config(path), PRIOR)
    command.upgrade(_fixture_config(path), CURRENT)
    row = conn.execute(
        "SELECT canonical_universe_json,universe_sha256 FROM research_snapshot_universe_commitments"
    ).fetchone()
    assert tuple(row) == (canonical, _sha(canonical))


@pytest.mark.parametrize("change", ["omit", "nested", "extra", "digest"])
def test_scope_content_and_digest_must_match_exact_header(
    conn: sqlite3.Connection, change: str
) -> None:
    universe = _universe(_scope())
    _header(conn, universe)
    candidate = _universe(_scope())
    if change == "omit":
        candidate.pop("analysis_scope")
    elif change == "nested":
        candidate["analysis_scope"] = {**_scope(), "scope_sha256": "c" * 64}
    elif change == "extra":
        candidate["analysis_scope"] = {**_scope(), "extra": True}
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        _insert(conn, candidate, digest="f" * 64 if change == "digest" else None)


@pytest.mark.parametrize("scope", [None, [], "scalar", 1])
def test_present_nonobject_scope_is_rejected(conn: sqlite3.Connection, scope: object) -> None:
    universe = _universe(scope)
    _header(conn, universe)
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        _insert(conn, universe)


@pytest.mark.parametrize("field", ["document_version_ids", "source_obligation_revision_ids"])
def test_scoped_payload_preserves_native_reference_guards(
    conn: sqlite3.Connection, field: str
) -> None:
    universe = _universe(_scope())
    universe[field] = ["missing-native-record"]
    _header(conn, universe)
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        _insert(conn, universe)
