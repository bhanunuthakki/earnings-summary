"""Source-bound recovery must preserve the invalid history and fail atomically."""

# All dates, prices, record IDs, and rationale below are fictional test data.
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from integrations.portfolio_tracker_v1 import PortfolioSnapshotV1, TransactionsV1Result
from synthesis.wix_avdv_postmortem import (
    apply_wix_history_correction,
    prepare_wix_history_correction,
)

NOW = datetime(2035, 6, 20, 20, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "tracker_v1"


def sources() -> tuple[PortfolioSnapshotV1, TransactionsV1Result]:
    snapshot = json.loads((FIXTURES / "portfolio-snapshot.json").read_text())
    snapshot["positions"] = []
    snapshot["meta"].update(as_of="2035-06-20", generated_at="2035-06-20T19:00:00Z")
    page = json.loads((FIXTURES / "transactions.json").read_text())
    page.update(start_date="2035-01-01", end_date="2035-06-20")
    page["meta"].update(as_of="2035-06-20", generated_at="2035-06-20T19:00:00Z")
    trade = page["transactions"][0]
    trade.update(
        transaction_id="fixture-final-sell",
        ticker="WIX",
        type="sell",
        date="2035-05-15",
        quantity="-3",
        amount="141.75",
        price="47.25",
        currency="USD",
    )
    page["transactions"] = [trade]
    return PortfolioSnapshotV1.model_validate(snapshot), TransactionsV1Result.model_validate(page)


@pytest.fixture
def connection(tmp_path: Path, migrated_db: Callable[..., Path]):
    conn = sqlite3.connect(migrated_db(tmp_path / "repair.db"))
    conn.executescript("""
        INSERT INTO position_entries(id,user_id,ticker,source,exit_date,exit_price,
            exit_reason,lessons,outcome_vs_thesis,created_at,updated_at)
        VALUES(4101,'bhanu','WIX','backfill','2035-05-05',52.50,'Invented break','Invented lesson',
            'broke','2035-02-01','2035-05-08');
        INSERT INTO analyst_notes(id,user_id,ticker,kind,status,body,source,position_entry_id,created_at,updated_at)
        VALUES(4201,'bhanu','WIX','observation','open','WIX Exit Postmortem: SYNTHETIC INVALID VERDICT',
            'manual',4101,'2035-05-08','2035-05-08');
        INSERT INTO decisions(id,ticker,recommendation_kind,decided_by,rationale_excerpt,made_at,created_at)
        VALUES(4301,'WIX','sell','owner','Test-only owner choice: rebalance synthetic exposure.',
            '2035-05-05','2035-05-05');
    """)
    yield conn
    conn.close()


def test_repair_uses_fills_and_preserves_original_observation(
    connection: sqlite3.Connection,
) -> None:
    snapshot, page = sources()
    before = list(connection.iterdump())
    plan = prepare_wix_history_correction(
        connection,
        entry_id=4101,
        note_id=4201,
        decision_id=4301,
        snapshot=snapshot,
        transaction_pages=(page,),
        transaction_request_cursors=(None,),
        now=NOW,
    )
    assert list(connection.iterdump()) == before
    assert plan.exit_date == "2035-05-15"
    assert plan.exit_price == Decimal("47.25")
    note_id = apply_wix_history_correction(
        connection, plan, now=NOW, approved_fingerprint=plan.fingerprint()
    )
    assert connection.execute(
        "SELECT exit_date,exit_price,exit_reason,lessons,outcome_vs_thesis FROM position_entries WHERE id=4101"
    ).fetchone() == (
        "2035-05-15",
        47.25,
        "Test-only owner choice: rebalance synthetic exposure.",
        None,
        None,
    )
    assert connection.execute("SELECT body,status FROM analyst_notes WHERE id=4201").fetchone() == (
        "WIX Exit Postmortem: SYNTHETIC INVALID VERDICT",
        "superseded",
    )
    note = connection.execute(
        "SELECT source,supersedes_id,context_json FROM analyst_notes WHERE id=?", (note_id,)
    ).fetchone()
    assert note[:2] == ("advisor", 4201)
    context = json.loads(note[2])
    assert context["plan"]["entry_before_json"] == plan.entry_before_json
    assert context["lesson_state"] == "owner_review_pending"


@pytest.mark.parametrize("failure", ["changed", "unapproved", "forged", "insert_failed"])
def test_repair_rejects_or_rolls_back_every_write(
    connection: sqlite3.Connection, failure: str
) -> None:
    snapshot, page = sources()
    plan = prepare_wix_history_correction(
        connection,
        entry_id=4101,
        note_id=4201,
        decision_id=4301,
        snapshot=snapshot,
        transaction_pages=(page,),
        transaction_request_cursors=(None,),
        now=NOW,
    )
    if failure == "changed":
        connection.execute(
            "UPDATE decisions SET rationale_excerpt='New owner decision' WHERE id=4301"
        )
        connection.commit()
    if failure == "forged":
        plan = plan.model_copy(update={"exit_price": Decimal("999")})
    if failure == "insert_failed":
        connection.execute(
            "CREATE TRIGGER fail_note BEFORE INSERT ON analyst_notes BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END"
        )
    before = list(connection.iterdump())
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        apply_wix_history_correction(
            connection,
            plan,
            now=NOW,
            approved_fingerprint="wrong" if failure == "unapproved" else plan.fingerprint(),
        )
    assert list(connection.iterdump()) == before


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "partial",
        "held",
        "page_missing",
        "duplicate",
        "future",
        "foreign_currency",
        "not_owner",
        "wrong_note",
    ],
)
def test_incomplete_sources_never_prepare_closure(
    connection: sqlite3.Connection, failure: str
) -> None:
    snapshot, page = sources()
    if failure == "stale":
        snapshot = snapshot.model_copy(
            update={"meta": snapshot.meta.model_copy(update={"is_stale": True})}
        )
    if failure == "partial":
        page = page.model_copy(update={"meta": page.meta.model_copy(update={"is_partial": True})})
    if failure == "held":
        original = PortfolioSnapshotV1.model_validate_json(
            (FIXTURES / "portfolio-snapshot.json").read_text()
        )
        snapshot = snapshot.model_copy(
            update={
                "positions": [
                    original.positions[0].model_copy(
                        update={"ticker": "WIX", "quantity": Decimal(1)}
                    )
                ]
            }
        )
    if failure == "page_missing":
        page = page.model_copy(update={"next_cursor": "more"})
    if failure == "duplicate":
        page = page.model_copy(update={"transactions": page.transactions * 2})
    if failure == "future":
        page = page.model_copy(
            update={
                "transactions": [
                    page.transactions[0].model_copy(update={"date": NOW.date().replace(year=2036)})
                ]
            }
        )
    if failure == "foreign_currency":
        page = page.model_copy(
            update={"transactions": [page.transactions[0].model_copy(update={"currency": "EUR"})]}
        )
    if failure == "not_owner":
        connection.execute(
            "UPDATE decisions SET decided_by='advisor',recommendation_kind='avoid' WHERE id=4301"
        )
    if failure == "wrong_note":
        connection.execute("UPDATE analyst_notes SET ticker='OTHER' WHERE id=4201")
    connection.commit()
    before = list(connection.iterdump())
    with pytest.raises(ValueError):
        prepare_wix_history_correction(
            connection,
            entry_id=4101,
            note_id=4201,
            decision_id=4301,
            snapshot=snapshot,
            transaction_pages=(page,),
            transaction_request_cursors=(None,),
            now=NOW,
        )
    assert list(connection.iterdump()) == before


