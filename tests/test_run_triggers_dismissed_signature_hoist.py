"""Run-scope hoist of the 90-day dismissed-signature lookback (audit F5).

``_load_dismissed_signatures`` takes no ticker argument: the dismissed-set
lookback is identical for every (ticker × trigger) pair, yet the driver used
to rebuild it — on a fresh read-only connection — once per pair that
produced candidates (``_compose_user_state`` inside the serial lane). The
driver now loads it ONCE per run and threads the same set into every pair's
user-state snapshot.

These tests pin the two contracts that must hold after the hoist:

* the lookback query (and its read-only connection) executes exactly once
  per run, no matter how many (ticker × trigger) pairs produce candidates;
* delivery semantics are unchanged — every pair's ``UserStateContext``
  still carries the exact same dismissed set (fresh dismissals in, stale
  dismissals beyond the 90-day lookback out).

The witness triggers are serial-lane probe sensors (they deliberately lack
the ``ScanSweepTrigger`` ``concurrent_scan`` marker) so candidate pairs form
without any LLM boundary at all — no network, no LLM, fixture schema only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from execution import run_triggers
from triggers.base import TriggerCandidate, UserStateContext

_DISMISSED_FRESH_SHA = "d" * 64
_DISMISSED_STALE_SHA = "s" * 64
# The sentinel the counting fake loader returns: proving the ONE run-scope
# load is the set every (ticker x trigger) pair received.
_SENTINEL_SHA = "e" * 64


# ---------------------------------------------------------------------------
# Schema fixture — the minimal table subset the driver + lookback touch.
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the driver-facing table subset (mirrors the production columns
    the code under test reads; defaults/triggers dropped where not
    load-bearing)."""
    _ = conn.execute(
        "CREATE TABLE alerts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', "
        + "ticker TEXT NOT NULL, "
        + "trigger_kind TEXT NOT NULL, "
        + "fired_at TEXT NOT NULL, "
        + "status TEXT NOT NULL DEFAULT 'pending', "
        + "memo_artifact_id INTEGER, "
        + "evidence_json TEXT NOT NULL, "
        + "signature_sha TEXT NOT NULL, "
        + "dismissed_at TEXT, "
        + "approved_at TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE queued_actions ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "alert_id INTEGER NOT NULL REFERENCES alerts(id), "
        + "action_kind TEXT NOT NULL, "
        + "payload_json TEXT NOT NULL, "
        + "status TEXT NOT NULL DEFAULT 'pending', "
        + "created_at TEXT NOT NULL, "
        + "applied_at TEXT, "
        + "cancelled_at TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE llm_calls ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "called_at TEXT NOT NULL, "
        + "purpose TEXT, "
        + "ticker TEXT, "
        + "model TEXT, "
        + "prompt_sha256 TEXT, "
        + "prompt_chars INTEGER, "
        + "elapsed_ms INTEGER, "
        + "cost_estimate_usd REAL, "
        + "error TEXT)"
    )
    _ = conn.execute(
        "CREATE TABLE user_kpi_registry ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', "
        + "ticker TEXT NOT NULL, "
        + "kpi_name TEXT NOT NULL, "
        + "threshold_direction TEXT, "
        + "threshold_value REAL, "
        + "is_thesis_breaker INTEGER NOT NULL DEFAULT 0, "
        + "scaffold_source TEXT, "
        + "notes TEXT, "
        + "created_at TEXT NOT NULL, "
        + "updated_at TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE TABLE position_sizing_intent ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "user_id TEXT NOT NULL DEFAULT 'bhanu', "
        + "ticker TEXT NOT NULL, "
        + "intent_kind TEXT NOT NULL, "
        + "intent_value REAL, "
        + "narrative TEXT, "
        + "created_at TEXT NOT NULL, "
        + "updated_at TEXT NOT NULL)"
    )
    _ = conn.execute("PRAGMA journal_mode=WAL")


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build the fresh fixture DB and route every ``DB_PATH`` consumer to it."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


