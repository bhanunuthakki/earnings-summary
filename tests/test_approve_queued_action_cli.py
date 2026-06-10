"""Tests for execution/approve_queued_action.py — the one-click approve CLI."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config

from alembic import command
from alerts import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    ALERT_STATUS_DISMISSED,
    fire_alert,
    get_action,
    get_alert,
    list_queued_actions_for_alert,
    queue_action,
)
from user_state.ledger import list_entries
from user_state.sizing import list_intents

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "approve_cli.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _load_cli() -> Any:
    src = PROJECT_ROOT / "execution" / "approve_queued_action.py"
    spec = importlib.util.spec_from_file_location("approve_queued_action", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approve_queued_action"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli() -> Any:
    return _load_cli()


def _seed_alert(db_path: Path, ticker: str = "GOOG", signature: str = "sig-1") -> int:
    """Seed one alert; return its id."""
    row = fire_alert(
        ticker=ticker,
        trigger_kind="kpi_inflection",
        fired_at=datetime.now(UTC),
        evidence_json='{"summary": "test"}',
        signature_sha=signature,
        db_path=db_path,
    )
    return row.id


# ----------------------------------------------------------------------------
# Approve thesis_update
# ----------------------------------------------------------------------------


def test_approve_thesis_update_writes_ledger_entry(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_id = _seed_alert(db_path, ticker="GOOG", signature="sig-thesis")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": "Cloud margin reset to 18%"},
        db_path=db_path,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    # The queued action is now 'applied'
    refreshed = get_action(qa.id, db_path=db_path)
    assert refreshed.status == ACTION_STATUS_APPLIED

    # The ledger has a matching entry
    entries = list_entries(ticker="GOOG", db_path=db_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.entry_kind == "thesis_update"
    assert entry.body == "Cloud margin reset to 18%"
    assert entry.source_alert_id == alert_id


def test_approve_bear_append_writes_ledger_entry(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_id = _seed_alert(db_path, ticker="META", signature="sig-bear")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="bear_append",
        payload={"ticker": "META", "body": "RL guidance cut worsens TAM risk"},
        db_path=db_path,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    entries = list_entries(ticker="META", db_path=db_path)
    assert len(entries) == 1
    assert entries[0].entry_kind == "bear_append"


def test_approve_earnings_prep_append_writes_ledger_entry(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_id = _seed_alert(db_path, ticker="NU", signature="sig-ep")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="earnings_prep_append",
        payload={"ticker": "NU", "body": "Ask about credit-loss trajectory"},
        db_path=db_path,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    entries = list_entries(ticker="NU", db_path=db_path)
    assert len(entries) == 1
    assert entries[0].entry_kind == "earnings_prep_append"


# ----------------------------------------------------------------------------
# Approve sizing_update → position_sizing_intent (not ledger)
# ----------------------------------------------------------------------------


def test_approve_sizing_update_writes_intent_row(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_id = _seed_alert(db_path, ticker="GOOG", signature="sig-sizing")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="sizing_update",
        payload={
            "ticker": "GOOG",
            "intent_kind": "target_pct",
            "intent_value": 0.04,
            "narrative": "trim cloud-margin uncertainty",
        },
        db_path=db_path,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    # The queued action is applied
    refreshed = get_action(qa.id, db_path=db_path)
    assert refreshed.status == ACTION_STATUS_APPLIED

    # A sizing intent row was written instead of a ledger entry
    intents = list_intents(ticker="GOOG", db_path=db_path)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.intent_kind == "target_pct"
    assert intent.intent_value == 0.04
    assert intent.narrative == "trim cloud-margin uncertainty"

    # The ledger should be empty for this ticker (sizing doesn't go there)
    entries = list_entries(ticker="GOOG", db_path=db_path)
    assert entries == []


# ----------------------------------------------------------------------------
# Regression: payload without `ticker` (the real trigger-drafted shape)
# ----------------------------------------------------------------------------


def test_approve_ledger_action_without_payload_ticker_uses_alert_ticker(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trigger-drafted payloads carry body + source_shift_topic but NOT
    `ticker` (it's an alert-level property). Approving must succeed and take
    the ticker from the parent alert. Before the fix this raised KeyError, so
    every queued action was un-approvable (pending forever, 0 ledger writes)."""
    alert_id = _seed_alert(db_path, ticker="NU", signature="sig-no-ticker")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="earnings_prep_append",
        payload={
            "body": "Probe risk-adjusted NIM seasonality next call",
            "source_shift_topic": "Risk-adjusted NIM 100bps QoQ contraction",
        },
        db_path=db_path,
    )
    assert "ticker" not in qa.payload  # guard: the real production shape
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    refreshed = get_action(qa.id, db_path=db_path)
    assert refreshed.status == ACTION_STATUS_APPLIED

    # Ledger entry written under the PARENT ALERT's ticker
    entries = list_entries(ticker="NU", db_path=db_path)
    assert len(entries) == 1
    assert entries[0].entry_kind == "earnings_prep_append"
    assert entries[0].body == "Probe risk-adjusted NIM seasonality next call"
    assert entries[0].source_alert_id == alert_id


