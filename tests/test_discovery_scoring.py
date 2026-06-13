"""The Discovery rule's scoring engine (src/discovery/scoring.py) + the
discovery_sources roster (src/discovery/sources.py).

The contract this pins (interaction_paradigm S6): a candidate scores by a
WEIGHTED sum of typed, dated signals through the source-weight registry, never
an equal-weight count — so the ranking MOVES when a weight moves. Plus the
four shaping rules: the new-vs-add action asymmetry, the investor-only clamp,
the super-linear corroboration bump, and recency decay.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from discovery.scoring import (
    ENTRY_THRESHOLD,
    INVESTOR_ONLY_CAP,
    adjacency_signal,
    investor_signal,
    passes_entry_threshold,
    score_candidate,
    score_evidence_line,
    screen_signal,
)
from discovery.sources import (
    list_sources,
    load_source_map,
    set_source_active,
    set_source_cik,
    set_source_weight,
    weight_for,
)

_NOW = datetime(2026, 6, 13, 12, 0, 0)


def _fresh(days_ago: int = 0) -> datetime:
    return _NOW - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# weighted sum + THE guard: ranking moves when a weight moves
# ---------------------------------------------------------------------------


def test_weighted_sum_replaces_count() -> None:
    sigs = [
        screen_signal("quality_compounder", weight=1.0, observed_at=_NOW, detail="x"),
        adjacency_signal("watchlist", weight=0.5, observed_at=_NOW, occurrences=1.0),
    ]
    result = score_candidate(sigs, now=_NOW)
    # 1.0*1.0*1.0 (screen) + 0.5*1.0*1.0 (adjacency) = 1.5, NOT a count of 2.
    assert result.score == pytest.approx(1.5)
    assert result.why["terms"] == {"screen": pytest.approx(1.0), "adjacency": pytest.approx(0.5)}
    assert result.why["fundamental"] == pytest.approx(1.5)


def test_ranking_moves_when_a_weight_moves() -> None:
    """The keystone guard: two names tie under equal weights; raising one
    source's weight re-ranks them — impossible under the old integer count."""
    a = [screen_signal("screen_a", weight=1.0, observed_at=_NOW)]
    b = [screen_signal("screen_b", weight=1.0, observed_at=_NOW)]
    assert score_candidate(a, now=_NOW).score == score_candidate(b, now=_NOW).score

    a_heavier = [screen_signal("screen_a", weight=1.6, observed_at=_NOW)]
    assert score_candidate(a_heavier, now=_NOW).score > score_candidate(b, now=_NOW).score


# ---------------------------------------------------------------------------
# action asymmetry: new >> add, gap WIDER for low-turnover funds
# ---------------------------------------------------------------------------


def _inv(action: str, tags: tuple[str, ...]) -> float:
    sig = investor_signal(
        "fund",
        weight=1.0,
        observed_at=_NOW,
        pct_of_book=0.02,  # strength 1.0
        action=action,
        style_tags=tags,
    )
    return score_candidate([sig], now=_NOW).why["investor_raw"]  # type: ignore[return-value]


def test_new_position_outweighs_an_add() -> None:
    assert _inv("new", ("hedge",)) > _inv("add", ("hedge",))
    assert _inv("new", ("long_only", "low_turnover")) > _inv("add", ("long_only", "low_turnover"))


def test_add_gap_is_wider_for_low_turnover_funds() -> None:
    """A low-turnover long-only ADD is near-noise; a hedge-fund ADD still
    carries momentum signal — so the new→add drop is steeper for low-turnover."""
    lt_new, lt_add = (
        _inv("new", ("long_only", "low_turnover")),
        _inv("add", ("long_only", "low_turnover")),
    )
    hf_new, hf_add = _inv("new", ("hedge",)), _inv("add", ("hedge",))
    assert lt_new == pytest.approx(hf_new)  # a NEW position is loud for both
    assert lt_add < hf_add  # but the low-turnover ADD is quieter
    assert (lt_new - lt_add) > (hf_new - hf_add)


def test_trim_and_exit_are_not_discovery_signals() -> None:
    for action in ("trim", "exit"):
        sig = investor_signal(
            "fund",
            weight=1.0,
            observed_at=_NOW,
            pct_of_book=0.05,
            action=action,
            style_tags=("hedge",),
        )
        assert score_candidate([sig], now=_NOW).score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# clamp: an investor-only name surfaces but can't top the queue
