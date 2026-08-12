# pyright: reportPrivateUsage=false
"""The squashed test DB cache ignores obsolete stamp labels."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


def test_legacy_stamps_share_one_current_head_template(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # The production fixture owns the one cached chain build. This test only
    # spies on that call; it is not another direct migration-chain builder.
    original = getattr(command, "upgrade")

    def counted_upgrade(config: Config, target: str) -> None:
        calls.append(target)
        original(config, target)

    monkeypatch.setattr(command, "upgrade", counted_upgrade)
    target = "0006_add_ask_proposal_approval"
    first = migrated_db(tmp_path / "first.db", stamp="archived-0100", target=target)
    calls_after_first = len(calls)
    second = migrated_db(tmp_path / "second.db", stamp="archived-0273", target=target)

    # Another test in this xdist worker may already have populated the
    # session cache. The invariant is that two compatibility stamp labels do
    # not trigger a second chain build, regardless of who warmed it first.
    assert calls_after_first in {0, 1}
    assert len(calls) == calls_after_first
    if calls:
        assert calls == [target]
    assert first.read_bytes() == second.read_bytes()