def test_approve_sizing_update_without_payload_ticker_uses_alert_ticker(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fix on the sizing path: ticker comes from the parent alert."""
    alert_id = _seed_alert(db_path, ticker="MELI", signature="sig-sizing-no-ticker")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="sizing_update",
        payload={"intent_kind": "max_pct", "intent_value": 0.06},
        db_path=db_path,
    )
    assert "ticker" not in qa.payload
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    intents = list_intents(ticker="MELI", db_path=db_path)
    assert len(intents) == 1
    assert intents[0].intent_kind == "max_pct"
    assert intents[0].intent_value == 0.06


# ----------------------------------------------------------------------------
# Dismiss single action
# ----------------------------------------------------------------------------


def test_dismiss_action_cancels_without_ledger_write(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_id = _seed_alert(db_path, ticker="GOOG", signature="sig-dismiss-1")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": "won't make it to the ledger"},
        db_path=db_path,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--dismiss",
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    refreshed = get_action(qa.id, db_path=db_path)
    assert refreshed.status == ACTION_STATUS_CANCELLED

    # No ledger entry
    entries = list_entries(ticker="GOOG", db_path=db_path)
    assert entries == []


# ----------------------------------------------------------------------------
# --dismiss-alert
# ----------------------------------------------------------------------------


def test_dismiss_alert_cancels_all_pending_actions(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_id = _seed_alert(db_path, ticker="GOOG", signature="sig-dismiss-alert")
    qa_1 = queue_action(
        alert_id=alert_id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": "draft 1"},
        db_path=db_path,
    )
    qa_2 = queue_action(
        alert_id=alert_id,
        action_kind="bear_append",
        payload={"ticker": "GOOG", "body": "draft 2"},
        db_path=db_path,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--alert-id",
            str(alert_id),
            "--dismiss-alert",
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    # The parent alert is dismissed
    refreshed_alert = get_alert(alert_id, db_path=db_path)
    assert refreshed_alert.status == ALERT_STATUS_DISMISSED

    # Both queued actions are cancelled
    actions = list_queued_actions_for_alert(alert_id, db_path=db_path)
    statuses = {qa.id: qa.status for qa in actions}
    assert statuses[qa_1.id] == ACTION_STATUS_CANCELLED
    assert statuses[qa_2.id] == ACTION_STATUS_CANCELLED

    # No ledger entries written
    entries = list_entries(ticker="GOOG", db_path=db_path)
    assert entries == []


# ----------------------------------------------------------------------------
# Already-applied conflict
# ----------------------------------------------------------------------------


def test_approving_already_applied_action_exits_1(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alert_id = _seed_alert(db_path, ticker="GOOG", signature="sig-conflict")
    qa = queue_action(
        alert_id=alert_id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": "first"},
        db_path=db_path,
    )
    # First approve — should succeed
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            str(qa.id),
            "--db-path",
            str(db_path),
        ],
    )
    assert cli.main() == 0

    # Second approve — should fail loudly with rc=1
    rc2 = cli.main()
    assert rc2 == 1
    captured = capsys.readouterr()
    # The error message lands on stderr (the CLI's _err writer)
    assert "approve_queued_action: error" in captured.err
    assert "cannot transition" in captured.err

    # And the conflict must NOT have appended a duplicate ledger row — the
    # status pre-check fires before the downstream dispatch.
    assert len(list_entries(ticker="GOOG", db_path=db_path)) == 1


def test_approving_missing_action_id_exits_1(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--action-id",
            "999999",
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 1
    captured = capsys.readouterr()
    assert "approve_queued_action: error" in captured.err


# ----------------------------------------------------------------------------
# Argument validation
# ----------------------------------------------------------------------------


def test_dismiss_alert_without_alert_id_exits_1(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--dismiss-alert",
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 1
    captured = capsys.readouterr()
    assert "requires --alert-id" in captured.err


def test_no_action_id_and_no_alert_id_exits_1(
    cli: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "approve_queued_action",
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 1
    captured = capsys.readouterr()
    assert "Pass exactly one" in captured.err
