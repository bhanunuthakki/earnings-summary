"""Tests for the qualitative-condition bridge (close_the_loops L9 PR2).

The numeric extractor skips "purely qualitative triggers that carry no number";
this module captures them into a separate column and routes them to the
material_news / earnings_tone triggers so they fire on NEWS, not next quarter's
XBRL. Covers: parse/validate, decode, the LLM extract pass (transport mocked),
the attach DB round-trip, the open-condition read with surface routing, and the
overlay rendering material_news/earnings_tone append to an alert.

No real LLM: ``decision_conditions.call_llm_structured`` is monkeypatched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import decision_conditions as dc
from triggers.base import TriggerCandidate
from triggers.material_news import MaterialNewsTrigger

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_lens VARCHAR(64),
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    qualitative_conditions TEXT,
    qualitative_conditions_extracted_at DATETIME
);
CREATE TABLE llm_artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, content_md TEXT);
CREATE TABLE advisor_memos (id INTEGER PRIMARY KEY AUTOINCREMENT, body_md TEXT);
"""

_WWCMM = (
    "## What would change my mind\n\n"
    "- If the CEO departs before the integration completes.\n"
    "- If a credible competitor enters the core market.\n"
    "- If R&D growth drops below 17% for two quarters.\n"
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# parse / decode
# ---------------------------------------------------------------------------


def test_parse_qualitative_condition_valid_and_defaults() -> None:
    ok = dc.parse_qualitative_condition(
        {"phrase": "the CEO departs", "watch_for": "news", "note": "CEO risk"}
    )
    assert ok == dc.QualitativeCondition("the CEO departs", "news", "CEO risk")
    # bad/missing watch_for defaults to 'both' rather than dropping a real one
    defaulted = dc.parse_qualitative_condition({"phrase": "x", "watch_for": "bogus"})
    assert defaulted is not None and defaulted.watch_for == "both"
    # missing phrase → dropped
    assert dc.parse_qualitative_condition({"watch_for": "news"}) is None
    assert dc.parse_qualitative_condition("not an object") is None


def test_qualitative_conditions_from_json_roundtrip_and_corrupt() -> None:
    conds = (
        dc.QualitativeCondition("CEO departs", "news", None),
        dc.QualitativeCondition("tone turns defensive", "earnings", "q"),
    )
    raw = json.dumps([c.as_json_obj() for c in conds])
    assert dc.qualitative_conditions_from_json(raw) == conds
    assert dc.qualitative_conditions_from_json("not json") == ()
    assert dc.qualitative_conditions_from_json(None) == ()


# ---------------------------------------------------------------------------
# extract (transport mocked)
# ---------------------------------------------------------------------------


# Mirrors decision_conditions._MAX_QUALITATIVE_PER_DECISION (asserted, so a bump
# there fails loudly here).
_CAP = 6


def test_extract_qualitative_conditions_parses_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"phrase": f"event {i}", "watch_for": "both", "note": None} for i in range(_CAP + 3)]

    def fake(prompt: str, **kwargs: object) -> object:
        assert kwargs.get("purpose") == dc.QUALITATIVE_PURPOSE
        return payload

    monkeypatch.setattr(dc, "call_llm_structured", fake)
    out = dc.extract_qualitative_conditions("section text", ticker="nu")
    assert len(out) == _CAP  # capped
    assert out[0].phrase == "event 0"


# ---------------------------------------------------------------------------
# attach (DB round-trip)
# ---------------------------------------------------------------------------


def _make_qual(payload: list[dict[str, object]]):
    def fake(prompt: str, **kwargs: object) -> object:
        return payload

    return fake


def test_attach_qualitative_conditions_stamps_separately(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn(db)
    try:
        conn.execute("INSERT INTO llm_artifacts (id, content_md) VALUES (1, ?)", (_WWCMM,))
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at) "
            "VALUES ('NU', 'add', 1, '2026-05-01')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        dc,
        "call_llm_structured",
        _make_qual([{"phrase": "the CEO departs", "watch_for": "news", "note": "CEO risk"}]),
    )
    tally = dc.attach_qualitative_conditions(db_path=db)
    assert tally["extracted"] == 1

    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT qualitative_conditions, qualitative_conditions_extracted_at, "
            "decision_conditions FROM decisions"
        ).fetchone()
    finally:
        conn.close()
    assert row["qualitative_conditions_extracted_at"] is not None
    assert row["decision_conditions"] is None  # numeric column untouched
    stored = dc.qualitative_conditions_from_json(row["qualitative_conditions"])
    assert [c.phrase for c in stored] == ["the CEO departs"]


