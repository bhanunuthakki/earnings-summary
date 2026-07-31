"""Tests for execution/cleanup_material_news_alerts.py — the one-time
dismissal of pending material_news alerts that predate the 2026-07-30 v3
signal-quality gate (topical-relevance noise: opinion pieces + earnings
recaps fired by the v2 classifier).

Covers: dry-run inertness, --apply dismissal + child queued_action
cancellation in the same run, idempotency, and that non-pending /
other-trigger-kind alerts are never touched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution import cleanup_material_news_alerts as cleanup

_SCHEMA = """
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    memo_artifact_id INTEGER,
    evidence_json TEXT,
    signature_sha TEXT,
    dismissed_at TEXT,
    approved_at TEXT,
    dismiss_reason TEXT
);
CREATE TABLE queued_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    action_kind TEXT NOT NULL,
    payload_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT,
    applied_at TEXT,
    cancelled_at TEXT
);
"""

_NOW = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _insert_alert(
    path: Path,
    *,
    ticker: str = "LMND",
    status: str = "pending",
    trigger_kind: str = "material_news",
    headline: str = "Is the CFO transition slowing profitability?",
    n_pending_actions: int = 0,
) -> int:
    evidence = {"news_id": 1, "headline": headline, "relevance_score": 0.8}
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, status, evidence_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, trigger_kind, _NOW, status, json.dumps(evidence)),
        )
        alert_id = int(cur.lastrowid or 0)
        for _ in range(n_pending_actions):
            conn.execute(
                "INSERT INTO queued_actions (alert_id, action_kind, payload_json, created_at) "
                "VALUES (?, 'thesis_update', '{}', ?)",
                (alert_id, _NOW),
            )
        conn.commit()
        return alert_id
    finally:
        conn.close()


def _statuses(path: Path) -> dict[int, str]:
    conn = sqlite3.connect(str(path))
    try:
        return {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, status FROM alerts")}
    finally:
        conn.close()


def _action_statuses(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [str(r[0]) for r in conn.execute("SELECT status FROM queued_actions ORDER BY id")]
    finally:
        conn.close()


def test_dry_run_touches_nothing(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    alert_id = _insert_alert(db, n_pending_actions=2)
    assert cleanup.main(["--db-path", str(db)]) == 0
    out = capsys.readouterr().out
    assert "WOULD-DISMISS" in out
    assert _statuses(db) == {alert_id: "pending"}
    assert _action_statuses(db) == ["pending", "pending"]


def test_apply_dismisses_and_cancels_children(db: Path) -> None:
    alert_id = _insert_alert(db, n_pending_actions=2)
    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert _statuses(db) == {alert_id: "dismissed"}
    assert _action_statuses(db) == ["cancelled", "cancelled"]
    conn = sqlite3.connect(str(db))
    try:
        reason, dismissed_at = conn.execute(
            "SELECT dismiss_reason, dismissed_at FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
    finally:
        conn.close()
    assert reason == cleanup.DISMISS_REASON
    assert dismissed_at


def test_apply_is_idempotent(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _ = _insert_alert(db)
    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    _ = capsys.readouterr()
    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert "apply: 0 of 0 pending material_news alerts" in capsys.readouterr().out


def test_other_kinds_and_settled_rows_untouched(db: Path) -> None:
    settled = _insert_alert(db, status="approved")
    other_kind = _insert_alert(db, trigger_kind="earnings_tone", n_pending_actions=1)
    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    statuses = _statuses(db)
    assert statuses[settled] == "approved"
    assert statuses[other_kind] == "pending"
    assert _action_statuses(db) == ["pending"]


def test_missing_db_exits_nonzero(tmp_path: Path) -> None:
    assert cleanup.main(["--db-path", str(tmp_path / "nope.db")]) == 1
