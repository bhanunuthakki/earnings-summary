"""Phase-2 tests: multi-channel decision capture (threshold + extract + persist + link)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from research.decision_capture import capture_decision, extract_decision


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
        ticker="NU",
        direction="trim",
        size_pct=0.4,
        size_threshold=1.0,
        persist_fn=_persist_spy(persisted),
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


# --- the governed musing_decision_extract gap-filler (extract_decision) ----------


def test_extract_normalizes_and_keeps_only_confident_fields() -> None:
    out = extract_decision(
        "trimmed a little NU into cash",
        call=lambda _t: {
            "ticker": "nu",
            "direction": "Trim",
            "size_pct": 3,
            "conviction": "high",
            "falsifier": "churn rises",
        },
    )
    assert out == {
        "ticker": "NU",
        "direction": "trim",
        "size_pct": 3.0,
        "conviction": "high",
        "falsifier": "churn rises",
    }


def test_extract_drops_unknown_ticker_and_out_of_enum_fields() -> None:
    out = extract_decision(
        "bought some ACME, betting big",
        call=lambda _t: {
            "ticker": "ACME",  # off-roster
            "direction": "accumulate",  # off-enum
            "conviction": "sky-high",  # off-enum
            "size_pct": -2,  # negative
        },
    )
    assert out == {}  # every field invalid -> nothing to fill


def test_extract_drops_nulls_rather_than_passing_them_through() -> None:
    out = extract_decision(
        "added to NU",
        call=lambda _t: {
            "ticker": "NU",
            "direction": "add",
            "size_pct": None,
            "conviction": None,
            "falsifier": None,
        },
    )
    assert out == {"ticker": "NU", "direction": "add"}


def test_extract_empty_text_never_calls_the_model() -> None:
    calls = {"n": 0}

    def spy(_t: str) -> dict[str, object]:
        calls["n"] += 1
        return {}

    assert extract_decision("   ", call=spy) == {}
    assert calls["n"] == 0


def test_extract_wires_into_capture_decision_as_extract_fn() -> None:
    persisted: list[dict[str, object]] = []

    def fake_llm(_t: str) -> dict[str, object]:
        return {"ticker": "NU", "direction": "trim", "size_pct": 3}

    def extract_fn(t: str) -> dict[str, object]:
        return extract_decision(t, call=fake_llm)

    def persist(**kw: object) -> int:
        persisted.append(kw)
        return 55

    did = capture_decision(
        text="trimmed NU 3% into cash", extract_fn=extract_fn, persist_fn=persist
    )
    assert did == 55
    assert persisted[0]["ticker"] == "NU" and persisted[0]["direction"] == "trim"


def _seed_tracked_companies(db_path: Path, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE tracked_companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'bhanu',
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                list_type TEXT NOT NULL,
                archived_at TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES ('bhanu', ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_extract_roster_is_dynamic_not_hardcoded(tmp_path: Path) -> None:
    """PRD personal_investment_partner_prd.md §9.3: the known-ticker list is no
    longer a hard-coded 14-symbol frozenset — an evaluation-list name entirely
    absent from that old list (FLKR) must be capturable the moment it's
    tracked, with zero code changes here."""
    db_path = tmp_path / "portfolio.db"
    _seed_tracked_companies(db_path, [("FLKR", "Flikr Inc", "evaluation")])

    out = extract_decision(
        "started a small position in FLKR today",
        call=lambda _t: {"ticker": "FLKR", "direction": "buy"},
        db_path=db_path,
    )
    assert out == {"ticker": "FLKR", "direction": "buy"}


def test_extract_roster_still_rejects_untracked_ticker(tmp_path: Path) -> None:
    """An off-roster ticker is dropped even though the roster is now dynamic
    -- ticker validity and direction validity are independent checks (the
    pre-existing behavior; direction alone is still a confidently-extractable
    field even when the ticker guess is untrusted)."""
    db_path = tmp_path / "portfolio.db"
    _seed_tracked_companies(db_path, [("FLKR", "Flikr Inc", "evaluation")])

    out = extract_decision(
        "bought some ACME",
        call=lambda _t: {"ticker": "ACME", "direction": "buy"},
        db_path=db_path,
    )
    assert out == {"direction": "buy"}
    assert "ticker" not in out


def test_extract_default_path_degrades_on_a_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import structured as structured_mod

    def boom(*_a: object, **_k: object) -> object:
        raise structured_mod.StructuredParseError("unusable", raw_head="{...")

    monkeypatch.setattr(structured_mod, "call_llm_structured", boom)
    # default caller (call=None) routes through _default_extract_call, which catches
    # the StructuredParseError and returns {} -> extract_decision fills nothing.
    assert extract_decision("trimmed NU") == {}
