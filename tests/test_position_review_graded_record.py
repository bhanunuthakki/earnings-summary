"""Tests for the live graded-sells base rate (PR5) — ``graded_sell_record``
and its interpolation into the behavioral guard's override text and the
governed-verdict prompt.

``decisions`` predates the 0059 stamp (``db.init_db()`` territory) and is
``_has_table``-guarded in later migrations, so the stamp+upgrade fixture never
creates it — hand-build the modern shape the query reads, mirroring
``tests/test_open_loops.py``'s ``_DECISIONS_DDL`` pattern (post-upgrade, so no
migration sees it mid-flight).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from advisor.position_review import (  # noqa: E402
    PreAnalysis,
    VerdictOutput,
    apply_behavioral_guard,
    graded_sell_record,
)

PRIOR_HEAD = "0059_kpi_facts_restatement"

# Mirrors tests/test_open_loops.py's _DECISIONS_DDL verbatim (the modern
# hand-DDL shape the band/guard/prompt all read).
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_decision(
    db_path: Path,
    *,
    ticker: str,
    recommendation_kind: str = "sell",
    outcome_label: str = "pending",
    decided_by: str = "owner",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, outcome_label, decided_by, "
            "made_at, created_at) VALUES (?, ?, ?, ?, '2026-06-01', '2026-06-01')",
            (ticker, recommendation_kind, outcome_label, decided_by),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# graded_sell_record
# --------------------------------------------------------------------------- #


def test_none_when_no_decisions_table(tmp_path: Path) -> None:
    # A bare DB with no schema at all — never raises, degrades to None.
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    assert graded_sell_record(bare) is None


def test_none_when_missing_db(tmp_path: Path) -> None:
    assert graded_sell_record(tmp_path / "missing" / "portfolio.db") is None
    assert graded_sell_record(None) is None


def test_none_when_nothing_graded_yet(db_path: Path) -> None:
    # Sells/trims exist but are all still pending — no line, not "0 of 0".
    _insert_decision(db_path, ticker="RBRK", outcome_label="pending")
    assert graded_sell_record(db_path) is None


def test_none_when_only_non_owner_or_non_sell_rows(db_path: Path) -> None:
    _insert_decision(db_path, ticker="NU", decided_by="advisor", outcome_label="wrong")
    _insert_decision(db_path, ticker="MELI", recommendation_kind="add", outcome_label="correct")
    assert graded_sell_record(db_path) is None


def test_counts_and_ticker_list(db_path: Path) -> None:
    # 5 of 8 wrong across a mix of sell/trim, owner rows only.
    for t in ("MU", "TSM", "NVDA", "AMZN", "GOOGL"):
        _insert_decision(db_path, ticker=t, outcome_label="wrong")
    _insert_decision(db_path, ticker="RBRK", recommendation_kind="trim", outcome_label="correct")
    _insert_decision(db_path, ticker="WIX", outcome_label="mixed")
    _insert_decision(db_path, ticker="NOW", recommendation_kind="trim", outcome_label="unfalsifiable")
    line = graded_sell_record(db_path)
    assert line == "Graded record on sells/trims: 5 of 8 wrong (AMZN, GOOGL, MU, NVDA, TSM)"


def test_excludes_non_owner_and_pending_rows_from_the_count(db_path: Path) -> None:
    _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    _insert_decision(db_path, ticker="TSM", outcome_label="correct")
    # Not owner-decided — excluded from both numerator and denominator.
    _insert_decision(db_path, ticker="DLO", decided_by="advisor", outcome_label="wrong")
    # Still pending — excluded.
    _insert_decision(db_path, ticker="BN", outcome_label="pending")
    line = graded_sell_record(db_path)
    assert line == "Graded record on sells/trims: 1 of 2 wrong (MU)"


def test_zero_wrong_omits_ticker_parenthetical(db_path: Path) -> None:
    _insert_decision(db_path, ticker="NU", outcome_label="correct")
    _insert_decision(db_path, ticker="MELI", recommendation_kind="trim", outcome_label="mixed")
    line = graded_sell_record(db_path)
    assert line == "Graded record on sells/trims: 0 of 2 wrong"
    assert "(" not in line


# --------------------------------------------------------------------------- #
# Guard prose carries the derived line, not the stale 3-ticker literal
# --------------------------------------------------------------------------- #

_PRE = PreAnalysis(
    ticker="RBRK",
    weight_pct=10.0,
    weight_source="live",
    market_value_usd=150_000.0,
    unrealized_pnl_usd=60_000.0,
    target_weight_pct=None,
    target_band=None,
    weight_vs_band="in_band",
    conviction_1_5=None,
    concentration_flag=False,
    thesis_present=True,
    verdict_label="Intact",
    key_driver="Net new subscription ARR cadence",
    break_rule_status="intact",
    tripped_rules=(),
    dcf_gap_pct=-10.0,
    npv_per_share=91.09,
    dcf_live_price=80.35,
    dcf_date="2026-06-15",
    at_price=None,
    mos_bar=0.30,
    valuation_verdict="fair",
    conviction_encoded=True,
    has_stance=True,
    has_decision_note=True,
    is_index_instrument=False,
)


def _trim_output() -> VerdictOutput:
    return VerdictOutput(
        verdict="trim",
        size="trim 2pp",
        reason="looks expensive",
        confidence="medium",
        behavioral_check="",
        suggested_expression="trim-to-target",
    )


def test_guard_override_cites_live_graded_line_not_hardcoded_names() -> None:
    graded_line = "Graded record on sells/trims: 5 of 8 wrong (MU, TSM, NVDA, AMZN, GOOGL)"
    guarded = apply_behavioral_guard(_PRE, _trim_output(), graded_line=graded_line)
    assert guarded.verdict == "hold"
    assert "MU, TSM, NVDA, AMZN, GOOGL" in guarded.behavioral_check
    # The stale hand-authored 3-ticker literal is gone from the codebase path.
    assert "MU/GOOGL/TSM" not in guarded.behavioral_check


def test_guard_override_degrades_to_generic_phrase_without_graded_line() -> None:
    guarded = apply_behavioral_guard(_PRE, _trim_output(), graded_line=None)
    assert guarded.verdict == "hold"
    assert "sell-winners-too-early pattern" in guarded.behavioral_check
    assert "MU" not in guarded.behavioral_check
    assert "(" not in guarded.behavioral_check.split("pattern", 1)[1].split(":")[0]


def test_guard_override_default_kwarg_also_degrades_generic() -> None:
    # No graded_line passed at all (existing callers) — same generic degrade.
    guarded = apply_behavioral_guard(_PRE, _trim_output())
    assert guarded.verdict == "hold"
    assert "sell-winners-too-early pattern" in guarded.behavioral_check