def test_duplicate_record_is_retained_but_excluded_from_active_lifecycle(
    connection: sqlite3.Connection,
) -> None:
    from position_lifecycle import get_entry, list_entries

    connection.execute(
        "INSERT INTO position_entries(id,user_id,ticker,source,entry_date,created_at,updated_at) VALUES(4102,'bhanu','WIX','reconciler','2035-05-08','2035-05-08','2035-05-08')"
    )
    connection.commit()
    snapshot, page = sources()
    before_duplicate = connection.execute(
        "SELECT entry_date,source,exit_date FROM position_entries WHERE id=4102"
    ).fetchone()
    plan = prepare_wix_history_correction(
        connection,
        entry_id=4101,
        note_id=4201,
        decision_id=4301,
        duplicate_entry_id=4102,
        snapshot=snapshot,
        transaction_pages=(page,),
        transaction_request_cursors=(None,),
        now=NOW,
    )
    apply_wix_history_correction(connection, plan, now=NOW, approved_fingerprint=plan.fingerprint())
    assert (
        connection.execute(
            "SELECT entry_date,source,exit_date FROM position_entries WHERE id=4102"
        ).fetchone()
        == before_duplicate
    )
    db_path = connection.execute("PRAGMA database_list").fetchone()[2]
    assert [entry.id for entry in list_entries(db_path=db_path, ticker="WIX")] == [4101]
    retained = get_entry(4102, db_path=db_path)
    assert retained is not None and retained.superseded_by_entry_id == 4101 and not retained.is_open


def test_expired_correction_never_writes(connection: sqlite3.Connection) -> None:
    from datetime import timedelta

    snapshot, page = sources()
    plan = prepare_wix_history_correction(
        connection,
        entry_id=4101,
        note_id=4201,
        decision_id=4301,
        snapshot=snapshot,
        transaction_pages=(page,),
        transaction_request_cursors=(None,),
        now=NOW,
    )
    before = list(connection.iterdump())
    with pytest.raises(ValueError, match="expired"):
        apply_wix_history_correction(
            connection, plan, now=NOW + timedelta(hours=2), approved_fingerprint=plan.fingerprint()
        )
    assert list(connection.iterdump()) == before


