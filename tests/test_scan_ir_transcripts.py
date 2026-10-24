"""Tests for execution/scan_ir_transcripts.py — the post-earnings IR-transcript scan.

The fetch path (Playwright + network) can't run in CI, so these tests pin the
pure window decision (`due_for_scan`), the per-ticker orchestration (`scan_one`,
via monkeypatched seams), and the `last_earnings_date` FMP-cache helper — never
hitting the network or a browser.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sources.earnings_calendar import last_earnings_date

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "scan_ir_transcripts.py"
    spec = importlib.util.spec_from_file_location("scan_ir_transcripts", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_ir_transcripts"] = mod
    spec.loader.exec_module(mod)
    return mod


# Shared fixtures for scan_one: today, an in-window earnings date, a Dec FYE.
_TODAY = date(2026, 5, 10)
_IN_WINDOW = date(2026, 5, 8)  # 2 days before _TODAY
_FYE_DEC = 12


def _earnings(_r: Path, _t: str) -> date:
    return _IN_WINDOW


def _no_earnings(_r: Path, _t: str) -> date | None:
    return None


def _stale_earnings(_r: Path, _t: str) -> date:
    return date(2026, 4, 1)  # 39 days before _TODAY → out of window


def _quarters(_fye: int, _today: date, _n: int) -> list[tuple[int, int]]:
    return [(2026, 1)]


def _true(*_a: object, **_k: object) -> bool:
    return True


def _false(*_a: object, **_k: object) -> bool:
    return False


def _fetch_hit(_spec: object, **_k: object) -> object:
    return SimpleNamespace(
        status="acquired", result=SimpleNamespace(source_name="issuer_ir", receipt_id="a" * 64)
    )


def _fetch_none(_spec: object, **_k: object) -> object | None:
    return SimpleNamespace(status="provider_miss", result=None)


def _fetch_boom(_spec: object, **_k: object) -> object:
    raise RuntimeError("playwright exploded")


def _no_env(_root: Path) -> None:
    pass


def _nu_target(_ticker: str | None) -> list[tuple[str, int]]:
    return [("NU", 12)]


def _fetch_without_receipt(*_args: object, **_kwargs: object) -> object:
    return SimpleNamespace(
        status="acquired", result=SimpleNamespace(source_name="issuer_ir", receipt_id="")
    )


# ---------------------------------------------------------------------------
# due_for_scan — pure window decision
# ---------------------------------------------------------------------------


def test_due_for_scan_in_window() -> None:
    mod = _load_module()
    today = date(2026, 5, 10)
    assert mod.due_for_scan(date(2026, 5, 10), today, 14) is True  # day 0
    assert mod.due_for_scan(date(2026, 5, 1), today, 14) is True  # day 9
    assert mod.due_for_scan(date(2026, 4, 26), today, 14) is True  # day 14 (inclusive edge)


def test_due_for_scan_out_of_window() -> None:
    mod = _load_module()
    today = date(2026, 5, 10)
    assert mod.due_for_scan(date(2026, 4, 25), today, 14) is False  # day 15
    assert mod.due_for_scan(None, today, 14) is False  # no calendar cache
    assert mod.due_for_scan(date(2026, 5, 20), today, 14) is False  # future date (negative delta)


# ---------------------------------------------------------------------------
# scan_one — per-ticker orchestration (monkeypatched seams)
# ---------------------------------------------------------------------------


def test_scan_one_out_of_window_no_earnings(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _no_earnings)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "out_of_window"


def test_scan_one_out_of_window_stale_earnings(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _stale_earnings)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "out_of_window"


def test_scan_one_already_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _true)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "already_ingested"
    assert r.quarter == "Q1_2026"


def test_scan_one_dry_run_would_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    # fetch_qa must not run in a dry-run.
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, True, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "would_fetch"
    assert r.quarter == "Q1_2026"


def test_scan_one_pending_ingest_when_raw_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _true)
    # A raw already on disk must NOT trigger a re-fetch — flag it for ingest.
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "pending_ingest"


def test_scan_one_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_hit)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "fetched"
    assert r.detail == "issuer_ir"


def test_scan_one_not_published_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_none)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "not_published_yet"


def test_scan_one_error_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    r = mod.scan_one(
        "NU", _FYE_DEC, Path("/x"), _TODAY, 14, False, Path("/x/data/portfolio.db"), False
    )
    assert r.status == "error"
    assert "RuntimeError" in r.detail


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_run_ingest_disables_promotion_and_preserves_child_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=9)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert (
        mod._run_ingest(
            tmp_path,
            tmp_path / "authority.sqlite",
            "NU",
            None,
            owner_requested=False,
            dry_run=False,
        )
        == 9
    )
    assert captured["cmd"][-1] == "--no-promote"
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert mod._terminal_exit_code(9) == 9
    assert mod._terminal_exit_code(None, scan_errors=1) == 1
    assert mod._terminal_exit_code(7, scan_errors=1) == 7


def test_ingested_evidence_requires_exact_ticker_period_path_and_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    raw = tmp_path / "transcripts" / "raw" / "NU_Q1_2026.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("issuer transcript bytes", encoding="utf-8")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, ticker TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL, period_end TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'NU', 'transcripts/raw/NU_Q1_2026.txt', ?)",
        (digest,),
    )
    conn.execute("INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31 00:00:00')")
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "get_connection", connect)

    assert mod._ingested_evidence_exists(tmp_path, "NU", 2026, 1, 12) is True
    assert mod._ingested_evidence_exists(tmp_path, "WIX", 2026, 1, 12) is False
    assert mod._ingested_evidence_exists(tmp_path, "NU", 2026, 2, 12) is False

    raw.write_text("different bytes", encoding="utf-8")
    assert mod._ingested_evidence_exists(tmp_path, "NU", 2026, 1, 12) is False


@pytest.mark.parametrize(
    "recorded",
    ["../outside.txt", "transcripts/raw/../outside.txt", "C:/outside.txt"],
)
def test_ingested_evidence_rejects_non_relative_recorded_paths(
    recorded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31');
        """
    )
    conn.execute("INSERT INTO documents VALUES (1, 'NU', ?, 'abc')", (recorded,))
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._ingested_evidence_exists(tmp_path, "NU", 2026, 1, 12) is False


