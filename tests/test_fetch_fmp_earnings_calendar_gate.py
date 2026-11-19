"""Tests for the FMP_TIER skip gate in execution/fetch_fmp_earnings_calendar.py.

On a no-paid FMP tier (basic/free) the /stable/earnings endpoint 402s for every
ticker and the calls bypass the budget ledger, so the fetch is skipped by
default; --force overrides it (e.g. right after upgrading the subscription).
"""

from __future__ import annotations

import argparse
import sys

import pytest

from execution import fetch_fmp_earnings_calendar as mod


@pytest.mark.parametrize("tier", ["basic", "free", "BASIC", " Free "])
def test_no_paid_tier_skips_fetch(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    monkeypatch.setenv("FMP_TIER", tier)
    called: list[str] = []

    def _record(ticker: str, limit: int) -> bool:
        called.append(ticker)
        return True

    monkeypatch.setattr(mod, "process_ticker", _record)
    monkeypatch.setattr(sys, "argv", ["prog", "--all"])

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 0
    assert called == []  # the FMP fetch never ran


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, called: list[str]) -> None:
    """Wire a paid-looking key + a one-ticker selection + a recording fetch."""
    monkeypatch.setattr(mod, "FMP_API_KEY", "dummy-key")

    def _select(_args: argparse.Namespace) -> list[str]:
        return ["NU"]

    def _record(ticker: str, limit: int) -> bool:
        called.append(ticker)
        return True

    monkeypatch.setattr(mod, "select_tickers", _select)
    monkeypatch.setattr(mod, "process_ticker", _record)


def test_force_bypasses_the_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FMP_TIER", "basic")
    called: list[str] = []
    _stub_fetch(monkeypatch, called)
    monkeypatch.setattr(sys, "argv", ["prog", "--all", "--force"])

    # --force pushes past the tier gate; an all-ok run returns without sys.exit.
    mod.main()

    assert called == ["NU"]


def test_paid_tier_does_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FMP_TIER", "premium")
    called: list[str] = []
    _stub_fetch(monkeypatch, called)
    monkeypatch.setattr(sys, "argv", ["prog", "--all"])

    mod.main()

    assert called == ["NU"]


def test_owner_disabled_fmp_exits_zero_without_force_and_nonzero_with_force(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: object,
) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    policy = tmp_path / "fmp-policy.json"
    policy.write_text('{"schema_version":1,"enabled":false}', encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_FMP_POLICY_FILE", str(policy))

    monkeypatch.setattr(sys, "argv", ["prog", "--all"])
    with pytest.raises(SystemExit) as clean_exit:
        mod.main()
    assert clean_exit.value.code == 0
    assert '"event": "fmp_calendar_skipped"' in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["prog", "--all", "--force"])
    with pytest.raises(SystemExit) as forced_exit:
        mod.main()
    assert forced_exit.value.code == 1
    assert '"event": "fmp_calendar_unavailable"' in capsys.readouterr().err


def test_calendar_select_tickers_excludes_etfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    import sqlite3
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db_path = tmp_path / "calendar.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, instrument_type TEXT, archived_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, ?, ?, NULL)",
        [
            ("META", "portfolio", "equity"),
            ("AVDV", "portfolio", "etf"),
            ("NU", "portfolio", "adr"),
            ("RSP", "evaluation", "etf"),
            ("1548.HK", "evaluation", "equity"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(mod.db, "DB_PATH", str(db_path))
    assert mod.select_tickers(
        argparse.Namespace(ticker=None, portfolio=True, watchlist=False, evaluation=False)
    ) == ["META", "NU"]
    assert mod.select_tickers(
        argparse.Namespace(ticker=None, portfolio=False, watchlist=False, evaluation=False)
    ) == ["1548.HK", "META", "NU"]
