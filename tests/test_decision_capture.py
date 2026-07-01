"""Phase-2 tests: multi-channel decision capture (threshold + extract + persist + link)."""

from __future__ import annotations

from research.decision_capture import capture_decision


def _persist_spy(store: list[dict[str, object]]):
    return lambda **kw: store.append(kw) or 55


def test_capture_persists_and_links_above_threshold() -> None:
    persisted: list[dict[str, object]] = []
    linked: list[dict[str, object]] = []
    did = capture_decision(
        ticker="nu",
        direction="Add",
        size_pct=3.0,
        conviction="high",
        note_id=7,
        persist_fn=_persist_spy(persisted),
        link_fn=lambda **kw: linked.append(kw),
    )
    assert did == 55
    assert persisted[0]["ticker"] == "NU"  # normalized
    assert persisted[0]["direction"] == "add"
    assert linked and linked[0]["decision_id"] == 55 and linked[0]["note_id"] == 7


def test_capture_below_size_threshold_is_skipped() -> None:
    persisted: list[dict[str, object]] = []
    did = capture_decision(
        ticker="NU", direction="trim", size_pct=0.4, size_threshold=1.0, persist_fn=_persist_spy(persisted)
    )
    assert did is None
    assert not persisted


def test_capture_fills_missing_fields_from_the_extractor() -> None:
    persisted: list[dict[str, object]] = []
    did = capture_decision(
        text="trimmed a little NU into cash",
        extract_fn=lambda _t: {"ticker": "NU", "direction": "trim", "size_pct": 2.0},
        persist_fn=_persist_spy(persisted),
    )
    assert did == 55
    assert persisted[0]["ticker"] == "NU"
    assert persisted[0]["direction"] == "trim"


def test_capture_without_ticker_or_direction_returns_none() -> None:
    persisted: list[dict[str, object]] = []
    # no explicit fields, no extractor -> nothing to log
    assert capture_decision(text="hmm", persist_fn=_persist_spy(persisted)) is None
    # extractor also can't resolve a ticker -> still none
    assert (
        capture_decision(text="x", extract_fn=lambda _t: {}, persist_fn=_persist_spy(persisted))
        is None
    )
    assert not persisted


def test_capture_with_no_persist_wired_returns_none() -> None:
    assert capture_decision(ticker="NU", direction="add", size_pct=5.0) is None
