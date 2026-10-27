"""ETF research stops at the boundary; held funds remain in the book."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import NoReturn

import pytest

import candidate_fit_cache
from allocation.candidate_fit import BookContext, CandidateFit
from execution import (
    build_artifacts,
    daily_fetch_and_brief,
    onboard_ticker,
    refresh_candidate_fit,
)
from pipeline.research_cockpit import build_cockpit_rows

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_onboard: Callable[[argparse.Namespace], int] = getattr(onboard_ticker, "_onboard")
_all_tracked: Callable[[Path], list[str]] = getattr(build_artifacts, "_all_tracked")
_regenerate_native_purpose: Callable[..., dict[str, object]] = getattr(
    build_artifacts, "_regenerate_native_purpose"
)
_resolve_tickers: Callable[[sqlite3.Connection, argparse.Namespace], list[str]] = getattr(
    daily_fetch_and_brief, "_resolve_tickers"
)
_refresh_one_ticker: Callable[[str, Path, Path, argparse.Namespace], dict[str, object]] = getattr(
    daily_fetch_and_brief, "_refresh_one_ticker"
)


def _seed(database: Path, ticker: str, kind: str) -> None:
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type,instrument_type) "
            "VALUES(?,?,'evaluation',?)",
            (ticker, ticker, kind),
        )
        conn.commit()


def _forbidden(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("retired ETF work crossed its side-effect boundary")


def test_company_fit_excludes_etf_candidates_but_keeps_held_funds(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "EQTY", "equity")
    _seed(database, "FUND", "etf")
    book = BookContext(weights={"FUND": 0.4, "EQTY": 0.6})
    seen: list[tuple[list[str], BookContext]] = []
    sector_inputs: list[list[str]] = []

    def sectors(root: Path, tickers: list[str]) -> dict[str, str]:
        sector_inputs.append(tickers.copy())
        return {}

    def compute(
        root: Path, tickers: list[str], context: BookContext, **kwargs: object
    ) -> dict[str, CandidateFit]:
        seen.append((tickers.copy(), context))
        return {}

    def assemble(*args: object, **kwargs: object) -> BookContext:
        return book

    monkeypatch.setattr(candidate_fit_cache, "assemble_book_context", assemble)
    monkeypatch.setattr(candidate_fit_cache, "_load_sectors", sectors)
    monkeypatch.setattr(candidate_fit_cache, "compute_candidate_fit", compute)
    with closing(sqlite3.connect(database)) as conn:
        assert candidate_fit_cache.materialize_candidate_fit(conn, tmp_path, db_path=database) == 0
    assert sector_inputs == [["EQTY"]]
    assert seen == [(["EQTY"], book)]
    assert seen[0][1] is book
    assert book.weights == {"FUND": 0.4, "EQTY": 0.6}


def test_company_fit_refresh_never_runs_retired_etf_tail(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    calls: list[str] = []

    def materialize(*args: object, **kwargs: object) -> int:
        calls.append("company")
        return 3

    monkeypatch.setattr(refresh_candidate_fit, "materialize_candidate_fit", materialize)
    monkeypatch.setattr(refresh_candidate_fit, "materialize_etf_scores", _forbidden, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["refresh_candidate_fit.py", "--repo-root", str(tmp_path), "--db-path", str(database)],
    )
    assert refresh_candidate_fit.main() == 0
    assert calls == ["company"]
    assert not (tmp_path / "data/etf_score.json").exists()


def test_unreadable_instrument_kind_cannot_fall_back_to_scoring(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "FUND", "etf")
    cache = tmp_path / "data/candidate_fit.json"
    cache.write_bytes(b"last-good-synthetic-cache")
    monkeypatch.setattr(candidate_fit_cache, "assemble_book_context", _forbidden)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("ALTER TABLE tracked_companies RENAME COLUMN instrument_type TO old_kind")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="instrument_type"):
            candidate_fit_cache.materialize_candidate_fit(conn, tmp_path, db_path=database)
    assert cache.read_bytes() == b"last-good-synthetic-cache"


@pytest.mark.parametrize("declared", [True, False])
def test_known_etf_onboarding_stops_before_providers_and_writers(
    declared: bool,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "FUND", "etf")
    monkeypatch.setattr(onboard_ticker, "_DB_PATH", database)
    monkeypatch.setattr(onboard_ticker, "_STATE_ROOT", tmp_path)
    monkeypatch.setattr(onboard_ticker, "_run_fmp_fetch", _forbidden)
    monkeypatch.setattr(onboard_ticker, "open_db", _forbidden)
    args = argparse.Namespace(
        ticker="FUND",
        instrument="etf" if declared else None,
        industry_template=None,
        skip_fmp=False,
    )
    assert _onboard(args) == 2
    assert "unsupported" in capsys.readouterr().out
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0


def test_etf_brief_request_returns_unsupported_without_creating_report(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "FUND", "etf")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_artifacts.py",
            "--ticker",
            "FUND",
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(database),
        ],
    )
    assert build_artifacts.main() == 2
    assert not (tmp_path / "output/research/FUND").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary[0]["status"] == "unsupported"
    assert summary[0]["instrument_kind"] == "etf"
    assert not (tmp_path / "output/research/report_artifacts.v1.json").exists()
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 0


def test_etf_native_generation_stops_before_company_producer(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "FUND", "etf")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    monkeypatch.setattr(build_artifacts.db, "DB_PATH", str(database))
    monkeypatch.setattr(build_artifacts.ticker_settings, "get_bypass_budget", _forbidden)
    result = _regenerate_native_purpose(
        "FUND", tmp_path, purpose="bear_case", force_budget_bypass=False
    )
    assert result["status"] == "unsupported"
    assert result["instrument_kind"] == "etf"
    assert not (tmp_path / "output").exists()


def test_company_report_batch_excludes_etfs(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "EQTY", "equity")
    _seed(database, "FUND", "etf")
    monkeypatch.setattr(build_artifacts.db, "DB_PATH", str(database))
    assert _all_tracked(tmp_path) == ["EQTY"]


def test_daily_company_batches_exclude_etfs(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "EQTY", "equity")
    _seed(database, "FUND", "etf")
    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE tracked_companies SET brief_dirty=1")
        conn.commit()
        for all_tracked in (True, False):
            args = argparse.Namespace(ticker=None, all_tracked=all_tracked, limit=0)
            assert _resolve_tickers(conn, args) == ["EQTY"]


def test_explicit_daily_etf_request_stops_before_company_chain(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "FUND", "etf")
    monkeypatch.setattr(daily_fetch_and_brief, "_check_skip_gates", _forbidden)
    monkeypatch.setattr(daily_fetch_and_brief, "_run_step", _forbidden)
    args = argparse.Namespace(force=True, ticker="FUND", eval_cadence_days=7, no_change_ttl_days=7)
    result = _refresh_one_ticker("FUND", tmp_path, database, args)
    assert result["status"] == "unsupported"
    assert result["reason"] == "etf_research_retired"


def test_held_etf_weight_survives_without_candidate_fit(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, "FUND", "etf")
    (tmp_path / "data/portfolio_weights.json").write_text(
        json.dumps({"computed_at": "synthetic", "weights": {"FUND": 0.031}}),
        encoding="utf-8",
    )
    assert not (tmp_path / "data/candidate_fit.json").exists()
    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        row = build_cockpit_rows(conn, tmp_path)["evaluation"][0]
    assert row.held_weight == pytest.approx(0.031)
    assert row.is_etf
    assert row.attractiveness is None and row.fit is None
    assert row.sharpe_delta_bps is None


@pytest.mark.parametrize(
    "entrypoint",
    [
        "build_etf_workup.py",
        "fetch_etf_data.py",
        "fetch_etf_published_data.py",
    ],
)
def test_retired_etf_command_entrypoints_are_removed(entrypoint: str) -> None:
    assert not (PROJECT_ROOT / "execution" / entrypoint).exists()
