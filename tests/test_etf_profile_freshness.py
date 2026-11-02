"""Retained stored-profile admission; no ETF acquisition or classifier."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from etf_sources.profile_evidence import admitted_profile_fields, capture_profile_fields
from instrument_store import get_etf_profile, upsert_etf_profile
from models.instruments import EtfProfile


def test_legacy_profile_is_not_backfilled_with_invented_capture_proof(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with closing(sqlite3.connect(migrated_db(tmp_path / "test.db"))) as conn:
        conn.row_factory = sqlite3.Row
        upsert_etf_profile(
            conn,
            EtfProfile(ticker="VDE", sector_label="Energy", profile_fetched_at=datetime.now(UTC)),
        )
        stored = get_etf_profile(conn, "VDE")
        assert stored is not None and not admitted_profile_fields(stored)


def test_capture_admission_bounds_and_value_identity() -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    for capture, expected in (
        (now - timedelta(days=7), True),
        (now - timedelta(days=7, seconds=1), False),
        (now + timedelta(seconds=1), False),
        (now.replace(tzinfo=None), False),
    ):
        profile = capture_profile_fields(
            EtfProfile(ticker="VDE", sector_label="Energy", profile_fetched_at=capture)
        )
        assert bool(admitted_profile_fields(profile, now=now)) is expected
    profile = capture_profile_fields(
        EtfProfile(ticker="VDE", sector_label="Energy", profile_fetched_at=now),
        source_as_of=now.date(),
    )
    assert admitted_profile_fields(profile, now=now)
    assert not admitted_profile_fields(
        profile.model_copy(update={"sector_label": "Utilities"}), now=now
    )
