"""Seed-corpus → decisions backfill (the Brier denominator, day one).

Covers the deterministic mapping (action→kind, mid-month made_at, $k size
parse, ETF/Roth detection), verbatim "(inferred)" falsifiers, idempotency,
and the LEAP intent landing pre-marked resolved-rejected (corpus freshness)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from synthesis.seed_decisions import backfill_seed_decisions
from user_state import notes

_SEED = {
    "decisions": [
        {
            "ticker": "MU",
            "action": "buy",
            "approx_date": "2025-07",
            "conviction": "high",
            "rationale": "Inside view that memory prices were exploding.",
            "falsifier": "Memory pricing cycle rolls over. (inferred)",
        },
        {
            "ticker": "WIX",
            "action": "trim",
            "approx_date": "2026-04-10",
            "conviction": "low",
            "rationale": "Held in the Roth IRA; cut ~$26k as Base44 margin drag persisted.",
            "falsifier": "n/a",
        },
        {
            "ticker": "FLKR",
            "action": "buy",
            "approx_date": "2026-02",
            "conviction": "medium",
            "rationale": "South Korea value-up basket.",
            "falsifier": "Value-up program stalls.",
        },
        {"ticker": "XLV", "action": "watch", "approx_date": "2026-01"},
    ]
}


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[[Path], Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db")


@pytest.fixture
def seed_path(tmp_path: Path) -> Path:
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_SEED), encoding="utf-8")
    return p


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM decisions WHERE decided_by='owner' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_backfill_maps_the_owner_shape(db_path: Path, seed_path: Path) -> None:
    tally = backfill_seed_decisions(db_path, seed_path)
    assert tally == {"inserted": 3, "skipped_existing": 0, "skipped_unmapped": 1, "intent": 1}

    mu, wix, flkr = _rows(db_path)
    assert (mu["recommendation_kind"], mu["conviction"]) == ("initiate", "high")
    assert mu["made_at"].startswith("2025-07-15")  # mid-month convention
    assert mu["falsifier"] == "Memory pricing cycle rolls over. (inferred)"  # verbatim
    assert mu["instrument"] == "equity" and mu["scope"] == "ticker"
    assert mu["user_notes"].startswith("seed:decision:1 ")
    assert mu["outcome_label"] is None  # grading is the standing grader's job

    assert (wix["recommendation_kind"], wix["account"]) == ("trim", "roth")
    assert wix["size_usd"] == 26000.0
    assert wix["made_at"].startswith("2026-04-10")

    assert flkr["instrument"] == "etf"


def test_backfill_is_idempotent(db_path: Path, seed_path: Path) -> None:
    backfill_seed_decisions(db_path, seed_path)
    again = backfill_seed_decisions(db_path, seed_path)
    assert again == {"inserted": 0, "skipped_existing": 3, "skipped_unmapped": 1, "intent": 0}
    assert len(_rows(db_path)) == 3


def test_leap_intent_lands_resolved_rejected(db_path: Path, seed_path: Path) -> None:
    backfill_seed_decisions(db_path, seed_path)
    rows = notes.list_notes(kind="intent", db_path=db_path, limit=10, status=None)
    assert len(rows) == 1
    intent = rows[0]
    assert intent.source_ref == "seed:intent:leap-sleeve"
    assert intent.status == "resolved"
    ctx = intent.context or {}
    assert ctx["status"] == "resolved-rejected"
    assert str(ctx["closed_by"]).startswith("claude_session:")
