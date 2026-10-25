from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from execution import run_lens as cli
from synthesis_lenses import Lens, list_lenses_for_ticker, list_portfolio_lenses


def test_portfolio_tickers_are_loaded_from_active_governed_roster(tmp_path: Path) -> None:
    from execution.run_lens import portfolio_tickers

    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "portfolio.db")
    try:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, ?)",
            (
                ("uber", "portfolio", None),
                ("BKNG", "portfolio", None),
                ("OLD", "portfolio", "2026-08-01"),
                ("DUOL", "evaluation", None),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    assert portfolio_tickers(tmp_path) == ["BKNG", "UBER"]


_TICKER_LENSES: list[str] = list(list_lenses_for_ticker())
_PORTFOLIO_LENSES: list[str] = list(list_portfolio_lenses())


def _noop_sync_db_path(repo_root: Path) -> None:
    return None


def _synthetic_portfolio_tickers(repo_root: Path) -> list[str]:
    assert isinstance(repo_root, Path)
    return ["GOOG", "UBER"]


def _fake_run_lens_factory(
    tmp_path: Path,
) -> tuple[Callable[..., SimpleNamespace], list[tuple[str, str | None, bool]]]:
    calls: list[tuple[str, str | None, bool]] = []

    def fake_run_lens(
        lens: Lens, *, ticker: str | None, repo_root: Path, force: bool
    ) -> SimpleNamespace:
        assert isinstance(lens, Lens)
        assert repo_root.resolve() == tmp_path.resolve()
        calls.append((lens.name, ticker, force))
        return SimpleNamespace(id=1, content_md="synthetic")

    return fake_run_lens, calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ("--ticker", "GOOG", "--lens", "thesis_drift_qoq", "--force"),
            [("thesis_drift_qoq", "GOOG", True)],
            id="single-ticker-single-lens-force",
        ),
        pytest.param(
            ("--tickers", "AAPL,MSFT", "--lens", "five_min_reread"),
            [("five_min_reread", "AAPL", False), ("five_min_reread", "MSFT", False)],
            id="two-tickers-five-min-reread",
        ),
        pytest.param(
            ("--ticker", "GOOG", "--all"),
            [(lens_name, "GOOG", False) for lens_name in _TICKER_LENSES],
            id="ticker-all-lenses",
        ),
        pytest.param(
            ("--lens", "cross_portfolio_synthesis"),
            [("cross_portfolio_synthesis", None, False)],
            id="portfolio-scope-single-lens",
        ),
        pytest.param(
            ("--ticker", "GOOG", "--lens", "cross_portfolio_synthesis"),
            [],
            id="portfolio-lens-rejects-ticker-context",
        ),
        pytest.param(
            ("--lens", "thesis_drift_qoq"),
            [],
            id="ticker-lens-requires-ticker-context",
        ),
        pytest.param(
            ("--portfolio", "--all"),
            [
                *[(lens_name, "GOOG", False) for lens_name in _TICKER_LENSES],
                *[(lens_name, "UBER", False) for lens_name in _TICKER_LENSES],
                *[(lens_name, None, False) for lens_name in _PORTFOLIO_LENSES],
            ],
            id="portfolio-all-lenses",
        ),
    ],
)
def test_main_retained_execution_plans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: tuple[str, ...],
    expected: list[tuple[str, str | None, bool]],
) -> None:
    fake_run_lens, calls = _fake_run_lens_factory(tmp_path)
    monkeypatch.setattr(cli, "_sync_db_path", _noop_sync_db_path)
    monkeypatch.setattr(cli, "portfolio_tickers", _synthetic_portfolio_tickers)
    monkeypatch.setattr(cli, "run_lens", fake_run_lens)
    monkeypatch.setattr(sys, "argv", ["run_lens.py", *argv, "--repo-root", str(tmp_path)])

    assert cli.main() == 0
    assert calls == expected
