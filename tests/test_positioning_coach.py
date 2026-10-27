"""Manual positioning approval and deterministic target display contracts.

These tests preserve submitted-value validation, active-profile form seeding,
and explicit unavailable/degraded book readings after coaching retirement.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.positioning_panel import (
    FormError,
    profile_from_form,
    render_active_target_card,
    render_approval_form,
    render_positioning_panel,
)
from positioning.profile import PositioningProfile, SectorTarget

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_profile_from_form_owner_edits_win() -> None:
    form = {
        "target_growth_tilt": "-0.1",
        "growth_tilt_band": "0.15",
        "vol_posture": "reduce",
        "target_vol_ann": "14",  # percent in the form → 0.14
        "sector_0": "Technology",
        "sector_target_0": "30",
        "sector_band_0": "4",
        "sector_1": "",  # blank row ignored
        "sector_target_1": "",
        "sector_band_1": "5",
        "sleeve_intl": "20",
        "sleeve_cash": "",
        "life_circumstances": "kid on the way\n",
        "narrative": "Less growth crowding; build intl value.",
    }
    profile, narrative = profile_from_form(form)
    assert profile.target_growth_tilt == pytest.approx(-0.1)
    assert profile.target_vol_ann == pytest.approx(0.14)
    assert profile.sector_targets == [
        SectorTarget(sector="Technology", target_weight=0.30, band=0.04)
    ]
    assert profile.sleeves == {"intl": 0.20}
    assert profile.life_circumstances == ["kid on the way"]
    assert narrative == "Less growth crowding; build intl value."


def test_profile_from_form_rejects_bad_values() -> None:
    with pytest.raises(FormError, match="not a number"):
        profile_from_form({"target_growth_tilt": "abc", "narrative": "x"})
    with pytest.raises(FormError, match="narrative is required"):
        profile_from_form({"target_growth_tilt": "-0.1", "narrative": " "})
    with pytest.raises(FormError, match="no quantitative target"):
        profile_from_form({"narrative": "words but no numbers"})
    with pytest.raises(FormError):  # closed sleeve vocabulary via Pydantic
        profile_from_form({"sleeve_intl": "150", "narrative": "x"})


# ---------------------------------------------------------------------------
# Panel renders
# ---------------------------------------------------------------------------


def _intent_db(tmp_path: Path) -> Path:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE positioning_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            narrative TEXT NOT NULL, profile_json TEXT NOT NULL,
            source TEXT NOT NULL, coach_session_id TEXT,
            created_at TEXT NOT NULL, is_latest INTEGER NOT NULL DEFAULT 1,
            superseded_at TEXT, superseded_by_id INTEGER
        );
        """
    )
    profile = PositioningProfile(
        target_growth_tilt=-0.1, sleeves={"intl": 0.2}, life_circumstances=["kid on the way"]
    )
    conn.execute(
        "INSERT INTO positioning_intents (narrative, profile_json, source, created_at) "
        "VALUES (?, ?, 'manual', '2026-07-10T12:00:00')",
        ("Less growth, more intl value.", profile.model_dump_json()),
    )
    conn.commit()
    conn.close()
    return db


def test_panel_renders_default_and_intent_states(tmp_path: Path) -> None:
    # No DB or cache: the current book is the default and manual inputs are blank.
    html = render_positioning_panel(tmp_path / "missing.db", tmp_path)
    assert "defaulting to current book" in html
    assert '<h3 class="k-well-title">Set targets</h3>' in html
    assert 'id="pos-approve-form"' in html
    assert "/api/positioning/approve" in html
    assert "pos-chat-form" not in html
    assert "/api/positioning/coach" not in html
    assert "/api/positioning/propose" not in html
    assert 'class="pos-hist"' not in html
    assert "grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr))" in html
    assert "repeat(2, minmax(var(--grid-card-lg), 1fr))" not in html
    # With a saved intent → versioned card + narrative + dimensions.
    db = _intent_db(tmp_path)
    card = render_active_target_card(db, tmp_path)
    assert "intent v1" in card
    assert "target -0.10" in card
    assert "Sleeve · intl" in card
    assert "Less growth, more intl value." in card
    editor = render_positioning_panel(db, tmp_path)
    assert 'name="target_growth_tilt" placeholder="e.g. -0.10" value="-0.1"' in editor
    assert 'name="sleeve_intl" placeholder="%" value="20"' in editor
    assert ">Less growth, more intl value.</textarea>" in editor


def test_approval_form_renders_manual_profile_values() -> None:
    profile = PositioningProfile(
        target_growth_tilt=0.0,
        sector_targets=[SectorTarget(sector="Technology", target_weight=0.35, band=0.05)],
        sleeves={"intl": 0.15},
    )
    html = render_approval_form(profile, "Neutral tilt; intl 15%.")
    assert 'name="session_id" value=""' in html
    assert 'name="target_growth_tilt"' in html and 'value="0"' in html
    assert 'name="sector_0"' in html and 'value="Technology"' in html
    assert 'name="sector_target_0"' in html and 'value="35"' in html
    assert 'name="sleeve_intl"' in html and 'value="15"' in html
    assert '<h3 class="k-well-title">Set targets' in html
    assert '<h4 class="k-well-title">Sector targets' in html
    assert '<h4 class="k-well-title">Narrative' in html
    assert "Save targets" in html
    assert "Neutral tilt; intl 15%." in html


# ---------------------------------------------------------------------------
# Appetite fields (target_vol_ann / sharpe_floor) wired into gap chips —
# Phase 0 of the tenet-2 advisory program: display-only fields becoming
# computationally live against the book's real vol/Sharpe.
# ---------------------------------------------------------------------------


