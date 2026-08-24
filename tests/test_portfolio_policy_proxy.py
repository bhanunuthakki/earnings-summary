"""Contract tests for the governed portfolio-policy write proxy."""

from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from integrations import portfolio_tracker_client as tracker  # noqa: E402


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _request() -> tracker.PolicyReplacementRequest:
    return tracker.PolicyReplacementRequest.model_validate(
        {
            "weights": [{"ticker": "VOO", "weight_pct": 100, "notes": None}],
            "expected_revision": 4,
            "idempotency_key": "policy-write-0001",
            "source": "earnings_summary",
            "as_of": "2026-08-23T12:00:00+00:00",
        }
    )


def _success_payload(*, recomputation_status: str = "required") -> dict[str, object]:
    when = datetime(2026, 8, 23, 12, tzinfo=UTC).isoformat()
    return {
        "weights": [{"ticker": "VOO", "weight_pct": "100.00", "notes": None, "updated_at": when}],
        "total_pct": "100.00",
        "is_balanced": True,
        "revision": 5,
        "source": "earnings_summary",
        "as_of": when,
        "recomputation": {
            "status": recomputation_status,
            "policy_revision": 5,
            "reason": "policy_weights_changed" if recomputation_status == "required" else None,
        },
        "receipt": {
            "receipt_id": "receipt-1",
            "idempotency_key": "policy-write-0001",
            "outcome": "applied",
            "recorded_at": when,
        },
    }


def test_policy_proxy_puts_typed_contract_and_returns_only_confirmed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def put(url: str, **kwargs: object) -> _Response:
        captured["url"] = url
        captured.update(kwargs)
        return _Response(200, _success_payload())

    monkeypatch.setattr(tracker.requests, "put", put)

    result = tracker.replace_portfolio_policy(_request(), api_url="http://tracker.test")

    assert result.accepted is False
    assert result.pending_recomputation is True
    assert result.policy is not None and result.policy.revision == 5
    assert captured["url"] == "http://tracker.test/api/policy"
    assert captured["headers"] == {"X-Portfolio-Write-Intent": "replace-policy"}
    assert cast(dict[str, object], captured["json"])["expected_revision"] == 4


def test_policy_proxy_marks_current_recomputation_as_final_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def put(*_args: object, **_kwargs: object) -> _Response:
        return _Response(200, _success_payload(recomputation_status="current"))

    monkeypatch.setattr(
        tracker.requests,
        "put",
        put,
    )

    result = tracker.replace_portfolio_policy(_request())

    assert result.accepted is True
    assert result.pending_recomputation is False


def test_policy_read_preserves_revisioned_recomputation_metadata() -> None:
    payload = _success_payload(recomputation_status="current")
    payload.pop("receipt")

    policy = tracker._parse_policy(payload)  # pyright: ignore[reportPrivateUsage]

    assert policy.revision == 5
    assert policy.source == "earnings_summary"
    assert policy.as_of == "2026-08-23T12:00:00+00:00"
    assert policy.recomputation_status == "current"
    assert policy.recomputation_policy_revision == 5
    assert policy.recomputation_reason is None
    assert policy.write_ready is True


def test_policy_read_disables_writes_when_governance_metadata_is_missing_or_pending() -> None:
    legacy = tracker._parse_policy(  # pyright: ignore[reportPrivateUsage]
        {"weights": [], "total_pct": "100.00", "is_balanced": True}
    )
    pending = tracker._parse_policy(  # pyright: ignore[reportPrivateUsage]
        _success_payload()
    )

    assert legacy.revision is None
    assert legacy.recomputation_status is None
    assert legacy.write_ready is False
    assert pending.recomputation_status == "required"
    assert pending.recomputation_reason == "policy_weights_changed"
    assert pending.write_ready is False


def _drop_receipt(payload: dict[str, object]) -> None:
    payload.pop("receipt")


def _mark_unbalanced(payload: dict[str, object]) -> None:
    payload["is_balanced"] = False


def _change_recomputation_revision(payload: dict[str, object]) -> None:
    cast(dict[str, object], payload["recomputation"])["policy_revision"] = 4