# ---------------------------------------------------------------------------


def test_investor_only_name_is_clamped() -> None:
    # Three big funds, each a large new position — a huge raw investor term.
    sigs = [
        investor_signal(
            f"fund{i}",
            weight=1.0,
            observed_at=_NOW,
            pct_of_book=0.06,
            action="new",
            style_tags=("hedge",),
        )
        for i in range(3)
    ]
    result = score_candidate(sigs, now=_NOW)
    assert result.why["clamped"] is True
    assert result.score == pytest.approx(INVESTOR_ONLY_CAP)


def test_fundamental_corroboration_lifts_the_clamp() -> None:
    """The SAME investor signals, but now with one screen pass — no longer
    clamped, and it outranks the investor-only version."""
    inv = [
        investor_signal(
            f"fund{i}",
            weight=1.0,
            observed_at=_NOW,
            pct_of_book=0.06,
            action="new",
            style_tags=("hedge",),
        )
        for i in range(3)
    ]
    clamped = score_candidate(inv, now=_NOW)
    with_screen = score_candidate(
        [*inv, screen_signal("quality_compounder", weight=1.0, observed_at=_NOW)], now=_NOW
    )
    assert with_screen.why["clamped"] is False
    assert with_screen.score > clamped.score


# ---------------------------------------------------------------------------
# corroboration: 2+ funds is super-linear
# ---------------------------------------------------------------------------


def test_corroboration_is_super_linear() -> None:
    def two_funds() -> float:
        sigs = [
            investor_signal(
                f"f{i}",
                weight=0.5,
                observed_at=_NOW,
                pct_of_book=0.02,
                action="new",
                style_tags=("hedge",),
            )
            for i in range(2)
        ]
        # add a screen so the clamp doesn't mask the corroboration math
        sigs.append(screen_signal("s", weight=2.0, observed_at=_NOW))
        return score_candidate(sigs, now=_NOW).why["investor"]  # type: ignore[return-value]

    def one_fund() -> float:
        sigs = [
            investor_signal(
                "f0",
                weight=0.5,
                observed_at=_NOW,
                pct_of_book=0.02,
                action="new",
                style_tags=("hedge",),
            ),
            screen_signal("s", weight=2.0, observed_at=_NOW),
        ]
        return score_candidate(sigs, now=_NOW).why["investor"]  # type: ignore[return-value]

    # Two funds' raw term is 2x one fund's; the super-linear bump makes the
    # final investor term MORE than 2x (n**0.4 multiplier, n=2 -> 1.32).
    assert two_funds() > 2.0 * one_fund()


def test_corroboration_multiplier_recorded() -> None:
    sigs = [
        investor_signal(
            f"f{i}",
            weight=0.5,
            observed_at=_NOW,
            pct_of_book=0.02,
            action="new",
            style_tags=("hedge",),
        )
        for i in range(3)
    ]
    sigs.append(screen_signal("s", weight=2.0, observed_at=_NOW))
    corr = score_candidate(sigs, now=_NOW).why["corroboration"]
    assert corr["n_funds"] == 3  # type: ignore[index]
    assert corr["multiplier"] > 1.0  # type: ignore[index]


# ---------------------------------------------------------------------------
# recency decay + entry threshold + evidence line
# ---------------------------------------------------------------------------


def test_recency_decay_fades_old_signals() -> None:
    fresh = score_candidate(
        [
            investor_signal(
                "f",
                weight=1.0,
                observed_at=_fresh(0),
                pct_of_book=0.02,
                action="new",
                style_tags=("hedge",),
            )
        ],
        now=_NOW,
    )
    old = score_candidate(
        [
            investor_signal(
                "f",
                weight=1.0,
                observed_at=_fresh(240),
                pct_of_book=0.02,
                action="new",
                style_tags=("hedge",),
            )
        ],
        now=_NOW,
    )
    assert old.score < fresh.score
    # 13F half-life is 120d, so ~240d (two half-lives) is ~1/4 strength.
    assert old.score == pytest.approx(fresh.score * 0.25, abs=0.05)