def _intent_db_with_appetite(tmp_path: Path) -> Path:
    db = tmp_path / "p2.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE positioning_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            narrative TEXT NOT NULL, profile_json TEXT NOT NULL,
            source TEXT NOT NULL, coach_session_id TEXT,
            created_at TEXT NOT NULL, is_latest INTEGER NOT NULL DEFAULT 1,
            superseded_at TEXT, superseded_by_id INTEGER
        );
        """
    )
    profile = PositioningProfile(target_vol_ann=0.14, sharpe_floor=0.5)
    conn.execute(
        "INSERT INTO positioning_intents (narrative, profile_json, source, created_at) "
        "VALUES (?, ?, 'manual', '2026-07-15T09:00:00')",
        ("Cap risk; keep efficiency.", profile.model_dump_json()),
    )
    conn.commit()
    conn.close()
    return db


def _write_cache(tmp_path: Path, *, sharpe: float | None, vol_ann: float | None) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "candidate_fit.json").write_text(
        json.dumps({"version": 2, "book": {"sharpe": sharpe, "vol_ann": vol_ann}, "fits": {}}),
        encoding="utf-8",
    )


def test_active_target_card_shows_appetite_gap_chips_over_and_below(tmp_path: Path) -> None:
    """Book vol/Sharpe available (materialized cache) → the target_vol_ann and
    sharpe_floor rows carry a live over/under · above/below gap chip. Book vol
    above target reads "over" (warn); book Sharpe under the floor reads
    "below" (warn)."""
    db = _intent_db_with_appetite(tmp_path)
    _write_cache(tmp_path, sharpe=0.3, vol_ann=0.20)
    card = render_active_target_card(db, tmp_path)
    assert "Target vol (ann.)" in card
    assert "book vol 20% vs target 14% (over)" in card
    assert "k-chip-warn" in card  # vol-over chip
    assert "Sharpe floor" in card
    assert "book Sharpe +0.30 vs floor +0.50 (below)" in card


def test_active_target_card_shows_appetite_gap_chips_under_and_above(tmp_path: Path) -> None:
    """Book vol under target reads "under" (ok); book Sharpe over the floor
    reads "above" (ok)."""
    db = _intent_db_with_appetite(tmp_path)
    _write_cache(tmp_path, sharpe=0.7, vol_ann=0.10)
    card = render_active_target_card(db, tmp_path)
    assert "book vol 10% vs target 14% (under)" in card
    assert "book Sharpe +0.70 vs floor +0.50 (above)" in card


def test_active_target_card_appetite_chips_at_exact_target_and_floor(tmp_path: Path) -> None:
    """Book vol exactly at target reads "under" (satisfied, not over); book
    Sharpe exactly at the floor reads "above" (satisfied, not below)."""
    db = _intent_db_with_appetite(tmp_path)
    _write_cache(tmp_path, sharpe=0.5, vol_ann=0.14)
    card = render_active_target_card(db, tmp_path)
    assert "book vol 14% vs target 14% (under)" in card
    assert "book Sharpe +0.50 vs floor +0.50 (above)" in card


def test_active_target_card_omits_appetite_chips_when_book_figures_absent(
    tmp_path: Path,
) -> None:
    """No materialized cache → book vol/Sharpe unknown; the target values still
    display (unchanged behavior) but no gap chip is fabricated."""
    db = _intent_db_with_appetite(tmp_path)
    card = render_active_target_card(db, tmp_path)
    assert "Target vol (ann.)" in card
    assert "vs target" not in card
    assert "Sharpe floor" in card
    assert "vs floor" not in card


def test_active_target_card_omits_appetite_chips_when_profile_fields_null(
    tmp_path: Path,
) -> None:
    """Null profile fields (the existing intent fixture sets neither) → no
    Target vol / Sharpe floor rows at all, exactly current behavior."""
    db = _intent_db(tmp_path)
    card = render_active_target_card(db, tmp_path)
    assert "Target vol (ann.)" not in card
    assert "Sharpe floor" not in card


def test_degraded_book_context_renders_owner_language_not_raw_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4 (surface_density_jit_redesign.md): the degraded strip is a warn pill
    + what-it-means line, with the raw engineering reasons behind a details
    peek — never dumped verbatim into the card body. The walkthrough caught
    prod showing "book context degraded: tracker offline and no risk snapshot
    — book Sharpe unknown · …" in monospace as the card's closing line."""
    import pipeline.positioning_panel as panel

    raw = (
        "tracker offline and no risk snapshot — book Sharpe unknown",
        "sector weights are tracker-only — sector factors unscored",
    )

    def degraded_meta(_root: Path) -> dict[str, object]:
        return {"book": {"degraded": list(raw)}}

    monkeypatch.setattr(panel, "read_materialized_fit_meta", degraded_meta)
    card = panel.render_active_target_card(tmp_path / "missing.db", tmp_path)

    # Owner-language strip: pill + count + consequence.
    assert 'class="k-pill k-pill-warn">context degraded</span>' in card
    assert "2 book-context leg(s) unscored" in card
    # The raw reasons survive — but only inside the details peek.
    details = card[card.index("<details") : card.index("</details>")]
    for reason in raw:
        assert reason in details
    # And never as the old free-floating monospace line.
    assert "book context degraded:" not in card


def test_healthy_book_context_has_no_degraded_strip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degraded strip must be absent — not empty — on a healthy book, so
    degraded stays visibly distinct from healthy (silent-degradation rule)."""
    import pipeline.positioning_panel as panel

    def healthy_meta(_root: Path) -> dict[str, object]:
        return {"book": {}}

    monkeypatch.setattr(panel, "read_materialized_fit_meta", healthy_meta)
    card = panel.render_active_target_card(tmp_path / "missing.db", tmp_path)
    assert "pos-degraded" not in card
    assert "context degraded" not in card
