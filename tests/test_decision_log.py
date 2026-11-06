"""The decision log separates owner decisions from imports and advisor rows."""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from pipeline import decision_log
from pipeline.decision_log import (
    build_decision_log,
    render_decision_log,
    render_decision_log_panel,
)
from ui.prose import prose_headline

_LIVE = "2026-09-15T06:50:19"


def _decision(conn: sqlite3.Connection, ticker: str, kind: str, **fields: object) -> int:
    row: dict[str, object] = {
        "ticker": ticker,
        "recommendation_kind": kind,
        "rationale_excerpt": f"{ticker} reason with enough words to stand as a full sentence.",
        "made_at": _LIVE,
        "created_at": _LIVE,
        "decided_by": "owner",
        **fields,
    }
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cursor = conn.execute(f"INSERT INTO decisions ({names}) VALUES ({marks})", tuple(row.values()))
    assert cursor.lastrowid is not None
    return cursor.lastrowid


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    connection = sqlite3.connect(migrated_db(tmp_path / "portfolio.db"))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def test_same_day_covered_calls_are_one_decision(conn: sqlite3.Connection) -> None:
    for ticker in ("VEEV", "RBRK", "NOW"):
        _decision(
            conn,
            ticker,
            "sell",
            instrument="covered_call",
            rationale_excerpt=f"Sold {ticker} calls. Purpose: take upside risk off the book today.",
        )

    log = build_decision_log(conn)

    assert len(log.recorded) == 1
    entry = log.recorded[0]
    assert [leg.ticker for leg in entry.legs] == ["NOW", "RBRK", "VEEV"]
    assert entry.why == "Take upside risk off the book today."
    assert len(entry.detail) == 3


def test_checkpoint_joins_legs_with_different_actions(conn: sqlite3.Connection) -> None:
    sale_time, purchase_time = "2026-08-14T08:46:19", "2026-08-14T08:46:25"
    sale = _decision(conn, "WIX", "sell", made_at=sale_time, created_at=sale_time)
    purchase = _decision(conn, "AVDV", "add", made_at=purchase_time, created_at=purchase_time)
    conn.execute(
        "INSERT INTO owner_decision_checkpoints (id, user_id, source_channel, source_event_id, "
        "checkpoint_schema_version, payload_sha256, payload_json, retrospective, created_at, "
        "confirmed_at) VALUES (1, 'u', 's', 'e', 1, ?, '{}', 0, 't', 't')",
        ("a" * 64,),
    )
    for ordinal, decision_id in enumerate((sale, purchase)):
        conn.execute(
            "INSERT INTO owner_decision_checkpoint_decisions "
            "(checkpoint_id, leg_id, leg_ordinal, decision_id, recorded_at) "
            "VALUES (1, ?, ?, ?, 't')",
            (f"leg-{ordinal}", ordinal, decision_id),
        )

    entries = build_decision_log(conn).recorded

    assert len(entries) == 1
    assert [(leg.action, leg.ticker) for leg in entries[0].legs] == [
        ("Sell", "WIX"),
        ("Add", "AVDV"),
    ]


def test_rows_are_classified_without_losing_any(conn: sqlite3.Connection) -> None:
    _decision(conn, "VDE", "initiate")
    _decision(conn, "MU", "sell", made_at="2026-01-15T00:00:00", created_at="2026-07-02T04:02:55.1")
    # Same import second, but the import had no original date to keep.
    _decision(
        conn, "TORM", "sell", made_at="2026-07-02T04:02:55", created_at="2026-07-02T04:02:55.2"
    )
    _decision(conn, "VOO", "buy", rationale_excerpt=None)
    _decision(conn, "VOO", "buy", rationale_excerpt=None)
    _decision(conn, "V", "pass", rationale_excerpt="Visa. STUB: needs user-authored thesis.")
    _decision(conn, "NVDA", "trim", decided_by="advisor", source_memo_id=1)
    _decision(conn, "FRVO", "avoid", decided_by="advisor", source_lens="manual_pass")

    log = build_decision_log(conn)

    assert sorted(entry.legs[0].ticker for entry in log.recorded) == ["FRVO", "VDE"]
    assert [entry.legs[0].ticker for entry in log.hindsight] == ["MU", "TORM"]
    assert [entry.date_text for entry in log.hindsight] == ["Jan 2026", "Date unknown"]
    assert log.without_reason == (("V", "Pass", 1), ("VOO", "Buy", 2))
    assert log.advisor_count == 1