def test_attach_qualitative_releases_writer_before_next_llm_call(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn(db)
    try:
        conn.execute("CREATE TABLE ledger_probe (id INTEGER PRIMARY KEY)")
        for artifact_id, ticker in ((1, "NU"), (2, "MELI")):
            conn.execute(
                "INSERT INTO llm_artifacts (id, content_md) VALUES (?, ?)",
                (artifact_id, _WWCMM),
            )
            conn.execute(
                "INSERT INTO decisions "
                "(ticker, recommendation_kind, source_artifact_id, made_at) "
                "VALUES (?, 'add', ?, ?)",
                (ticker, artifact_id, f"2026-05-0{artifact_id}"),
            )
        conn.commit()
    finally:
        conn.close()
    calls = 0

    def fake_extract(*_args: object, **_kwargs: object) -> list[dc.QualitativeCondition]:
        nonlocal calls
        calls += 1
        if calls == 2:
            ledger = sqlite3.connect(str(db), timeout=0.1)
            try:
                ledger.execute("INSERT INTO ledger_probe DEFAULT VALUES")
                ledger.commit()
            finally:
                ledger.close()
        return []

    monkeypatch.setattr(dc, "extract_qualitative_conditions", fake_extract)
    tally = dc.attach_qualitative_conditions(db_path=db)

    assert calls == 2
    assert tally["deferred_transient"] == 0
    conn = _conn(db)
    assert conn.execute("SELECT COUNT(*) FROM ledger_probe").fetchone()[0] == 1
    conn.close()


def test_attach_qualitative_no_section_stamps_empty(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn(db)
    try:
        conn.execute("INSERT INTO llm_artifacts (id, content_md) VALUES (1, 'no section here')")
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at) "
            "VALUES ('NU', 'add', 1, '2026-05-01')"
        )
        conn.commit()
    finally:
        conn.close()

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("must not call the LLM when there is no section")

    monkeypatch.setattr(dc, "call_llm_structured", _boom)
    tally = dc.attach_qualitative_conditions(db_path=db)
    assert tally["no_section"] == 1
    conn = _conn(db)
    try:
        val = conn.execute("SELECT qualitative_conditions FROM decisions").fetchone()[0]
    finally:
        conn.close()
    assert val == "[]"  # stamped done, distinct from NULL


def test_attach_qualitative_transient_failure_does_not_block_other_decisions(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same resilience contract as attach_conditions: a transient per-call
    failure (subprocess non-zero exit / timeout / both backends momentarily
    down) on one decision must not stop the loop from reaching the next one."""
    conn = _conn(db)
    try:
        conn.execute("INSERT INTO llm_artifacts (id, content_md) VALUES (1, ?)", (_WWCMM,))
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at) "
            "VALUES ('NU', 'add', 1, '2026-05-01')"
        )
        conn.execute("INSERT INTO llm_artifacts (id, content_md) VALUES (2, ?)", (_WWCMM,))
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at) "
            "VALUES ('MELI', 'add', 2, '2026-05-02')"
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[str] = []

    def flaky(prompt: str, **kwargs: object) -> object:
        ticker = str(kwargs.get("ticker"))
        calls.append(ticker)
        if ticker == "NU":
            raise RuntimeError("claude -p exited 1 and Gemini fallback is not configured")
        return [{"phrase": "the CEO departs", "watch_for": "news", "note": None}]

    monkeypatch.setattr(dc, "call_llm_structured", flaky)
    tally = dc.attach_qualitative_conditions(db_path=db)

    assert tally["deferred_transient"] == 1
    assert tally["extracted"] == 1
    assert calls == ["NU", "MELI"]

    conn = _conn(db)
    try:
        rows = {
            r["ticker"]: r
            for r in conn.execute(
                "SELECT ticker, qualitative_conditions, qualitative_conditions_extracted_at "
                "FROM decisions"
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows["NU"]["qualitative_conditions_extracted_at"] is None  # retried next run
    assert rows["MELI"]["qualitative_conditions_extracted_at"] is not None


def test_attach_qualitative_hard_stop_propagates(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMSetupError must still propagate — a missing CLI is an operator
    problem, not a per-decision extraction quality issue."""
    from llm.cli import LLMSetupError

    conn = _conn(db)
    try:
        conn.execute("INSERT INTO llm_artifacts (id, content_md) VALUES (1, ?)", (_WWCMM,))
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at) "
            "VALUES ('NU', 'add', 1, '2026-05-01')"
        )
        conn.commit()
    finally:
        conn.close()

    def setup_broken(prompt: str, **kwargs: object) -> object:
        raise LLMSetupError("Claude Code CLI ('claude') not found in PATH.")

    monkeypatch.setattr(dc, "call_llm_structured", setup_broken)
    with pytest.raises(LLMSetupError):
        dc.attach_qualitative_conditions(db_path=db)


