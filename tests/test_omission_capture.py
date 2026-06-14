"""Errors of omission — making the pass GRADEABLE + feeding L8 (L11 PR2).

The grader now inverts the verdict for a pass (a passed name that ran away is the
omission error, the mirror of a hold), passes grade on a longer horizon, and the
omission ledger surfaces into build_calibration → the L1 pack, the L8 coach
grounding, and the calibration panel — all min-n gated.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ask.packs import load_packs
from calibration_coach import CoachInputs, grounding_block
from decision_calibration import (
    CalibrationStats,
    OmissionMiss,
    OmissionStats,
    build_calibration,
    omission_clause,
)
from pipeline.allocation_decisions_panel import _omission_block

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import grade_decisions as gd  # noqa: E402

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
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
    created_at DATETIME NOT NULL
);
"""


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _ins(
    db: Path,
    *,
    ticker: str,
    kind: str = "avoid",
    label: str | None = None,
    pct: float | None = None,
    notes: str | None = None,
    made_at: str = "2026-01-10T00:00:00",
    outcome_at: str | None = "2026-03-01T00:00:00",
) -> None:
    outcome_at_val = outcome_at if label is not None else None
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO decisions(ticker, recommendation_kind, made_at, outcome_at, "
        "outcome_label, outcome_pct, outcome_notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, kind, made_at, outcome_at_val, label, pct, notes, made_at),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# the inverted (omission) verdict
# ---------------------------------------------------------------------------


def test_avoid_verdict_is_omission_inverted() -> None:
    # A pass that ran away is the omission error; a decline/flat band vindicates it.
    assert gd._verdict(kind="avoid", pct_change=0.20, threshold_pct=0.05)[0] == "wrong"
    assert gd._verdict(kind="avoid", pct_change=0.08, threshold_pct=0.05)[0] == "mixed"
    assert gd._verdict(kind="avoid", pct_change=0.03, threshold_pct=0.05)[0] == "correct"
    assert gd._verdict(kind="avoid", pct_change=-0.30, threshold_pct=0.05)[0] == "correct"
    # And it is genuinely the INVERSE of hold (a held name that ran is fine/mixed,
    # a held name that fell hard is wrong).
    assert gd._verdict(kind="hold", pct_change=0.20, threshold_pct=0.05)[0] == "mixed"
    assert gd._verdict(kind="hold", pct_change=-0.20, threshold_pct=0.05)[0] == "wrong"
    assert gd._verdict(kind="hold", pct_change=0.01, threshold_pct=0.05)[0] == "correct"


# ---------------------------------------------------------------------------
# the omission ledger from build_calibration
# ---------------------------------------------------------------------------


def test_build_calibration_omission_stats(tmp_path: Path) -> None:
    db = tmp_path / "cal.db"
    _make_db(db)
    _ins(db, ticker="DODGE", label="correct", pct=-0.12)  # dodged a decliner
    _ins(db, ticker="NVDA", label="wrong", pct=0.50, notes="ran +50%")  # missed
    _ins(db, ticker="ABCD", label="wrong", pct=1.80, notes="ran +180%")  # bigger miss
    _ins(db, ticker="MEH", label="mixed", pct=0.08)
    _ins(db, ticker="OPEN", label=None)  # ungraded avoid — not counted
    _ins(db, ticker="HELD", kind="add", label="correct", pct=0.2)  # not an omission

    stats = build_calibration(db_path=db)
    assert stats is not None
    om = stats.omissions
    assert om is not None
    assert (om.graded, om.dodged, om.missed, om.mixed) == (4, 1, 2, 1)
    assert om.miss_rate == 0.5
    # Worst misses are the rallies, biggest first.
    assert [m.ticker for m in om.worst_misses] == ["ABCD", "NVDA"]
    assert om.worst_misses[0].outcome_pct == 1.80


def test_no_omissions_when_no_avoid_graded(tmp_path: Path) -> None:
    db = tmp_path / "cal.db"
    _make_db(db)
    _ins(db, ticker="HELD", kind="add", label="correct", pct=0.2)
    _ins(db, ticker="OPEN", kind="avoid", label=None)  # avoid logged, none graded
    stats = build_calibration(db_path=db)
    assert stats is not None and stats.omissions is None


# ---------------------------------------------------------------------------
# the shared, min-n-gated clause
# ---------------------------------------------------------------------------


def _om(graded: int, missed: int) -> OmissionStats:
    return OmissionStats(
        graded=graded,
        dodged=graded - missed,
        missed=missed,
        mixed=0,
        miss_rate=missed / graded if graded else None,
        worst_misses=[OmissionMiss("NVDA", "2026-01-01", 0.5, "ran")],
    )


def test_omission_clause_min_n_framed() -> None:
    assert omission_clause(None) is None
    assert omission_clause(_om(0, 0)) is None
    sparse = omission_clause(_om(3, 2))
    assert sparse is not None and "low confidence" in sparse and "NVDA +50%" in sparse
    confident = omission_clause(_om(12, 4))
    assert confident is not None and "low confidence" not in confident and "n=12" in confident


# ---------------------------------------------------------------------------
# the feedback surfaces: L8 coach grounding, L1 pack, the panel
# ---------------------------------------------------------------------------


def _stats_with_omissions() -> CalibrationStats:
    return CalibrationStats(
        total=20,
        graded=14,
        overall_hit_rate=0.5,
        by_conviction=[],
        action_mix={},
        reversals=[],
        reversals_vindicated=0,
        reversals_cost=0,
        time_to_outcome=[],
        omissions=_om(12, 5),
    )


def test_coach_grounding_carries_omissions() -> None:
    block = grounding_block(
        CoachInputs(calibration=_stats_with_omissions(), skill=None, closed_lessons=[], n_graded=14)
    )
    assert "OMISSIONS —" in block
    assert "ran away from you" in block
    assert "NVDA +50%" in block  # the named miss the coach can ground a bias in


def test_calibration_pack_carries_omissions(tmp_path: Path) -> None:
    db = tmp_path / "cal.db"
    _make_db(db)
    for i in range(11):  # clear the min-n floor so it reads confident
        _ins(db, ticker=f"DG{i}", label="correct", pct=-0.1)
    _ins(db, ticker="NVDA", label="wrong", pct=0.9, notes="ran")
    items = load_packs(["calibration"], db_path=db, focus_tickers=[])
    assert len(items) == 1
    text = str(items[0]["text"])
    assert "omissions —" in text
    assert "NVDA +90%" in text


def test_panel_omission_block_renders() -> None:
    html = _omission_block(_stats_with_omissions())
    assert "Errors of omission" in html
    assert "miss rate" in html
    assert "NVDA" in html
    # Hidden until something is graded.
    assert (
        _omission_block(
            _stats_with_omissions().__class__(
                total=0,
                graded=0,
                overall_hit_rate=None,
                by_conviction=[],
                action_mix={},
                reversals=[],
                reversals_vindicated=0,
                reversals_cost=0,
                time_to_outcome=[],
                omissions=None,
            )
        )
        == ""
    )


# ---------------------------------------------------------------------------
# grading end to end: an old pass grades inverted; a young pass is held open
# ---------------------------------------------------------------------------


def _repo_with_prices(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "data" / "historical" / "fmp").mkdir(parents=True)
    _make_db(repo / "data" / "portfolio.db")
    today = datetime.now(UTC).date()
    old = (today - timedelta(days=200)).isoformat()
    # NVDA ran +50% since the pass; AMD irrelevant (young pass is skipped pre-price).
    series = [{"date": today.isoformat(), "adjClose": 150.0}, {"date": old, "adjClose": 100.0}]
    (repo / "data" / "historical" / "fmp" / "NVDA_price_chart_10y_div_adj.json").write_text(
        json.dumps(series), encoding="utf-8"
    )
    return repo


def test_grade_pass_inverts_and_respects_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_prices(tmp_path)
    db = repo / "data" / "portfolio.db"
    today = datetime.now(UTC).date()
    old_made = (today - timedelta(days=200)).isoformat() + "T00:00:00"
    young_made = (today - timedelta(days=60)).isoformat() + "T00:00:00"
    _ins(db, ticker="NVDA", kind="avoid", label=None, made_at=old_made, outcome_at=None)
    _ins(db, ticker="AMD", kind="avoid", label=None, made_at=young_made, outcome_at=None)

    monkeypatch.setattr(sys, "argv", ["grade_decisions", "--repo-root", str(repo)])
    assert gd.main() == 0

    conn = _conn(db)
    try:
        rows = {r["ticker"]: r for r in conn.execute("SELECT * FROM decisions")}
    finally:
        conn.close()
    # The 200-day-old pass that ran +50% grades 'wrong' — the omission error.
    assert rows["NVDA"]["outcome_label"] == "wrong"
    assert rows["NVDA"]["outcome_pct"] == pytest.approx(0.5)
    # The 60-day-old pass is younger than the 180-day floor — left open + watchable.
    assert rows["AMD"]["outcome_at"] is None
    assert rows["AMD"]["outcome_label"] is None
