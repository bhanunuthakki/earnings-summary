"""Adversarial admission and retained-reader boundaries for WIX correction."""

# All dates, prices, record IDs, and rationale below are fictional test data.
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from integrations.portfolio_tracker_v1 import PortfolioSnapshotV1, TransactionsV1Result
from journal_links import get_target
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
def connection(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
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


def test_reject_transaction_coverage_missing_held_accounts(connection: sqlite3.Connection) -> None:
    snapshot, page = sources()
    covered = snapshot.meta.account_coverage.included_account_ids
    assert len(covered) >= 2
    page = page.model_copy(
        update={
            "meta": page.meta.model_copy(
                update={
                    "account_coverage": page.meta.account_coverage.model_copy(
                        update={"included_account_ids": [covered[0]]}
                    )
                }
            )
        }
    )
    try:
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
    except ValueError:
        return
    raise AssertionError(
        "Accepted a transaction subset as complete disposal evidence for a wider holdings account set"
    )


def test_reject_disposal_before_bound_owner_decision(connection: sqlite3.Connection) -> None:
    snapshot, page = sources()
    page = page.model_copy(
        update={
            "transactions": [page.transactions[0].model_copy(update={"date": date(2035, 4, 1)})]
        }
    )
    try:
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
    except ValueError:
        return
    raise AssertionError(
        "Bound an earlier disposal to a later synthetic owner sell rationale without chronology validation"
    )


def test_reject_terminal_page_started_from_noninitial_cursor(
    connection: sqlite3.Connection,
) -> None:
    snapshot, page = sources()
    try:
        prepare_wix_history_correction(
            connection,
            entry_id=4101,
            note_id=4201,
            decision_id=4301,
            snapshot=snapshot,
            transaction_pages=(page,),
            transaction_request_cursors=("terminal-page-request",),
            now=NOW,
        )
    except ValueError:
        return
    raise AssertionError("Terminal response alone was admitted as full pagination")


def test_reject_nonmatching_page_chain(connection: sqlite3.Connection) -> None:
    snapshot, page = sources()
    first = page.model_copy(update={"next_cursor": "expected-next", "transactions": []})
    try:
        prepare_wix_history_correction(
            connection,
            entry_id=4101,
            note_id=4201,
            decision_id=4301,
            snapshot=snapshot,
            transaction_pages=(first, page),
            transaction_request_cursors=(None, "wrong-next"),
            now=NOW,
        )
    except ValueError:
        return
    raise AssertionError("Mismatched cursor chain admitted")


def test_retained_duplicate_journal_anchor_is_not_open(connection: sqlite3.Connection) -> None:

    connection.execute(
        "INSERT INTO position_entries(id,user_id,ticker,source,entry_date,created_at,updated_at) VALUES(4102,'bhanu','WIX','reconciler','2035-05-08','2035-05-08','2035-05-08')"
    )
    connection.execute("UPDATE position_entries SET superseded_by_entry_id=4101 WHERE id=4102")
    connection.commit()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    target = get_target(kind="position", target_id=4102, db_path=path)
    assert target is not None and "superseded" in target.label and "open" not in target.label
    assert target.concluded is True and target.conclusion == "superseded by #4101"


def test_later_transfer_out_does_not_become_old_sell_exit(connection: sqlite3.Connection) -> None:
    snapshot, page = sources()
    sale = page.transactions[0]
    transfer = sale.model_copy(
        update={
            "transaction_id": "later-transfer",
            "date": date(2035, 5, 16),
            "type": "transfer",
            "quantity": sale.quantity,
            "price": None,
        }
    )
    page = page.model_copy(update={"transactions": [sale, transfer]})
    try:
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
    except ValueError:
        return
    raise AssertionError(
        "Historical sale was called final disposal despite later position-changing transfer"
    )


def test_apply_rechecks_actual_evidence_age_without_any_write(
    connection: sqlite3.Connection,
) -> None:

    snapshot, page = sources()
    old = NOW - timedelta(hours=23, minutes=59)
    snapshot = snapshot.model_copy(
        update={"meta": snapshot.meta.model_copy(update={"generated_at": old})}
    )
    page = page.model_copy(update={"meta": page.meta.model_copy(update={"generated_at": old})})
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
    try:
        apply_wix_history_correction(
            connection,
            plan,
            approved_fingerprint=plan.fingerprint(),
            now=NOW + timedelta(minutes=59),
        )
    except ValueError:
        assert list(connection.iterdump()) == before
        return
    raise AssertionError("Expired evidence passed because only preparation time was validated")
