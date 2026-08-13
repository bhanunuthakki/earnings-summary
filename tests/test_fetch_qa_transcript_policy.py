# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    source = PROJECT_ROOT / "execution" / "fetch_qa_transcript.py"
    spec = importlib.util.spec_from_file_location("fetch_qa_policy_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_qa_policy_test"] = module
    spec.loader.exec_module(module)
    return module


def _db(path: Path, role: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies ("
            "ticker TEXT, list_type TEXT, archived_at TEXT, fiscal_year_end TEXT)"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('ACME', ?, NULL, '12-31')",
            (role,),
        )


def test_direct_aggregator_fetch_denies_stored_watchlist_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    _db(db_path, "watchlist")

    def _unexpected_fetch(*_args: object) -> None:
        pytest.fail("network boundary was crossed")

    monkeypatch.setattr(
        mod,
        "fetch_qa_with_fallback",
        _unexpected_fetch,
    )

    assert (
        mod.fetch_qa(
            mod.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
            db_path=db_path,
            owner_requested=False,
        )
        is None
    )


def test_direct_aggregator_fetch_denies_quarter_outside_canonical_five_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    _db(db_path, "portfolio")
    monkeypatch.setattr(mod, "_policy_today", lambda: date(2026, 8, 12))

    def _unexpected_fetch(*_args: object) -> None:
        pytest.fail("network boundary was crossed")

    monkeypatch.setattr(
        mod,
        "fetch_qa_with_fallback",
        _unexpected_fetch,
    )

    assert (
        mod.fetch_qa(
            mod.FetchQaSpec(ticker="ACME", year=2024, quarter=4),
            db_path=db_path,
            owner_requested=False,
        )
        is None
    )


def test_fetch_authorization_uses_the_caller_database_not_import_time_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    denied_db = tmp_path / "denied.db"
    allowed_db = tmp_path / "allowed.db"
    _db(denied_db, "watchlist")
    _db(allowed_db, "portfolio")
    monkeypatch.setattr(mod.db, "DB_PATH", str(allowed_db))

    def _unexpected_fetch(*_args: object) -> None:
        pytest.fail("network boundary was crossed using the import-time database")

    monkeypatch.setattr(mod, "fetch_qa_with_fallback", _unexpected_fetch)

    assert (
        mod.fetch_qa(
            mod.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
            db_path=denied_db,
            owner_requested=False,
            as_of=date(2026, 8, 12),
        )
        is None
    )
