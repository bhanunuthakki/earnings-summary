"""P5.2 NL → ViewSpec compile: the compiler's tri-state contract (ok /
budget_skipped / error), grounding + repair behavior, the seed migration,
the /api/viewspec/compile route, and the panel's NL box markup.

All LLM calls are monkeypatched — the suite never spends. The route test
builds its DB via alembic (stamp 0079 → head runs 0080) like
test_explore_panel.py.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

import viewspec.nl_compile as nlc
from alembic import command
from pipeline.explore_panel import render_explore_panel
from viewspec.spec import ViewSpec

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT, source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL, locator TEXT
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL
);
"""


def _seed_facts(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES (1, 'TST', 'fmp', 'fmp_income_statement',"
        " 'f.json', 'a', '2026-01-05 10:00:00', 'ok')"
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, source_doc_id) VALUES ('TST', '2025-12-31 00:00:00', 'Q4', 'revenue', 100, 1)"
    )
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type)"
        " VALUES ('bhanu', 'TST', 'Test Co', 'portfolio')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "nl.db"
    _seed_facts(path)
    return path


_GOOD_SPEC_JSON = (
    '{"tickers": ["TST"], "metrics": ["fin:revenue"], "transform": "yoy",'
    ' "cadence": "quarterly", "periods": 8, "cagr_years": 3}'
)


# ----------------------------------------------------------------------------
# compiler unit behavior (call_llm monkeypatched)
# ----------------------------------------------------------------------------


