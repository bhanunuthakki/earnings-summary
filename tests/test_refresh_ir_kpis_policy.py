from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from execution import refresh_ir_kpis, refresh_ir_kpis_all
from ir_pipeline.config import IrConfig


def _db(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, "
            "archived_at TEXT, fiscal_year_end TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL, '12-31')",
            rows,
        )


@pytest.mark.parametrize(
    ("role", "owner_requested", "allowed"),
    [
        ("portfolio", False, True),
        ("evaluation", False, False),
        ("evaluation", True, True),
        ("watchlist", True, False),
        ("index_member", True, False),
    ],
)
def test_direct_kpi_refresh_authorizes_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    owner_requested: bool,
    allowed: bool,
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    _db(db_path, [("ACME", role)])
    args = argparse.Namespace(
        ticker="ACME",
        quarters=5,
        file=None,
        url="https://issuer.example/kpis.xlsx",
        discover=False,
        platform=None,
        results_center_url=None,
        repo_root=tmp_path,
        db=db_path,
        owner_requested=owner_requested,
    )
    monkeypatch.setattr(refresh_ir_kpis, "_parse_args", lambda: args)

    class ReachedNetworkError(RuntimeError):
        pass

    def reach_network(_url: str, _repo_root: Path, _ticker: str) -> Path:
        raise ReachedNetworkError

    monkeypatch.setattr(
        refresh_ir_kpis,
        "download_spreadsheet",
        reach_network,
    )
    if allowed:
        with pytest.raises(ReachedNetworkError):
            refresh_ir_kpis.main()
    else:
        assert refresh_ir_kpis.main() == 2


def test_direct_kpi_refresh_rejects_more_than_five_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(
        ticker="ACME",
        quarters=6,
        file=None,
        url="https://issuer.example/kpis.xlsx",
        discover=False,
        platform=None,
        results_center_url=None,
        repo_root=tmp_path,
        db=tmp_path / "portfolio.db",
        owner_requested=True,
    )
    monkeypatch.setattr(refresh_ir_kpis, "_parse_args", lambda: args)

    def fail_network(_url: str, _repo_root: Path, _ticker: str) -> Path:
        pytest.fail("network boundary crossed")

    monkeypatch.setattr(
        refresh_ir_kpis,
        "download_spreadsheet",
        fail_network,
    )
    assert refresh_ir_kpis.main() == 2


def test_q4cdn_discover_fails_cleanly_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    _db(db_path, [("ACME", "portfolio")])
    args = argparse.Namespace(
        ticker="ACME",
        quarters=5,
        file=None,
        url=None,
        discover=True,
        platform=None,
        results_center_url=None,
        repo_root=tmp_path,
        db=db_path,
        owner_requested=False,
    )
    monkeypatch.setattr(refresh_ir_kpis, "_parse_args", lambda: args)
    monkeypatch.setattr(
        refresh_ir_kpis,
        "get_config",
        lambda _ticker, _repo_root: IrConfig(
            ticker="ACME",
            platform="q4cdn",
            results_center_url="https://issuer.example/results",
        ),
    )
    monkeypatch.setattr(
        refresh_ir_kpis,
        "download_spreadsheet",
        lambda *_args: pytest.fail("network boundary crossed"),
    )

    assert refresh_ir_kpis.main() == 3
    error = capsys.readouterr().err
    assert "No precise IR discovery adapter" in error
    assert "generic discovery or a direct URL" in error
    assert "Traceback" not in error


def test_batch_scope_is_portfolio_automatic_and_explicit_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    _db(
        db_path,
        [
            ("PORT", "portfolio"),
            ("EVAL", "evaluation"),
            ("WATCH", "watchlist"),
            ("IDX", "index_member"),
        ],
    )

    def configured(_root: Path) -> list[str]:
        return ["PORT", "EVAL", "WATCH", "IDX", "UNKNOWN"]

    def on_disk(_root: Path) -> dict[str, Path]:
        return {}

    monkeypatch.setattr(refresh_ir_kpis_all, "configured_tickers", configured)
    monkeypatch.setattr(refresh_ir_kpis_all, "_spreadsheet_tickers", on_disk)

    automatic, automatic_skipped = refresh_ir_kpis_all.resolve_tickers(
        repo_root=tmp_path,
        db_path=db_path,
        requested=None,
    )
    requested, requested_skipped = refresh_ir_kpis_all.resolve_tickers(
        repo_root=tmp_path,
        db_path=db_path,
        requested=["EVAL", "WATCH", "IDX", "UNKNOWN"],
    )

    assert [job.ticker for job in automatic] == ["PORT"]
    assert set(automatic_skipped) == {"EVAL", "WATCH", "IDX", "UNKNOWN"}
    assert [job.ticker for job in requested] == ["EVAL"]
    assert set(requested_skipped) == {"WATCH", "IDX", "UNKNOWN"}
