from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import date
from pathlib import Path

import pytest

from compute.evidence_snapshot import EvidenceSnapshot, capture_snapshot
from execution import reconcile_recorded_trades as cli
from execution.reconcile_recorded_trades import collect_transactions, run_request
from integrations.portfolio_tracker_v1 import AccountsV1Result, TransactionsV1Result, V1Fetch
from research.owner_reconciliation import ReconciliationRequest, ReconciliationResult


def _test_fence(path: Path, _root: Path, *, max_bytes: int) -> nullcontext[EvidenceSnapshot]:
    assert max_bytes > 0
    return nullcontext(capture_snapshot(path, path.parent))


def _page(*, cursor: str | None = None, **updates: object) -> V1Fetch[TransactionsV1Result]:
    meta: dict[str, object] = {
        "schema_version": "1.4.0",
        "generated_at": "2026-01-03T00:00:00Z",
        "as_of": "2026-01-02",
        "currency": "USD",
        "source_providers": ["example"],
        "account_coverage": {"included_account_ids": [1]},
        "last_successful_sync_at": "2026-01-03T00:00:00Z",
        "is_partial": False,
        "is_stale": False,
    }
    meta.update(updates)
    return V1Fetch(
        available=True,
        endpoint="/api/v1/transactions",
        data=TransactionsV1Result.model_validate(
            {
                "meta": meta,
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "transactions": [],
                "next_cursor": cursor,
            }
        ),
    )


class Reader:
    def __init__(self, pages: list[V1Fetch[TransactionsV1Result]]) -> None:
        self.pages = iter(pages)
        self.cursors: list[str | None] = []

    def get_accounts(self) -> V1Fetch[AccountsV1Result]:
        page = _page().data
        assert page is not None
        data = AccountsV1Result.model_validate(
            {
                "meta": page.meta.model_dump(mode="json"),
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
                        "last_successful_sync_at": "2026-01-03T00:00:00Z",
                    }
                ],
            }
        )
        return V1Fetch(available=True, endpoint="/api/v1/accounts", data=data)

    def get_transactions_page(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> V1Fetch[TransactionsV1Result]:
        assert start_date == "2026-01-01" and end_date == "2026-01-02"
        self.cursors.append(cursor)
        return next(self.pages)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows request lease")
def test_windows_request_lease_denies_mutation_and_releases_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "event"
    directory.mkdir()
    path = directory / "reconciliation-request.json"
    payload = json.dumps(
        {
            "version": 1,
            "event_id": "example",
            "start_date": "2026-01-01",
            "decision_date": "2026-01-02",
            "legs": [
                {
                    "decision_id": 1,
                    "source_event_id": "example:leg",
                    "ticker": "EXA",
                    "direction": "buy",
                    "currency": "USD",
                }
            ],
        }
    ).encode()
    path.write_bytes(payload)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"revoked")

    class NativeReader(Reader):
        def get_transactions_page(
            self,
            *,
            start_date: str | None = None,
            end_date: str | None = None,
            limit: int = 500,
            cursor: str | None = None,
        ) -> V1Fetch[TransactionsV1Result]:
            with pytest.raises(PermissionError):
                path.write_bytes(b"changed")
            with pytest.raises(PermissionError):
                os.replace(replacement, path)
            with pytest.raises(PermissionError):
                directory.rename(tmp_path / "moved")
            return super().get_transactions_page(
                start_date=start_date, end_date=end_date, limit=limit, cursor=cursor
            )

    def no_database_write(
        _request: ReconciliationRequest, _result: ReconciliationResult, *, db_path: Path | str
    ) -> int:
        assert Path(db_path).name == "synthetic.db"
        assert path.read_bytes() == payload
        return 0

    monkeypatch.setattr(cli, "apply_reconciliation", no_database_write)
    assert run_request(
        path,
        db_path=tmp_path / "data" / "synthetic.db",
        client=NativeReader([_page()]),
        today=date(2026, 1, 2),
        apply=True,
    ) == (0, False)
    assert path.read_bytes() == payload
    path.write_bytes(payload)
    directory.rename(tmp_path / "released")