def test_compile_ok_grounds_prompt(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake_call(prompt: str, **_kw: object) -> str:
        prompts.append(prompt)
        return f"```json\n{_GOOD_SPEC_JSON}\n```"  # fenced output must parse too

    monkeypatch.setattr(nlc, "call_llm", fake_call)
    res = nlc.compile_nl_to_viewspec(
        "revenue growth for TST, last 8 quarters", db_path=db, context_tickers=["TST"]
    )
    assert res.status == "ok"
    assert res.attempts == 1
    assert res.spec == ViewSpec.from_dict(
        {
            "tickers": ["TST"],
            "metrics": ["fin:revenue"],
            "transform": "yoy",
            "periods": 8,
        }
    )
    # The prompt carries the real vocabulary and the question.
    assert "fin:revenue" in prompts[0]
    assert "revenue growth for TST" in prompts[0]
    assert res.message is None
    # No context spec → no refine block.
    assert "Current view" not in prompts[0]


def test_compile_context_spec_grounds_refinement(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR5: the Ask thread sends the previous turn's spec — the prompt must
    carry it with the refine instruction so "now annual" updates rather than
    restarts."""
    prompts: list[str] = []

    def fake_call(prompt: str, **_kw: object) -> str:
        prompts.append(prompt)
        return _GOOD_SPEC_JSON

    monkeypatch.setattr(nlc, "call_llm", fake_call)
    prev = {
        "tickers": ["TST"],
        "metrics": ["fin:revenue"],
        "transform": "yoy",
        "cadence": "quarterly",
        "periods": 8,
    }
    res = nlc.compile_nl_to_viewspec(
        "now annual", db_path=db, context_tickers=["TST"], context_spec=prev
    )
    assert res.status == "ok"
    assert "Current view" in prompts[0]
    assert '"transform": "yoy"' in prompts[0]
    assert "REFINING" in prompts[0]


def test_compile_repairs_once_then_succeeds(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = ["this is not json at all", _GOOD_SPEC_JSON]
    prompts: list[str] = []

    def fake_call(prompt: str, **_kw: object) -> str:
        prompts.append(prompt)
        return outputs[len(prompts) - 1]

    monkeypatch.setattr(nlc, "call_llm", fake_call)
    res = nlc.compile_nl_to_viewspec("rev yoy", db_path=db, context_tickers=["TST"])
    assert res.status == "ok"
    assert res.attempts == 2
    assert "rejected" in prompts[1]  # the repair pass feeds the error back


def test_compile_two_failures_degrade_to_builder(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call(*_a: object, **_k: object) -> str:
        return '{"tickers": [], "metrics": []}'

    monkeypatch.setattr(nlc, "call_llm", fake_call)
    res = nlc.compile_nl_to_viewspec("nonsense", db_path=db, context_tickers=["TST"])
    assert res.status == "error"
    assert res.attempts == 2
    assert res.spec is None
    assert res.message is not None and "builder" in res.message


def test_compile_call_exception_degrades(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("CLI exploded")

    monkeypatch.setattr(nlc, "call_llm", boom)
    res = nlc.compile_nl_to_viewspec("rev yoy", db_path=db, context_tickers=["TST"])
    assert res.status == "error"
    assert res.spec is None


def test_compile_budget_skip_makes_no_call(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Check:
        cap = 5.0
        current_spend = 5.25

    calls: list[str] = []

    def fake_skip(*_a: object, **_k: object) -> _Check:
        return _Check()

    def fake_call(prompt: str, **_k: object) -> str:
        calls.append(prompt)
        return _GOOD_SPEC_JSON

    monkeypatch.setattr(nlc, "should_skip_for_budget", fake_skip)
    monkeypatch.setattr(nlc, "call_llm", fake_call)
    res = nlc.compile_nl_to_viewspec("rev yoy", db_path=db, context_tickers=["TST"])
    assert res.status == "budget_skipped"
    assert calls == []  # forgone means NO spend
    assert res.message is not None and "budget" in res.message


def test_compile_empty_query(db: Path) -> None:
    assert nlc.compile_nl_to_viewspec("   ", db_path=db).status == "error"


def test_query_ticker_resolution(db: Path) -> None:
    tracked = nlc._tracked_tickers(db)  # pyright: ignore[reportPrivateUsage]
    assert tracked == {"TST"}
    named = nlc._tickers_in_query(  # pyright: ignore[reportPrivateUsage]
        "compare TST against SPY and apple", tracked
    )
    assert named == ["TST"]  # only tracked symbols resolve


# ----------------------------------------------------------------------------
# seed migration (0080)
# ----------------------------------------------------------------------------


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migration_seeds_budget_row(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    cfg = _alembic_cfg(db)
    command.stamp(cfg, "0079_saved_views")
    conn = sqlite3.connect(db)
    # llm_budgets as 0052+0066 shape it (the stamp skipped those migrations).
    conn.executescript(
        """
        CREATE TABLE llm_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose VARCHAR(64) NOT NULL UNIQUE,
            monthly_cap_usd NUMERIC(10, 2) NOT NULL,
            warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
            hard_block BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            notes TEXT,
            on_exceed TEXT NOT NULL DEFAULT 'warn'
                CHECK (on_exceed IN ('skip', 'block', 'warn'))
        );
        """
    )
    conn.commit()
    conn.close()
    # Pin to the revision under test — later migrations are someone else's.
    command.upgrade(cfg, "0080_viewspec_compile_budget")
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT monthly_cap_usd, on_exceed FROM llm_budgets WHERE purpose = 'viewspec_compile'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert float(row[0]) == 5.00
    assert row[1] == "skip"


def test_migration_tolerates_missing_llm_budgets(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    cfg = _alembic_cfg(db)
    command.stamp(cfg, "0079_saved_views")
    # Pinned (not "head") so later migrations don't fail this assertion;
    # the point is only that 0080 tolerates an absent llm_budgets table.
    command.upgrade(cfg, "0080_viewspec_compile_budget")
    conn = sqlite3.connect(db)
    rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    conn.close()
    assert rev == "0080_viewspec_compile_budget"


# ----------------------------------------------------------------------------
# route + panel surface
# ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    cfg = _alembic_cfg(db)
    command.stamp(cfg, "0078_stance_scores")
    command.upgrade(cfg, "head")
    _seed_facts_into_existing(db)

    def fake_call(*_a: object, **_k: object) -> str:
        return _GOOD_SPEC_JSON

    monkeypatch.setattr(nlc, "call_llm", fake_call)
    return comments_server.create_app(tmp_path).test_client()


def _seed_facts_into_existing(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES (1, 'TST', 'fmp', 'fmp_income_statement',"
        " 'f.json', 'a', '2026-01-05 10:00:00', 'ok')"
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, source_doc_id) VALUES ('TST', '2025-12-31 00:00:00', 'Q4', 'revenue', 100, 1)"
    )
    conn.commit()
    conn.close()


def test_compile_route_ok(client: FlaskClient) -> None:
    res = client.post(
        "/api/viewspec/compile", json={"query": "TST revenue yoy", "tickers": ["TST"]}
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["spec"]["metrics"] == [{"domain": "fin", "key": "revenue"}]


def test_compile_route_requires_query(client: FlaskClient) -> None:
    assert client.post("/api/viewspec/compile", json={}).status_code == 400


def test_panel_carries_nl_box(db: Path) -> None:
    html_out = render_explore_panel(db)
    assert 'id="vx-nl-q"' in html_out
    assert 'id="vx-nl-go"' in html_out
    assert "/api/viewspec/compile" in html_out
