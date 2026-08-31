"""Editable global DCF assumptions panel (PR4): the GET/POST /api/dcf-globals
endpoints + the /api/panel/dcf_globals settings-drawer fragment.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest
from flask.testing import FlaskClient

# execution/ isn't on sys.path by default (only src/ via pyproject pythonpath).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from dispatch_registry import Job, Registry  # noqa: E402
from pipeline.dcf_globals_panel import render_dcf_globals_panel  # noqa: E402


class _NonSpawningRegistry(Registry):
    """Records job starts without forking a real subprocess (so the rebuild
    route test never actually runs refresh_dcf)."""

    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
        write_sets: list[str] | None = None,
        code_root: str | Path | None = None,
    ) -> Job:
        del spawn
        return super().start(
            ticker=ticker,
            kind=kind,
            argv=argv,
            spawn=False,
            cwd=cwd,
            write_sets=write_sets,
            code_root=code_root,
        )


def _seed(repo_root: Path) -> None:
    data_dir = repo_root / "data"
    (data_dir / "dcf_assumptions").mkdir(parents=True)
    (data_dir / "bank_assumptions").mkdir(parents=True)
    conn = sqlite3.connect(str(data_dir / "portfolio.db"))
    try:
        conn.executescript(
            "CREATE TABLE global_dcf_assumptions "
            "(field TEXT PRIMARY KEY, value REAL NOT NULL, updated_at TEXT NOT NULL);"
        )
        conn.executemany(
            "INSERT INTO global_dcf_assumptions VALUES (?, ?, 't')",
            [("risk_free_rate", 0.043), ("equity_risk_premium", 0.045), ("tax_rate", 0.24)],
        )
        conn.commit()
    finally:
        conn.close()
    # An FCFF name pinning rf, and a bank name pinning erp/tax → exercise the
    # override scan.
    (data_dir / "dcf_assumptions" / "AMZN.json").write_text(
        json.dumps({"redesign": {"risk_free_rate": 0.041}}), encoding="utf-8"
    )
    (data_dir / "bank_assumptions" / "HDB.json").write_text(
        json.dumps({"erp": 0.05, "tax": 0.25}), encoding="utf-8"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _seed(tmp_path)
    return tmp_path


@pytest.fixture
def client(repo: Path) -> FlaskClient:
    return comments_server.create_app(repo).test_client()


@pytest.fixture
def action_client(repo: Path) -> FlaskClient:
    return comments_server.create_app(repo, registry=_NonSpawningRegistry()).test_client()


def _stored(repo: Path, field: str) -> float | None:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    try:
        row = conn.execute(
            "SELECT value FROM global_dcf_assumptions WHERE field = ?", (field,)
        ).fetchone()
        return float(row[0]) if row else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- endpoints


def test_get_globals(client: FlaskClient) -> None:
    resp = client.get("/api/dcf-globals")
    assert resp.status_code == 200
    g = cast(dict[str, object], resp.get_json())["globals"]
    assert g == {"risk_free_rate": 0.043, "equity_risk_premium": 0.045, "tax_rate": 0.24}


def test_post_updates_field(client: FlaskClient, repo: Path) -> None:
    resp = client.post("/api/dcf-globals", json={"field": "risk_free_rate", "value": 0.05})
    assert resp.status_code == 200
    assert cast(dict[str, object], resp.get_json())["value"] == 0.05
    assert _stored(repo, "risk_free_rate") == 0.05


def test_post_unknown_field_400(client: FlaskClient) -> None:
    resp = client.post("/api/dcf-globals", json={"field": "terminal_growth", "value": 0.03})
    assert resp.status_code == 400


def test_post_out_of_range_400(client: FlaskClient, repo: Path) -> None:
    resp = client.post("/api/dcf-globals", json={"field": "tax_rate", "value": 4.3})
    assert resp.status_code == 400
    assert _stored(repo, "tax_rate") == 0.24  # unchanged


def test_post_missing_field_400(client: FlaskClient) -> None:
    resp = client.post("/api/dcf-globals", json={"value": 0.05})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- panel


def test_panel_fragment_renders(client: FlaskClient) -> None:
    resp = client.get("/api/panel/dcf_globals")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-field="risk_free_rate"' in html
    assert 'data-field="tax_rate"' in html
    assert "Global DCF assumptions" in html
    assert 'id="dcfg-rebuild"' in html  # the one-click rebuild button
    assert "work-os:investment-evidence-updated" in html
    assert "detail: {kind: 'dcf', scope: 'all'}" in html


def test_rebuild_dcfs_action(action_client: FlaskClient) -> None:
    resp = action_client.post("/actions/rebuild-dcfs", json={})
    assert resp.status_code == 201
    body = cast(dict[str, object], resp.get_json())
    assert body["kind"] == "rebuild-dcfs"
    job_id = cast(str, body["job_id"])
    assert job_id.startswith("job_")
    assert body["stream_url"] == f"/actions/stream/{job_id}"


def test_panel_lists_overrides(repo: Path) -> None:
    html = render_dcf_globals_panel(repo / "data" / "portfolio.db")
    # FCFF rf override (AMZN) + bank erp/tax override (HDB) surface in the panel.
    assert "AMZN (FCFF)" in html
    assert "HDB (bank)" in html