def _change_receipt_key(payload: dict[str, object]) -> None:
    cast(dict[str, object], payload["receipt"])["idempotency_key"] = "policy-write-other"


def _change_revision(payload: dict[str, object]) -> None:
    payload["revision"] = 6


def _change_source(payload: dict[str, object]) -> None:
    payload["source"] = "portfolio_tracker_ui"


def _change_as_of(payload: dict[str, object]) -> None:
    payload["as_of"] = "2026-08-24T12:00:00+00:00"


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_receipt,
        _mark_unbalanced,
        _change_recomputation_revision,
        _change_receipt_key,
        _change_revision,
        _change_source,
        _change_as_of,
    ],
)
def test_policy_proxy_rejects_incoherent_success_responses(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    payload = deepcopy(_success_payload(recomputation_status="current"))
    mutate(payload)

    def put(*_args: object, **_kwargs: object) -> _Response:
        return _Response(200, payload)

    monkeypatch.setattr(tracker.requests, "put", put)
    result = tracker.replace_portfolio_policy(_request())

    assert result.status == "malformed_response"
    assert result.policy is None


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_revision"),
    [
        (
            _Response(422, {"detail": {"code": "POLICY_VALIDATION_FAILED"}}),
            "validation_error",
            None,
        ),
        (
            _Response(
                409,
                {"detail": {"code": "POLICY_REVISION_CONFLICT", "current_revision": 8}},
            ),
            "revision_conflict",
            8,
        ),
        (
            _Response(409, {"detail": {"code": "POLICY_IDEMPOTENCY_CONFLICT"}}),
            "idempotency_conflict",
            None,
        ),
        (_Response(403, {"detail": {"code": "POLICY_WRITE_UNAUTHORIZED"}}), "unauthorized", None),
        (
            _Response(503, {"detail": {"code": "POLICY_RECOMPUTATION_INVALIDATION_FAILED"}}),
            "recomputation_failure",
            None,
        ),
    ],
)
def test_policy_proxy_classifies_provider_failures_without_draft_state(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
    expected_status: str,
    expected_revision: int | None,
) -> None:
    def put(*_args: object, **_kwargs: object) -> _Response:
        return response

    monkeypatch.setattr(tracker.requests, "put", put)

    result = tracker.replace_portfolio_policy(_request())

    assert result.status == expected_status
    assert result.policy is None
    assert result.current_revision == expected_revision


def test_policy_proxy_classifies_offline_without_retrying_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def offline(*_args: object, **_kwargs: object) -> _Response:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(tracker.requests, "put", offline)
    result = tracker.replace_portfolio_policy(_request())
    assert result.status == "offline"
    assert result.policy is None


def test_comments_server_requires_owner_intent_and_proxies_confirmed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = comments_server.create_app(tmp_path).test_client()
    body = _request().model_dump(mode="json")

    assert client.put("/api/portfolio/policy", json=body).status_code == 403

    captured: list[tracker.PolicyReplacementRequest] = []

    def replace(request: tracker.PolicyReplacementRequest) -> tracker.PolicyWriteResult:
        captured.append(request)
        policy = tracker.GovernedPolicyMix.model_validate(_success_payload())
        return tracker.PolicyWriteResult(status="accepted_pending_recomputation", policy=policy)

    monkeypatch.setattr(tracker, "replace_portfolio_policy", replace)
    response = client.put(
        "/api/portfolio/policy",
        json=body,
        headers={"X-Portfolio-Write-Intent": "replace-policy"},
    )

    assert response.status_code == 202
    assert response.get_json()["status"] == "accepted_pending_recomputation"
    assert response.get_json()["policy"]["revision"] == 5
    assert captured == [_request()]


def test_comments_server_does_not_accept_spoofed_audit_source(tmp_path: Path) -> None:
    client = comments_server.create_app(tmp_path).test_client()
    body = _request().model_dump(mode="json")
    body["source"] = "portfolio_tracker_ui"

    response = client.put(
        "/api/portfolio/policy",
        json=body,
        headers={"X-Portfolio-Write-Intent": "replace-policy"},
    )

    assert response.status_code == 400
