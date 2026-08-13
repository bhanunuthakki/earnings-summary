# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    source = PROJECT_ROOT / "execution" / "refetch_aggregator_transcripts.py"
    spec = importlib.util.spec_from_file_location("refetch_aggregator_policy_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["refetch_aggregator_policy_test"] = module
    spec.loader.exec_module(module)
    return module


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT, "
        "instrument_type TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, ?, NULL, 'equity')",
        (
            ("PORT", "portfolio"),
            ("EVAL", "evaluation"),
            ("WATCH", "watchlist"),
            ("IDX", "index_member"),
        ),
    )
    return conn


def test_refetch_scope_is_portfolio_automatic_and_explicit_evaluation_only() -> None:
    mod = _load_module()
    with _conn() as conn:
        assert mod._scope_tickers(conn, "portfolio_evaluation", None) == frozenset({"PORT"})
        assert mod._scope_tickers(conn, "all_active", None) == frozenset({"PORT"})
        assert mod._scope_tickers(conn, "portfolio", ["EVAL"]) == frozenset({"EVAL"})
        assert mod._scope_tickers(conn, "portfolio", ["WATCH", "IDX", "UNKNOWN"]) == frozenset()


def test_refetch_index_is_bounded_to_five_latest_quarters_per_ticker(tmp_path: Path) -> None:
    mod = _load_module()
    mod._TRANSCRIPT_INDEX = tmp_path / "transcript_index.json"
    payload = {
        f"PORT_202{year}_Q{quarter}": {"source": "aggregator_roic"}
        for year, quarter in ((4, 4), (5, 1), (5, 2), (5, 3), (5, 4), (6, 1), (6, 2))
    }
    mod._TRANSCRIPT_INDEX.write_text(json.dumps(payload), encoding="utf-8")

    assert mod._roic_quarters_in_scope(frozenset({"PORT"})) == [
        ("PORT", 2025, 2),
        ("PORT", 2025, 3),
        ("PORT", 2025, 4),
        ("PORT", 2026, 1),
        ("PORT", 2026, 2),
    ]
