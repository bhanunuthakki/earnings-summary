"""Tests for explicit decision capture with threshold, persistence, and linking."""

from __future__ import annotations

from collections.abc import Callable

from research.decision_capture import capture_decision


def _persist_spy(store: list[dict[str, object]]) -> Callable[..., int]:
    def persist(**kwargs: object) -> int:
        store.append(kwargs)
        return 55

    return persist


def test_capture_persists_and_links_above_threshold() -> None:
    persisted: list[dict[str, object]] = []
    linked: list[dict[str, object]] = []

    def link(**kwargs: object) -> None:
        linked.append(kwargs)

    did = capture_decision(
        ticker="nu",
        direction="Add",
        size_pct=3.0,
        conviction="high",
        note_id=7,
        persist_fn=_persist_spy(persisted),
        link_fn=link,
    )
    assert did == 55
    assert persisted[0]["ticker"] == "NU"  # normalized
    assert persisted[0]["direction"] == "add"
    assert linked and linked[0]["decision_id"] == 55 and linked[0]["note_id"] == 7


def test_capture_below_size_threshold_is_skipped() -> None:
    persisted: list[dict[str, object]] = []
    did = capture_decision(
        ticker="NU",
        direction="trim",
        size_pct=0.4,
        size_threshold=1.0,
        persist_fn=_persist_spy(persisted),
    )
    assert did is None
    assert not persisted


def test_capture_with_no_persist_wired_returns_none() -> None:
    assert capture_decision(ticker="NU", direction="add", size_pct=5.0) is None
