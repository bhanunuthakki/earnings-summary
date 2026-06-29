"""Tests for execution/sync_thesis_state.py — the thesis_state mirror audit + backfill.

Covers the drift classifier (one record per kind), the all-rows audit, and the
--apply re-ingest path that repairs a stub row and leaves breach_status alone.
"""

from __future__ import annotations

import json
import sqlite3
import sys
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


def _write(tmp_path: Path, ticker: str, payload: dict[str, object]) -> None:
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
