from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from integrations.portfolio_tracker_v1 import AccountsV1Result, TransactionV1, V1Meta
from research.owner_reconciliation import (
    ExecutionLeg,
    ReconciliationRequest,
    ReconciliationResult,
    apply_reconciliation,
)
from research.owner_reconciliation import (
    evaluate_request as evaluate_with_accounts,
)
from user_state.notes import create_note_once


def _meta(**updates: object) -> V1Meta:
    data: dict[str, object] = {
        "schema_version": "1.0.0",
        "generated_at": "2026-01-03T00:00:00Z",
        "as_of": "2026-01-02",
        "currency": "USD",
        "source_providers": ["example"],
        "account_coverage": {
            "included_account_ids": [1],
            "excluded_account_ids": [],
            "lagging_account_ids": [],
        },
        "last_successful_sync_at": "2026-01-03T00:00:00Z",
        "is_partial": False,
        "is_stale": False,
        "warnings": [],
    }
    data.update(updates)
    return V1Meta.model_validate(data)


def _accounts(sync: str | None = "2026-01-03T00:00:00Z") -> AccountsV1Result:
    return AccountsV1Result.model_validate(
        {
            "meta": _meta().model_dump(mode="json"),
            "accounts": [
                {
                    "account_id": 1,
                    "canonical_account_id": 1,
                    "provider": "example",
                    "institution": None,
                    "name": "Example",
                    "official_name": None,
                    "type": "investment",
                    "subtype": None,
                    "mask": None,
                    "active": True,
                    "included_in_totals": True,
                    "exclusion_reason": None,
                    "tax_treatment": "unknown",
                    "tax_treatment_evidence": None,
                    "tax_treatment_confidence": "low",
                    "value": None,
                    "value_currency": "USD",
                    "holdings_as_of": None,
                    "last_successful_sync_at": sync,
                }
            ],
        }
    )


def evaluate_request(
    request: ReconciliationRequest, transactions: list[TransactionV1], meta: V1Meta
) -> ReconciliationResult:
    return evaluate_with_accounts(request, transactions, meta, _accounts())


def _transaction(**updates: object) -> TransactionV1:
    data: dict[str, object] = {
        "transaction_id": "fill-a",
        "account_id": 1,
        "account_name": "Example",
        "security_id": 1,
        "ticker": "EXA",
        "date": "2026-01-02",
        "name": "Example",
        "quantity": "2",
        "amount": "-20",
        "price": "10",
        "fees": None,
        "type": "buy",
        "subtype": None,
        "currency": "USD",
        "override_classification": None,
        "effective_classification": None,
    }
    data.update(updates)
    return TransactionV1.model_validate(data)


def _request(decision_id: int = 1) -> ReconciliationRequest:
    return ReconciliationRequest(
        version=1,
        event_id="example-event",
        start_date=date(2026, 1, 1),
        decision_date=date(2026, 1, 2),
        legs=[
            ExecutionLeg(
                decision_id=decision_id,
                source_event_id="example:EXA",
                ticker="EXA",
                direction="buy",
                currency="USD",
            )
        ],
        funding_tickers=["EXB"],
    )


def test_exact_matching_pending_ambiguous_and_fee_values() -> None:
    request = _request()
    assert evaluate_request(request, [], _meta()).legs[0].disposition == "pending"
    tx = _transaction()
    result = evaluate_request(request, [tx], _meta())
    assert result.complete and result.funding_status == "unresolved"
    assert result.legs[0].transaction is not None
    assert result.legs[0].transaction.fees is None
    zero = evaluate_request(request, [_transaction(fees="0")], _meta())
    assert zero.legs[0].transaction is not None and str(zero.legs[0].transaction.fees) == "0"
    assert (
        evaluate_request(request, [tx, _transaction(transaction_id="fill-b")], _meta())
        .legs[0]
        .disposition
        == "ambiguous"
    )
    assert evaluate_request(request, [tx, tx], _meta()).complete
    with pytest.raises(ValueError, match="conflicting transaction"):
        evaluate_request(request, [tx, _transaction(amount="-21")], _meta())


