"""Tests for src/pipeline/you_said.py -- the "You said" strip (owner-ratified
design review, 2026-08-02).

Covers:
  * excerpt_text -- word-boundary truncation, whitespace collapse, blank/None
  * nearest_live_condition -- breached-at-attach skipped, future not_before
    skipped, first eligible condition wins, empty/all-skipped -> None
  * load_you_said -- pre-0130 schema (no decided_by) -> None; no owner row ->
    None; owner row present -> populated, latest-first
  * render_you_said_line -- verb/date/excerpt(+title on truncation)/
    conviction/condition/outcome all present when set, omitted when absent
  * render_you_said_strip -- degrades to k_empty + a capture-tray doorway
    chip when nothing is on file
"""

from __future__ import annotations

import json
import sqlite3

from decision_conditions import DecisionCondition
from pipeline.you_said import (
    YouSaid,
    excerpt_text,
    load_you_said,
    nearest_live_condition,
    render_you_said_line,
    render_you_said_strip,
)

# ---------------------------------------------------------------------------
# excerpt_text
# ---------------------------------------------------------------------------


def test_excerpt_text_short_unchanged() -> None:
    assert excerpt_text("the growth is decelerating") == "the growth is decelerating"


def test_excerpt_text_none_and_blank() -> None:
    assert excerpt_text(None) is None
    assert excerpt_text("   ") is None
    assert excerpt_text(123) is None  # not a string


def test_excerpt_text_collapses_whitespace() -> None:
    assert excerpt_text("line one\n\n  line two") == "line one line two"


def test_excerpt_text_truncates_on_word_boundary() -> None:
    raw = (
        "the growth is decelerating faster than management is willing to admit "
        "publicly and this no longer earns its weight in the book at this price"
    )
    out = excerpt_text(raw, max_len=90)
    assert out is not None
    assert out.endswith("…")
    assert len(out) <= 91  # 90 + ellipsis
    # Never cuts mid-word: strip the ellipsis and confirm it's a prefix of a
    # real word boundary in the source.
    prefix = out[:-1].rstrip()
    assert raw.startswith(prefix)
    assert not raw[len(prefix) : len(prefix) + 1].isalnum()


def test_excerpt_text_one_long_word_falls_back_to_hard_cut() -> None:
    raw = "x" * 200
    out = excerpt_text(raw, max_len=90)
    assert out is not None
    assert out.endswith("…")
    assert len(out) <= 91


# ---------------------------------------------------------------------------
# nearest_live_condition
# ---------------------------------------------------------------------------

_C1 = DecisionCondition(
    metric="NPL 90d+",
    metric_source="kpi",
    op="ge",
    threshold=7.0,
    unit="percent",
    for_periods=2,
    note="NPL above 7% for two quarters",
    breached_at_attach=True,
)
_C2 = DecisionCondition(
    metric="ARR",
    metric_source="kpi",
    op="ge",
    threshold=200.0,
    unit="millions",
    for_periods=1,
    note="ARR reaches $200M",
    not_before="2099-01-01",
)
_C3 = DecisionCondition(
    metric="Take rate",
    metric_source="financial",
    op="lt",
    threshold=3.0,
    unit="percent",
    for_periods=1,
    note="take rate compresses below 3%",
)


def test_nearest_live_condition_skips_breached_and_future_milestone() -> None:
    # C1 breached_at_attach; C2's not_before is in the future -> both skipped.
    assert nearest_live_condition((_C1, _C2, _C3), today="2026-08-02") is _C3


def test_nearest_live_condition_empty_is_none() -> None:
    assert nearest_live_condition(()) is None


def test_nearest_live_condition_all_skipped_is_none() -> None:
    assert nearest_live_condition((_C1, _C2), today="2026-08-02") is None


def test_nearest_live_condition_milestone_opens_once_reached() -> None:
    assert nearest_live_condition((_C2,), today="2099-06-01") is _C2


# ---------------------------------------------------------------------------
# load_you_said -- DB-backed
# ---------------------------------------------------------------------------

_PRE_0130_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    recommendation_kind TEXT NOT NULL,
    conviction TEXT,
    rationale_excerpt TEXT,
    made_at TEXT NOT NULL,
    decision_conditions TEXT,
    outcome_label TEXT
);
"""

_POST_0130_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    decided_by TEXT NOT NULL DEFAULT 'advisor',
    recommendation_kind TEXT NOT NULL,
    conviction TEXT,
    rationale_excerpt TEXT,
    made_at TEXT NOT NULL,
    decision_conditions TEXT,
    outcome_label TEXT
);
"""


