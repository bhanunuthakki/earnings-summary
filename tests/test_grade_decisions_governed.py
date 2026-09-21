"""Synthetic price-only grading must fail closed and preserve its input evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import db
from execution import grade_decisions
from sources.adapters import CorporateActionAdjustment, FmpProviderAdapter
from sources.readers import DualReadShadowingVerifier


def test_adjusted_price_request_never_relabels_unadjusted_close() -> None:
    packet = json.dumps([{"date": "2035-01-02", "close": 10, "volume": 0}]).encode()
    profile = b'[{"symbol":"SYNTH","currency":"USD"}]'
    adapter = FmpProviderAdapter()
    with pytest.raises(ValueError):
        adapter.parse_prices(packet, "SYNTH", currency_packet=profile)
    raw = adapter.parse_prices(
        packet,
        "SYNTH",
        currency_packet=profile,
        adjustment_method=CorporateActionAdjustment.UNADJUSTED,
    )
    assert raw.points[0].close == 10


@pytest.fixture
def grading_input(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    folder = repo / "data" / "historical" / "fmp"
    folder.mkdir(parents=True)
    database = migrated_db(tmp_path / "isolated.db")
    today = datetime.now(UTC).date()
    made = today - timedelta(days=200)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute(
            "INSERT INTO decisions(id,ticker,recommendation_kind,made_at,created_at,decided_by) VALUES(1,'SYNTH','add',?,?,'owner')",
            (made.isoformat(), made.isoformat()),
        )
    records = [
        {"date": today.isoformat(), "adjClose": 150, "volume": 0},
        {"date": made.isoformat(), "adjClose": 100, "volume": 0},
    ]
    (folder / "SYNTH_price_chart_10y_div_adj.json").write_text(json.dumps(records))
    (folder / "SYNTH_profile.json").write_text('[{"symbol":"SYNTH","currency":"USD"}]')
    return repo, database


def test_grader_requires_configured_existing_database_without_global_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "empty-repo"
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    before = (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT)
    assert grade_decisions.main(["--repo-root", str(repo)]) == 2
    assert not (repo / "data" / "portfolio.db").exists()
    assert before == (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT)
    assert (
        grade_decisions.main(["--repo-root", str(repo), "--db", str(tmp_path / "missing.db")]) == 2
    )
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_profile",
        "wrong_currency_symbol",
        "wrong_price_symbol",
        "stale_current",
        "stale_reference",
        "unadjusted",
        "duplicate",
        "future",
        "sister_symbol",
    ],
)
def test_unavailable_prices_do_not_grade_or_calibrate(
    grading_input: tuple[Path, Path], failure: str
) -> None:
    repo, database = grading_input
    folder = repo / "data" / "historical" / "fmp"
    price = folder / "SYNTH_price_chart_10y_div_adj.json"
    profile = folder / "SYNTH_profile.json"
    records = json.loads(price.read_text())
    today = datetime.now(UTC).date()
    if failure == "missing_profile":
        profile.unlink()
    elif failure == "wrong_currency_symbol":
        profile.write_text('[{"symbol":"OTHER","currency":"EUR"}]')
    elif failure == "wrong_price_symbol":
        price.write_text(json.dumps({"symbol": "OTHER", "historical": records}))
    elif failure == "stale_current":
        records[0]["date"] = (today - timedelta(days=8)).isoformat()
    elif failure == "stale_reference":
        records[1]["date"] = (today - timedelta(days=208)).isoformat()
    elif failure == "unadjusted":
        for row in records:
            row["close"] = row.pop("adjClose")
    elif failure == "duplicate":
        records.append(records[0])
    elif failure == "future":
        records[0]["date"] = (today + timedelta(days=1)).isoformat()
    elif failure == "sister_symbol":
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("UPDATE decisions SET ticker='GOOG'")
        price.rename(folder / "GOOGL_price_chart_10y_div_adj.json")
        profile.rename(folder / "GOOGL_profile.json")
    if failure not in {"wrong_price_symbol", "sister_symbol"}:
        price.write_text(json.dumps(records))
    with closing(sqlite3.connect(database)) as conn, conn:
        before = list(conn.iterdump())
    assert grade_decisions.main(["--repo-root", str(repo), "--db", str(database)]) == 0
    with closing(sqlite3.connect(database)) as conn, conn:
        assert list(conn.iterdump()) == before


def test_same_instrument_grades_with_bound_price_manifest_and_parity(
    grading_input: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, database = grading_input
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    before = (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT)
    parity = DualReadShadowingVerifier(repo).verify_price_parity("SYNTH")
    assert parity.parity_passed is True and parity.legacy_record_count == 2
    assert grade_decisions.main(["--repo-root", str(repo)]) == 0
    assert before == (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT)
    with closing(sqlite3.connect(database)) as conn, conn:
        label, pct, notes = conn.execute(
            "SELECT outcome_label,outcome_pct,outcome_notes FROM decisions WHERE id=1"
        ).fetchone()
    assert label == "correct" and pct == pytest.approx(0.5)
    evidence = json.loads(notes.split("\nprice_evidence=", 1)[1])
    assert evidence["ticker"] == "SYNTH"
    assert evidence["adjustment_method"] == "split_and_dividend"
    assert evidence["currency_binding"]["currency"] == "USD"
    folder = repo / "data" / "historical" / "fmp"
    assert (
        evidence["source_payload_hash"]
        == hashlib.sha256((folder / "SYNTH_price_chart_10y_div_adj.json").read_bytes()).hexdigest()
    )
    assert (
        evidence["currency_binding"]["source_payload_hash"]
        == hashlib.sha256((folder / "SYNTH_profile.json").read_bytes()).hexdigest()
    )
    assert evidence["reference"]["close"] == "100" and evidence["outcome"]["close"] == "150"
    assert evidence["reference"]["as_of_date"] != evidence["outcome"]["as_of_date"]
    assert evidence["capture_freshness"] == "unverified"


def test_existing_checkout_database_remains_prohibited(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    checkout_db = repo / "data" / "portfolio.db"
    checkout_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(checkout_db)) as conn, conn:
        conn.execute("CREATE TABLE decisions(id INTEGER)")
    before = checkout_db.read_bytes()
    assert grade_decisions.main(["--repo-root", str(repo), "--db", str(checkout_db)]) == 2
    assert checkout_db.read_bytes() == before
