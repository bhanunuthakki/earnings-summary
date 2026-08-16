from __future__ import annotations

import sqlite3

from pipeline.work_os_earnings import load_latest_earnings_readouts


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            fiscal_period_type TEXT,
            period_end TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            scope TEXT NOT NULL DEFAULT 'ticker',
            purpose TEXT NOT NULL,
            fiscal_period TEXT,
            content_md TEXT,
            generated_at TEXT,
            superseded_by_id INTEGER
        );
        """
    )
    return conn


def test_latest_readouts_prefer_current_quarter_artifacts_and_label_the_period() -> None:
    conn = _connection()
    conn.executemany(
        "INSERT INTO transcripts VALUES (?, ?, ?, ?, ?)",
        [
            (1, "WIX", "Q1", "2026-03-31", 1),
            (2, "WIX", "Q2", "2026-06-30", 1),
            (3, "NU", "Q2", "2026-06-30", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO llm_artifacts VALUES (?, ?, 'ticker', ?, ?, ?, ?, ?)",
        [
            (10, "WIX", "post_earnings_readout", "2026-03-31", "old", "2026-05-01T00:00:00Z", None),
            (
                11,
                "WIX",
                "post_earnings_readout",
                "2026-06-30",
                "current",
                "2026-08-14T11:48:07Z",
                None,
            ),
            (
                12,
                "WIX",
                "post_earnings_readout",
                "2026-06-30",
                "superseded",
                "2026-08-13T00:00:00Z",
                11,
            ),
            (13, "NU", "post_earnings_readout", "2026-06-30", "   ", "2026-08-14T00:00:00Z", None),
            (14, "WIX", "pre_earnings_brief", "2026-08-05", "prep", "2026-08-03T00:00:00Z", None),
        ],
    )

    projection = load_latest_earnings_readouts(
        conn,
        ["wix", "NU"],
        coverage_roles={"WIX": "evaluation", "NU": "portfolio"},
    )

    assert projection.status == "ok"
    assert projection.warnings == []
    assert list(projection.readouts) == ["WIX"]
    readout = projection.readouts["WIX"]
    assert readout.artifact_id == 11
    assert readout.fiscal_period == "2026-06-30"
    assert readout.period_label == "Q2 · Jun 2026"
    assert readout.generated_at == "2026-08-14T11:48:07Z"
    assert readout.route == "/api/peek/earnings-readout?ticker=WIX&artifact_id=11"
    assert readout.coverage_role == "evaluation"


def test_latest_readouts_fail_closed_when_artifact_storage_is_absent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    projection = load_latest_earnings_readouts(conn, ["NU"])

    assert projection.status == "degraded"
    assert projection.readouts == {}
    assert projection.warnings == ["earnings_readout_projection_unavailable"]


def test_period_label_does_not_infer_fiscal_year_from_period_end() -> None:
    conn = _connection()
    conn.execute("INSERT INTO transcripts VALUES (1, 'VEEV', 'Q1', '2026-04-30', 1)")
    conn.execute(
        "INSERT INTO llm_artifacts VALUES "
        "(21, 'VEEV', 'ticker', 'post_earnings_readout', '2026-04-30', "
        "'readout', '2026-06-01T00:00:00Z', NULL)"
    )

    readout = load_latest_earnings_readouts(conn, ["VEEV"]).readouts["VEEV"]

    assert readout.period_label == "Q1 · Apr 2026"