def _conn(schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


def test_load_you_said_pre_0130_schema_is_none() -> None:
    conn = _conn(_PRE_0130_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, made_at) "
        "VALUES ('NU', 'trim', '2026-07-01T00:00:00')"
    )
    conn.commit()
    assert load_you_said(conn, "NU") is None


def test_load_you_said_no_owner_row_is_none() -> None:
    conn = _conn(_POST_0130_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (ticker, decided_by, recommendation_kind, made_at) "
        "VALUES ('NU', 'advisor', 'trim', '2026-07-01T00:00:00')"
    )
    conn.commit()
    assert load_you_said(conn, "NU") is None


def test_load_you_said_returns_latest_owner_row() -> None:
    conn = _conn(_POST_0130_SCHEMA)
    conditions_json = json.dumps([c.as_json_obj() for c in (_C3,)])
    conn.execute(
        "INSERT INTO decisions (ticker, decided_by, recommendation_kind, conviction, "
        "rationale_excerpt, made_at, decision_conditions, outcome_label) VALUES "
        "('NU', 'owner', 'trim', 'high', "
        "'the growth is decelerating faster than management is willing to admit', "
        "'2026-06-01T00:00:00', NULL, 'pending')"
    )
    conn.execute(
        "INSERT INTO decisions (ticker, decided_by, recommendation_kind, conviction, "
        "rationale_excerpt, made_at, decision_conditions, outcome_label) VALUES "
        "('NU', 'owner', 'add', 'medium', 'buying the dip', "
        "'2026-07-12T00:00:00', ?, 'wrong')",
        (conditions_json,),
    )
    conn.commit()
    you_said = load_you_said(conn, "nu")  # lowercase in -> uppercased lookup
    assert you_said is not None
    assert you_said.ticker == "NU"
    assert you_said.verb == "add"  # the latest by made_at, not insertion order
    assert you_said.made_at == "2026-07-12T00:00:00"
    assert you_said.conviction == "medium"
    assert you_said.excerpt == "buying the dip"
    assert you_said.outcome_label == "wrong"
    assert you_said.condition_text is not None
    assert "take rate" in you_said.condition_text


def test_load_you_said_blank_ticker_is_none() -> None:
    conn = _conn(_POST_0130_SCHEMA)
    assert load_you_said(conn, "  ") is None


# ---------------------------------------------------------------------------
# render_you_said_line -- pure render
# ---------------------------------------------------------------------------


def test_render_you_said_line_includes_all_present_fields() -> None:
    you_said = YouSaid(
        ticker="NU",
        verb="trim",
        made_at="2026-07-12T00:00:00",
        excerpt="buying the dip",
        rationale_full="buying the dip",
        conviction="high",
        condition_text="NPL 90d+ ≥ 7 percent",
        outcome_label="wrong",
    )
    html = render_you_said_line(you_said)
    assert "TRIM" in html
    assert "Jul 12" in html
    assert "buying the dip" in html
    assert "high conviction" in html
    assert "k-pill" in html
    assert "watching: NPL 90d+" in html
    assert 'class="k-pill k-pill-bad"' in html
    assert "wrong" in html


def test_render_you_said_line_truncated_excerpt_carries_full_title() -> None:
    full = "x" * 200
    you_said = YouSaid(
        ticker="NU",
        verb="add",
        made_at="2026-07-12T00:00:00",
        excerpt=excerpt_text(full),
        rationale_full=full,
        conviction=None,
        condition_text=None,
        outcome_label=None,
    )
    html = render_you_said_line(you_said)
    assert f'title="{full}"' in html


def test_render_you_said_line_omits_absent_fields() -> None:
    you_said = YouSaid(
        ticker="NU",
        verb="hold",
        made_at="2026-07-12T00:00:00",
        excerpt=None,
        rationale_full=None,
        conviction=None,
        condition_text=None,
        outcome_label=None,
    )
    html = render_you_said_line(you_said)
    assert "HOLD" in html
    assert "conviction" not in html
    assert "watching:" not in html
    assert "k-pill" not in html  # no conviction, no outcome pill


# ---------------------------------------------------------------------------
# render_you_said_strip -- degraded (D4) path
# ---------------------------------------------------------------------------


def test_render_you_said_strip_degrades_to_capture_doorway() -> None:
    conn = _conn(_POST_0130_SCHEMA)
    html = render_you_said_strip(conn, "NU")
    assert "No decision on file for NU" in html
    assert "k-empty" in html
    assert "data-open-capture-tray" in html
    assert 'data-capture-ticker="NU"' in html


def test_render_you_said_strip_blank_ticker_is_empty_string() -> None:
    conn = _conn(_POST_0130_SCHEMA)
    assert render_you_said_strip(conn, "  ") == ""
