"""Focused regressions for comments-server dependency and concurrency boundaries."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
import refresh_dcf  # noqa: E402
from comments_server_panel_cache import (  # noqa: E402
    PanelCacheEntry,
    PanelCacheHit,
    PanelCacheReservation,
    PanelResponseCache,
)

from dcf import fact_drivers, redesign  # noqa: E402


def test_request_read_connection_uses_injected_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    default_db = repo_root / "data" / "portfolio.db"
    injected_db = tmp_path / "runtime" / "injected.db"
    default_db.parent.mkdir(parents=True)
    injected_db.parent.mkdir(parents=True)
    for path, marker in ((default_db, "shadow"), (injected_db, "injected")):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            conn.execute("INSERT INTO marker VALUES (?)", (marker,))

    opened: list[Path] = []

    def connect(path: Path, **_kwargs: object) -> sqlite3.Connection:
        opened.append(Path(path).resolve())
        return sqlite3.connect(path)

    def rows(conn: sqlite3.Connection, _repo_root: Path) -> dict[str, list[object]]:
        marker = str(conn.execute("SELECT value FROM marker").fetchone()[0])
        return {marker: []}

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    monkeypatch.setattr(comments_server, "build_dashboard_rows", rows)

    client = comments_server.create_app(repo_root, db_path=injected_db).test_client()
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.get_json() == {"injected": []}
    assert opened == [injected_db.resolve()]


def test_panel_cache_single_flights_same_key_without_serializing_other_keys() -> None:
    cache = PanelResponseCache(ttl_seconds=30.0, max_entries=8)
    first = cache.get_or_reserve("/api/panel/overview")
    other = cache.get_or_reserve("/api/panel/actions")
    assert isinstance(first, PanelCacheReservation)
    assert isinstance(other, PanelCacheReservation)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(cache.get_or_reserve, "/api/panel/overview")
        with pytest.raises(concurrent.futures.TimeoutError):
            waiting.result(timeout=0.05)

        cache.store(
            first,
            PanelCacheEntry(
                body=b"overview",
                content_type="text/html",
                etag='"overview"',
            ),
        )
        result = waiting.result(timeout=1.0)

    assert isinstance(result, PanelCacheHit)
    assert result.entry.body == b"overview"
    cache.abandon(other)


_BASE = redesign.RedesignInputs(
    segments=("Total company",),
    base_revenue_by_segment={"Total company": 1000.0},
    near_growth_by_segment={"Total company": 0.10},
    terminal_growth_by_segment={"Total company": 0.03},
    near_op_margin=0.20,
    terminal_op_margin=0.25,
    tax_rate=0.24,
    capex_2026_m=60.0,
    terminal_capex_da=1.05,
    da_ratio=0.05,
    consensus_years=5,
    wacc=0.09,
    beta=1.2,
    risk_free_rate=0.043,
    equity_risk_premium=0.045,
    cost_of_debt=0.045,
    terminal_method="Exit multiple",
    terminal_basis="EV/EBITDA",
    exit_multiple=12.0,
    terminal_growth_g=0.03,
    current_price=50.0,
    cash_m=100.0,
    total_debt_m=200.0,
    diluted_shares_m=100.0,
    fx_to_usd=1.0,
)


def test_dcf_injection_records_retry_receipt_when_lineage_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite3.connect(data_dir / "portfolio.db").close()
    workbook = tmp_path / "dcf" / "TEST.xlsx"
    workbook.parent.mkdir()
    workbook.touch()

    def read_inputs(_path: Path) -> redesign.RedesignInputs:
        return _BASE

    def resolve_fact_value(*_args: object, **_kwargs: object) -> fact_drivers.ResolvedFact:
        return fact_drivers.ResolvedFact(
            value=42.0,
            unit="percent",
            period_end="2026-06-30",
            source="sec_official",
            fact_id=17,
            load_key="Operating margin",
        )

    def apply_edits(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "ok"}

    def record_driver_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(redesign, "read_inputs", read_inputs)
    monkeypatch.setattr(fact_drivers, "resolve_fact_value", resolve_fact_value)
    monkeypatch.setattr(refresh_dcf, "apply_edits", apply_edits)
    monkeypatch.setattr(fact_drivers, "record_driver_provenance", record_driver_provenance)

    client = comments_server.create_app(tmp_path).test_client()
    response = client.post(
        "/api/dcf/inject-fact",
        json={
            "ticker": "TEST",
            "token": "kpi:Operating margin",
            "field": "near_op_margin",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["injected"] is True
    assert body["provenance"]["status"] == "retry_pending"
    receipt_id = body["provenance"]["receipt_id"]
    receipt_path = data_dir / "dcf_assumptions" / "provenance_retry" / f"{receipt_id}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["ticker"] == "TEST"
    assert receipt["field_key"] == "near_op_margin"
    assert receipt["payload"]["fact_id"] == 17
    assert receipt["status"] == "retry_pending"
