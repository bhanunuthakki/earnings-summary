"""The IR-home CLI selects only reviewed, typed candidates."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from execution import verify_ir_home_authorities
from ir_pipeline.home_authority_batch import IRHomeBatchResult

select_candidates = verify_ir_home_authorities.select_candidates


def test_select_candidates_normalizes_and_preserves_registry_order() -> None:
    selected = select_candidates(("meta", "AMZN"))

    assert [candidate.ticker for candidate in selected] == ["AMZN", "META"]


def test_select_candidates_rejects_unknown_ticker() -> None:
    with pytest.raises(ValueError, match="no reviewed IR-home candidate"):
        select_candidates(("UNKNOWN",))


def test_select_candidates_collapses_ticker_alias_to_one_authority_target() -> None:
    selected = select_candidates(("GOOGL", "GOOG"))

    assert [candidate.ticker for candidate in selected] == ["GOOG"]


def test_apply_commits_verified_authority_records(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Connection:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    conn = _Connection()
    expected = IRHomeBatchResult(mode="apply", items=())

    def fake_connect(*_args: object, **_kwargs: object) -> object:
        return conn

    def fake_verify(*_args: object, **_kwargs: object) -> IRHomeBatchResult:
        return expected

    monkeypatch.setattr(verify_ir_home_authorities, "connect_sqlite", fake_connect)
    monkeypatch.setattr(
        verify_ir_home_authorities,
        "verify_ir_home_candidates",
        fake_verify,
    )
    args = argparse.Namespace(
        all_candidates=False,
        ticker=["WIX"],
        blob_root=Path("blobs"),
        apply=True,
        recorded_at=None,
        user_agent="earnings-summary verifier",
        connect_timeout_seconds=10,
        read_timeout_seconds=60,
        max_body_bytes=10_000_000,
        max_redirects=5,
        max_workers=1,
        refresh_existing=False,
        db=Path("portfolio.db"),
    )

    assert verify_ir_home_authorities.run_authority_verification(args) is expected
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True