def test_scan_scope_applies_shared_acquisition_policy_and_instrument_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT, "
            "fiscal_year_end TEXT, instrument_type TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL, '12-31', 'equity')",
            [
                ("PORT", "portfolio"),
                ("EVAL", "evaluation"),
                ("WATCH", "watchlist"),
                ("IDX", "index_member"),
            ],
        )

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, 'evaluation', NULL, '12-31', ?)",
            [("FUND", "etf"), ("UNTYPED", None), ("AMBIG", "equity"), ("AMBIG", "equity")],
        )
    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._resolve_tickers(None) == [("PORT", 12), ("EVAL", 12), ("WATCH", 12)]
    assert mod._resolve_tickers("EVAL") == [("EVAL", 12)]
    assert mod._resolve_tickers("WATCH") == [("WATCH", 12)]
    assert mod._resolve_tickers("IDX") == []


# ---------------------------------------------------------------------------
# last_earnings_date — most-recent past date from the FMP calendar cache
# ---------------------------------------------------------------------------


def _write_cal(root: Path, ticker: str, dates: list[str]) -> None:
    fmp = root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / f"{ticker}_earnings_calendar.json").write_text(
        json.dumps([{"date": d} for d in dates]), encoding="utf-8"
    )


def test_last_earnings_date_picks_max_past(tmp_path: Path) -> None:
    # 2099 is future for any plausible run date; max past is 2021-05-08.
    _write_cal(tmp_path, "NU", ["2020-02-20", "2021-05-08", "2099-08-14"])
    assert last_earnings_date(tmp_path, "NU") == date(2021, 5, 8)


def test_last_earnings_date_none_when_cache_missing(tmp_path: Path) -> None:
    assert last_earnings_date(tmp_path, "ZZZZ") is None


def test_last_earnings_date_none_when_all_future(tmp_path: Path) -> None:
    _write_cal(tmp_path, "NU", ["2099-01-01", "2099-08-14"])
    assert last_earnings_date(tmp_path, "NU") is None


def test_last_earnings_date_none_on_garbled_json(tmp_path: Path) -> None:
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "NU_earnings_calendar.json").write_text("{not json", encoding="utf-8")
    assert last_earnings_date(tmp_path, "NU") is None


