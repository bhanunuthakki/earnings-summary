"""Tests for src/alerts/store.py — fire_alert, status transitions, queued_actions FK,
and the deterministic signature hash."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from alerts import store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "alerts.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _fire(
    db_path: Path,
    *,
    ticker: str = "GOOG",
    trigger_kind: str = "kpi_inflection",
    fired_at: datetime | None = None,
    evidence: str = '{"kpi":"cloud_op_margin","value":0.18}',
    signature: str = "sig-default",
    memo_artifact_id: int | None = None,
) -> store.AlertRow:
    """Helper for the common fire_alert call shape."""
    return store.fire_alert(
        ticker=ticker,
        trigger_kind=trigger_kind,
        fired_at=fired_at or datetime.now(UTC),
        evidence_json=evidence,
        signature_sha=signature,
        memo_artifact_id=memo_artifact_id,
        db_path=db_path,
    )


# ----------------------------------------------------------------------------
# Round-trip
# ----------------------------------------------------------------------------


def test_fire_alert_round_trip(db_path: Path) -> None:
    fired = datetime(2026, 5, 27, 14, 30, tzinfo=UTC)
    row = _fire(
        db_path,
        ticker="GOOG",
        trigger_kind="kpi_inflection",
        fired_at=fired,
        evidence='{"kpi":"cloud_op_margin","value":0.18}',
        signature="sig-abc-123",
        memo_artifact_id=99,
    )
    assert row.id > 0
    assert row.user_id == "bhanu"
    assert row.ticker == "GOOG"
    assert row.trigger_kind == "kpi_inflection"
    assert row.fired_at == fired
    assert row.status == store.ALERT_STATUS_PENDING
    assert row.evidence_json == '{"kpi":"cloud_op_margin","value":0.18}'
    assert row.signature_sha == "sig-abc-123"
    assert row.memo_artifact_id == 99
    assert row.approved_at is None
    assert row.dismissed_at is None


def test_queue_action_round_trip_decodes_payload(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-queue-1")
    qa = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "Cloud margin breached", "tags": ["margin", "cloud"]},
        db_path=db_path,
    )
    assert qa.id > 0
    assert qa.alert_id == alert.id
    assert qa.action_kind == "thesis_update"
    assert qa.status == store.ACTION_STATUS_PENDING
    assert qa.payload["body"] == "Cloud margin breached"
    assert qa.payload["tags"] == ["margin", "cloud"]
    assert qa.applied_at is None
    assert qa.cancelled_at is None


# ----------------------------------------------------------------------------
# FK enforcement
# ----------------------------------------------------------------------------


def test_queue_action_fk_enforces_existing_alert(db_path: Path) -> None:
    """The connection enables `PRAGMA foreign_keys = ON` — a queued_action
    with a nonexistent alert_id must raise IntegrityError."""
    with pytest.raises(sqlite3.IntegrityError):
        store.queue_action(
            alert_id=99999,
            action_kind="thesis_update",
            payload={"body": "orphan"},
            db_path=db_path,
        )


# ----------------------------------------------------------------------------
# Status transitions
# ----------------------------------------------------------------------------


def test_approve_alert_transitions_and_stamps(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-approve-1")
    before = datetime.now(UTC)
    approved = store.approve_alert(alert.id, db_path=db_path)
    after = datetime.now(UTC)

    assert approved.id == alert.id
    assert approved.status == store.ALERT_STATUS_APPROVED
    assert approved.approved_at is not None
    assert before - timedelta(seconds=1) <= approved.approved_at <= after + timedelta(seconds=1)
    assert approved.dismissed_at is None


def test_approve_alert_raises_on_already_approved(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-approve-twice")
    store.approve_alert(alert.id, db_path=db_path)
    with pytest.raises(ValueError, match="cannot transition"):
        store.approve_alert(alert.id, db_path=db_path)


def test_approve_alert_raises_on_missing_id(db_path: Path) -> None:
    with pytest.raises(LookupError, match="not found"):
        store.approve_alert(99999, db_path=db_path)


def test_dismiss_alert_transitions_and_stamps(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-dismiss-1")
    dismissed = store.dismiss_alert(alert.id, db_path=db_path)
    assert dismissed.status == store.ALERT_STATUS_DISMISSED
    assert dismissed.dismissed_at is not None
    assert dismissed.approved_at is None


def test_dismiss_alert_raises_on_already_dismissed(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-dismiss-twice")
    store.dismiss_alert(alert.id, db_path=db_path)
    with pytest.raises(ValueError, match="cannot transition"):
        store.dismiss_alert(alert.id, db_path=db_path)


def test_apply_action_transitions_and_stamps(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-apply-1")
    qa = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "x"},
        db_path=db_path,
    )
    applied = store.apply_action(qa.id, db_path=db_path)
    assert applied.status == store.ACTION_STATUS_APPLIED
    assert applied.applied_at is not None
    assert applied.cancelled_at is None


def test_cancel_action_transitions_and_stamps(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-cancel-1")
    qa = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "x"},
        db_path=db_path,
    )
    cancelled = store.cancel_action(qa.id, db_path=db_path)
    assert cancelled.status == store.ACTION_STATUS_CANCELLED
    assert cancelled.cancelled_at is not None
    assert cancelled.applied_at is None


def test_apply_action_raises_after_cancellation(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-double-action")
    qa = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "x"},
        db_path=db_path,
    )
    store.cancel_action(qa.id, db_path=db_path)
    with pytest.raises(ValueError, match="cannot transition"):
        store.apply_action(qa.id, db_path=db_path)


# ----------------------------------------------------------------------------
# find_by_signature / list_pending_alerts / list_pending_actions
# ----------------------------------------------------------------------------


def test_find_by_signature_returns_most_recent_non_expired(db_path: Path) -> None:
    """Two alerts with the same signature, both non-expired — the more recent
    must come back. (Sensors use this for re-fire dedup.)"""
    older = _fire(
        db_path,
        signature="sig-dedup",
        fired_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _fire(
        db_path,
        signature="sig-dedup",
        fired_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    found = store.find_by_signature(signature_sha="sig-dedup", db_path=db_path)
    assert found is not None
    assert found.id == newer.id
    assert found.id != older.id


def test_find_by_signature_skips_expired(db_path: Path) -> None:
    """An alert in 'expired' state must NOT mask a fresh re-fire — the
    signature lookup intentionally excludes it."""
    expired = _fire(db_path, signature="sig-expired")
    # Manually mark it expired (this is what the daily expire-sweep would do;
    # this PR doesn't ship that path, but the dedup query must respect it).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE alerts SET status = ? WHERE id = ?",
            (store.ALERT_STATUS_EXPIRED, expired.id),
        )
        conn.commit()
    finally:
        conn.close()

    assert store.find_by_signature(signature_sha="sig-expired", db_path=db_path) is None


def test_find_by_signature_returns_none_when_no_match(db_path: Path) -> None:
    assert store.find_by_signature(signature_sha="never-fired", db_path=db_path) is None


def test_list_pending_alerts_filters_by_status_and_ticker(db_path: Path) -> None:
    a = _fire(db_path, ticker="GOOG", signature="s1")
    b = _fire(db_path, ticker="META", signature="s2")
    c = _fire(db_path, ticker="GOOG", signature="s3")
    store.approve_alert(b.id, db_path=db_path)  # exclude from pending list

    pending_all = store.list_pending_alerts(db_path=db_path)
    assert {p.id for p in pending_all} == {a.id, c.id}

    pending_goog = store.list_pending_alerts(ticker="GOOG", db_path=db_path)
    assert {p.id for p in pending_goog} == {a.id, c.id}

    pending_meta = store.list_pending_alerts(ticker="META", db_path=db_path)
    assert pending_meta == []


def test_list_pending_alerts_filters_by_since(db_path: Path) -> None:
    old = _fire(db_path, signature="s-old", fired_at=datetime(2026, 1, 1, tzinfo=UTC))
    new = _fire(db_path, signature="s-new", fired_at=datetime(2026, 5, 1, tzinfo=UTC))

    recent = store.list_pending_alerts(
        since=datetime(2026, 3, 1, tzinfo=UTC),
        db_path=db_path,
    )
    assert {r.id for r in recent} == {new.id}
    assert old.id not in {r.id for r in recent}


def test_list_pending_actions_joins_user_id_through_alert(db_path: Path) -> None:
    """list_pending_actions filters by user_id via the alert join. Insert an
    alert under a different user_id by going direct-to-SQL, then verify our
    JOIN excludes its queued actions from the default 'bhanu' query."""
    own_alert = _fire(db_path, signature="own")
    store.queue_action(
        alert_id=own_alert.id,
        action_kind="thesis_update",
        payload={"body": "own"},
        db_path=db_path,
    )

    # Inject a foreign-user alert + queued action via raw SQL — there's no
    # public API for non-default user_id yet.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, "
            "evidence_json, signature_sha) VALUES (?,?,?,?,?,?)",
            (
                "alice",
                "META",
                "kpi_inflection",
                datetime.now(UTC).isoformat(),
                "{}",
                "alice-sig",
            ),
        )
        alice_alert_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO queued_actions (alert_id, action_kind, payload_json, "
            "created_at) VALUES (?,?,?,?)",
            (
                alice_alert_id,
                "thesis_update",
                '{"body":"alice"}',
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    bhanu_pending = store.list_pending_actions(db_path=db_path)
    assert [qa.alert_id for qa in bhanu_pending] == [own_alert.id]


def test_list_queued_actions_for_alert_oldest_first(db_path: Path) -> None:
    alert = _fire(db_path, signature="sig-list-qa")
    first = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"order": 1},
        db_path=db_path,
    )
    second = store.queue_action(
        alert_id=alert.id,
        action_kind="bear_append",
        payload={"order": 2},
        db_path=db_path,
    )
    rows = store.list_queued_actions_for_alert(alert.id, db_path=db_path)
    assert [r.id for r in rows] == [first.id, second.id]


# ----------------------------------------------------------------------------
# compute_signature_sha
# ----------------------------------------------------------------------------


def test_compute_signature_sha_is_deterministic() -> None:
    a = store.compute_signature_sha(
        "kpi_inflection",
        "GOOG",
        {"kpi": "cloud_op_margin", "period": "2026Q1"},
    )
    b = store.compute_signature_sha(
        "kpi_inflection",
        "GOOG",
        {"kpi": "cloud_op_margin", "period": "2026Q1"},
    )
    assert a == b


def test_compute_signature_sha_independent_of_evidence_key_order() -> None:
    """`sort_keys=True` is the contract — two dicts that differ only in
    key-insertion order MUST hash to the same value."""
    a = store.compute_signature_sha(
        "kpi_inflection",
        "GOOG",
        {"kpi": "cloud_op_margin", "period": "2026Q1", "direction": "below"},
    )
    b = store.compute_signature_sha(
        "kpi_inflection",
        "GOOG",
        {"direction": "below", "period": "2026Q1", "kpi": "cloud_op_margin"},
    )
    assert a == b


def test_compute_signature_sha_changes_with_inputs() -> None:
    """Different inputs MUST produce different hashes — no collision between
    trigger_kind, ticker, or evidence variants."""
    base = store.compute_signature_sha("kpi_inflection", "GOOG", {"kpi": "x"})
    different_trigger = store.compute_signature_sha(
        "earnings_tone",
        "GOOG",
        {"kpi": "x"},
    )
    different_ticker = store.compute_signature_sha("kpi_inflection", "META", {"kpi": "x"})
    different_evidence = store.compute_signature_sha(
        "kpi_inflection",
        "GOOG",
        {"kpi": "y"},
    )
    assert len({base, different_trigger, different_ticker, different_evidence}) == 4
