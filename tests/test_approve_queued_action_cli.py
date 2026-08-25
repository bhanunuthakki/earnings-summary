"""Tests for execution/approve_queued_action.py — the one-click approve CLI."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alerts import (
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_PENDING,
    ALERT_STATUS_APPROVED,
    ALERT_STATUS_DISMISSED,
    ALERT_STATUS_PENDING,
    fire_alert,
    get_action,
    get_alert,
    list_queued_actions_for_alert,
    queue_action,
)
from user_state.ledger import list_entries
from user_state.notes import list_notes
from user_state.sizing import list_intents

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "approve_cli.db", stamp=PRIOR_HEAD)


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


def _seed_alert(
    db_path: Path,
    ticker: str = "GOOG",
    signature: str = "sig-1",
    trigger_kind: str = "kpi_inflection",
) -> int:
    """Seed one alert; return its id."""
    row = fire_alert(
        ticker=ticker,
        trigger_kind=trigger_kind,
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


def test_material_news_action_cannot_write_future_ledger_entry(cli: Any, db_path: Path) -> None:
    """The writer enforces the same rule used by the historical ledger projection."""
    alert_id = _seed_alert(
        db_path,
        ticker="MSFT",
        signature="sig-material-news",
        trigger_kind="material_news",
    )
    qa = queue_action(
        alert_id=alert_id,
        action_kind="thesis_update",
        payload={"body": "Incorporate development: headline"},
        db_path=db_path,
    )

    with pytest.raises(ValueError, match=r"material_news.*not eligible"):
        cli.approve_and_apply(qa.id, db_path=db_path)

    assert get_action(qa.id, db_path=db_path).status == ACTION_STATUS_PENDING
    assert list_entries(ticker="MSFT", db_path=db_path, include_ineligible=True) == []


@pytest.mark.parametrize("action_kind", ["thesis_update", "bear_append"])
def test_earnings_tone_cannot_write_machine_thesis_or_bear_case(
    cli: Any, db_path: Path, action_kind: str
) -> None:
    alert_id = _seed_alert(
        db_path,
        ticker="NU",
        signature=f"sig-earnings-tone-{action_kind}",
        trigger_kind="earnings_tone",
    )
    qa = queue_action(
        alert_id=alert_id,
        action_kind=action_kind,
        payload={"body": "Machine-generated interpretation"},
        db_path=db_path,
    )

    with pytest.raises(ValueError, match=rf"earnings_tone.*{action_kind}.*not eligible"):
        cli.approve_and_apply(qa.id, db_path=db_path)

    assert get_action(qa.id, db_path=db_path).status == ACTION_STATUS_PENDING
    assert list_entries(ticker="NU", db_path=db_path, include_ineligible=True) == []


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


def test_approve_earnings_prep_append_writes_open_question_not_ledger(
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

    assert list_entries(ticker="NU", db_path=db_path, include_ineligible=True) == []
    notes = list_notes(ticker="NU", kind="question", status="open", db_path=db_path)
    assert len(notes) == 1
    assert notes[0].body == "Ask about credit-loss trajectory"
    assert notes[0].source == "alert"
    assert notes[0].context == {
        "queued_action_id": qa.id,
        "source_alert_id": alert_id,
        "trigger_kind": "kpi_inflection",
        "purpose": "earnings_call_open_question",
    }


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

    # Open question written under the PARENT ALERT's ticker; no decision-ledger row.
    notes = list_notes(ticker="NU", kind="question", status="open", db_path=db_path)
    assert len(notes) == 1
    assert notes[0].body == "Probe risk-adjusted NIM seasonality next call"
    assert notes[0].source_ref == f"queued_action:{qa.id}:earnings_prep"
    retry = cli._write_earnings_question(qa, db_path)
    assert retry.id == notes[0].id
    assert list_entries(ticker="NU", db_path=db_path, include_ineligible=True) == []


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


# ----------------------------------------------------------------------------
# Parent-alert settlement — the inbox "approve does nothing" defect
#
# Settling a queued action used to move ONLY the queued_actions row. The parent
# alert stayed 'pending', and the inbox fetches pending alerts unbounded, so an
# approved card never left the queue — while its new ledger entry rendered as a
# second card. Prod carried four alerts stuck this way (NU alert 1 pending since
# 2026-06-01 with all 17 of its actions already settled).
# ----------------------------------------------------------------------------


def _queue(db_path: Path, alert_id: int, body: str) -> Any:
    return queue_action(
        alert_id=alert_id,
        action_kind="thesis_update",
        payload={"ticker": "GOOG", "body": body},
        db_path=db_path,
    )


def test_approving_last_action_closes_parent_alert(cli: Any, db_path: Path) -> None:
    alert_id = _seed_alert(db_path, signature="sig-settle-one")
    qa = _queue(db_path, alert_id, "only draft")

    summary = cli.approve_and_apply(qa.id, db_path=db_path)

    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_APPROVED
    assert "cleared from the inbox" in summary


def test_approving_one_of_several_leaves_alert_open(cli: Any, db_path: Path) -> None:
    """The drafter's "one action per alert" norm does not hold in prod, so a
    partial approve must NOT close a decision the owner hasn't finished."""
    alert_id = _seed_alert(db_path, signature="sig-settle-partial")
    first = _queue(db_path, alert_id, "draft one")
    _queue(db_path, alert_id, "draft two")

    summary = cli.approve_and_apply(first.id, db_path=db_path)

    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_PENDING
    assert "stays open (1 action(s) still pending)" in summary


