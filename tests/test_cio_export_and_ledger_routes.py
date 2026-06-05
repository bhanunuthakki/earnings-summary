"""Route tests for the Richness surfaces wired into the command center:
``GET /export/cio`` (the .xlsx download) and ``GET /api/panel/thesis_ledger``.

Before this, the CIO .xlsx export existed only as a CLI (unreachable from the
app), and the populated thesis-ledger history had no command-center tab (only the
/digest route surfaced it). v6 re-grade, Richness & surfacing. The substrate is
built via alembic; one ledger entry is seeded so the panel is non-trivial.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from user_state.ledger import append_entry  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    append_entry(
        user_id="bhanu",
        ticker="NU",
        entry_kind="thesis_update",
        body="NIM held despite mix shift",
        db_path=db_path,
    )


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return comments_server.create_app(tmp_path).test_client()


def test_export_cio_route_streams_xlsx(client: FlaskClient) -> None:
    resp = client.get("/export/cio")
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert resp.data[:2] == b"PK"  # xlsx is a zip container


def test_thesis_ledger_panel_route_renders_entry(client: FlaskClient) -> None:
    resp = client.get("/api/panel/thesis_ledger")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.data.decode()
    assert "Thesis ledger" in body
    assert "NIM held despite mix shift" in body
    assert "NU" in body
