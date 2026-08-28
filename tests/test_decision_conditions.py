"""Tests for src/decision_conditions.py (fund-grade S5, PR 1).

Covers:
  * section extraction — numbered (five_min_reread) + unnumbered (Socratic)
    header shapes, absent section, empty body
  * parse_condition — every rejection path + the note/metric caps
  * conditions_from_json — corrupt column degrades to (), never raises
  * extract_conditions — LLM mocked at the module seam
    (``decision_conditions.call_llm_structured``): invalid entries dropped,
    cap enforced, StructuredParseError propagates
  * record_socratic_decisions — insert / idempotent re-run / no-stance skip /
    stance→kind mapping (buy→add)
  * attach_conditions — artifact + memo sources, '[]' stamp for sectionless
    prose, parse-failure leaves the row unstamped (retried next run),
    idempotency (stamped rows are not re-walked)
  * load_open_decisions — open + conditioned rows only

All LLM transport is monkeypatched (conftest enforces no real spend).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import decision_conditions as dc
from decision_conditions import (
    DecisionCondition,
    attach_conditions,
    conditions_from_json,
    extract_conditions,
    extract_section,
    load_open_decisions,
    metric_vocabulary,
    parse_condition,
    record_socratic_decisions,
)
from llm.structured import StructuredParseError
from tests.kpi_semantic_support import admit_all_kpi_facts

# ---------------------------------------------------------------------------
# Fixture DB — decisions in the post-0086 shape + the source tables
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_lens VARCHAR(64),
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    user_notes TEXT,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    outcome_notes TEXT,
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    created_at DATETIME NOT NULL,
    CHECK (source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL)
);
CREATE UNIQUE INDEX idx_decisions_source_artifact ON decisions(source_artifact_id);
CREATE UNIQUE INDEX uq_decisions_source_memo ON decisions(source_memo_id)
    WHERE source_memo_id IS NOT NULL;
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    purpose TEXT NOT NULL,
    content_md TEXT,
    generated_at TEXT NOT NULL,
    superseded_by_id INTEGER
);
CREATE TABLE advisor_memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    kind TEXT NOT NULL,
    ticker TEXT,
    counter_ticker TEXT,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    context_json TEXT,
    stance TEXT,
    horizon_days INTEGER,
    score_status TEXT NOT NULL DEFAULT 'pending',
    note_id INTEGER,
    ledger_entry_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source_doc_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source_doc_id INTEGER
);
"""

_NOW = datetime.now(UTC).replace(tzinfo=None).isoformat()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


_FIVE_MIN_MD = """## 1. What changed
- NPLs ticked up 40bps.

## 2. Recommended action
**TRIM 20%** The credit book is wobbling.

## 3. What would change my mind
90d+ NPLs pushing above 7% for two consecutive quarters, or monthly ARPAC
falling below $10.
"""

_SOCRATIC_MD = """## Bull
Strong deposit franchise.

## Bear
Funding costs rising.

## What would change my mind
Risk-adjusted NIM printing below 9% for two quarters.

## Stance if forced
The deposit moat carries it.

STANCE: hold
"""