def test_entry_threshold() -> None:
    assert not passes_entry_threshold(ENTRY_THRESHOLD - 0.01)
    assert passes_entry_threshold(ENTRY_THRESHOLD)
    weak = score_candidate([screen_signal("s", weight=1.0, observed_at=_NOW)], now=_NOW)
    assert not weak.passes_entry  # one screen alone never clears the bar
    strong = score_candidate(
        [
            screen_signal("s", weight=1.0, observed_at=_NOW),
            adjacency_signal("watchlist", weight=1.0, observed_at=_NOW),
        ],
        now=_NOW,
    )
    assert strong.passes_entry


def test_evidence_line_is_human() -> None:
    sigs = [
        screen_signal("quality_compounder", weight=1.0, observed_at=_NOW),
        adjacency_signal("watchlist", weight=1.0, observed_at=_NOW),
    ]
    line = score_evidence_line(score_candidate(sigs, now=_NOW).why)
    assert line.startswith("score ")
    assert "screens" in line and "adjacency" in line


# ---------------------------------------------------------------------------
# discovery_sources roster CRUD + the weight-edit -> re-rank loop
# ---------------------------------------------------------------------------

_SOURCES_DDL = """
CREATE TABLE discovery_sources (
    source_key TEXT PRIMARY KEY,
    signal_class TEXT NOT NULL,
    display_name TEXT NOT NULL,
    base_weight FLOAT NOT NULL DEFAULT 1.0,
    tier TEXT NOT NULL DEFAULT 'structural',
    style_tags TEXT,
    cik TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_calibrated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@pytest.fixture
def sources_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(_SOURCES_DDL)
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, base_weight,"
        " tier, style_tags, cik, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'seed', 'seed')",
        [
            ("watchlist", "adjacency", "Watchlist", 1.0, "structural", "intentional", None),
            ("appaloosa", "investor_13f", "Appaloosa", 0.85, "multi_cycle", "hedge", "0001656456"),
            (
                "sands",
                "investor_13f",
                "Sands Capital",
                0.8,
                "multi_cycle",
                "long_only,low_turnover",
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_roster_read_and_weight_resolution(sources_db: Path) -> None:
    rows = list_sources(db_path=sources_db)
    assert {r.source_key for r in rows} == {"watchlist", "appaloosa", "sands"}
    appaloosa = next(r for r in rows if r.source_key == "appaloosa")
    assert appaloosa.cik == "0001656456"
    sands = next(r for r in rows if r.source_key == "sands")
    assert sands.style_tags == ("long_only", "low_turnover")  # csv split to tuple

    smap = load_source_map(db_path=sources_db)
    assert weight_for(smap, "appaloosa") == pytest.approx(0.85)
    assert weight_for(smap, "unknown_source") == pytest.approx(1.0)  # default


def test_weight_edit_changes_the_score(sources_db: Path) -> None:
    """End-to-end the contract: editing a source's weight changes a candidate's
    re-scored result — the roster is a live lever, not a static seed."""
    smap = load_source_map(db_path=sources_db)
    before = score_candidate(
        [adjacency_signal("watchlist", weight=weight_for(smap, "watchlist"), observed_at=_NOW)],
        now=_NOW,
    ).score

    edited = set_source_weight("watchlist", 2.5, db_path=sources_db)
    assert edited is not None and edited.base_weight == pytest.approx(2.5)

    smap2 = load_source_map(db_path=sources_db)
    after = score_candidate(
        [adjacency_signal("watchlist", weight=weight_for(smap2, "watchlist"), observed_at=_NOW)],
        now=_NOW,
    ).score
    assert after > before


def test_roster_edits(sources_db: Path) -> None:
    assert set_source_active("sands", active=False, db_path=sources_db).active is False  # type: ignore[union-attr]
    assert [s.source_key for s in list_sources(active_only=True, db_path=sources_db)] != ["sands"]
    # CIK normalizes to 10-digit zero-padded; a blank clears it.
    updated = set_source_cik("sands", "0012345", db_path=sources_db)
    assert updated is not None and updated.cik == "0000012345"
    assert set_source_cik("sands", "", db_path=sources_db).cik is None  # type: ignore[union-attr]
    assert set_source_weight("does_not_exist", 1.0, db_path=sources_db) is None
