"""Tests for execution/sync_thesis_state.py — the thesis_state mirror audit + backfill.

Covers the drift classifier (one record per kind), the all-rows audit, and the
--apply re-ingest path that repairs a stub row and leaves breach_status alone.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import sync_thesis_state as sync  # noqa: E402


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE thesis_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            thesis TEXT,
            last_updated TIMESTAMP,
            breach_status TEXT,
            raw_json TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        );
        """
    )
    return c


def _seed(
    conn: sqlite3.Connection,
    ticker: str,
    raw_json: str,
    *,
    thesis: str | None = None,
    breach_status: str | None = "ok",
) -> None:
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, breach_status, raw_json, ingested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, thesis, breach_status, raw_json, datetime(2026, 5, 3)),
    )
    conn.commit()


def _write(tmp_path: Path, ticker: str, payload: Mapping[str, object]) -> None:
    (tmp_path / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_classify_clean(tmp_path: Path) -> None:
    payload = {"ticker": "CLN", "thesis": "t", "a": 1}
    _write(tmp_path, "CLN", payload)
    rec = sync.classify_row("CLN", "t", json.dumps(payload), tmp_path)
    assert rec.kind is sync.DriftKind.CLEAN
    assert rec.needs_sync is False


def test_classify_placeholder(tmp_path: Path) -> None:
    _write(tmp_path, "PLC", {"ticker": "PLC", "thesis": "t"})
    rec = sync.classify_row("PLC", "t", "{}", tmp_path)
    assert rec.kind is sync.DriftKind.PLACEHOLDER
    assert rec.needs_sync is True


def test_classify_stub(tmp_path: Path) -> None:
    _write(tmp_path, "NU", {"ticker": "NU", "thesis": "real", "verdict": "Intact"})
    stub = json.dumps({"thesis": "stale", "_status": "stub_regenerated_from_corruption"})
    rec = sync.classify_row("NU", "stale", stub, tmp_path)
    assert rec.kind is sync.DriftKind.STUB
    assert "Intact" in rec.detail
    assert rec.needs_sync is True


def test_classify_thesis_drift(tmp_path: Path) -> None:
    _write(tmp_path, "WIX", {"ticker": "WIX", "thesis": "new thesis"})
    rec = sync.classify_row("WIX", "old thesis", json.dumps({"thesis": "old thesis"}), tmp_path)
    assert rec.kind is sync.DriftKind.THESIS_DRIFT
    assert rec.needs_sync is True


def test_classify_body_drift(tmp_path: Path) -> None:
    _write(tmp_path, "MA", {"ticker": "MA", "thesis": "t", "kpis": ["new"]})
    rec = sync.classify_row("MA", "t", json.dumps({"ticker": "MA", "thesis": "t"}), tmp_path)
    assert rec.kind is sync.DriftKind.BODY_DRIFT
    assert rec.needs_sync is True


def test_classify_no_file(tmp_path: Path) -> None:
    rec = sync.classify_row("GONE", "t", json.dumps({"thesis": "t"}), tmp_path)
    assert rec.kind is sync.DriftKind.NO_FILE
    assert rec.needs_sync is False  # cannot re-ingest a missing file


def test_classify_bad_json(tmp_path: Path) -> None:
    _write(tmp_path, "BAD", {"ticker": "BAD", "thesis": "t"})
    rec = sync.classify_row("BAD", "t", "{not json", tmp_path)
    assert rec.kind is sync.DriftKind.BAD_JSON
    assert rec.needs_sync is True


def test_audit_and_apply_repairs_stub(tmp_path: Path) -> None:
    """End-to-end: audit flags the stub, sync_drifted repairs it, re-audit is CLEAN."""
    conn = _conn()
    real = {
        "ticker": "NU",
        "thesis": "Nu compounds members.",
        "verdict": "Intact",
        "break_rules": [1, 2, 3],
    }
    _write(tmp_path, "NU", real)
    _seed(
        conn,
        "NU",
        json.dumps({"thesis": "stub", "_status": "stub_regenerated_from_corruption"}),
        thesis="stub",
        breach_status="ok",
    )
    # A genuinely clean row should be left alone.
    clean_payload = {"ticker": "MU", "thesis": "t"}
    _write(tmp_path, "MU", clean_payload)
    _seed(conn, "MU", json.dumps(clean_payload), thesis="t")

    records = sync.audit_thesis_state(conn, tmp_path)
    by_ticker = {r.ticker: r for r in records}
    assert by_ticker["NU"].kind is sync.DriftKind.STUB
    assert by_ticker["MU"].kind is sync.DriftKind.CLEAN

    drifted = [r for r in records if r.needs_sync]
    synced = sync.sync_drifted(conn, tmp_path, drifted)
    assert synced == ["NU"]

    post = sync.audit_thesis_state(conn, tmp_path, ticker="NU")
    assert post[0].kind is sync.DriftKind.CLEAN
    row = conn.execute(
        "SELECT raw_json, breach_status FROM thesis_state WHERE ticker='NU'"
    ).fetchone()
    assert json.loads(row["raw_json"]) == real
    assert row["breach_status"] == "ok"  # evaluator-owned column preserved


def test_sync_drifted_is_noop_without_drift(tmp_path: Path) -> None:
    conn = _conn()
    payload = {"ticker": "OK", "thesis": "t"}
    _write(tmp_path, "OK", payload)
    _seed(conn, "OK", json.dumps(payload), thesis="t")
    records = sync.audit_thesis_state(conn, tmp_path)
    assert sync.sync_drifted(conn, tmp_path, records) == []


def test_audit_and_apply_bootstraps_empty_mirror(tmp_path: Path) -> None:
    """Holdings files, not a migration snapshot, bootstrap an empty mirror."""
    conn = _conn()
    alpha = {"ticker": "AAA", "thesis": "Alpha thesis", "verdict": "Intact"}
    beta = {"ticker": "BBB", "thesis": "Beta thesis", "verdict": "Watch"}
    _write(tmp_path, "BBB", beta)
    _write(tmp_path, "AAA", alpha)

    records = sync.audit_thesis_state(conn, tmp_path)

    assert [(r.ticker, r.kind, r.needs_sync) for r in records] == [
        ("AAA", sync.DriftKind.MISSING_ROW, True),
        ("BBB", sync.DriftKind.MISSING_ROW, True),
    ]
    assert sync.sync_drifted(conn, tmp_path, records) == ["AAA", "BBB"]
    assert sync.audit_thesis_state(conn, tmp_path) == [
        sync.DriftRecord("AAA", sync.DriftKind.CLEAN, "mirror matches file"),
        sync.DriftRecord("BBB", sync.DriftKind.CLEAN, "mirror matches file"),
    ]
    rows = conn.execute(
        "SELECT ticker, thesis, raw_json FROM thesis_state ORDER BY ticker"
    ).fetchall()
    assert [(row["ticker"], row["thesis"], json.loads(row["raw_json"])) for row in rows] == [
        ("AAA", "Alpha thesis", alpha),
        ("BBB", "Beta thesis", beta),
    ]


def test_ticker_audit_reports_missing_row_only_when_file_exists(tmp_path: Path) -> None:
    conn = _conn()
    payload = {"ticker": "NU", "thesis": "Nu compounds members."}
    _write(tmp_path, "NU", payload)

    assert sync.audit_thesis_state(conn, tmp_path, ticker="nu") == [
        sync.DriftRecord(
            "NU",
            sync.DriftKind.MISSING_ROW,
            "holdings file exists but thesis_state row is absent",
        )
    ]
    assert sync.audit_thesis_state(conn, tmp_path, ticker="GONE") == []


def test_setup_documents_fresh_install_thesis_bootstrap() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "python execution/sqlite_bootstrap.py execution/sync_thesis_state.py --apply" in readme
