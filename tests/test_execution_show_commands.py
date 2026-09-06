from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from execution import show_llm_spend, show_prompt_calibration, show_routing, show_thesis_history

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _ClosableConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fake_window_clause(since_days: int, run_id: str | None) -> tuple[str, list[Any]]:
    return " WHERE 1=1", []


def _fake_summary(*_args: object) -> dict[str, int]:
    return {"calls": 2, "distinct_prompts": 1}


def _fake_by_group(
    _conn: sqlite3.Connection,
    _where: str,
    _params: list[Any],
    group_col: str,
    limit: int = 50,
) -> list[dict[str, object]]:
    return [
        {
            "group_key": group_col,
            "calls": 1,
            "cost_usd": 1.25,
            "cache_read_tok": 0,
            "total_input_tok": 1,
            "avg_ms": 9.5,
        }
    ]


def _fake_latency(*_args: object) -> list[dict[str, object]]:
    return [{"purpose": "analysis", "calls": 1, "p50_ms": 10, "p95_ms": 11, "max_ms": 12}]


def _fake_dedup(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
    return []


def _fake_recent_errors(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
    return []


def _fake_fallbacks(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
    return []


def _fake_prompt_load_summaries(**kwargs: object) -> list[SimpleNamespace]:
    captured_prompt.update(kwargs)
    return _prompt_summaries


def _fake_plan_for_ticker(_conn: sqlite3.Connection, ticker: str) -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        instrument_type=SimpleNamespace(value="equity"),
        filing_regime=SimpleNamespace(value="10-K"),
        sources=[SimpleNamespace(value="sec"), SimpleNamespace(value="ir")],
        ir_urls=["https://example.test/ir"],
        primary_kpi_names=["revenue"],
    )


def _fake_tracked_companies(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
    return [SimpleNamespace(ticker="GOOG"), SimpleNamespace(ticker="META")]


def _fake_portfolio_summary(_conn: sqlite3.Connection) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            ticker="AAPL",
            current_status=SimpleNamespace(value="hold"),
            streak_length=3,
            streak_started_at=SimpleNamespace(isoformat=lambda: "2026-09-01T00:00:00"),
            last_evaluated_at=SimpleNamespace(isoformat=lambda: "2026-09-05T00:00:00"),
            total_evaluations=4,
        )
    ]


def _fake_fetch_history(_conn: sqlite3.Connection, ticker: str) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            evaluated_at=SimpleNamespace(isoformat=lambda: "2026-09-04T00:00:00"),
            status=SimpleNamespace(value="watch"),
            run_id="run-1",
        )
    ]


def _fake_transitions(_conn: sqlite3.Connection, ticker: str) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            from_status=SimpleNamespace(value="watch"),
            to_status=SimpleNamespace(value="hold"),
            transitioned_at=SimpleNamespace(isoformat=lambda: "2026-09-05T00:00:00"),
        )
    ]


captured_prompt: dict[str, object] = {}
_prompt_summaries = [
    SimpleNamespace(
        purpose="bear_case",
        prompt_version="v3",
        score_count=3,
        avg_score=0.85,
        p25=0.8,
        p50=0.85,
        p75=0.9,
        min_score=0.75,
        max_score=0.9,
        last_scored_at="2026-09-05T00:00:00",
    )
]


def test_show_llm_spend_uses_shared_db_flag_and_preserves_report_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _ClosableConnection()

    def fake_open_db(_path: Path) -> _ClosableConnection:
        return conn

    monkeypatch.setattr(show_llm_spend, "_open_db", fake_open_db)
    monkeypatch.setattr(show_llm_spend, "_window_clause", _fake_window_clause)
    monkeypatch.setattr(show_llm_spend, "_summary", _fake_summary)
    monkeypatch.setattr(show_llm_spend, "_by_group", _fake_by_group)
    monkeypatch.setattr(show_llm_spend, "_latency", _fake_latency)
    monkeypatch.setattr(show_llm_spend, "_dedup_candidates", _fake_dedup)
    monkeypatch.setattr(show_llm_spend, "_recent_errors", _fake_recent_errors)
    monkeypatch.setattr(show_llm_spend, "_fallbacks", _fake_fallbacks)

    rc = show_llm_spend.main(["--db", str(tmp_path / "portfolio.db"), "--since", "0", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["calls"] == 2
    assert payload["by_model"][0]["group_key"] == "model"
    assert conn.closed


def test_show_prompt_calibration_uses_shared_db_flag_and_uppercases_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_prompt.clear()
    monkeypatch.setattr(show_prompt_calibration, "_load_summaries", _fake_prompt_load_summaries)

    rc = show_prompt_calibration.main(
        ["--db", str(tmp_path / "portfolio.db"), "--ticker", "meta", "--json"]
    )
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert captured_prompt["ticker"] == "META"
    assert payload["ticker"] == "META"
    assert payload["summaries"][0]["prompt_version"] == "v3"


def test_show_routing_uses_shared_db_flag_for_ticker_and_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "portfolio.db"
    db.write_text("", encoding="utf-8")
    conn = _ClosableConnection()

    def fake_open_db(_path: Path) -> _ClosableConnection:
        return conn

    monkeypatch.setattr(show_routing, "open_db", fake_open_db)
    monkeypatch.setattr(show_routing, "plan_for_ticker", _fake_plan_for_ticker)
    monkeypatch.setattr(show_routing, "tracked_companies_for_user", _fake_tracked_companies)

    rc_single = show_routing.main(["--db", str(db), "--ticker", "GOOG"])
    assert rc_single == 0
    single = json.loads(capsys.readouterr().out)
    assert single["ticker"] == "GOOG"

    rc_all = show_routing.main(["--db", str(db), "--all", "--include-index-members"])
    assert rc_all == 0
    all_plans = json.loads(capsys.readouterr().out)
    assert [row["ticker"] for row in all_plans] == ["GOOG", "META"]
    assert conn.closed


def test_show_thesis_history_uses_shared_db_flag_for_portfolio_and_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _ClosableConnection()

    def fake_open_db(_path: Path) -> _ClosableConnection:
        return conn

    monkeypatch.setattr(show_thesis_history, "open_db", fake_open_db)
    monkeypatch.setattr(show_thesis_history, "portfolio_summary", _fake_portfolio_summary)
    monkeypatch.setattr(show_thesis_history, "fetch_history", _fake_fetch_history)
    monkeypatch.setattr(show_thesis_history, "transitions_for", _fake_transitions)

    rc_portfolio = show_thesis_history.main(["--db", str(tmp_path / "portfolio.db")])
    assert rc_portfolio == 0
    portfolio = json.loads(capsys.readouterr().out)
    assert portfolio["tickers"] == 1
    assert portfolio["rows"][0]["ticker"] == "AAPL"

    rc_ticker = show_thesis_history.main(
        ["--db", str(tmp_path / "portfolio.db"), "--ticker", "now"]
    )
    assert rc_ticker == 0
    ticker = json.loads(capsys.readouterr().out)
    assert ticker["ticker"] == "NOW"
    assert ticker["history"][0]["run_id"] == "run-1"
    assert conn.closed


@pytest.mark.parametrize(
    ("script_name", "expected_flag"),
    [
        ("show_llm_spend.py", "--db"),
        ("show_prompt_calibration.py", "--db"),
        ("show_routing.py", "--db"),
        ("show_thesis_history.py", "--db"),
    ],
)
def test_show_command_scripts_support_direct_help(script_name: str, expected_flag: str) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "execution" / script_name), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert expected_flag in result.stdout