def test_cancelling_every_action_dismisses_parent_alert(cli: Any, db_path: Path) -> None:
    alert_id = _seed_alert(db_path, signature="sig-settle-cancel")
    first = _queue(db_path, alert_id, "draft one")
    second = _queue(db_path, alert_id, "draft two")

    cli.dismiss_action(first.id, db_path=db_path)
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_PENDING

    summary = cli.dismiss_action(second.id, db_path=db_path)

    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_DISMISSED
    assert "dismissed and cleared" in summary


def test_settled_status_is_derived_not_assumed(cli: Any, db_path: Path) -> None:
    """An alert with at least one APPLIED action was acted on, so it settles
    'approved' even when the owner waved off the remaining drafts."""
    alert_id = _seed_alert(db_path, signature="sig-settle-mixed")
    first = _queue(db_path, alert_id, "draft one")
    second = _queue(db_path, alert_id, "draft two")

    cli.approve_and_apply(first.id, db_path=db_path)
    cli.dismiss_action(second.id, db_path=db_path)

    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_APPROVED


def test_approve_alert_applies_every_pending_action(cli: Any, db_path: Path) -> None:
    """One click settles the whole card: FCX alert 28 carries 9 actions and NU
    alert 1 carries 17, but the footer only ever surfaced the first."""
    alert_id = _seed_alert(db_path, signature="sig-batch")
    for n in range(3):
        _queue(db_path, alert_id, f"draft {n}")

    summary = cli.approve_alert_and_apply_all(alert_id, db_path=db_path)

    actions = list_queued_actions_for_alert(alert_id, db_path=db_path)
    assert [qa.status for qa in actions] == [ACTION_STATUS_APPLIED] * 3
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_APPROVED
    assert len(list_entries(ticker="GOOG", db_path=db_path)) == 3
    assert "3 action(s) applied" in summary


def test_approve_alert_rejects_already_settled_alert(cli: Any, db_path: Path) -> None:
    alert_id = _seed_alert(db_path, signature="sig-batch-terminal")
    qa = _queue(db_path, alert_id, "only draft")
    cli.approve_and_apply(qa.id, db_path=db_path)

    with pytest.raises(ValueError, match="cannot transition"):
        cli.approve_alert_and_apply_all(alert_id, db_path=db_path)


def test_approve_alert_rejects_alert_with_nothing_pending(cli: Any, db_path: Path) -> None:
    alert_id = _seed_alert(db_path, signature="sig-batch-empty")

    with pytest.raises(ValueError, match="no pending queued actions"):
        cli.approve_alert_and_apply_all(alert_id, db_path=db_path)


def test_dismiss_alert_core_cancels_actions_and_closes_alert(cli: Any, db_path: Path) -> None:
    """The shared core behind both the CLI's --dismiss-alert and the card's
    alert-level dismiss link."""
    alert_id = _seed_alert(db_path, signature="sig-dismiss-core")
    first = _queue(db_path, alert_id, "draft one")
    _queue(db_path, alert_id, "draft two")

    summary = cli.dismiss_alert_and_cancel_actions(alert_id, db_path=db_path)

    actions = list_queued_actions_for_alert(alert_id, db_path=db_path)
    assert [qa.status for qa in actions] == [ACTION_STATUS_CANCELLED] * 2
    assert get_alert(alert_id, db_path=db_path).status == ALERT_STATUS_DISMISSED
    assert get_action(first.id, db_path=db_path).status == ACTION_STATUS_CANCELLED
    assert "Cancelled 2 pending action(s)" in summary