def _good_condition_obj(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "metric": "NPL 90d+",
        "metric_source": "kpi",
        "op": "gt",
        "threshold": 7.0,
        "unit": "percent",
        "for_periods": 2,
        "note": "90d+ NPLs above 7% for two straight quarters",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


def test_extract_section_numbered_five_min_shape() -> None:
    body = extract_section(_FIVE_MIN_MD)
    assert body is not None
    assert body.startswith("90d+ NPLs pushing above 7%")
    assert "Recommended action" not in body


def test_extract_section_unnumbered_socratic_shape() -> None:
    body = extract_section(_SOCRATIC_MD)
    assert body is not None
    assert body.startswith("Risk-adjusted NIM printing below 9%")
    # Stops at the next section header.
    assert "Stance if forced" not in body


def test_extract_section_absent_or_empty() -> None:
    assert extract_section("## 1. What changed\nnothing") is None
    assert extract_section(None) is None
    assert extract_section("") is None
    assert extract_section("## What would change my mind\n\n## Next\nx") is None


# ---------------------------------------------------------------------------
# Condition validation
# ---------------------------------------------------------------------------


def test_parse_condition_happy_path() -> None:
    cond = parse_condition(_good_condition_obj())
    assert cond == DecisionCondition(
        metric="NPL 90d+",
        metric_source="kpi",
        op="gt",
        threshold=7.0,
        unit="percent",
        for_periods=2,
        note="90d+ NPLs above 7% for two straight quarters",
    )


def test_parse_condition_unresolved_metric_source_none() -> None:
    cond = parse_condition(_good_condition_obj(metric_source=None))
    assert cond is not None
    assert cond.metric_source is None


@pytest.mark.parametrize(
    "override",
    [
        {"metric": ""},
        {"metric": 7},
        {"metric_source": "vibes"},
        {"op": "eq"},  # directional ops only
        {"op": "below"},
        {"threshold": "seven"},
        {"threshold": True},
        {"unit": "furlongs"},
        {"for_periods": 0},
        {"for_periods": 13},
        {"for_periods": 2.5},
        {"for_periods": True},
    ],
)
def test_parse_condition_rejections(override: dict[str, object]) -> None:
    assert parse_condition(_good_condition_obj(**override)) is None


def test_parse_condition_not_a_dict_and_default_periods() -> None:
    assert parse_condition("nope") is None
    obj = _good_condition_obj()
    del obj["for_periods"]
    cond = parse_condition(obj)
    assert cond is not None
    assert cond.for_periods == 1


def test_parse_condition_watermark_fields_round_trip() -> None:
    """baseline_period_end / breached_at_attach (alert-quality gates) survive
    a JSON round-trip; legacy JSON without the keys defaults them."""
    cond = parse_condition(
        _good_condition_obj(baseline_period_end="2026-03-31", breached_at_attach=True)
    )
    assert cond is not None
    assert cond.baseline_period_end == "2026-03-31"
    assert cond.breached_at_attach is True
    encoded = cond.as_json_obj()
    assert encoded["baseline_period_end"] == "2026-03-31"
    assert encoded["breached_at_attach"] is True
    again = parse_condition(encoded)
    assert again == cond

    legacy = parse_condition(_good_condition_obj())
    assert legacy is not None
    assert legacy.baseline_period_end is None
    assert legacy.breached_at_attach is False

    # Malformed annotation values degrade to defaults, never reject the row.
    junk = parse_condition(_good_condition_obj(baseline_period_end=42, breached_at_attach="yes"))
    assert junk is not None
    assert junk.baseline_period_end is None
    assert junk.breached_at_attach is False


def test_parse_condition_not_before_round_trip() -> None:
    """not_before (milestone-dated tripwires, 2026-07-16) survives a JSON
    round-trip; legacy JSON without the key defaults to None (evaluable now);
    a malformed value degrades to None rather than rejecting the condition."""
    cond = parse_condition(_good_condition_obj(not_before="2027-07-01"))
    assert cond is not None
    assert cond.not_before == "2027-07-01"
    encoded = cond.as_json_obj()
    assert encoded["not_before"] == "2027-07-01"
    assert parse_condition(encoded) == cond

    legacy = parse_condition(_good_condition_obj())
    assert legacy is not None
    assert legacy.not_before is None

    junk = parse_condition(_good_condition_obj(not_before=42))
    assert junk is not None
    assert junk.not_before is None
    blank = parse_condition(_good_condition_obj(not_before="   "))
    assert blank is not None
    assert blank.not_before is None


def test_conditions_from_json_corrupt_degrades() -> None:
    assert conditions_from_json(None) == ()
    assert conditions_from_json("") == ()
    assert conditions_from_json("{not json") == ()
    assert conditions_from_json('{"an": "object"}') == ()
    good = json.dumps([_good_condition_obj(), {"metric": ""}])
    parsed = conditions_from_json(good)
    assert len(parsed) == 1
    assert parsed[0].metric == "NPL 90d+"


# ---------------------------------------------------------------------------
# LLM extraction (mocked transport)
# ---------------------------------------------------------------------------


def test_extract_conditions_drops_invalid_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_structured(prompt: str, **kwargs: object) -> object:
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        entries: list[object] = [_good_condition_obj(metric=f"Metric {i}") for i in range(10)]
        entries.insert(0, {"metric": "", "op": "gt"})  # invalid — dropped
        return entries

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    out = extract_conditions(
        "section text",
        ticker="nu",
        kpi_names=["NPL 90d+"],
        line_items=["revenue"],
    )
    assert len(out) == 8  # _MAX_CONDITIONS_PER_DECISION
    assert all(c.metric.startswith("Metric ") for c in out)
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["purpose"] == "decision_conditions_extract"
    assert kwargs["ticker"] == "NU"
    assert kwargs["expect"] == "array"
    prompt = seen["prompt"]
    assert isinstance(prompt, str)
    assert "NPL 90d+" in prompt and "revenue" in prompt


def test_extract_conditions_carries_decision_date_and_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v2 prompt anchors milestone horizons on the decision date (made_at)
    and the response schema's not_before flows through to the parsed
    condition. Without made_at (the eval harness path) the date renders as
    'unknown' rather than crashing the format."""
    seen: dict[str, object] = {}

    def fake_structured(prompt: str, **kwargs: object) -> object:
        seen["prompt"] = prompt
        return [_good_condition_obj(not_before="2027-07-01")]

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    out = extract_conditions(
        "Base44 ARR reaches $200M in ~12 months",
        ticker="WIX",
        kpi_names=[],
        line_items=[],
        made_at="2026-07-01T08:00:00",
    )
    assert len(out) == 1
    assert out[0].not_before == "2027-07-01"
    prompt = seen["prompt"]
    assert isinstance(prompt, str)
    assert "decision was made on 2026-07-01" in prompt
    assert '"not_before"' in prompt

    # No made_at → 'unknown' decision date, still a well-formed prompt.
    extract_conditions("s", ticker="WIX", kpi_names=[], line_items=[])
    prompt = seen["prompt"]
    assert isinstance(prompt, str)
    assert "decision was made on unknown" in prompt


def test_decision_conditions_extract_prompt_version_bumped() -> None:
    """The v2 prompt rewrite (not_before + decision date) is registered in the
    single bump-point registry."""
    from llm.prompt_versions import prompt_version_for

    assert prompt_version_for("decision_conditions_extract") == "v2"


def test_extract_conditions_parse_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_structured(prompt: str, **kwargs: object) -> object:
        raise StructuredParseError("bad twice", raw_head="x")

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    with pytest.raises(StructuredParseError):
        extract_conditions("s", ticker="NU", kpi_names=[], line_items=[])


# ---------------------------------------------------------------------------
# Socratic memo → decisions rows
# ---------------------------------------------------------------------------


def _insert_memo(path: Path, *, kind: str = "socratic", stance: str | None = "hold") -> int:
    conn = _connect(path)
    cur = conn.execute(
        "INSERT INTO advisor_memos (kind, ticker, title, body_md, stance, created_at) "
        "VALUES (?, 'NU', 'Socratic: NU', ?, ?, ?)",
        (kind, _SOCRATIC_MD, stance, _NOW),
    )
    conn.commit()
    memo_id = int(cur.lastrowid or 0)
    conn.close()
    return memo_id


def test_record_socratic_decisions_inserts_and_is_idempotent(db: Path) -> None:
    memo_id = _insert_memo(db, stance="buy")
    tally = record_socratic_decisions(db_path=db)
    assert tally["inserted"] == 1

    conn = _connect(db)
    row = conn.execute("SELECT * FROM decisions").fetchone()
    conn.close()
    assert row["ticker"] == "NU"
    assert row["recommendation_kind"] == "add"  # buy→add on a holding
    assert row["source_memo_id"] == memo_id
    assert row["source_artifact_id"] is None
    assert row["source_lens"] == "advisor_socratic"
    assert "deposit moat" in row["rationale_excerpt"]

    again = record_socratic_decisions(db_path=db)
    assert again["inserted"] == 0
    assert again["skipped_existing"] == 1


def test_record_socratic_decisions_skips_no_stance_and_other_kinds(db: Path) -> None:
    _insert_memo(db, stance=None)
    _insert_memo(db, kind="next_dollar", stance="hold")  # not socratic — ignored
    tally = record_socratic_decisions(db_path=db)
    assert tally["inserted"] == 0
    assert tally["skipped_no_stance"] == 1


def test_record_socratic_decisions_missing_db() -> None:
    tally = record_socratic_decisions(db_path=Path("Z:/nope/portfolio.db"))
    assert tally["db_unavailable"] == 1


# ---------------------------------------------------------------------------
# attach_conditions
# ---------------------------------------------------------------------------


def _insert_artifact_decision(path: Path, *, content_md: str, ticker: str = "NU") -> int:
    conn = _connect(path)
    cur = conn.execute(
        "INSERT INTO llm_artifacts (ticker, purpose, content_md, generated_at) "
        "VALUES (?, 'lens:five_min_reread', ?, ?)",
        (ticker, content_md, _NOW),
    )
    artifact_id = int(cur.lastrowid or 0)
    cur = conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
        "source_lens, made_at, outcome_label, created_at) "
        "VALUES (?, 'trim', ?, 'five_min_reread', ?, 'pending', ?)",
        (ticker, artifact_id, _NOW, _NOW),
    )
    conn.commit()
    decision_id = int(cur.lastrowid or 0)
    conn.close()
    return decision_id


def test_attach_conditions_artifact_and_memo_sources(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_decision = _insert_artifact_decision(db, content_md=_FIVE_MIN_MD)
    _insert_memo(db, stance="hold")
    record_socratic_decisions(db_path=db)

    def fake_structured(prompt: str, **kwargs: object) -> object:
        return [_good_condition_obj()]

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    tally = attach_conditions(db_path=db)
    assert tally["extracted"] == 2
    assert tally["no_section"] == 0

    conn = _connect(db)
    rows = conn.execute(
        "SELECT id, decision_conditions, conditions_extracted_at FROM decisions"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    for row in rows:
        assert row["conditions_extracted_at"] is not None
        stored = json.loads(row["decision_conditions"])
        assert stored[0]["metric"] == "NPL 90d+"
    assert {r["id"] for r in rows} >= {artifact_decision}

    # Idempotent: stamped rows are not re-walked (LLM would raise if called).
    def exploding(prompt: str, **kwargs: object) -> object:
        raise AssertionError("re-extraction attempted on a stamped row")

    monkeypatch.setattr(dc, "call_llm_structured", exploding)
    again = attach_conditions(db_path=db)
    assert again["extracted"] == 0
    assert again["no_section"] == 0


def test_attach_conditions_releases_writer_before_next_llm_call(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each persisted result commits before the next call writes its ledger.

    The production LLM ledger uses a separate writer connection.  Holding the
    decision UPDATE transaction across the loop makes that ledger wait for the
    busy timeout on every later item.
    """
    _insert_artifact_decision(db, content_md=_FIVE_MIN_MD, ticker="NU")
    _insert_artifact_decision(db, content_md=_FIVE_MIN_MD, ticker="MELI")
    conn = _connect(db)
    conn.execute("CREATE TABLE ledger_probe (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    calls = 0

    def fake_extract(*_args: object, **_kwargs: object) -> list[DecisionCondition]:
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

    monkeypatch.setattr(dc, "extract_conditions", fake_extract)
    tally = attach_conditions(db_path=db)

    assert calls == 2
    assert tally["deferred_transient"] == 0
    conn = _connect(db)
    assert conn.execute("SELECT COUNT(*) FROM ledger_probe").fetchone()[0] == 1
    conn.close()


def test_attach_conditions_passes_made_at_into_the_prompt(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch rung feeds each decision's made_at to the extractor so the
    v2 prompt can anchor 'in ~12 months'-style horizons on the real decision
    date, and the extractor's not_before lands in the stamped JSON."""
    _insert_artifact_decision(db, content_md=_FIVE_MIN_MD)
    seen: dict[str, object] = {}

    def fake_structured(prompt: str, **kwargs: object) -> object:
        seen["prompt"] = prompt
        return [_good_condition_obj(not_before="2027-07-01")]

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    tally = attach_conditions(db_path=db)
    assert tally["extracted"] == 1
    prompt = seen["prompt"]
    assert isinstance(prompt, str)
    assert f"decision was made on {_NOW[:10]}" in prompt

    conn = _connect(db)
    row = conn.execute("SELECT decision_conditions FROM decisions").fetchone()
    conn.close()
    stored = json.loads(row["decision_conditions"])
    assert stored[0]["not_before"] == "2027-07-01"


def test_attach_conditions_stamps_baseline_and_breached_at_attach(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The breach-freshness watermark: attach stamps each condition with the
    latest ALREADY-OBSERVED period_end for its metric, and marks a condition
    that is already breached against that history (so it can never later
    push-fire on the same stale data)."""
    _insert_artifact_decision(db, content_md=_FIVE_MIN_MD)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES ('NU', 'NPL 90d+', 'percent')"
    )
    # Two consecutive periods above the 7.0 threshold → already BREACHED.
    for i, (period_end, value) in enumerate([("2025-12-31", 7.2), ("2026-03-31", 7.5)]):
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
            "value, unit, source_doc_id) VALUES ('NU', ?, 'Q1', 1, ?, 'percent', ?)",
            (period_end, value, i + 1),
        )
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES ('NU', 'Monthly ARPAC', 'actual')"
    )
    # Healthy (not breached): 12 > the 10 threshold on an 'lt' condition.
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('NU', '2026-03-31', 'Q1', 2, 12.0, 'actual', 1)"
    )
    admit_all_kpi_facts(conn)
    conn.commit()
    conn.close()

    def fake_structured(prompt: str, **kwargs: object) -> object:
        return [
            _good_condition_obj(),  # NPL 90d+ gt 7.0 for 2 — already breached
            _good_condition_obj(
                metric="Monthly ARPAC", op="lt", threshold=10.0, unit="actual", for_periods=1
            ),
            _good_condition_obj(metric="Ghost Metric"),  # no facts → no baseline
        ]

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    tally = attach_conditions(db_path=db)
    assert tally["extracted"] == 1

    conn = _connect(db)
    row = conn.execute("SELECT decision_conditions FROM decisions").fetchone()
    conn.close()
    stored = json.loads(row["decision_conditions"])
    by_metric = {c["metric"]: c for c in stored}
    assert by_metric["NPL 90d+"]["baseline_period_end"] == "2026-03-31"
    assert by_metric["NPL 90d+"]["breached_at_attach"] is True
    assert by_metric["Monthly ARPAC"]["baseline_period_end"] == "2026-03-31"
    assert by_metric["Monthly ARPAC"]["breached_at_attach"] is False
    assert by_metric["Ghost Metric"]["baseline_period_end"] is None
    assert by_metric["Ghost Metric"]["breached_at_attach"] is False


def test_attach_conditions_sectionless_prose_stamps_empty(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision_id = _insert_artifact_decision(
        db, content_md="## 2. Recommended action\n**HOLD**\nNothing else."
    )

    def exploding(prompt: str, **kwargs: object) -> object:
        raise AssertionError("LLM must not be called without a section")

    monkeypatch.setattr(dc, "call_llm_structured", exploding)
    tally = attach_conditions(db_path=db)
    assert tally["no_section"] == 1

    conn = _connect(db)
    row = conn.execute(
        "SELECT decision_conditions, conditions_extracted_at FROM decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    conn.close()
    assert row["decision_conditions"] == "[]"
    assert row["conditions_extracted_at"] is not None


def test_attach_conditions_parse_failure_leaves_row_unstamped(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision_id = _insert_artifact_decision(db, content_md=_FIVE_MIN_MD)

    def fake_structured(prompt: str, **kwargs: object) -> object:
        raise StructuredParseError("unusable twice", raw_head="?")

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    tally = attach_conditions(db_path=db)
    assert tally["parse_failed"] == 1

    conn = _connect(db)
    row = conn.execute(
        "SELECT decision_conditions, conditions_extracted_at FROM decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    conn.close()
    assert row["decision_conditions"] is None
    assert row["conditions_extracted_at"] is None  # retried next run


def test_attach_conditions_transient_failure_does_not_block_other_decisions(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient LLM-call failure (subprocess non-zero exit, timeout, both
    Claude + Gemini momentarily down — modeled here as the plain RuntimeError
    ``llm.fallback.try_gemini_fallback`` raises when the Gemini fallback isn't
    configured either) on ONE decision must not deny every other decision its
    conditions for the day (the bug this test guards against: it used to
    propagate out of attach_conditions and crash the whole morning-pipeline
    stage). Decision A stays unstamped (retried next run); decision B —
    processed AFTER A in made_at order — still gets extracted."""
    made_at_a = _NOW
    made_at_b = (datetime.now(UTC).replace(tzinfo=None)).isoformat()
    decision_a = _insert_artifact_decision(db, content_md=_FIVE_MIN_MD, ticker="NU")
    decision_b = _insert_artifact_decision(db, content_md=_FIVE_MIN_MD, ticker="MELI")
    conn = _connect(db)
    conn.execute("UPDATE decisions SET made_at = ? WHERE id = ?", (made_at_a, decision_a))
    conn.execute("UPDATE decisions SET made_at = ? WHERE id = ?", (made_at_b, decision_b))
    conn.commit()
    conn.close()

    calls: list[str] = []

    def flaky_structured(prompt: str, **kwargs: object) -> object:
        ticker = kwargs.get("ticker")
        calls.append(str(ticker))
        if ticker == "NU":
            raise RuntimeError(
                "Claude CLI failed AND no Gemini fallback configured. "
                "Original Claude error: CalledProcessError: exit 1"
            )
        return [_good_condition_obj()]

    monkeypatch.setattr(dc, "call_llm_structured", flaky_structured)
    tally = attach_conditions(db_path=db)

    assert tally["deferred_transient"] == 1
    assert tally["extracted"] == 1
    assert calls == ["NU", "MELI"]  # both attempted — the loop kept going

    conn = _connect(db)
    row_a = conn.execute(
        "SELECT decision_conditions, conditions_extracted_at FROM decisions WHERE id = ?",
        (decision_a,),
    ).fetchone()
    row_b = conn.execute(
        "SELECT decision_conditions, conditions_extracted_at FROM decisions WHERE id = ?",
        (decision_b,),
    ).fetchone()
    conn.close()
    # A: unstamped, so the existing unstamped→auto-retry design retries it
    # next run.
    assert row_a["decision_conditions"] is None
    assert row_a["conditions_extracted_at"] is None
    # B: stamped normally, unaffected by A's failure.
    assert row_b["conditions_extracted_at"] is not None
    assert json.loads(row_b["decision_conditions"])[0]["metric"] == "NPL 90d+"


def test_attach_conditions_hard_stop_propagates(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMSetupError (the CLI binary missing) is a hard, operator-actionable
    stop per llm.cli.is_hard_stop — it must still propagate out of
    attach_conditions and fail the stage loudly, unlike a transient failure."""
    from llm.cli import LLMSetupError

    _insert_artifact_decision(db, content_md=_FIVE_MIN_MD, ticker="NU")

    def setup_broken(prompt: str, **kwargs: object) -> object:
        raise LLMSetupError("Claude Code CLI ('claude') not found in PATH.")

    monkeypatch.setattr(dc, "call_llm_structured", setup_broken)
    with pytest.raises(LLMSetupError):
        attach_conditions(db_path=db)


def test_attach_conditions_budget_exceeded_propagates(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMBudgetExceeded (a hard per-purpose monthly cap) is likewise a hard
    stop — degrading would silently mask that spend is over budget."""
    from llm.cli import LLMBudgetExceeded

    _insert_artifact_decision(db, content_md=_FIVE_MIN_MD, ticker="NU")

    def budget_blocked(prompt: str, **kwargs: object) -> object:
        raise LLMBudgetExceeded("decision_conditions_extract: monthly cap exceeded")

    monkeypatch.setattr(dc, "call_llm_structured", budget_blocked)
    with pytest.raises(LLMBudgetExceeded):
        attach_conditions(db_path=db)


def test_attach_conditions_pre_migration_db(tmp_path: Path) -> None:
    # decisions table without the 0086 columns → best-effort unavailable.
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.commit()
    conn.close()
    tally = attach_conditions(db_path=path)
    assert tally["db_unavailable"] == 1


# ---------------------------------------------------------------------------
# Vocabulary + open-decision reads
# ---------------------------------------------------------------------------


def test_metric_vocabulary_reads_both_tables(db: Path) -> None:
    conn = _connect(db)
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES ('NU', 'NPL 90d+', 'percent')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('NU', '2026-03-31', 'Q1', 1, 7.4, 'percent', 1)"
    )
    # A definition with no facts must NOT appear (unresolvable at eval time).
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES ('NU', 'Ghost KPI', 'percent')"
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        "value, unit, source_doc_id) VALUES ('NU', '2026-03-31', 'Q1', 'revenue', 1.0, 'millions', 1)"
    )
    conn.commit()
    kpi_names, line_items = metric_vocabulary(conn, "nu")
    conn.close()
    assert kpi_names == ["NPL 90d+"]
    assert line_items == ["revenue"]


def test_load_open_decisions_filters(db: Path) -> None:
    conditions_json = json.dumps([_good_condition_obj()])
    conn = _connect(db)
    # Open + conditioned → returned.
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, "
        "source_artifact_id, made_at, created_at, decision_conditions) "
        "VALUES ('NU', 'trim', 20.0, 1, ?, ?, ?)",
        (_NOW, _NOW, conditions_json),
    )
    # Graded → excluded.
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at, "
        "created_at, decision_conditions, outcome_at) "
        "VALUES ('NU', 'hold', 2, ?, ?, ?, ?)",
        (_NOW, _NOW, conditions_json, _NOW),
    )
    # No conditions / empty conditions → excluded.
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at, "
        "created_at, decision_conditions) VALUES ('NU', 'add', 3, ?, ?, '[]')",
        (_NOW, _NOW),
    )
    conn.commit()

    open_decisions = load_open_decisions(conn, "NU")
    conn.close()
    assert len(open_decisions) == 1
    d = open_decisions[0]
    assert d.recommendation_kind == "trim"
    assert d.recommendation_value == 20.0
    assert len(d.conditions) == 1
    assert d.conditions[0].metric == "NPL 90d+"
    # Pre-0130 schema (no decided_by column) → None, read as advisor-authored.
    assert d.decided_by is None


