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
    # fired_at round-trips to naive-UTC: the store strips any offset on read
    # (and triggers write naive to begin with). See store._parse_dt / _now_iso.
    assert row.fired_at == fired.replace(tzinfo=None)
    assert row.fired_at.tzinfo is None
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


def test_store_stamps_round_trip_as_naive_utc(db_path: Path) -> None:
    """Every store-written timestamp (``_now_iso``) round-trips tz-NAIVE.

    ``created_at`` / ``applied_at`` / ``cancelled_at`` on queued_actions and
    ``approved_at`` / ``dismissed_at`` on alerts must all come back with
    ``.tzinfo is None`` — matching ``fired_at`` (written naive by triggers)
    and the repo-wide naive-UTC convention. A tz-aware stamp is the landmine
    that crashes any consumer comparing it against a naive datetime, so this
    guards against ``_now_iso`` regressing to an aware offset.
    """
    # created_at — stamped on insert.
    alert = _fire(db_path, signature="sig-naive-stamps")
    qa = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "x"},
        db_path=db_path,
    )
    assert qa.created_at.tzinfo is None

    # applied_at — stamped on the action transition (the alert stays pending).
    applied = store.apply_action(qa.id, db_path=db_path)
    assert applied.created_at.tzinfo is None
    assert applied.applied_at is not None
    assert applied.applied_at.tzinfo is None

    # approved_at — stamped on the alert transition.
    approved = store.approve_alert(alert.id, db_path=db_path)
    assert approved.approved_at is not None
    assert approved.approved_at.tzinfo is None

    # cancelled_at / dismissed_at — a second alert+action reaches the other
    # terminal states (apply/cancel and approve/dismiss are mutually exclusive).
    alert2 = _fire(db_path, signature="sig-naive-stamps-2")
    qa2 = store.queue_action(
        alert_id=alert2.id,
        action_kind="thesis_update",
        payload={"body": "y"},
        db_path=db_path,
    )
    cancelled = store.cancel_action(qa2.id, db_path=db_path)
    assert cancelled.cancelled_at is not None
    assert cancelled.cancelled_at.tzinfo is None

    dismissed = store.dismiss_alert(alert2.id, db_path=db_path)
    assert dismissed.dismissed_at is not None
    assert dismissed.dismissed_at.tzinfo is None


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
    # Naive-UTC bounds: the store stamps ``approved_at`` naive (see
    # store._now_iso), so the window bounds must be naive too or the
    # comparison would raise on naive-vs-aware.
    before = datetime.now(UTC).replace(tzinfo=None)
    approved = store.approve_alert(alert.id, db_path=db_path)
    after = datetime.now(UTC).replace(tzinfo=None)

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


def test_uncancel_action_restores_pending(db_path: Path) -> None:
    """Wave 3b: the inbox's optimistic-dismiss Undo flips a cancelled action
    back to pending and clears cancelled_at; uncancelling an action that isn't
    cancelled is a transition conflict (nothing to reverse)."""
    alert = _fire(db_path, signature="sig-uncancel-1")
    qa = store.queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"body": "x"},
        db_path=db_path,
    )
    store.cancel_action(qa.id, db_path=db_path)
    restored = store.uncancel_action(qa.id, db_path=db_path)
    assert restored.status == store.ACTION_STATUS_PENDING
    assert restored.cancelled_at is None
    # Pending again — a second uncancel has nothing to reverse.
    with pytest.raises((ValueError, KeyError, LookupError)):
        store.uncancel_action(qa.id, db_path=db_path)


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


def test_active_signature_is_unique(db_path: Path) -> None:
    """At most ONE active (non-expired) alert may exist per signature — the DB
    enforces it via the alembic 0068 partial unique index, upgrading the
    sensor-side dedup from an advisory SELECT into a real idempotency guarantee
    that survives a race between the SELECT and the INSERT. A second active fire
    with the same signature is rejected outright; ``find_by_signature`` returns
    the surviving active alert. (A re-fire becomes possible again only once the
    prior alert expires — see ``test_find_by_signature_skips_expired``.)"""
    first = _fire(db_path, signature="sig-dedup", fired_at=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(sqlite3.IntegrityError):
        _fire(db_path, signature="sig-dedup", fired_at=datetime(2026, 5, 1, tzinfo=UTC))
    found = store.find_by_signature(signature_sha="sig-dedup", db_path=db_path)
    assert found is not None
    assert found.id == first.id


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
    # public API for non-default user_id yet. Register 'alice' as a tenant first:
    # alerts.user_id is a FK to tenants.id (alembic 0073), so a second tenant must
    # exist before its rows can land.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO tenants (id, created_at) VALUES (?, ?)",
            ("alice", datetime.now(UTC).isoformat()),
        )
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


