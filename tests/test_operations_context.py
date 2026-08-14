from __future__ import annotations

import os

import pytest

from operations import context


def test_operation_context_propagates_validated_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(context.ENV_OPERATION_ID, raising=False)
    monkeypatch.delenv(context.ENV_TRACE_ID, raising=False)
    monkeypatch.delenv(context.ENV_STAGE, raising=False)

    with context.activate(
        operation_id="operation:" + "a" * 64,
        trace_id="b" * 32,
        stage="refresh_cache",
    ) as active:
        assert context.current() == active
        child = context.child_env({"SAFE": "1"}, stage="refresh_cache.fetch")

    assert child == {
        "SAFE": "1",
        context.ENV_OPERATION_ID: "operation:" + "a" * 64,
        context.ENV_TRACE_ID: "b" * 32,
        context.ENV_STAGE: "refresh_cache.fetch",
    }
    assert context.current() is None


def test_operation_context_rejects_partial_or_unsafe_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(context.ENV_OPERATION_ID, "operation:" + "a" * 64)
    monkeypatch.setenv(context.ENV_TRACE_ID, "trace")
    monkeypatch.delenv(context.ENV_STAGE, raising=False)
    assert context.current() is None

    monkeypatch.setenv(context.ENV_STAGE, "../../credentialed")
    assert context.current() is None


def test_operation_context_does_not_mutate_process_environment() -> None:
    before = dict(os.environ)
    with context.activate(
        operation_id="operation:" + "c" * 64,
        trace_id="d" * 32,
        stage="manual_job",
    ):
        pass
    assert dict(os.environ) == before