def test_complete_pagination_requires_stable_source_snapshot() -> None:
    reader = Reader([_page(cursor="next"), _page(generated_at="2026-01-03T00:00:01Z")])
    transactions, meta, pages = collect_transactions(
        reader, start=date(2026, 1, 1), end=date(2026, 1, 2)
    )
    assert transactions == [] and pages == 2 and meta.currency == "USD"
    assert reader.cursors == [None, "next"]
    changed = Reader([_page(cursor="next"), _page(last_successful_sync_at="2026-01-03T01:00:00Z")])
    with pytest.raises(ValueError, match="snapshot_changed"):
        collect_transactions(changed, start=date(2026, 1, 1), end=date(2026, 1, 2))


def test_later_page_failure_does_not_return_partial_evidence() -> None:
    reader = Reader(
        [_page(cursor="next"), V1Fetch(available=False, endpoint="/api/v1/transactions")]
    )
    with pytest.raises(ValueError, match="page_unavailable"):
        collect_transactions(reader, start=date(2026, 1, 1), end=date(2026, 1, 2))


def test_cursor_cycle_fails_closed() -> None:
    with pytest.raises(ValueError, match="cursor_cycle"):
        collect_transactions(
            Reader([_page(cursor="same"), _page(cursor="same")]),
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
        )


def test_dry_run_does_not_touch_database_or_create_a_receipt(tmp_path: Path) -> None:
    path = tmp_path / "reconciliation-request.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "example",
                "start_date": "2026-01-01",
                "decision_date": "2026-01-02",
                "legs": [
                    {
                        "decision_id": 1,
                        "source_event_id": "example:leg",
                        "ticker": "EXA",
                        "direction": "buy",
                        "currency": "USD",
                    }
                ],
            }
        )
    )
    assert run_request(
        path,
        db_path=tmp_path / "does-not-exist.db",
        client=Reader([_page()]),
        today=date(2026, 1, 2),
        apply=False,
    ) == (0, False)
    assert list(tmp_path.iterdir()) == [path]


def test_changed_request_is_rejected_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "reconciliation-request.json"
    payload = {
        "version": 1,
        "event_id": "example",
        "start_date": "2026-01-01",
        "decision_date": "2026-01-02",
        "legs": [
            {
                "decision_id": 1,
                "source_event_id": "example:leg",
                "ticker": "EXA",
                "direction": "buy",
                "currency": "USD",
            }
        ],
    }
    path.write_text(json.dumps(payload))

    class ChangingReader(Reader):
        def get_transactions_page(
            self,
            *,
            start_date: str | None = None,
            end_date: str | None = None,
            limit: int = 500,
            cursor: str | None = None,
        ) -> V1Fetch[TransactionsV1Result]:
            path.write_text("withdrawn")
            return super().get_transactions_page(
                start_date=start_date, end_date=end_date, limit=limit, cursor=cursor
            )

    monkeypatch.setattr(
        cli,
        "hold_windows_evidence_snapshot",
        _test_fence,
    )
    with pytest.raises(ValueError, match="approved_request_changed"):
        run_request(
            path,
            db_path=tmp_path / "missing.db",
            client=ChangingReader([_page()]),
            today=date(2026, 1, 2),
            apply=True,
        )
    assert not (tmp_path / "reconciliation-latest.json").exists()


def test_multiple_pages_cannot_confirm_exact_uniqueness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "reconciliation-request.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "example",
                "start_date": "2026-01-01",
                "decision_date": "2026-01-02",
                "legs": [
                    {
                        "decision_id": 1,
                        "source_event_id": "example:leg",
                        "ticker": "EXA",
                        "direction": "buy",
                        "currency": "USD",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        cli,
        "hold_windows_evidence_snapshot",
        _test_fence,
    )
    with pytest.raises(ValueError, match="snapshot_unproven_across_pages"):
        run_request(
            path,
            db_path=tmp_path / "missing.db",
            client=Reader([_page(cursor="more"), _page()]),
            today=date(2026, 1, 2),
            apply=True,
        )
    assert not (tmp_path / "reconciliation-latest.json").exists()