# ---------------------------------------------------------------------------
# arming_status — the ratify route's zero-LLM receipt read (consequence
# receipts PR). Never calls the LLM extractor; only reports what a prior
# attach_conditions pass already found.
# ---------------------------------------------------------------------------


def test_arming_status_unextracted_when_never_ran(db: Path) -> None:
    conn = _connect(db)
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at) VALUES (1, 'NU', 'trim', 1, ?, ?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    assert dc.arming_status(1, db) == "unextracted"


def test_arming_status_armed_when_conditions_stamped(db: Path) -> None:
    conditions_json = json.dumps([_good_condition_obj()])
    conn = _connect(db)
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at, decision_conditions, conditions_extracted_at) "
        "VALUES (1, 'NU', 'trim', 1, ?, ?, ?, ?)",
        (_NOW, _NOW, conditions_json, _NOW),
    )
    conn.commit()
    conn.close()
    assert dc.arming_status(1, db) == "armed"


def test_arming_status_no_conditions_when_stamped_empty(db: Path) -> None:
    conn = _connect(db)
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at, decision_conditions, conditions_extracted_at) "
        "VALUES (1, 'NU', 'trim', 1, ?, ?, '[]', ?)",
        (_NOW, _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    assert dc.arming_status(1, db) == "no_conditions"


def test_arming_status_unknown_decision_degrades(db: Path) -> None:
    assert dc.arming_status(424242, db) == "unextracted"


def test_arming_status_missing_db_degrades(tmp_path: Path) -> None:
    assert dc.arming_status(1, tmp_path / "nope.db") == "unextracted"


def test_arming_status_never_calls_llm(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The zero-LLM contract: even an UNEXTRACTED row must not trigger
    extraction as a side effect of the status read."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("arming_status must never call the LLM extractor")

    monkeypatch.setattr(dc, "call_llm_structured", _boom)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at) VALUES (1, 'NU', 'trim', 1, ?, ?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    assert dc.arming_status(1, db) == "unextracted"


# ---------------------------------------------------------------------------
# load_all_open_decisions — the armed-falsifiers table's cross-ticker read,
# built from the SAME per-ticker accessor the trigger evaluates.
# ---------------------------------------------------------------------------


def test_load_all_open_decisions_spans_tickers(db: Path) -> None:
    conditions_json = json.dumps([_good_condition_obj()])
    conn = _connect(db)
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at, decision_conditions) "
        "VALUES ('NU', 'trim', 1, ?, ?, ?)",
        (_NOW, _NOW, conditions_json),
    )
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at, decision_conditions) "
        "VALUES ('MELI', 'add', 2, ?, ?, ?)",
        (_NOW, _NOW, conditions_json),
    )
    # No conditions — must not contribute a ticker/row.
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
        "made_at, created_at, decision_conditions) VALUES ('WIX', 'hold', 3, ?, ?, '[]')",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    decisions = dc.load_all_open_decisions(db)
    tickers = {d.ticker for d in decisions}
    assert tickers == {"NU", "MELI"}
    assert all(d.conditions for d in decisions)


def test_load_all_open_decisions_missing_db_degrades(tmp_path: Path) -> None:
    assert dc.load_all_open_decisions(tmp_path / "nope.db") == []