def test_cli_plan_and_exact_apply_are_source_bound(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import tzinfo

    from execution import repair_wix_history
    from synthesis import wix_avdv_postmortem

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(repair_wix_history, "datetime", FixedDatetime)
    monkeypatch.setattr(wix_avdv_postmortem, "datetime", FixedDatetime)
    snapshot, page = sources()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        repair_wix_history.TrackerCorrectionEvidence(
            snapshot=snapshot, transaction_pages=(page,), transaction_request_cursors=(None,)
        ).model_dump_json()
    )
    plan_path = tmp_path / "plan.json"
    database = str(connection.execute("PRAGMA database_list").fetchone()[2])
    before = list(connection.iterdump())
    assert (
        repair_wix_history.main(
            [
                "--db",
                database,
                "prepare",
                "--evidence",
                str(evidence),
                "--entry-id",
                "4101",
                "--note-id",
                "4201",
                "--decision-id",
                "4301",
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    assert list(connection.iterdump()) == before
    plan = wix_avdv_postmortem.WixHistoryCorrection.model_validate_json(plan_path.read_text())
    assert (
        repair_wix_history.main(
            [
                "--db",
                database,
                "apply",
                "--plan",
                str(plan_path),
                "--approved-sha256",
                "wrong",
            ]
        )
        == 2
    )
    assert list(connection.iterdump()) == before
    assert (
        repair_wix_history.main(
            [
                "--db",
                database,
                "apply",
                "--plan",
                str(plan_path),
                "--approved-sha256",
                plan.fingerprint(),
            ]
        )
        == 0
    )
    assert connection.execute("SELECT status FROM analyst_notes WHERE id=4201").fetchone() == (
        "superseded",
    )


@pytest.mark.parametrize("tampered", [False, True])
def test_frozen_checkpoint_hash_uses_original_bytes_not_new_schema_defaults(
    connection: sqlite3.Connection, tampered: bool
) -> None:
    import hashlib

    from research.owner_decision_checkpoint import (
        DecisionLeg,
        HoldingBasisPosition,
        HoldingsBasis,
        OwnerDecisionCheckpointPayload,
        SizingIntentSpec,
    )

    payload = OwnerDecisionCheckpointPayload(
        source_channel="fixture",
        source_event_id="synthetic-old-checkpoint",
        holdings_basis=HoldingsBasis(
            source="fixture",
            as_of="2035-05-05T12:00:00Z",
            embedded_positions=(
                HoldingBasisPosition(ticker="WIX", availability="observed", weight_pct=1),
            ),
        ),
        legs=(
            DecisionLeg(
                leg_id="wix",
                ticker="WIX",
                action="sell",
                horizon="not_provided",
                thesis_state="not_the_reason",
                changed_since_prior="Synthetic allocation scenario changed",
                why_now="Synthetic review window",
                conviction="low",
                falsifier="not_provided",
                portfolio_role="software",
                qualitative_stress_implication="not_provided",
                alternative_use_of_capital="not_provided",
            ),
        ),
        sizing_intents=(
            SizingIntentSpec(
                leg_id="wix", ticker="WIX", intent_kind="target_weight_pct", narrative="Exit"
            ),
        ),
    )
    # A historical v1 artifact predates today's optional price_action_bands.
    serialized = payload.model_dump_json(exclude_unset=True)
    assert "price_action_bands" not in serialized
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    if tampered:
        serialized = serialized.replace("Synthetic review window", "Changed rationale")
    connection.execute(
        "INSERT INTO owner_decision_checkpoints(id,user_id,source_channel,source_event_id,checkpoint_schema_version,payload_json,payload_sha256,retrospective,confirmed_at,created_at) VALUES(4401,'bhanu','fixture','synthetic-old-checkpoint','owner-decision-checkpoint/v1',?,?,0,'2035-05-05','2035-05-05')",
        (serialized, digest),
    )
    connection.execute(
        "INSERT INTO owner_decision_checkpoint_decisions(checkpoint_id,decision_id,leg_id,leg_ordinal,recorded_at) VALUES(4401,4301,'wix',0,'2035-05-05')"
    )
    connection.commit()
    snapshot, page = sources()
    if tampered:
        with pytest.raises(ValueError, match="integrity mismatch"):
            prepare_wix_history_correction(
                connection,
                entry_id=4101,
                note_id=4201,
                decision_id=4301,
                snapshot=snapshot,
                transaction_pages=(page,),
                transaction_request_cursors=(None,),
                now=NOW,
            )
    else:
        plan = prepare_wix_history_correction(
            connection,
            entry_id=4101,
            note_id=4201,
            decision_id=4301,
            snapshot=snapshot,
            transaction_pages=(page,),
            transaction_request_cursors=(None,),
            now=NOW,
        )
        assert plan.checkpoint_before_json is not None
        assert json.loads(plan.checkpoint_before_json)["payload_json"] == serialized