def test_list_queued_actions_for_alerts_batches(db_path: Path) -> None:
    """The batched IN-query returns the SAME per-alert lists the per-alert
    function does, in one round trip — the inbox N+1 fix."""
    a1 = _fire(db_path, signature="sig-batch-1")
    a2 = _fire(db_path, signature="sig-batch-2")
    a3 = _fire(db_path, signature="sig-batch-3")  # no actions
    a1_first = store.queue_action(
        alert_id=a1.id, action_kind="thesis_update", payload={"o": 1}, db_path=db_path
    )
    a1_second = store.queue_action(
        alert_id=a1.id, action_kind="bear_append", payload={"o": 2}, db_path=db_path
    )
    a2_only = store.queue_action(
        alert_id=a2.id, action_kind="sizing_update", payload={"o": 3}, db_path=db_path
    )

    batched = store.list_queued_actions_for_alerts([a1.id, a2.id, a3.id], db_path=db_path)

    # Every requested id is present (a3 maps to an empty list — index without a guard).
    assert set(batched) == {a1.id, a2.id, a3.id}
    assert [r.id for r in batched[a1.id]] == [a1_first.id, a1_second.id]  # oldest-first
    assert [r.id for r in batched[a2.id]] == [a2_only.id]
    assert batched[a3.id] == []
    # Parity with the per-alert function.
    for aid in (a1.id, a2.id, a3.id):
        assert [r.id for r in batched[aid]] == [
            r.id for r in store.list_queued_actions_for_alert(aid, db_path=db_path)
        ]


def test_list_queued_actions_for_alerts_empty_no_db_touch() -> None:
    """Empty input short-circuits — no db_path needed, no connection opened."""
    assert store.list_queued_actions_for_alerts([], db_path=None) == {}


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


# ----------------------------------------------------------------------------
# Legacy aware rows normalize to naive on read
# ----------------------------------------------------------------------------


def test_legacy_aware_rows_normalize_to_naive(db_path: Path) -> None:
    """Rows carrying an aware ``+00:00`` offset — legacy rows written before
    the naive-UTC convention was enforced (the pre-#222 ``_now_iso`` stamped
    every ``queued_actions.created_at`` aware; 17 such rows sit in prod) —
    must be folded to naive-UTC on read, so the store never hands back a mix
    of aware (legacy) and naive (new) datetimes. ``_now_iso`` going naive
    only fixes *new* writes; this is the read-side complement.

    Inserts an aware-offset alert + queued action via raw SQL (bypassing the
    store's writers) and asserts both read back naive at the same instant.
    """
    aware = datetime(2026, 6, 1, 17, 14, 2, tzinfo=UTC)  # stored as +00:00
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, "
            "evidence_json, signature_sha) VALUES (?,?,?,?,?,?)",
            ("bhanu", "NU", "earnings_tone", aware.isoformat(), "{}", "sig-legacy-aware"),
        )
        alert_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO queued_actions (alert_id, action_kind, payload_json, "
            "created_at) VALUES (?,?,?,?)",
            (alert_id, "thesis_update", '{"body":"x"}', aware.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    row = store.get_alert(alert_id, db_path=db_path)
    assert row.fired_at.tzinfo is None
    assert row.fired_at == aware.replace(tzinfo=None)

    qa = store.list_queued_actions_for_alert(alert_id, db_path=db_path)[0]
    assert qa.created_at.tzinfo is None
    assert qa.created_at == aware.replace(tzinfo=None)


# ----------------------------------------------------------------------------
# Write-path enum validation (clear error before the DB CHECK; pre-0068-safe)
# ----------------------------------------------------------------------------


def test_fire_alert_rejects_unknown_trigger_kind(db_path: Path) -> None:
    """A trigger_kind outside the canonical four fails loud at the write path —
    before the INSERT — so the caller gets a clear ValueError rather than an
    opaque IntegrityError, and the guard holds even on a DB predating 0068."""
    with pytest.raises(ValueError, match="unknown trigger_kind"):
        _fire(db_path, trigger_kind="bogus_kind")


def test_queue_action_rejects_unknown_action_kind(db_path: Path) -> None:
    """An action_kind outside the canonical four is rejected before the INSERT."""
    alert = _fire(db_path, signature="sig-qa-validate")
    with pytest.raises(ValueError, match="unknown action_kind"):
        store.queue_action(
            alert_id=alert.id, action_kind="bogus", payload={"body": "x"}, db_path=db_path
        )
