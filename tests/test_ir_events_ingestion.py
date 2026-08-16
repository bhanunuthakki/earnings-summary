"""Unit tests for Forward IR Events Ingestion & Batch Persistence (BHA-15)."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from signals.ir_events import (
    IREventObservation,
    generate_event_id,
    generate_revision_id,
    record_ir_events_batch,
)


def test_event_id_and_revision_id_are_deterministic() -> None:
    eid1 = generate_event_id("NVDA", "investor_day", "https://investor.nvidia.com/events/day2026")
    eid2 = generate_event_id("NVDA", "investor_day", "https://investor.nvidia.com/events/day2026")
    assert eid1 == eid2
    assert eid1.startswith("ir-event:v1:")

    rev1 = generate_revision_id(
        eid1, {"date": "2026-11-15", "title": "Nvidia Financial Analyst Day"}, "obs-1"
    )
    rev2 = generate_revision_id(
        eid1, {"date": "2026-11-15", "title": "Nvidia Financial Analyst Day"}, "obs-1"
    )
    assert rev1 == rev2
    assert rev1.startswith("ir-rev:v1:")


def test_record_ir_events_batch_lifecycle_and_reconciliation(
    tmp_path: Path, migrated_db: Any
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    conn = sqlite3.connect(db_path)

    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    cal_date = date(2026, 8, 15)

    eid = generate_event_id("NOW", "analyst_day", "https://ir.servicenow.com/events/2026")
    rev1 = generate_revision_id(
        eid, {"date": "2026-10-20", "title": "Financial Analyst Day 2026"}, "obs-100"
    )

    obs1 = IREventObservation(
        event_id=eid,
        revision_id=rev1,
        issuer_id="NOW",
        ticker="NOW",
        event_kind="analyst_day",
        status="scheduled",
        title="ServiceNow Financial Analyst Day 2026",
        event_date=date(2026, 10, 20),
        source_tier="publisher_event_authority",
        source_url="https://ir.servicenow.com/events/2026",
        source_observation_id="obs-100",
        raw_sha256="abc123sha",
        observed_at=now,
    )

    # 1. First insert
    res1 = record_ir_events_batch(conn, [obs1], mode="apply", now=now, calendar_date=cal_date)
    assert res1.inserted == 1
    assert res1.replayed == 0
    assert res1.dispositions[0].disposition == "inserted"

    cur = conn.execute("SELECT ticker, title, event_date FROM signals WHERE ticker = 'NOW'")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "NOW"
    assert rows[0][2] == "2026-10-20"

    # 2. Idempotent replay
    res2 = record_ir_events_batch(conn, [obs1], mode="apply", now=now, calendar_date=cal_date)
    assert res2.inserted == 0
    assert res2.replayed == 1
    assert res2.dispositions[0].disposition == "replayed"

    # 3. Rescheduled event
    rev2 = generate_revision_id(
        eid, {"date": "2026-11-05", "title": "ServiceNow Analyst Day (Rescheduled)"}, "obs-101"
    )
    obs2 = IREventObservation(
        event_id=eid,
        revision_id=rev2,
        issuer_id="NOW",
        ticker="NOW",
        event_kind="analyst_day",
        status="rescheduled",
        title="ServiceNow Financial Analyst Day (Rescheduled)",
        event_date=date(2026, 11, 5),
        source_tier="publisher_event_authority",
        source_url="https://ir.servicenow.com/events/2026",
        source_observation_id="obs-101",
        raw_sha256="abc123sha2",
        observed_at=now,
    )

    res3 = record_ir_events_batch(conn, [obs2], mode="apply", now=now, calendar_date=cal_date)
    assert res3.superseded == 1
    assert res3.dispositions[0].disposition == "superseded"

    cur = conn.execute("SELECT ticker, title, event_date FROM signals WHERE ticker = 'NOW'")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][2] == "2026-11-05"

    # 4. Cancellation event
    obs_cancelled = IREventObservation(
        event_id=eid,
        revision_id=rev2,
        issuer_id="NOW",
        ticker="NOW",
        event_kind="analyst_day",
        status="cancelled",
        title="Cancelled Event",
        event_date=date(2026, 11, 5),
        source_tier="publisher_event_authority",
        source_url="https://ir.servicenow.com/events/2026",
        source_observation_id="obs-102",
        raw_sha256="abc123sha3",
        observed_at=now,
    )

    res4 = record_ir_events_batch(
        conn, [obs_cancelled], mode="apply", now=now, calendar_date=cal_date
    )
    assert res4.cancelled == 1

    cur = conn.execute("SELECT id FROM signals WHERE ticker = 'NOW'")
    assert cur.fetchall() == []

    conn.close()


def test_admission_rules_reject_past_and_excessive_dates(tmp_path: Path, migrated_db: Any) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    conn = sqlite3.connect(db_path)

    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    cal_date = date(2026, 8, 15)

    eid_past = generate_event_id("NVDA", "investor_day", "url1")
    obs_past = IREventObservation(
        event_id=eid_past,
        revision_id="rev-p",
        issuer_id="NVDA",
        ticker="NVDA",
        event_kind="investor_day",
        status="scheduled",
        title="Past Day",
        event_date=date(2026, 8, 10),  # before 2026-08-15
        source_tier="publisher_event_authority",
        source_url="url1",
        source_observation_id="obs-p",
        raw_sha256="hash-p",
        observed_at=now,
    )

    eid_far = generate_event_id("NVDA", "investor_day", "url2")
    obs_far = IREventObservation(
        event_id=eid_far,
        revision_id="rev-f",
        issuer_id="NVDA",
        ticker="NVDA",
        event_kind="investor_day",
        status="scheduled",
        title="Far Future Day",
        event_date=cal_date + timedelta(days=600),  # beyond 548d
        source_tier="publisher_event_authority",
        source_url="url2",
        source_observation_id="obs-f",
        raw_sha256="hash-f",
        observed_at=now,
    )

    res = record_ir_events_batch(
        conn, [obs_past, obs_far], mode="apply", now=now, calendar_date=cal_date
    )
    assert res.rejected == 2
    assert res.inserted == 0

    conn.close()


def test_dry_run_mode_does_not_mutate_db(tmp_path: Path, migrated_db: Any) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    conn = sqlite3.connect(db_path)

    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    cal_date = date(2026, 8, 15)

    eid = generate_event_id("META", "investor_day", "url_meta")
    obs = IREventObservation(
        event_id=eid,
        revision_id="rev-m",
        issuer_id="META",
        ticker="META",
        event_kind="investor_day",
        status="scheduled",
        title="Meta Strategy Day 2026",
        event_date=date(2026, 9, 25),
        source_tier="issuer_ir_announcement",
        source_url="url_meta",
        source_observation_id="obs-m",
        raw_sha256="hash-m",
        observed_at=now,
    )

    res = record_ir_events_batch(conn, [obs], mode="dry_run", now=now, calendar_date=cal_date)
    assert res.inserted == 1  # would be inserted
    assert res.mode == "dry_run"

    # Database must still be empty
    cur = conn.execute("SELECT id FROM signals WHERE ticker = 'META'")
    assert cur.fetchall() == []

    conn.close()


def test_ingest_ir_events_cli_runs_cleanly(tmp_path: Path, migrated_db: Any) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    cmd = [
        "python",
        "execution/sqlite_bootstrap.py",
        "execution/ingest_ir_events.py",
        "--db",
        str(db_path),
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert proc.returncode == 0
    assert '"schema_version": "ir-events-run.v1"' in proc.stdout
