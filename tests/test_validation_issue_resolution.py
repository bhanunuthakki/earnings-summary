"""Resolve writer + POST /actions/resolve-issue (S10 — provenance is actionable).

The quarantine ledger gained ``resolved_by`` / ``resolution_note`` (alembic
0094) and a resolve verb. These pin: the writer is idempotent (a re-resolve is a
no-op) and the route wires it with the right status codes.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from validation_issues_store import resolve_validation_issue  # noqa: E402

_SCHEMA = """
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, source_doc_id INTEGER, ticker TEXT,
    severity TEXT NOT NULL, rule TEXT NOT NULL,
    raw_value TEXT, expected TEXT,
    raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP,
    resolved_by TEXT, resolution_note TEXT
);
"""


def _seed_issue(db: Path, ticker: str = "NU") -> int:
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SCHEMA)
        cur = conn.execute(
            "INSERT INTO validation_issues "
            "(ticker, severity, rule, raw_value, expected, raised_at) "
            "VALUES (?, 'halt', 'magnitude_jump', '9', '1', '2026-05-03 09:00:00')",
            (ticker,),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    _seed_issue(tmp_path / "data" / "portfolio.db")
    return tmp_path


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def test_resolve_writer_sets_audit_columns(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    issue_id = _seed_issue(db)
    resolved_at = resolve_validation_issue(
        issue_id,
        resolved_by="bhanu",
        resolution_note="  confirmed real, fixed upstream  ",
        db_path=db,
    )
    assert resolved_at is not None
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT resolved_at, resolved_by, resolution_note FROM validation_issues WHERE id = ?",
            (issue_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["resolved_at"] is not None
    assert row["resolved_by"] == "bhanu"
    assert row["resolution_note"] == "confirmed real, fixed upstream"  # trimmed


def test_resolve_writer_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    issue_id = _seed_issue(db)
    assert resolve_validation_issue(issue_id, resolved_by="bhanu", db_path=db) is not None
    # A second resolve of the same (now-closed) issue is a no-op.
    assert resolve_validation_issue(issue_id, resolved_by="bhanu", db_path=db) is None


def test_resolve_writer_unknown_id_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed_issue(db)
    assert resolve_validation_issue(9999, db_path=db) is None


def test_resolve_writer_blank_note_stored_null(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    issue_id = _seed_issue(db)
    resolve_validation_issue(issue_id, resolved_by="bhanu", resolution_note="   ", db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        note = conn.execute(
            "SELECT resolution_note FROM validation_issues WHERE id = ?", (issue_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert note is None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def test_route_resolves_open_issue(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    client = comments_server.create_app(repo).test_client()
    resp = client.post("/actions/resolve-issue", json={"issue_id": 1, "resolution_note": "fixed"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["issue_id"] == 1
    assert body["resolved_at"]


def test_route_missing_issue_id_400(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    client = comments_server.create_app(repo).test_client()
    resp = client.post("/actions/resolve-issue", json={})
    assert resp.status_code == 400


def test_route_already_resolved_404(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    client = comments_server.create_app(repo).test_client()
    assert client.post("/actions/resolve-issue", json={"issue_id": 1}).status_code == 200
    second = client.post("/actions/resolve-issue", json={"issue_id": 1})
    assert second.status_code == 404