@pytest.mark.parametrize(
    "meta",
    [
        {"is_stale": True},
        {"is_partial": True},
        {"last_successful_sync_at": None},
        {
            "account_coverage": {
                "included_account_ids": [1],
                "excluded_account_ids": [1],
                "lagging_account_ids": [],
            }
        },
    ],
)
def test_invalid_coverage_fails_closed(meta: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        evaluate_request(_request(), [_transaction()], _meta(**meta))
    with pytest.raises(ValueError):
        evaluate_request(_request(), [_transaction(account_id=2)], _meta())


def test_old_sync_does_not_prove_current_transaction_evidence() -> None:
    with pytest.raises(ValueError, match="sync"):
        evaluate_request(
            _request(), [_transaction()], _meta(last_successful_sync_at="2025-12-31T12:00:00Z")
        )


@pytest.mark.parametrize("sync", [None, "2025-12-31T12:00:00Z", "2026-01-03T01:00:00Z"])
def test_account_sync_requires_same_day_and_not_future(sync: str | None) -> None:
    with pytest.raises(ValueError, match="sync"):
        evaluate_with_accounts(_request(), [_transaction()], _meta(), _accounts(sync))


def test_account_sync_requires_each_included_account_once() -> None:
    accounts = _accounts()
    for rows in ([], accounts.accounts * 2):
        with pytest.raises(ValueError, match="coverage"):
            evaluate_with_accounts(
                _request(),
                [_transaction()],
                _meta(),
                accounts.model_copy(update={"accounts": rows}),
            )


@pytest.mark.parametrize("field", ["active", "included_in_totals"])
def test_included_account_must_be_active_and_in_totals(field: str) -> None:
    accounts = _accounts()
    row = accounts.accounts[0].model_copy(update={field: False})
    with pytest.raises(ValueError, match="coverage"):
        evaluate_with_accounts(
            _request(), [_transaction()], _meta(), accounts.model_copy(update={"accounts": [row]})
        )


def test_accounts_snapshot_must_match_query_day() -> None:
    accounts = _accounts()
    old_meta = accounts.meta.model_copy(update={"as_of": date(2026, 1, 1)})
    with pytest.raises(ValueError, match="sync"):
        evaluate_with_accounts(
            _request(), [_transaction()], _meta(), accounts.model_copy(update={"meta": old_meta})
        )


def test_naive_tracker_sync_is_utc_and_original_is_retained() -> None:
    meta = _meta(last_successful_sync_at="2026-01-02T12:00:00")
    accounts = _accounts("2026-01-02T12:00:00").model_copy(update={"meta": meta})
    result = evaluate_with_accounts(_request(), [_transaction()], meta, accounts)
    assert result.accounts.accounts[0].last_successful_sync_at is not None
    assert result.accounts.accounts[0].last_successful_sync_at.tzinfo is None
    stale = _accounts("2026-01-02T06:00:00").model_copy(update={"meta": meta})
    with pytest.raises(ValueError, match="sync"):
        evaluate_with_accounts(_request(), [_transaction()], meta, stale)


def test_apply_preserves_notes_and_is_idempotent(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "test.db")
    metadata = {
        "execution_verification": "owner_reported_not_broker_reconciled",
        "source_event_id": "example:EXA",
        "owner_rationale": "Keep",
        "source_note_id": 99,
    }
    with sqlite3.connect(db) as conn:
        cursor = conn.execute(
            "INSERT INTO decisions (ticker,recommendation_kind,decided_by,made_at,created_at,user_notes,size_usd) VALUES (?,?,?,?,?,?,?)",
            (
                "EXA",
                "add",
                "owner",
                "2026-01-02T12:00:00",
                "2026-01-02T12:00:00",
                json.dumps(metadata),
                25,
            ),
        )
        decision_id = int(cursor.lastrowid or 0)
    note = create_note_once(
        ticker="EXA",
        kind="observation",
        body="Original rationale",
        source="manual",
        source_ref="example",
        context={"decision_ids": [decision_id], "trigger": "retain", "funding_tickers": ["EXB"]},
        db_path=db,
    )
    request = _request(decision_id)
    result = evaluate_request(request, [_transaction()], _meta())
    assert apply_reconciliation(request, result, db_path=db) == 1
    assert apply_reconciliation(request, result, db_path=db) == 0
    for transactions in ([], [_transaction(), _transaction(transaction_id="fill-b")]):
        with pytest.raises(ValueError, match="execution evidence conflict"):
            apply_reconciliation(
                request, evaluate_request(request, transactions, _meta()), db_path=db
            )
    with sqlite3.connect(db) as conn:
        notes, amount, action = conn.execute(
            "SELECT user_notes,size_usd,user_action_kind FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        old = conn.execute(
            "SELECT body,status FROM analyst_notes WHERE id=?", (note.id,)
        ).fetchone()
        current = conn.execute(
            "SELECT body,context_json FROM analyst_notes WHERE supersedes_id=?", (note.id,)
        ).fetchone()
    stored = json.loads(notes)
    assert stored["owner_rationale"] == "Keep" and stored["source_note_id"] == 99
    assert (
        stored["broker_execution"]["amount"] == "-20" and stored["broker_execution"]["fees"] is None
    )
    assert amount == 25 and action is None
    assert old == ("Original rationale", "superseded")
    assert "Original rationale" in current[0] and "Funding remains unverified" in current[0]
    assert json.loads(current[1])["trigger"] == "retain"


def test_grouped_notes_and_binding_conflict_roll_back(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "groups.db")
    metadata = json.dumps(
        {
            "execution_verification": "owner_reported_not_broker_reconciled",
            "source_event_id": "example:EXA",
        }
    )
    ids: list[int] = []
    with sqlite3.connect(db) as conn:
        for ticker in ("EXA", "EXB"):
            row = conn.execute(
                "INSERT INTO decisions (ticker,recommendation_kind,decided_by,made_at,created_at,user_notes) VALUES (?,?,?,?,?,?)",
                (
                    ticker,
                    "add",
                    "owner",
                    "2026-01-02T12:00:00",
                    "2026-01-02T12:00:00",
                    metadata.replace("example:EXA", f"example:{ticker}"),
                ),
            )
            ids.append(int(row.lastrowid or 0))
    notes = [
        create_note_once(
            ticker=ticker,
            kind="observation",
            body=ticker,
            source="manual",
            source_ref=f"group-{ticker}",
            context={"decision_ids": [did]},
            db_path=db,
        )
        for ticker, did in zip(("EXA", "EXB"), ids, strict=True)
    ]
    request = _request(ids[0]).model_copy(
        update={
            "legs": [
                ExecutionLeg(
                    decision_id=ids[0],
                    source_event_id="example:EXA",
                    ticker="EXA",
                    direction="buy",
                    currency="USD",
                ),
                ExecutionLeg(
                    decision_id=ids[1],
                    source_event_id="example:EXB",
                    ticker="EXB",
                    direction="buy",
                    currency="USD",
                ),
            ]
        }
    )
    result = evaluate_request(
        request, [_transaction(), _transaction(transaction_id="fill-b", ticker="EXB")], _meta()
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE decisions SET user_notes=? WHERE id=?",
            (
                json.dumps(
                    {
                        "execution_verification": "owner_reported_not_broker_reconciled",
                        "source_event_id": "example:EXB",
                        "broker_execution": {"transaction_id": "other"},
                    }
                ),
                ids[1],
            ),
        )
    with pytest.raises(ValueError, match="different execution"):
        apply_reconciliation(request, result, db_path=db)
    with sqlite3.connect(db) as conn:
        first = json.loads(
            conn.execute("SELECT user_notes FROM decisions WHERE id=?", (ids[0],)).fetchone()[0]
        )
        assert first == json.loads(metadata)
        assert (
            conn.execute("SELECT COUNT(*) FROM analyst_notes WHERE status='superseded'").fetchone()[
                0
            ]
            == 0
        )
        conn.execute(
            "UPDATE decisions SET user_notes=? WHERE id=?",
            (metadata.replace("example:EXA", "example:EXB"), ids[1]),
        )
    assert apply_reconciliation(request, result, db_path=db) == 2
    with sqlite3.connect(db) as conn:
        for note, decision_id in zip(notes, ids, strict=True):
            body, context = conn.execute(
                "SELECT body,context_json FROM analyst_notes WHERE supersedes_id=?", (note.id,)
            ).fetchone()
            assert json.loads(context)["decision_ids"] == [decision_id]
            assert "Execution confirmation" in body
            assert "Funding remains unverified" not in body
            assert json.loads(context)["reconciliation_status"] == "broker_reconciled"


def test_sequential_group_confirmations_replace_block_and_links(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "sequential.db")
    ids: list[int] = []
    with sqlite3.connect(db) as conn:
        for ticker in ("EXA", "EXB"):
            metadata = {
                "execution_verification": "owner_reported_not_broker_reconciled",
                "source_event_id": f"example:{ticker}",
                "current_rationale_note_id": 0,
            }
            row = conn.execute(
                "INSERT INTO decisions (ticker,recommendation_kind,decided_by,made_at,created_at,user_notes) VALUES (?,?,?,?,?,?)",
                (
                    ticker,
                    "add",
                    "owner",
                    "2026-01-02T12:00:00",
                    "2026-01-02T12:00:00",
                    json.dumps(metadata),
                ),
            )
            ids.append(int(row.lastrowid or 0))
    note = create_note_once(
        ticker=None,
        kind="observation",
        body="Owner rationale and trigger",
        source="manual",
        source_ref="sequential",
        context={"decision_ids": ids, "funding_tickers": ["EXB"], "trigger": "keep"},
        db_path=db,
    )
    request = _request(ids[0]).model_copy(
        update={
            "legs": [
                ExecutionLeg(
                    decision_id=ids[0],
                    source_event_id="example:EXA",
                    ticker="EXA",
                    direction="buy",
                    currency="USD",
                ),
                ExecutionLeg(
                    decision_id=ids[1],
                    source_event_id="example:EXB",
                    ticker="EXB",
                    direction="buy",
                    currency="USD",
                ),
            ]
        }
    )
    first = _transaction(fees=None)
    second = _transaction(
        transaction_id="fill-b", ticker="EXB", fees="0", amount="-30", quantity="3"
    )
    assert (
        apply_reconciliation(request, evaluate_request(request, [first], _meta()), db_path=db) == 1
    )
    with sqlite3.connect(db) as conn:
        body, context = conn.execute(
            "SELECT body,context_json FROM analyst_notes WHERE status='open'"
        ).fetchone()
        assert json.loads(context)["execution_verification"] == "partially_broker_reconciled"
        assert "not supplied" in body
    assert (
        apply_reconciliation(
            request, evaluate_request(request, [first, second], _meta()), db_path=db
        )
        == 1
    )
    with sqlite3.connect(db) as conn:
        active_id, body, context = conn.execute(
            "SELECT id,body,context_json FROM analyst_notes WHERE status='open'"
        ).fetchone()
        rows = conn.execute(
            "SELECT user_notes,user_action_kind FROM decisions ORDER BY id"
        ).fetchall()
    assert body.count("Execution confirmation") == 1
    assert "Owner rationale and trigger" in body and "fill-a" in body and "fill-b" in body
    assert "not supplied" in body and "fee: 0" in body
    assert "-20 USD" in body and "-30 USD" in body and "account: 1" in body
    assert json.loads(context)["execution_verification"] == "broker_reconciled"
    assert json.loads(context)["trigger"] == "keep"
    assert all(
        json.loads(row[0])["current_rationale_note_id"] == active_id and row[1] is None
        for row in rows
    )
    assert note.id != active_id
