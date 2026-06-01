"""Editable LLM budget panel (PR3): the GET/POST /api/llm-budgets endpoints on
execution/comments_server.py, plus the analytical dashboard budget panel
rendering editable cap/mode controls.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# execution/ isn't on sys.path by default (only src/ via pyproject pythonpath).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from pipeline.analytical_dashboard import _build_llm_budget_panel  # noqa: E402
from pipeline.analytical_dashboard_html import _llm_budget_section  # noqa: E402


def _seed(repo_root: Path) -> None:
    data_dir = repo_root / "data"
    data_dir.mkdir()
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(data_dir / "portfolio.db"))
    try:
        conn.executescript(
            """
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, called_at DATETIME NOT NULL,
                purpose VARCHAR(64), model VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL,
                prompt_chars INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL, cost_estimate_usd FLOAT);
            CREATE TABLE llm_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, purpose VARCHAR(64) NOT NULL,
                monthly_cap_usd NUMERIC(10,2) NOT NULL, warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
                hard_block BOOLEAN NOT NULL DEFAULT 0,
                on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (on_exceed IN ('skip', 'block', 'warn')),
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, notes TEXT,
                CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose));
            """
        )
        conn.execute(
            "INSERT INTO llm_budgets (purpose, monthly_cap_usd, hard_block, on_exceed, "
            "created_at, updated_at) VALUES ('bear_case', 50, 0, 'warn', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, model, prompt_sha256, prompt_chars, "
            "elapsed_ms, cost_estimate_usd) VALUES (?, 'bear_case', 'm', 'x', 1, 1, 12.0)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _seed(tmp_path)
    return tmp_path


@pytest.fixture
def client(repo: Path):
    return comments_server.create_app(repo).test_client()


def _row(repo: Path, purpose: str) -> tuple[object, ...]:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    try:
        return conn.execute(
            "SELECT monthly_cap_usd, on_exceed, hard_block FROM llm_budgets WHERE purpose = ?",
            (purpose,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_get_llm_budgets(client) -> None:
    resp = client.get("/api/llm-budgets")
    assert resp.status_code == 200
    rows = {r["purpose"]: r for r in resp.get_json()["budgets"]}
    assert rows["bear_case"]["on_exceed"] == "warn"
    assert rows["bear_case"]["monthly_cap_usd"] == 50.0
    assert rows["bear_case"]["current_spend_usd"] == 12.0


def test_post_sets_cap(client, repo: Path) -> None:
    resp = client.post("/api/llm-budgets/bear_case", json={"cap_usd": 123})
    assert resp.status_code == 200
    assert float(_row(repo, "bear_case")[0]) == 123.0


def test_post_sets_mode_and_syncs_hard_block(client, repo: Path) -> None:
    resp = client.post("/api/llm-budgets/bear_case", json={"on_exceed": "block"})
    assert resp.status_code == 200
    _cap, mode, hard_block = _row(repo, "bear_case")
    assert mode == "block"
    assert hard_block == 1  # set_mode syncs the legacy bool


def test_post_cap_and_mode_together(client, repo: Path) -> None:
    resp = client.post("/api/llm-budgets/bear_case", json={"cap_usd": 75, "on_exceed": "skip"})
    assert resp.status_code == 200
    cap, mode, _ = _row(repo, "bear_case")
    assert float(cap) == 75.0
    assert mode == "skip"


def test_post_invalid_mode_400(client) -> None:
    resp = client.post("/api/llm-budgets/bear_case", json={"on_exceed": "nope"})
    assert resp.status_code == 400


def test_post_negative_cap_400(client) -> None:
    resp = client.post("/api/llm-budgets/bear_case", json={"cap_usd": -5})
    assert resp.status_code == 400


def test_post_unknown_purpose_404(client) -> None:
    resp = client.post("/api/llm-budgets/does_not_exist", json={"cap_usd": 10})
    assert resp.status_code == 404


def test_post_empty_body_400(client) -> None:
    resp = client.post("/api/llm-budgets/bear_case", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Renderer — the panel reads on_exceed + emits editable controls
# ---------------------------------------------------------------------------


def test_panel_renders_editable_controls(repo: Path) -> None:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        panel = _build_llm_budget_panel(conn)
    finally:
        conn.close()
    assert any(r.purpose == "bear_case" and r.on_exceed == "warn" for r in panel.rows)
    html = _llm_budget_section(panel)
    assert 'data-purpose="bear_case"' in html
    assert "budget-cap" in html  # editable cap input
    assert "budget-mode" in html  # mode <select>
    assert "budget-save" in html  # Save button
