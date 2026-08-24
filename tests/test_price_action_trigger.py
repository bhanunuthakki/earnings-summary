"""BHA-93 deterministic price-action classification and atomic state tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from triggers.price_action import (
    PriceAction,
    PriceActionLadderSnapshot,
    PriceActionRung,
    PriceActionTrigger,
    PriceObservation,
    Side,
    classify_rung,
    rearm_safe,
)
from triggers.registry import ALL_TRIGGERS, ENABLED_TRIGGERS


def _rung(action: PriceAction, side: Side, level: float, approach: float | None) -> PriceActionRung:
    return PriceActionRung(
        rung_id=action,
        action=action,
        side=side,
        level=level,
        approach_level=approach,
    )


def test_favorable_add_and_high_side_thresholds_are_exact() -> None:
    add = _rung("add", "at_or_below", 90.0, 95.0)
    trim = _rung("trim", "at_or_above", 120.0, 115.0)
    assert classify_rung(add, 90.0) == "breached"
    assert classify_rung(add, 95.0) == "approaching"
    assert classify_rung(trim, 120.0) == "breached"
    assert classify_rung(trim, 115.0) == "approaching"
    assert rearm_safe(add, 95.1)
    assert rearm_safe(trim, 114.9)


def test_price_action_is_registered_but_dark_by_default() -> None:
    assert "price_action" not in {item.kind for item in ENABLED_TRIGGERS}
    assert "price_action" in {item.kind for item in ALL_TRIGGERS}


def _snapshot(price: float) -> PriceActionLadderSnapshot:
    observed = datetime(2026, 8, 23, 9, 0, 0)
    return PriceActionLadderSnapshot(
        ladder_id="checkpoint:1:price-action-bands",
        revision_sha256="a" * 64,
        checkpoint_id=1,
        checkpoint_payload_sha256="b" * 64,
        ticker="ACME",
        currency="USD",
        rungs=(_rung("trim", "at_or_above", 120.0, 115.0),),
        observation=PriceObservation(price, "USD", observed, "dcf_runs:1", "c" * 64),
    )


def test_approach_then_breach_expires_old_alert_and_dedupes(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "price-action.db", target="head")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        trigger = PriceActionTrigger()
        now = datetime(2026, 8, 23, 10, 0, 0)
        first = trigger.advance(
            conn, snapshot=_snapshot(115.0), user_id="bhanu", now=now, dry_run=False
        )
        assert first.alerts_fired == 1
        assert (
            trigger.advance(
                conn, snapshot=_snapshot(115.0), user_id="bhanu", now=now, dry_run=False
            ).dedup_skips
            == 1
        )
        second = trigger.advance(
            conn, snapshot=_snapshot(120.0), user_id="bhanu", now=now, dry_run=False
        )
        assert second.alerts_fired == 1
        alerts = conn.execute(
            "SELECT status FROM alerts WHERE trigger_kind='price_action' ORDER BY id"
        ).fetchall()
        assert [str(row["status"]) for row in alerts] == ["expired", "pending"]
        assert conn.execute("SELECT count(*) FROM queued_actions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM price_action_sensor_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_rearm_increments_generation_before_new_breach(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "price-action-rearm.db", target="head")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        trigger = PriceActionTrigger()
        now = datetime(2026, 8, 23, 10, 0, 0)
        trigger.advance(conn, snapshot=_snapshot(120.0), user_id="bhanu", now=now, dry_run=False)
        trigger.advance(conn, snapshot=_snapshot(114.0), user_id="bhanu", now=now, dry_run=False)
        again = trigger.advance(
            conn, snapshot=_snapshot(120.0), user_id="bhanu", now=now, dry_run=False
        )
        assert again.alerts_fired == 1
        row = conn.execute("SELECT generation FROM price_action_sensor_state").fetchone()
        assert row is not None and row["generation"] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM alerts WHERE trigger_kind='price_action'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()
