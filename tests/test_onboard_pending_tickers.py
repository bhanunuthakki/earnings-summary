"""Unit tests for execution/onboard_pending_tickers.py.

Tests focus on the SQL `find_pending_tickers` selector — the rest of the
script is subprocess plumbing covered by integration tests in CI.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    """Import execution/onboard_pending_tickers.py without executing main()."""
    src = PROJECT_ROOT / "execution" / "onboard_pending_tickers.py"
    spec = importlib.util.spec_from_file_location("onboard_pending_tickers", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["onboard_pending_tickers"] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_minimal_schema(db_path: Path) -> None:
    """Create only the tables the selector touches; mirrors prod shape."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            instrument_type TEXT,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_end TEXT,
            value REAL
        );
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            valuation_date TEXT,
            npv REAL
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_end TEXT
        );
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_target TEXT,
            kpi_name TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _add_ticker(
    db_path: Path,
    ticker: str,
    list_type: str,
    *,
    instrument_type: str | None,
    facts: int = 0,
    dcf: int = 0,
    transcripts: int = 0,
    commitments: int = 0,
    added_at: str = "2026-05-01",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, f"{ticker} Corp", list_type, instrument_type, added_at),
    )
    for i in range(facts):
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, value) VALUES (?, ?, ?)",
            (ticker, "2025-12-31", float(i)),
        )
    for i in range(dcf):
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv) VALUES (?, ?, ?)",
            (ticker, "2025-12-31", float(i)),
        )
    for i in range(transcripts):
        conn.execute(
            "INSERT INTO transcripts (ticker, period_end) VALUES (?, ?)",
            (ticker, "2025-12-31"),
        )
    for i in range(commitments):
        conn.execute(
            "INSERT INTO management_commitments (ticker, period_target, kpi_name) VALUES (?, ?, ?)",
            (ticker, "2026-03-31", "Revenue YoY Growth (USD)"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    _seed_minimal_schema(p)
    return p


def test_no_pending_when_universe_empty(db: Path) -> None:
    mod = _load_module()
    assert mod.find_pending_tickers(db) == []


def test_fully_onboarded_ticker_with_no_transcripts_is_not_pending(db: Path) -> None:
    """Onboarded ticker with no transcripts at all has nothing for the
    extractor to do — it shouldn't be flagged."""
    mod = _load_module()
    _add_ticker(db, "GOOG", "portfolio", instrument_type="equity", facts=10, dcf=1)
    assert mod.find_pending_tickers(db) == []


def test_missing_instrument_type_flags_pending(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "MSFT", "watchlist", instrument_type=None, facts=10, dcf=1)
    pending = mod.find_pending_tickers(db)
    assert pending == [("MSFT", "no_instrument_type")]


def test_zero_facts_flags_pending(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "AMD", "watchlist", instrument_type="equity", facts=0, dcf=0)
    pending = mod.find_pending_tickers(db)
    # Reason precedence: no_instrument_type beats no_financial_facts.
    # Here instrument_type is set, so no_financial_facts wins.
    assert pending == [("AMD", "no_financial_facts")]


def test_zero_dcf_flags_pending_when_facts_present(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "BKNG", "watchlist", instrument_type="equity", facts=5, dcf=0)
    pending = mod.find_pending_tickers(db)
    assert pending == [("BKNG", "no_dcf_run")]


def test_excludes_index_member_and_none_rows(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "AAPL", "index_member", instrument_type=None, facts=0, dcf=0)
    _add_ticker(db, "WPM", "none", instrument_type=None, facts=0, dcf=0)
    _add_ticker(db, "MU", "watchlist", instrument_type="equity", facts=10, dcf=1)
    assert mod.find_pending_tickers(db) == []


def test_orders_by_added_at_then_ticker(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "ZZZ", "watchlist", instrument_type=None, added_at="2026-05-01")
    _add_ticker(db, "AAA", "watchlist", instrument_type=None, added_at="2026-05-02")
    _add_ticker(db, "MMM", "watchlist", instrument_type=None, added_at="2026-05-01")
    pending = mod.find_pending_tickers(db)
    assert [t for t, _ in pending] == ["MMM", "ZZZ", "AAA"]


def test_reason_precedence(db: Path) -> None:
    """instrument_type wins over facts wins over dcf wins over commitments —
    matches the CASE WHEN ladder in _PENDING_SQL."""
    mod = _load_module()
    _add_ticker(db, "T1", "watchlist", instrument_type=None, facts=0, dcf=0)
    _add_ticker(db, "T2", "watchlist", instrument_type="equity", facts=0, dcf=0)
    _add_ticker(db, "T3", "watchlist", instrument_type="equity", facts=5, dcf=0)
    _add_ticker(
        db,
        "T4",
        "watchlist",
        instrument_type="equity",
        facts=5,
        dcf=1,
        transcripts=2,
        commitments=0,
    )
    pending = dict(mod.find_pending_tickers(db))
    assert pending["T1"] == "no_instrument_type"
    assert pending["T2"] == "no_financial_facts"
    assert pending["T3"] == "no_dcf_run"
    assert pending["T4"] == "no_commitments"


def test_no_commitments_pending_only_when_transcripts_exist(db: Path) -> None:
    """A fully onboarded ticker with NO transcripts is NOT pending —
    nothing for the extractor to chew on. Only flag when the ticker has
    transcripts but zero management_commitments rows."""
    mod = _load_module()
    # Fully onboarded, no transcripts -> not pending.
    _add_ticker(
        db,
        "NOPE",
        "watchlist",
        instrument_type="equity",
        facts=5,
        dcf=1,
        transcripts=0,
        commitments=0,
    )
    # Fully onboarded, has transcripts, zero commitments -> pending.
    _add_ticker(
        db,
        "EXTRACT_ME",
        "watchlist",
        instrument_type="equity",
        facts=5,
        dcf=1,
        transcripts=3,
        commitments=0,
    )
    pending = mod.find_pending_tickers(db)
    assert pending == [("EXTRACT_ME", "no_commitments")]


def test_fully_onboarded_with_commitments_is_not_pending(db: Path) -> None:
    """Once at least one commitment row exists, the ticker is no longer pending —
    the extractor itself decides whether each individual transcript needs
    re-processing (idempotent at the transcript level)."""
    mod = _load_module()
    _add_ticker(
        db,
        "DONE",
        "portfolio",
        instrument_type="equity",
        facts=10,
        dcf=1,
        transcripts=4,
        commitments=2,
    )
    assert mod.find_pending_tickers(db) == []


# ---------------------------------------------------------------------------
# onboard_one stage selection
# ---------------------------------------------------------------------------


def test_onboard_one_skips_heavy_stages_for_no_commitments(
    tmp_path: Path,
) -> None:
    """When pending_reason='no_commitments', onboard_ticker / eval / refresh_dcf
    should be SKIPPED (the ticker is already fully onboarded), and only the
    extract_commitments stage should run.

    We can't run the actual subprocesses in a unit test, so we monkey-patch
    _run_subprocess to just record which stages would have been invoked."""
    mod = _load_module()
    log_path = tmp_path / "log.txt"

    invoked: list[str] = []

    def fake_run_subprocess(cmd, stage, log_path):
        invoked.append(stage)
        return mod.StageResult(stage=stage, outcome=mod.StageOutcome.OK, rc=0, detail="")

    mod._run_subprocess = fake_run_subprocess  # type: ignore[assignment]

    result = mod.onboard_one(
        ticker="DONE",
        pending_reason="no_commitments",
        skip_fmp=False,
        skip_commitments=False,
        log_path=log_path,
    )

    # Only the extract_commitments subprocess should have been invoked.
    assert invoked == ["extract_commitments"]
    # The first three stage results should be SKIPPED (filled by _skipped helper).
    assert [s.stage for s in result.stages[:3]] == [
        "onboard_ticker",
        "run_thesis_evaluator",
        "refresh_dcf",
    ]
    assert all(s.outcome is mod.StageOutcome.SKIPPED for s in result.stages[:3])


def test_onboard_one_runs_full_chain_for_data_pending(tmp_path: Path) -> None:
    """When pending_reason isn't 'no_commitments', all 4 stages should fire."""
    mod = _load_module()
    log_path = tmp_path / "log.txt"

    invoked: list[str] = []

    def fake_run_subprocess(cmd, stage, log_path):
        invoked.append(stage)
        return mod.StageResult(stage=stage, outcome=mod.StageOutcome.OK, rc=0, detail="")

    mod._run_subprocess = fake_run_subprocess  # type: ignore[assignment]

    mod.onboard_one(
        ticker="NEW",
        pending_reason="no_instrument_type",
        skip_fmp=False,
        skip_commitments=False,
        log_path=log_path,
    )
    assert invoked == [
        "onboard_ticker",
        "run_thesis_evaluator",
        "refresh_dcf",
        "extract_commitments",
    ]


def test_onboard_one_skip_commitments_flag(tmp_path: Path) -> None:
    """--skip-commitments suppresses the extractor stage for both chain types."""
    mod = _load_module()
    log_path = tmp_path / "log.txt"

    invoked: list[str] = []

    def fake_run_subprocess(cmd, stage, log_path):
        invoked.append(stage)
        return mod.StageResult(stage=stage, outcome=mod.StageOutcome.OK, rc=0, detail="")

    mod._run_subprocess = fake_run_subprocess  # type: ignore[assignment]

    result_full = mod.onboard_one(
        ticker="NEW",
        pending_reason="no_instrument_type",
        skip_fmp=False,
        skip_commitments=True,
        log_path=log_path,
    )
    assert invoked == ["onboard_ticker", "run_thesis_evaluator", "refresh_dcf"]
    assert result_full.stages[3].stage == "extract_commitments"
    assert result_full.stages[3].outcome is mod.StageOutcome.SKIPPED
