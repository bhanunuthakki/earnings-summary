"""Operator boundary tests for the holdings-to-roster reconciler CLI."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

import db
from execution import sync_list_type_from_holdings as cli
from integrations.portfolio_tracker_v1 import (
    AccountV1,
    PortfolioSnapshotV1,
    SecuritiesV1Result,
    SecurityV1,
    V1AccountCoverage,
    V1Fetch,
    V1Meta,
)
from list_type_reconcile import Reclassification


def _es_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tracked_companies (
            user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
            ir_url TEXT, archived_at TIMESTAMP,
            brief_dirty BOOLEAN DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def _track(path: Path, ticker: str, list_type: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracked_companies(user_id, ticker, name, list_type) VALUES (?, ?, ?, ?)",
        ("bhanu", ticker, f"{ticker} Inc", list_type),
    )
    conn.commit()
    conn.close()


def _list_type(path: Path, ticker: str) -> str:
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT list_type FROM tracked_companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    return str(row[0])


def _tracker_fetches() -> tuple[V1Fetch[PortfolioSnapshotV1], V1Fetch[SecuritiesV1Result]]:
    meta = V1Meta(
        schema_version="1.0.0",
        generated_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
        as_of=date(2026, 6, 14),
        currency="USD",
        account_coverage=V1AccountCoverage(included_account_ids=[1]),
        last_successful_sync_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
        is_partial=False,
        is_stale=False,
    )
    account = AccountV1.model_validate(
        {
            "account_id": 1,
            "canonical_account_id": None,
            "provider": "test",
            "institution": None,
            "name": "Account 1",
            "official_name": None,
            "type": "investment",
            "subtype": None,
            "mask": "0000",
            "active": True,
            "included_in_totals": True,
            "exclusion_reason": None,
            "tax_treatment": "taxable",
            "tax_treatment_evidence": None,
            "tax_treatment_confidence": "high",
            "value": None,
            "value_currency": "USD",
            "holdings_as_of": "2026-06-14",
            "last_successful_sync_at": "2026-06-14T12:00:00+00:00",
            "warnings": [],
        }
    )
    snapshot = PortfolioSnapshotV1.model_validate(
        {
            "meta": meta.model_dump(mode="json"),
            "accounts": [account.model_dump(mode="json")],
            "total_market_value": "88000",
            "positions": [
                {
                    "security_id": 1,
                    "ticker": "MELI",
                    "name": "MercadoLibre",
                    "quantity": "10",
                    "market_value": "88000",
                    "cost_basis": None,
                    "unrealized_pnl": None,
                    "percent_of_portfolio": None,
                    "accounts": [
                        {
                            "account_id": 1,
                            "account_name": "Account 1",
                            "quantity": "10",
                            "market_value": "88000",
                            "cost_basis": None,
                            "cost_basis_source": None,
                            "tax_treatment": "taxable",
                        }
                    ],
                }
            ],
            "equity_fraction": {
                "equity_value": "88000",
                "denominator_value": "88000",
                "equity_fraction": "1",
                "unit": "fraction",
                "cash_equivalent_policy": "exclude",
                "included_account_ids": [1],
                "holdings_as_of": "2026-06-14",
                "is_partial": False,
                "is_stale": False,
                "methodology": "test",
                "methodology_version": "1",
            },
        }
    )
    security = SecurityV1(
        security_id=1,
        ticker="MELI",
        name="MercadoLibre",
        cusip=None,
        isin=None,
        type="cs",
        currency="USD",
        is_cash_equivalent=False,
        asset_type="equity",
        sector=None,
        region=None,
        classification_source="test",
        classification_updated_at=None,
    )
    securities = SecuritiesV1Result(meta=meta, securities=[security])
    return (
        V1Fetch[PortfolioSnapshotV1](available=True, endpoint="portfolio-snapshot", data=snapshot),
        V1Fetch[SecuritiesV1Result](available=True, endpoint="securities", data=securities),
    )


class _FakeTrackerClient:
    def __init__(self, base_url: str | None = None, **_kwargs: Any) -> None:
        self.base_url = base_url

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
        return V1Fetch[PortfolioSnapshotV1](
            available=False,
            endpoint="portfolio-snapshot",
            error="tracker unavailable for test",
        )

    def get_securities(self) -> V1Fetch[SecuritiesV1Result]:
        return V1Fetch[SecuritiesV1Result](available=True, endpoint="securities")


class _AvailableTrackerClient:
    def __init__(self, base_url: str | None = None, **_kwargs: Any) -> None:
        self.base_url = base_url

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
        return _tracker_fetches()[0]

    def get_securities(self) -> V1Fetch[SecuritiesV1Result]:
        return _tracker_fetches()[1]


def test_onboard_untracked_requires_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["sync_list_type_from_holdings.py", "--onboard-untracked"])

    with pytest.raises(SystemExit, match="2"):
        cli.main()

    assert "requires --apply" in capsys.readouterr().err


def test_dry_run_prints_reviewed_onboarding_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = Reclassification(untracked_held=[("NEW", "equity", 12_345.0)])

    cli.print_plan(plan, {}, applied=False, onboarded_untracked=False)

    output = capsys.readouterr().out
    assert "--apply --onboard-untracked" in output


def test_onboard_untracked_reuses_governed_tracking_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, ...]] = []

    def record_db_path(path: str | os.PathLike[str]) -> None:
        calls.append(("db", path))

    def record_track(ticker: str, name: str, list_type: str, user_id: str) -> None:
        calls.append(("track", ticker, name, list_type, user_id))

    monkeypatch.setattr(db, "set_db_path", record_db_path)
    monkeypatch.setattr(db, "track_company", record_track)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    db_path = tmp_path / "portfolio.db"
    plan = Reclassification(
        untracked_held=[
            ("NEW", "equity", 12_345.0),
            ("NEXT", "equity", 8_765.0),
        ]
    )

    count = cli.onboard_untracked(plan, db_path=db_path, user_id="owner")

    assert count == 2
    assert os.environ["EARNINGS_SUMMARY_DB_PATH"] == str(db_path.resolve())
    assert calls == [
        ("db", db_path.resolve()),
        ("track", "NEW", "NEW", "portfolio", "owner"),
        ("track", "NEXT", "NEXT", "portfolio", "owner"),
    ]


@pytest.mark.parametrize("extra_args", [["--repo-root", "{project_root}"]])
def test_checkout_default_database_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setattr(cli, "TrackerV1Client", _FakeTrackerClient)
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    args = [value.format(project_root=tmp_path) for value in extra_args]
    monkeypatch.setattr(sys, "argv", ["sync_list_type_from_holdings.py", *args])

    exit_code = cli.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR: refusing the checkout-default database" in captured.err


def test_missing_db_selection_is_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setattr(cli, "TrackerV1Client", _FakeTrackerClient)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py"],
    )

    exit_code = cli.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (
        "ERROR: EARNINGS_SUMMARY_DB_PATH is required when no database path is given" in captured.err
    )


def test_checkout_default_env_path_is_refused_before_symlink_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_data = tmp_path / "state" / "data"
    state_data.mkdir(parents=True)
    (state_data / "portfolio.db").write_bytes(b"")
    try:
        os.symlink(state_data, tmp_path / "data")
    except OSError as exc:
        pytest.skip(f"this platform cannot create symlinks: {exc}")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "data" / "portfolio.db"))
    monkeypatch.setattr(cli, "TrackerV1Client", _FakeTrackerClient)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py"],
    )

    exit_code = cli.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR: refusing the checkout-default database" in captured.err


def test_real_state_path_beyond_symlink_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checkout whose data/ redirects to state still accepts the REAL state
    spelling — the refusal compares the named path, not the resolved one."""
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setattr(cli, "TrackerV1Client", _AvailableTrackerClient)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state_data = tmp_path / "state" / "data"
    state_data.mkdir(parents=True)
    db_path = state_data / "portfolio.db"
    _es_db(db_path)
    _track(db_path, "MELI", "portfolio")
    try:
        os.symlink(state_data, checkout / "data")
    except OSError as exc:
        pytest.skip(f"this platform cannot create symlinks: {exc}")
    monkeypatch.setattr(cli, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py", "--db-path", str(db_path)],
    )

    exit_code = cli.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "=== list_type reconcile" in output
    assert "MELI" in output


