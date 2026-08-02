"""Tests for src/pipeline/calibration_receipt.py -- the "when you've been
here before" calibration receipt (owner-ratified design review, 2026-08-02).

Covers:
  * compute_calibration_receipt -- right/wrong/mixed/unfalsifiable/ungraded
    bucketing, the <2-graded suppression (signal-quality bar), ticker
    scoping, blank verb, missing table degrades to None
  * render_calibration_receipt -- pluralization, the tooltip's cohort rows
  * render_calibration_receipt_for -- the combined compute+render, "" on
    suppression
"""

from __future__ import annotations

import sqlite3

import pytest

from pipeline.calibration_receipt import (
    CalibrationReceipt,
    compute_calibration_receipt,
    render_calibration_receipt,
    render_calibration_receipt_for,
)

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    recommendation_kind TEXT NOT NULL,
    made_at TEXT NOT NULL,
    outcome_label TEXT
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _seed(conn: sqlite3.Connection, rows: list[tuple[str, str, str, str | None]]) -> None:
    conn.executemany(
        "INSERT INTO decisions (ticker, recommendation_kind, made_at, outcome_label) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# compute_calibration_receipt
# ---------------------------------------------------------------------------


def test_suppressed_under_two_graded() -> None:
    conn = _conn()
    _seed(
        conn,
        [
            ("NU", "trim", "2026-07-01", "correct"),
            ("MELI", "trim", "2026-06-01", None),  # ungraded
            ("AAPL", "trim", "2026-05-01", "pending"),  # ungraded
        ],
    )
    assert compute_calibration_receipt(conn, action="trim") is None


def test_exactly_two_graded_renders() -> None:
    conn = _conn()
    _seed(
        conn,
        [
            ("NU", "trim", "2026-07-12", "correct"),
            ("MELI", "trim", "2026-06-01", "wrong"),
            ("AAPL", "trim", "2026-05-01", None),
            ("GOOG", "trim", "2026-04-01", "pending"),
        ],
    )
    receipt = compute_calibration_receipt(conn, action="TRIM")  # case-insensitive verb
    assert receipt is not None
    assert receipt.verb == "trim"
    assert receipt.total == 4
    assert receipt.right == 1
    assert receipt.wrong == 1
    assert receipt.mixed == 0
    assert receipt.unfalsifiable == 0
    assert receipt.ungraded == 2
    assert receipt.graded == 2


def test_mixed_and_unfalsifiable_bucketed_separately() -> None:
    conn = _conn()
    _seed(
        conn,
        [
            ("NU", "add", "2026-07-01", "mixed"),
            ("MELI", "add", "2026-06-01", "unfalsifiable"),
        ],
    )
    receipt = compute_calibration_receipt(conn, action="add")
    assert receipt is not None
    assert receipt.mixed == 1
    assert receipt.unfalsifiable == 1
    assert receipt.graded == 2


def test_ticker_scoped_cohort_excludes_other_tickers() -> None:
    conn = _conn()
    _seed(
        conn,
        [
            ("NU", "trim", "2026-07-01", "correct"),
            ("NU", "trim", "2026-06-01", "wrong"),
            ("MELI", "trim", "2026-05-01", "correct"),
        ],
    )
    receipt = compute_calibration_receipt(conn, action="trim", ticker="nu")
    assert receipt is not None
    assert receipt.ticker == "NU"
    assert receipt.total == 2
    assert receipt.right == 1
    assert receipt.wrong == 1


def test_blank_verb_is_none() -> None:
    conn = _conn()
    assert compute_calibration_receipt(conn, action="  ") is None


def test_missing_table_degrades_to_none() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert compute_calibration_receipt(conn, action="trim") is None


def test_limit_caps_cohort_size() -> None:
    conn = _conn()
    rows = [("NU", "trim", f"2026-01-{d:02d}", "correct") for d in range(1, 11)]
    _seed(conn, rows)
    receipt = compute_calibration_receipt(conn, action="trim", limit=3)
    assert receipt is not None
    assert receipt.total == 3
    # Most-recent-first: 01-10, 01-09, 01-08.
    assert [d for _, d, _ in receipt.rows] == ["2026-01-10", "2026-01-09", "2026-01-08"]


# ---------------------------------------------------------------------------
# render_calibration_receipt
# ---------------------------------------------------------------------------


def test_render_matches_the_worked_example() -> None:
    receipt = CalibrationReceipt(
        verb="trim",
        ticker=None,
        total=4,
        right=2,
        wrong=1,
        mixed=0,
        unfalsifiable=0,
        ungraded=1,
        rows=(("NU", "2026-07-01", "correct"), ("MELI", "2026-06-01", "wrong")),
    )
    html = render_calibration_receipt(receipt)
    assert "Your last 4 trims: 2 right, 1 wrong, 1 ungraded." in html
    assert 'class="cr-receipt"' in html
    assert "NU 2026-07-01: correct" in html


@pytest.mark.parametrize(
    ("verb", "plural"),
    [
        ("trim", "trims"),
        ("add", "adds"),
        ("hold", "holds"),
        ("buy", "buys"),
        ("sell", "sells"),
        ("pass", "passes"),
        ("watch", "watches"),
        ("promote", "promotes"),
    ],
)
def test_verb_pluralization(verb: str, plural: str) -> None:
    receipt = CalibrationReceipt(
        verb=verb,
        ticker=None,
        total=2,
        right=1,
        wrong=1,
        mixed=0,
        unfalsifiable=0,
        ungraded=0,
        rows=(),
    )
    assert f"last 2 {plural}:" in render_calibration_receipt(receipt)


def test_render_portfolio_scope_row_shown_as_portfolio_in_title() -> None:
    receipt = CalibrationReceipt(
        verb="pass",
        ticker=None,
        total=2,
        right=2,
        wrong=0,
        mixed=0,
        unfalsifiable=0,
        ungraded=0,
        rows=((None, "2026-07-01", "correct"), (None, "2026-06-01", "correct")),
    )
    html = render_calibration_receipt(receipt)
    assert "PORTFOLIO 2026-07-01: correct" in html


# ---------------------------------------------------------------------------
# render_calibration_receipt_for
# ---------------------------------------------------------------------------


def test_render_for_returns_empty_string_when_suppressed() -> None:
    conn = _conn()
    _seed(conn, [("NU", "trim", "2026-07-01", "correct")])
    assert render_calibration_receipt_for(conn, action="trim") == ""


def test_render_for_returns_line_when_signal_present() -> None:
    conn = _conn()
    _seed(
        conn,
        [
            ("NU", "trim", "2026-07-01", "correct"),
            ("MELI", "trim", "2026-06-01", "wrong"),
        ],
    )
    html = render_calibration_receipt_for(conn, action="trim")
    assert "Your last 2 trims:" in html