@pytest.mark.parametrize("owner_requested", [False, True])
def test_main_routes_explicit_database_state_and_exact_fetch_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_requested: bool
) -> None:
    mod = _load_module()
    state = tmp_path / "state"
    state.mkdir()
    database = state / "authority.sqlite"
    database.touch()
    for field in ("DB_PATH", "DATA_DIR", "FMP_DIR", "PROJECT_ROOT", "STATE_ROOT"):
        monkeypatch.setattr(mod.db, field, getattr(mod.db, field))
    for field in ("RAW_DIR", "STAGING_DIR"):
        monkeypatch.setattr(mod.fetch_qa_transcript, field, getattr(mod.fetch_qa_transcript, field))
    monkeypatch.setattr(mod, "load_project_env", _no_env)
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "due_for_scan", _true)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    seen: dict[str, Any] = {}

    def resolve(ticker: str | None) -> list[tuple[str, int]]:
        assert Path(mod.db.DB_PATH) == database
        assert Path(mod.db.DATA_DIR) == state / "data"
        assert ticker == ("NU" if owner_requested else None)
        return [("NU", 12)]

    def fetch(spec: object, **kwargs: Any) -> object:
        seen["fetch"] = kwargs
        assert state / "transcripts/raw" == mod.fetch_qa_transcript.RAW_DIR
        assert state / ".tmp/transcript-acquisition" == mod.fetch_qa_transcript.STAGING_DIR
        return _fetch_hit(spec)

    def run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        seen["cmd"] = cmd
        seen["cwd"] = kwargs["cwd"]
        return _FakeProc()

    monkeypatch.setattr(mod, "_resolve_tickers", resolve)
    monkeypatch.setattr(mod, "fetch_qa", fetch)
    monkeypatch.setattr(mod.subprocess, "run", run)
    argv = ["scan", "--db", str(database), "--repo-root", str(state)]
    if owner_requested:
        argv.extend(["--ticker", "NU"])
    monkeypatch.setattr(sys, "argv", argv)

    assert mod.main() == 0
    assert seen["fetch"]["db_path"] == database
    assert seen["fetch"]["owner_requested"] is owner_requested
    cmd = seen["cmd"]
    assert str(PROJECT_ROOT / "execution/ingest_transcripts_state.py") in cmd
    assert cmd[cmd.index("--db") + 1] == str(database)
    assert cmd[cmd.index("--repo-root") + 1] == str(state)
    assert cmd[cmd.index("--ticker") + 1] == "NU"
    assert cmd[cmd.index("--receipt-id") + 1] == "a" * 64
    assert ("--owner-requested" in cmd) is owner_requested
    assert "--no-promote" in cmd
    assert seen["cwd"] == str(state)
    assert not (state / "data/portfolio.db").exists()


def test_main_dry_run_uses_configured_database_without_download_or_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_module()
    state = tmp_path / "state"
    state.mkdir()
    database = state / "configured.sqlite"
    database.write_bytes(b"unchanged database")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    monkeypatch.setattr(mod, "load_project_env", _no_env)
    for field in ("DB_PATH", "DATA_DIR", "FMP_DIR", "PROJECT_ROOT", "STATE_ROOT"):
        monkeypatch.setattr(mod.db, field, getattr(mod.db, field))
    for field in ("RAW_DIR", "STAGING_DIR"):
        monkeypatch.setattr(mod.fetch_qa_transcript, field, getattr(mod.fetch_qa_transcript, field))
    monkeypatch.setattr(mod, "_resolve_tickers", _nu_target)
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "due_for_scan", _true)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    monkeypatch.setattr(mod.subprocess, "run", _fetch_boom)
    monkeypatch.setattr(sys, "argv", ["scan", "--repo-root", str(state), "--dry-run"])

    assert mod.main() == 0
    assert Path(mod.db.DB_PATH) == database
    assert database.read_bytes() == b"unchanged database"
    assert not (state / "data/portfolio.db").exists()
    assert json.loads(capsys.readouterr().out)["status_counts"] == {"would_fetch": 1}


def test_scan_one_refuses_acquired_result_without_exact_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_without_receipt)
    result = mod.scan_one("NU", 12, tmp_path, _TODAY, 14, False, tmp_path / "db", False)
    assert result.status == "error"


def test_pending_ingest_keeps_legacy_period_scope_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_ingested_evidence_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _true)
    result = mod.scan_one("NU", 12, tmp_path, _TODAY, 14, False, tmp_path / "db", False)
    assert result.receipt_id is None
    assert result.ingest_scope == "legacy_pending_ticker_retained_periods"


def test_invalid_explicit_database_stops_before_roster_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "load_project_env", _no_env)
    monkeypatch.setattr(mod, "_resolve_tickers", _fetch_boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(tmp_path / "missing.sqlite"),
        ],
    )
    with pytest.raises(FileNotFoundError):
        mod.main()
    assert not (tmp_path / "missing.sqlite").exists()
    assert not (tmp_path / "data/portfolio.db").exists()


def test_mismatched_database_state_stops_before_roster_fetch_or_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    state = tmp_path / "state"
    state.mkdir()
    database = tmp_path / "outside.sqlite"
    database.write_bytes(b"unchanged")
    monkeypatch.setattr(mod, "load_project_env", _no_env)
    monkeypatch.setattr(mod, "_resolve_tickers", _fetch_boom)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    monkeypatch.setattr(mod.subprocess, "run", _fetch_boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan",
            "--repo-root",
            str(state),
            "--db",
            str(database),
        ],
    )
    with pytest.raises(ValueError, match=r"database.*state root"):
        mod.main()
    assert database.read_bytes() == b"unchanged"
    assert not (state / "data/portfolio.db").exists()
