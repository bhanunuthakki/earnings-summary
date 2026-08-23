"""Phase boundaries for the managed issuer-document source CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pytest

from execution import manage_issuer_document_sources as cli


@dataclass(frozen=True)
class _Request:
    attempt_id: str = "attempt-0001"


@dataclass(frozen=True)
class _Receipt:
    request: _Request
    receipt_sha256: str = "a" * 64
    documents: tuple[object, ...] = (object(),)


@dataclass(frozen=True)
class _Publication:
    staging_receipt: _Receipt
    receipt_sha256: str = "a" * 64
    result_sha256: str = "b" * 64
    committed: bool = True
    inserted_document_ids: tuple[int, ...] = (7,)
    reused_document_ids: tuple[int, ...] = (8,)


class _RequestModel:
    @staticmethod
    def model_validate_json(json_data: str | bytes) -> _Request:
        raw = json_data.decode() if isinstance(json_data, bytes) else json_data
        if raw != '{"request":"valid"}':
            raise ValueError("invalid request")
        return _Request()


@final
class _PreparationError(RuntimeError):
    code: str = "preparation_failed"


@final
class _PublisherError(RuntimeError):
    def __init__(self, *, committed: bool = False) -> None:
        super().__init__("publication failed")
        self.code = "publication_committed_partial" if committed else "publication_failed"
        self.committed = committed
        self.inventory_state = "failed" if committed else "not_started"
        self.result_state = "not_started"


@final
class _AuthenticationError(RuntimeError):
    pass


@final
class _ContentionError(RuntimeError):
    pass


def _write_request(tmp_path: Path, text: str = '{"request":"valid"}') -> Path:
    path = tmp_path / "request.json"
    _ = path.write_text(text, encoding="utf-8")
    return path


def _argv(phase: str, request: Path, tmp_path: Path) -> list[str]:
    return [
        phase,
        "--request",
        str(request),
        "--state-root",
        str(tmp_path / "state"),
        "--db",
        str(tmp_path / "state" / "data" / "portfolio.db"),
    ]


def _seams(calls: list[str]) -> cli.ManagedIssuerDocumentSeams:
    request = _Request()

    def prepare(received: _Request, *, state_root: Path, db_path: Path) -> _Receipt:
        assert received == request
        assert state_root.name == "state"
        assert db_path.name == "portfolio.db"
        calls.append("prepare")
        return _Receipt(request)

    def validate(received: _Request, *, state_root: Path, db_path: Path) -> _Receipt:
        assert received == request
        assert state_root.name == "state"
        assert db_path.name == "portfolio.db"
        calls.append("validate")
        return _Receipt(request)

    def publish(received: _Request, *, state_root: Path, db_path: Path) -> _Publication:
        assert received == request
        assert state_root.name == "state"
        assert db_path.name == "portfolio.db"
        calls.append("publish")
        return _Publication(_Receipt(request))

    return cli.ManagedIssuerDocumentSeams(
        request_model=_RequestModel,
        prepare=prepare,
        validate=validate,
        publish=publish,
        preparation_error=_PreparationError,
        publisher_error=_PublisherError,
        authentication_error=_AuthenticationError,
        contention_error=_ContentionError,
    )


@pytest.mark.parametrize(
    ("phase", "expected_call"),
    [("prepare", "prepare"), ("validate", "validate"), ("publish", "publish")],
)
def test_phase_routes_to_exactly_one_existing_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    expected_call: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "require_managed_sqlite_runtime", lambda: "3.53.4")
    monkeypatch.setattr(cli, "_load_seams", lambda: _seams(calls))

    assert cli.main(_argv(phase, _write_request(tmp_path), tmp_path)) == 0

    assert calls == [expected_call]
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == phase
    assert result["attempt_id"] == "attempt-0001"
    assert result["document_count"] == 1
    assert result["receipt_sha256"] == "a" * 64
    if phase == "publish":
        assert result["committed"] is True
        assert result["inserted_document_ids"] == [7]
        assert result["reused_document_ids"] == [8]
        assert result["result_path"].endswith("publication_result.json")


def test_runtime_gate_precedes_request_reading_and_all_phase_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _write_request(tmp_path)
    monkeypatch.setattr(
        cli,
        "require_managed_sqlite_runtime",
        lambda: (_ for _ in ()).throw(RuntimeError("not bootstrapped")),
    )
    monkeypatch.setattr(
        cli,
        "_load_seams",
        lambda: (_ for _ in ()).throw(AssertionError("seams must not load")),
    )

    assert cli.main(_argv("prepare", request, tmp_path)) == 78

    assert json.loads(capsys.readouterr().err) == {
        "code": "managed_runtime_required",
        "phase": "prepare",
    }


def test_invalid_request_fails_without_invoking_a_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "require_managed_sqlite_runtime", lambda: "3.53.4")
    monkeypatch.setattr(cli, "_load_seams", lambda: _seams(calls))

    assert cli.main(_argv("publish", _write_request(tmp_path, "not json"), tmp_path)) == 2

    assert calls == []
    assert json.loads(capsys.readouterr().err) == {
        "code": "request_invalid",
        "phase": "publish",
    }


@pytest.mark.parametrize(
    ("error", "expected_exit", "expected_code"),
    [
        (_PreparationError(), 2, "preparation_failed"),
        (_PublisherError(), 2, "publication_failed"),
        (_PublisherError(committed=True), 2, "publication_committed_partial"),
        (_AuthenticationError(), 10, "source_authentication_denied"),
        (_ContentionError(), 75, "managed_lock_contended"),
    ],
)
def test_known_failures_have_stable_exit_and_reason_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: RuntimeError,
    expected_exit: int,
    expected_code: str,
) -> None:
    calls: list[str] = []
    seams = _seams(calls)

    def fail_prepare(*_args: object, **_kwargs: object) -> _Receipt:
        raise error

    monkeypatch.setattr(cli, "require_managed_sqlite_runtime", lambda: "3.53.4")
    monkeypatch.setattr(
        cli,
        "_load_seams",
        lambda: cli.ManagedIssuerDocumentSeams(
            request_model=seams.request_model,
            prepare=fail_prepare,
            validate=seams.validate,
            publish=seams.publish,
            preparation_error=seams.preparation_error,
            publisher_error=seams.publisher_error,
            authentication_error=seams.authentication_error,
            contention_error=seams.contention_error,
        ),
    )

    assert cli.main(_argv("prepare", _write_request(tmp_path), tmp_path)) == expected_exit

    assert calls == []
    event = json.loads(capsys.readouterr().err)
    assert event["code"] == expected_code
    assert event["phase"] == "prepare"
    if isinstance(error, _PublisherError):
        assert event["committed"] is error.committed
        assert event["inventory_state"] == error.inventory_state
        assert event["result_state"] == error.result_state
