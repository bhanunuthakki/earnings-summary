"""Tests for execution/cleanup_condition_alerts.py — the one-time dismissal of
pending decision_condition alerts that predate the 2026-07-15 alert-quality
gates (advisor-on-evaluation tripwires + stale-data breaches).

Covers: the relevance gate (advisor + non-portfolio dismissed; owner and
advisor-on-portfolio kept), the freshness gate (stale legacy evidence and
at-or-before-baseline evidence dismissed), decided_by resolution through the
decisions table for legacy evidence, dry-run inertness, --apply idempotency,
and that non-pending / other-kind alerts are never touched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution import cleanup_condition_alerts as cleanup

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
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    decided_by TEXT
);
CREATE TABLE tracked_companies (
    ticker TEXT PRIMARY KEY,
    list_type TEXT NOT NULL,
    archived_at TEXT
);
"""

_NOW = datetime.now(UTC).replace(tzinfo=None)
_FRESH = (_NOW - timedelta(days=30)).date().isoformat()
_STALE = (_NOW - timedelta(days=200)).date().isoformat()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('WIX', 'portfolio')")
    conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('FCX', 'evaluation')")
    conn.commit()
    conn.close()
    return path


def _insert_alert(
    path: Path,
    *,
    ticker: str,
    decision_id: int | None = None,
    period_end: str | None = None,
    decided_by_in_evidence: str | None = None,
    baseline_period_end: str | None = None,
    status: str = "pending",
    trigger_kind: str = "decision_condition",
) -> int:
    evidence: dict[str, object] = {"condition_index": 0}
    if decision_id is not None:
        evidence["decision_id"] = decision_id
    if period_end is not None:
        evidence["period_end"] = period_end
    if decided_by_in_evidence is not None:
        evidence["decided_by"] = decided_by_in_evidence
    if baseline_period_end is not None:
        evidence["baseline_period_end"] = baseline_period_end
    conn = sqlite3.connect(str(path))
    cur = conn.execute(
        "INSERT INTO alerts (ticker, trigger_kind, fired_at, status, evidence_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, trigger_kind, _NOW.isoformat(), status, json.dumps(evidence)),
    )
    conn.commit()
    alert_id = int(cur.lastrowid or 0)
    conn.close()
    return alert_id


def _insert_decision(path: Path, *, ticker: str, decided_by: str | None) -> int:
    conn = sqlite3.connect(str(path))
    cur = conn.execute(
        "INSERT INTO decisions (ticker, decided_by) VALUES (?, ?)", (ticker, decided_by)
    )
    conn.commit()
    decision_id = int(cur.lastrowid or 0)
    conn.close()
    return decision_id


def _alert_row(path: Path, alert_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()
    return row


def test_dry_run_reports_but_changes_nothing(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    decision = _insert_decision(db, ticker="FCX", decided_by="advisor")
    alert = _insert_alert(db, ticker="FCX", decision_id=decision, period_end=_FRESH)

    assert cleanup.main(["--db-path", str(db)]) == 0
    out = capsys.readouterr().out
    assert f"WOULD-DISMISS alert {alert} FCX" in out
    assert "dry-run: 1 dismissed, 0 kept" in out
    assert _alert_row(db, alert)["status"] == "pending"


def test_apply_dismisses_advisor_on_evaluation_and_is_idempotent(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    decision = _insert_decision(db, ticker="FCX", decided_by="advisor")
    # Legacy evidence: no decided_by key — resolved through the decisions table.
    alert = _insert_alert(db, ticker="FCX", decision_id=decision, period_end=_FRESH)

    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    row = _alert_row(db, alert)
    assert row["status"] == "dismissed"
    assert row["dismissed_at"] is not None
    assert row["dismiss_reason"] == cleanup.DISMISS_REASON

    # Idempotent: a second --apply finds nothing pending.
    _ = capsys.readouterr()
    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert "apply: 0 dismissed, 0 kept" in capsys.readouterr().out


def test_owner_and_portfolio_fresh_alerts_are_kept(db: Path) -> None:
    owner_decision = _insert_decision(db, ticker="FCX", decided_by="owner")
    owner_alert = _insert_alert(db, ticker="FCX", decision_id=owner_decision, period_end=_FRESH)
    advisor_decision = _insert_decision(db, ticker="WIX", decided_by="advisor")
    held_alert = _insert_alert(db, ticker="WIX", decision_id=advisor_decision, period_end=_FRESH)

    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert _alert_row(db, owner_alert)["status"] == "pending"
    assert _alert_row(db, held_alert)["status"] == "pending"


def test_stale_breach_dismissed_even_on_portfolio_name(db: Path) -> None:
    """The WIX #22/#23 class: advisor decision on a HELD name, but the
    breaching observation predates the decision by months — stale, dismissed."""
    decision = _insert_decision(db, ticker="WIX", decided_by="advisor")
    stale_alert = _insert_alert(db, ticker="WIX", decision_id=decision, period_end=_STALE)
    owner_decision = _insert_decision(db, ticker="WIX", decided_by="owner")
    owner_stale = _insert_alert(db, ticker="WIX", decision_id=owner_decision, period_end=_STALE)

    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert _alert_row(db, stale_alert)["status"] == "dismissed"
    # Stale is stale regardless of author — the trigger would not re-fire it.
    assert _alert_row(db, owner_stale)["status"] == "dismissed"


def test_baseline_evidence_at_or_before_baseline_dismissed(db: Path) -> None:
    decision = _insert_decision(db, ticker="WIX", decided_by="owner")
    alert = _insert_alert(
        db,
        ticker="WIX",
        decision_id=decision,
        period_end="2026-03-31",
        baseline_period_end="2026-03-31",
    )
    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert _alert_row(db, alert)["status"] == "dismissed"


def test_non_pending_and_other_kinds_untouched(db: Path) -> None:
    decision = _insert_decision(db, ticker="FCX", decided_by="advisor")
    approved = _insert_alert(
        db, ticker="FCX", decision_id=decision, period_end=_STALE, status="approved"
    )
    tone = _insert_alert(db, ticker="FCX", period_end=_STALE, trigger_kind="earnings_tone")

    assert cleanup.main(["--db-path", str(db), "--apply"]) == 0
    assert _alert_row(db, approved)["status"] == "approved"
    assert _alert_row(db, approved)["dismiss_reason"] is None
    assert _alert_row(db, tone)["status"] == "pending"


def test_missing_db_exits_nonzero(tmp_path: Path) -> None:
    assert cleanup.main(["--db-path", str(tmp_path / "nope.db")]) == 1