def test_unavailable_tracker_exits_3_without_db_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "TrackerV1Client", _FakeTrackerClient)
    db_path = tmp_path / "state" / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    _es_db(db_path)
    _track(db_path, "MELI", "portfolio")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py", "--db-path", str(db_path)],
    )

    exit_code = cli.main()
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "ERROR: tracker holdings unavailable (tracker unavailable for test)" in captured.err
    assert _list_type(db_path, "MELI") == "portfolio"


@pytest.mark.parametrize(
    "payload",
    [
        '{"pinned_portfolio": ["META"]}',
        '{"pinned_portfolio": [{"reason": "kept"}]}',
        '{"pinned_portfolio": [{"ticker": "   "}]}',
    ],
)
def test_malformed_pins_exit_3_without_db_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    monkeypatch.setattr(cli, "TrackerV1Client", _AvailableTrackerClient)
    db_path = tmp_path / "state" / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    (db_path.parent / "portfolio_pins.json").write_text(payload, encoding="utf-8")
    _es_db(db_path)
    _track(db_path, "MELI", "portfolio")
    _track(db_path, "META", "portfolio")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py", "--db-path", str(db_path), "--apply"],
    )

    exit_code = cli.main()
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "ERROR: pins unreadable" in captured.err
    assert "no list_type changes made" in captured.err
    assert _list_type(db_path, "META") == "portfolio"


def test_unreadable_pins_exit_3_without_db_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "TrackerV1Client", _AvailableTrackerClient)
    db_path = tmp_path / "state" / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    pins_path = db_path.parent / "portfolio_pins.json"
    pins_path.mkdir()
    _es_db(db_path)
    _track(db_path, "MELI", "portfolio")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py", "--db-path", str(db_path), "--apply"],
    )

    exit_code = cli.main()
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "ERROR: pins unreadable" in captured.err
    assert "no list_type changes made" in captured.err
    assert _list_type(db_path, "MELI") == "portfolio"


def test_pins_resolve_from_product_state_root_derived_from_db_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "TrackerV1Client", _AvailableTrackerClient)
    state_root = tmp_path / "product-state"
    db_path = state_root / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    (db_path.parent / "portfolio_pins.json").write_text(
        '{"pinned_portfolio": [{"ticker": "META", "reason": "test pin"}]}',
        encoding="utf-8",
    )
    _es_db(db_path)
    _track(db_path, "MELI", "portfolio")
    _track(db_path, "META", "portfolio")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_list_type_from_holdings.py", "--db-path", str(db_path)],
    )

    exit_code = cli.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "kept in portfolio via pin" in output
    assert "META" in output
    assert _list_type(db_path, "META") == "portfolio"


def test_tracker_db_option_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "TrackerV1Client", _FakeTrackerClient)
    db_path = tmp_path / "state" / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_list_type_from_holdings.py",
            "--db-path",
            str(db_path),
            "--tracker-db",
            "unused.db",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        cli.main()