def test_open_decision_note_extends_or_adds_a_decision(conn: sqlite3.Connection) -> None:
    linked = _decision(conn, "FRVO", "avoid", source_lens="manual_pass")
    for ticker, decision_id, body in (
        ("FRVO", linked, "# FRVO pass\n\nFull reasoning from the owner."),
        ("MBGL", None, "# MBGL — track for now\n\nOwner chose to keep tracking this name."),
    ):
        conn.execute(
            "INSERT INTO analyst_notes (user_id, ticker, kind, status, body, source, created_at, "
            "updated_at, decision_id) VALUES ('u', ?, 'decision', 'open', ?, 'manual', ?, ?, ?)",
            (ticker, body, _LIVE, _LIVE, decision_id),
        )

    entries = {entry.detail[0][0]: entry for entry in build_decision_log(conn).recorded}

    assert "Full reasoning" in entries["FRVO"].detail[0][1]
    assert entries["MBGL"].headline == "MBGL — track for now"
    assert entries["MBGL"].why == "Owner chose to keep tracking this name."


def test_outcomes_show_real_percentages_and_hide_absent_evidence(conn: sqlite3.Connection) -> None:
    _decision(conn, "MU", "buy", outcome_label="correct", outcome_pct=4.68)
    _decision(conn, "QCOM", "watch", outcome_label="unfalsifiable", outcome_pct=0.0)
    _decision(conn, "BKNG", "add", outcome_label="mixed", outcome_pct=0.0)
    _decision(conn, "VDE", "initiate", outcome_label="pending", user_action_kind="ignored")

    html = render_decision_log(build_decision_log(conn))

    assert "+468%" in html
    assert "Not testable" in html
    assert "Too early" in html
    assert "+0%" not in html
    assert "ignored" not in html.casefold()


def test_stored_text_is_escaped(conn: sqlite3.Connection) -> None:
    _decision(
        conn, "NU", "add", rationale_excerpt="<script>alert(1)</script> adds to the position."
    )

    html = render_decision_log(build_decision_log(conn))

    assert "<script>alert" not in html


def test_missing_tables_show_the_empty_state(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    sqlite3.connect(db_path).close()

    assert "No owner decisions are recorded yet." in render_decision_log_panel(db_path)


def test_date_has_no_zero_padding_and_needs_no_platform_format(conn: sqlite3.Connection) -> None:
    _decision(
        conn,
        "META",
        "buy",
        made_at="2026-01-05T09:30:00+00:00",
        created_at="2026-01-05T09:30:00+00:00",
    )

    assert build_decision_log(conn).recorded[0].date_text == "Jan 5, 2026"
    # The canonical host is Windows, where strftime rejects the "%-d" form.
    assert "%-" not in inspect.getsource(decision_log)


def test_headline_keeps_meaning_and_removes_markup() -> None:
    assert prose_headline("# NU: open questions\n\nBody text.") == "NU: open questions"
    assert (
        prose_headline("**Bet** on sub-ARR >25% with RBRK 9.5% -> ~7% over the coming years now.")
        == "Bet on sub-ARR >25% with RBRK 9.5% -> ~7% over the coming years now."
    )
    assert prose_headline("Short start. Second sentence adds the real reason here today.") == (
        "Short start. Second sentence adds the real reason here today."
    )
    assert prose_headline("word " * 60, limit=20).endswith("…")
    assert prose_headline("# Title\n\nFirst real sentence.", skip_title=True) == (
        "First real sentence."
    )