def _seed_dismissed_alert(db_path: Path, *, signature_sha: str, dismissed_at: datetime) -> None:
    """Insert one dismissed alert row for the default user."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, status, "
            "evidence_json, signature_sha, dismissed_at) "
            "VALUES ('bhanu', 'AAA', 'earnings_tone', ?, 'dismissed', '{}', ?, ?)",
            (dismissed_at.isoformat(), signature_sha, dismissed_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Witness triggers — serial-lane probes that record the delivered set.
# ---------------------------------------------------------------------------

_WITNESS_OBSERVATIONS: list[tuple[str, str, frozenset[str]]] = []


class _WitnessTrigger:
    """Minimal serial-lane trigger: one candidate per (ticker) pair.

    ``should_fire`` records the dismissed set the driver delivered for that
    pair and returns False, so the run stops before ``build_alert`` — no LLM
    boundary is needed. Deliberately NOT a ``ScanSweepTrigger`` (no
    ``concurrent_scan`` marker), so the lane partition keeps it serial.
    """

    kind: ClassVar[str] = "witness_base"

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]:
        return [
            TriggerCandidate(
                ticker=ticker,
                kind=self.kind,
                key="witness",
                evidence={},
                computed_at=datetime.now(UTC),
            )
        ]

    def should_fire(self, candidate: TriggerCandidate, user_state: UserStateContext) -> bool:
        _WITNESS_OBSERVATIONS.append(
            (
                self.kind,
                candidate.ticker,
                frozenset(user_state.recent_dismissed_signatures),
            )
        )
        return False


class _WitnessOne(_WitnessTrigger):
    kind = "witness_one"


class _WitnessTwo(_WitnessTrigger):
    kind = "witness_two"


# ---------------------------------------------------------------------------
# The hoist: one query per run
# ---------------------------------------------------------------------------


def test_dismissed_signature_query_runs_once_per_run(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Six candidate pairs (3 tickers x 2 triggers) execute the ticker-
    independent lookback exactly once — the structural F5 hoist proof.

    Pre-hoist the driver rebuilt the identical set per pair (six lookback
    queries and six read-only connections for this fixture). The counting
    fake replaces the loader — the only site that runs the 90-day ``alerts``
    lookback — so calls counted are exactly lookback queries executed, and
    the sentinel set proves the one run-scope load reaches every pair.
    """
    calls = {"count": 0}

    def counting_loader(db_path_arg: Path, *, user_id: str, lookback_days: int) -> set[str]:
        calls["count"] += 1
        return {_SENTINEL_SHA}

    monkeypatch.setattr(run_triggers, "_load_dismissed_signatures", counting_loader)
    monkeypatch.setattr(run_triggers, "ENABLED_TRIGGERS", [_WitnessOne, _WitnessTwo])
    _WITNESS_OBSERVATIONS.clear()

    exit_code = run_triggers.main(
        ["--tickers", "AAA,BBB,CCC", "--db-path", str(db_path), "--max-cost-usd", "10"]
    )

    assert exit_code == 0
    # Six (ticker x trigger) pairs produced candidates (the witness records
    # one observation per pair), so a per-pair recompute would be six calls.
    assert len(_WITNESS_OBSERVATIONS) == 6
    assert calls["count"] == 1
    # The single run-scope load is the set every pair received.
    for kind, ticker, delivered in _WITNESS_OBSERVATIONS:
        assert delivered == {_SENTINEL_SHA}, (kind, ticker, delivered)

    summary = json_summary(capsys)
    assert summary["tickers_processed"] == 3
    assert summary["should_fire_skips"] == 6
    assert summary["alerts_fired"] == 0
    assert summary["errors"] == 0


def test_hoisted_set_delivery_matches_the_lookback_semantics(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real hoisted lookback still delivers its semantics to every pair:
    the fresh dismissal is in, the 91-day-old one is out."""
    naive_now = datetime.now(UTC).replace(tzinfo=None)
    _seed_dismissed_alert(db_path, signature_sha=_DISMISSED_FRESH_SHA, dismissed_at=naive_now)
    _seed_dismissed_alert(
        db_path,
        signature_sha=_DISMISSED_STALE_SHA,
        dismissed_at=naive_now - timedelta(days=91),
    )

    monkeypatch.setattr(run_triggers, "ENABLED_TRIGGERS", [_WitnessOne, _WitnessTwo])
    _WITNESS_OBSERVATIONS.clear()

    exit_code = run_triggers.main(
        ["--tickers", "AAA,BBB", "--db-path", str(db_path), "--max-cost-usd", "10"]
    )

    assert exit_code == 0
    assert len(_WITNESS_OBSERVATIONS) == 4
    for kind, ticker, delivered in _WITNESS_OBSERVATIONS:
        assert delivered == {_DISMISSED_FRESH_SHA}, (kind, ticker, delivered)


def json_summary(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    """Parse the driver's stdout run summary."""
    return dict(json.loads(capsys.readouterr().out))