def test_attach_qualitative_no_column_degrades(tmp_path: Path) -> None:
    # A pre-0108 DB (decisions without the qualitative column) → db_unavailable.
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY, ticker TEXT, "
            "recommendation_kind TEXT, made_at TEXT, decision_conditions TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    assert dc.attach_qualitative_conditions(db_path=path)["db_unavailable"] == 1


# ---------------------------------------------------------------------------
# load_open_qualitative_conditions — surface routing
# ---------------------------------------------------------------------------


def _insert_decision(
    db: Path, *, ticker: str, conds: list[dict[str, object]], outcome_at: str | None = None
) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO decisions "
            "(ticker, recommendation_kind, made_at, outcome_at, qualitative_conditions) "
            "VALUES (?, 'add', '2026-05-01', ?, ?)",
            (ticker, outcome_at, json.dumps(conds)),
        )
        conn.commit()
    finally:
        conn.close()


def test_load_open_qualitative_conditions_surface_routing(db: Path) -> None:
    _insert_decision(
        db,
        ticker="NU",
        conds=[
            {"phrase": "CEO departs", "watch_for": "news", "note": None},
            {"phrase": "tone turns defensive", "watch_for": "earnings", "note": None},
            {"phrase": "lawsuit filed", "watch_for": "both", "note": None},
        ],
    )
    conn = _conn(db)
    try:
        news = dc.load_open_qualitative_conditions(conn, "NU", surface="news")
        earnings = dc.load_open_qualitative_conditions(conn, "NU", surface="earnings")
    finally:
        conn.close()
    assert sorted(c.phrase for c in news) == ["CEO departs", "lawsuit filed"]
    assert sorted(c.phrase for c in earnings) == ["lawsuit filed", "tone turns defensive"]
    assert news[0].decision_summary.startswith("the 2026-05-01 ADD decision")


def test_load_open_qualitative_conditions_excludes_graded(db: Path) -> None:
    _insert_decision(
        db,
        ticker="NU",
        conds=[{"phrase": "CEO departs", "watch_for": "news", "note": None}],
        outcome_at="2026-06-01",  # graded → not open
    )
    conn = _conn(db)
    try:
        assert dc.load_open_qualitative_conditions(conn, "NU", surface="news") == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# overlay rendering
# ---------------------------------------------------------------------------


def test_render_qualitative_overlay() -> None:
    payloads: list[dict[str, object]] = [
        {"phrase": "CEO departs", "decision_id": 1},
        {"phrase": "competitor enters", "decision_id": 1},
    ]
    memo = dc.render_qualitative_overlay(payloads)
    assert "May bear on" in memo and "CEO departs" in memo and "competitor enters" in memo
    assert dc.render_qualitative_overlay([]) == ""


# ---------------------------------------------------------------------------
# material_news build_alert renders the overlay (deterministic — no scan/LLM)
# ---------------------------------------------------------------------------


def test_material_news_build_alert_renders_overlay() -> None:
    candidate = TriggerCandidate(
        ticker="NU",
        kind="material_news",
        key="NU:news:7",
        evidence={
            "news_id": 7,
            "headline": "NU names new CEO",
            "url": "https://x/7",
            "published_at": "2026-06-10 09:00:00",
            "relevance_score": 0.9,
            "why_material": "leadership change",
            "thesis_conditions": [
                {"decision_id": 3, "phrase": "the CEO departs", "note": "CEO risk"}
            ],
        },
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    alert = MaterialNewsTrigger().build_alert(candidate, None)
    assert alert.memo_text is not None and "May bear on" in alert.memo_text
    assert "the CEO departs" in alert.memo_text
    payload = json.loads(alert.evidence_json)
    assert payload["thesis_conditions"][0]["phrase"] == "the CEO departs"


def test_material_news_build_alert_without_overlay_is_unchanged() -> None:
    candidate = TriggerCandidate(
        ticker="NU",
        kind="material_news",
        key="NU:news:8",
        evidence={
            "news_id": 8,
            "headline": "NU prints Q1",
            "url": "https://x/8",
            "published_at": "2026-06-10 09:00:00",
            "relevance_score": 0.8,
            "why_material": "earnings",
        },
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    alert = MaterialNewsTrigger().build_alert(candidate, None)
    assert alert.memo_text is not None and "May bear on" not in alert.memo_text
    assert "thesis_conditions" not in json.loads(alert.evidence_json)
