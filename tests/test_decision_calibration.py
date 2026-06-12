"""S15 PR2(a) — decision-calibration analytics + the panel section.

Deterministic SQL only (no LLM, no network): hit rate by conviction,
action mix + reversal vindication, time-to-outcome. The renderer rides
``compose_decisions_page`` with an added optional param — the existing
no-calibration call shape stays valid (pinned here).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from decision_calibration import build_calibration
from integrations.portfolio_tracker_client import LivePortfolio
from pipeline.allocation_decisions_panel import compose_decisions_page

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    created_at DATETIME NOT NULL
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "cal.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _insert(
    db: Path,
    *,
    ticker: str = "NU",
    kind: str = "trim",
    conviction: str | None = "high",
    action: str | None = None,
    outcome_label: str | None = None,
    made_at: str = "2026-01-10T09:00:00",
    outcome_at: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, user_action_kind, "
        "made_at, outcome_at, outcome_label, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, kind, conviction, action, made_at, outcome_at, outcome_label, made_at),
    )
    conn.commit()
    conn.close()


def test_substrate_absent_returns_none(tmp_path: Path) -> None:
    assert build_calibration(db_path=tmp_path / "missing.db") is None
    empty = tmp_path / "no_table.db"
    sqlite3.connect(str(empty)).close()
    assert build_calibration(db_path=empty) is None


def test_empty_ledger_returns_zeroed_stats(db: Path) -> None:
    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.total == 0 and stats.graded == 0
    assert stats.overall_hit_rate is None
    assert stats.by_conviction == []


def test_hit_rate_by_conviction_and_denominator(db: Path) -> None:
    # high: 2 correct, 1 wrong, 1 pending → hit 2/3 (pending never scored)
    _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="wrong", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction="high", outcome_label="pending")
    # unstated conviction: 1 mixed, 1 unfalsifiable → hit 0/1
    _insert(db, conviction=None, outcome_label="mixed", outcome_at="2026-02-01T00:00:00")
    _insert(db, conviction=None, outcome_label="unfalsifiable", outcome_at="2026-02-01T00:00:00")

    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.total == 6
    assert stats.graded == 4  # 2 correct + 1 wrong + 1 mixed
    assert stats.overall_hit_rate == pytest.approx(0.5)

    by = {b.conviction: b for b in stats.by_conviction}
    assert set(by) == {"high", "unstated"}
    high = by["high"]
    assert (high.graded, high.correct, high.wrong, high.ungraded) == (3, 2, 1, 1)
    assert high.hit_rate == pytest.approx(2 / 3)
    unstated = by["unstated"]
    assert unstated.graded == 1 and unstated.mixed == 1
    assert unstated.ungraded == 1  # unfalsifiable counts ungraded
    assert unstated.hit_rate == pytest.approx(0.0)
    # Ordered high → unstated per CONVICTION_ORDER
    assert [b.conviction for b in stats.by_conviction] == ["high", "unstated"]


def test_reversals_and_action_mix(db: Path) -> None:
    _insert(db, action="followed", outcome_label="correct", outcome_at="2026-02-01T00:00:00")
    _insert(db, action="reversed", outcome_label="wrong", outcome_at="2026-02-01T00:00:00",
            made_at="2026-01-20T09:00:00")  # fmt: skip
    _insert(db, action="reversed", outcome_label="correct", outcome_at="2026-02-01T00:00:00",
            made_at="2026-01-15T09:00:00")  # fmt: skip
    _insert(db, action="reversed", outcome_label="pending", made_at="2026-01-25T09:00:00")
    _insert(db, action=None, outcome_label="pending")

    stats = build_calibration(db_path=db)
    assert stats is not None
    assert stats.action_mix["followed"] == 1
    assert stats.action_mix["reversed"] == 3
    assert stats.action_mix["unacted"] == 1
    assert len(stats.reversals) == 3
    # newest-first by made_at
    assert [r.made_at for r in stats.reversals] == ["2026-01-25", "2026-01-20", "2026-01-15"]
    assert stats.reversals[0].vindicated is None  # pending → unresolved
    assert stats.reversals[0].outcome_label is None
    assert stats.reversals[1].vindicated is True  # call graded wrong → override right
    assert stats.reversals[2].vindicated is False  # call graded correct → override cost
    assert stats.reversals_vindicated == 1
    assert stats.reversals_cost == 1


def test_time_to_outcome(db: Path) -> None:
    _insert(db, kind="trim", outcome_label="correct",
            made_at="2026-01-01T00:00:00", outcome_at="2026-01-11T00:00:00")  # fmt: skip
    _insert(db, kind="trim", outcome_label="wrong",
            made_at="2026-01-01T00:00:00", outcome_at="2026-01-31T00:00:00")  # fmt: skip
    _insert(db, kind="add", outcome_label="mixed",
            made_at="2026-01-01T00:00:00", outcome_at="2026-01-06T00:00:00")  # fmt: skip
    _insert(db, kind="add", outcome_label="pending")  # ungraded — excluded

    stats = build_calibration(db_path=db)
    assert stats is not None
    timing = {t.kind: t for t in stats.time_to_outcome}
    assert timing["trim"].n == 2
    assert timing["trim"].avg_days == pytest.approx(20.0)
    assert timing["trim"].median_days == pytest.approx(20.0)
    assert timing["add"].n == 1
    assert timing["add"].avg_days == pytest.approx(5.0)
    # descending n
    assert [t.kind for t in stats.time_to_outcome] == ["trim", "add"]


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _offline() -> LivePortfolio:
    return LivePortfolio(available=False, api_url="http://x", error="down")


def test_panel_renders_calibration_section(db: Path) -> None:
    _insert(db, conviction="high", action="reversed", outcome_label="wrong",
            outcome_at="2026-02-01T00:00:00")  # fmt: skip
    stats = build_calibration(db_path=db)
    assert stats is not None
    html = compose_decisions_page([], [], _offline(), None, calibration=stats)
    assert "<h2>Decision calibration</h2>" in html
    assert "hit rate" in html
    assert "reversal vindicated" in html
    assert "Reversals" in html
    assert 'class="adc-kpis"' in html


def test_panel_empty_state_and_backward_call_shape(db: Path) -> None:
    stats = build_calibration(db_path=db)
    assert stats is not None
    html = compose_decisions_page([], [], _offline(), None, calibration=stats)
    assert "No decisions recorded yet" in html
    # Pre-S15 call shape (no calibration arg) still renders, without the section.
    legacy = compose_decisions_page([], [], _offline(), None)
    assert "<h2>Decision calibration</h2>" not in legacy
