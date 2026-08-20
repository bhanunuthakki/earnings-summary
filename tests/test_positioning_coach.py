"""Positioning coach: pack grounding, encode proposal, owner-wins approval.

Network-free: the book assembly and the structured LLM call are monkeypatched;
what's under test is the grounding content contract (absence-explicit, closed
vocabulary), the encode validation boundary (loud on bad profiles, never a
silent empty proposal), the approval form's re-validation FROM FORM VALUES,
and the panel's render paths.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import positioning.coach_pack as coach_pack  # noqa: E402
import positioning.encode as encode_mod  # noqa: E402
from allocation.candidate_fit import BookContext  # noqa: E402
from pipeline.positioning_panel import (  # noqa: E402
    FormError,
    profile_from_form,
    render_active_target_card,
    render_approval_form,
    render_positioning_panel,
)
from positioning.encode import EncodeError, propose_profile  # noqa: E402
from positioning.profile import PositioningProfile, SectorTarget  # noqa: E402

# ---------------------------------------------------------------------------
# Coach pack — grounding contract
# ---------------------------------------------------------------------------


_LIVE_BOOK = BookContext(
    weights={"NU": 0.4, "MELI": 0.35, "RBRK": 0.25},
    sharpe=0.85,
    risk_free_annual=0.045,
    growth_tilt=0.25,
    sector_weights={"Technology": 0.45, "Financial Services": 0.35},
)

_OFFLINE_BOOK = BookContext(
    weights={},
    sharpe=None,
    risk_free_annual=None,
    growth_tilt=None,
    sector_weights={},
    degraded=(
        "tracker offline and no risk snapshot — book Sharpe unknown",
        "sector weights are tracker-only — sector factors unscored",
    ),
)


def test_pack_grounds_in_live_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coach_pack, "assemble_book_context", lambda *a, **k: _LIVE_BOOK)
    pack = coach_pack.build_positioning_pack(tmp_path, tmp_path / "missing.db")
    ctx = pack.system_context or ""
    assert pack.narrative_purpose == "positioning_coach_turn"
    assert pack.scope == "portfolio"
    # Socratic posture + never-claims-saved rule are in the instructions.
    assert "PUSH BACK" in ctx
    assert "NEVER save anything" in ctx
    # Live numbers quoted verbatim.
    assert "book Sharpe: +0.85" in ctx
    assert "growth tilt (QQQ beta - SPY beta): +0.25" in ctx
    assert "NU 40%" in ctx
    assert "Technology 45%" in ctx
    # Closed vocabularies for the encoder.
    assert "SECTOR VOCABULARY" in ctx and "Financial Services" in ctx
    assert "SLEEVE VOCABULARY: intl, small_cap, em, cash" in ctx
    # No saved intent → book-default statement.
    assert "no saved positioning" in ctx


def test_pack_is_absence_explicit_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coach_pack, "assemble_book_context", lambda *a, **k: _OFFLINE_BOOK)
    ctx = coach_pack.build_positioning_pack(tmp_path, tmp_path / "missing.db").system_context or ""
    assert "book Sharpe: unavailable" in ctx
    assert "sector weights: unavailable (tracker offline)" in ctx
    assert "DEGRADED: tracker offline and no risk snapshot" in ctx


# ---------------------------------------------------------------------------
# Encode — loud boundary
# ---------------------------------------------------------------------------


_THREAD = [
    {"role": "user", "text": "I want less mega-cap growth and an intl small-cap value sleeve."},
    {"role": "assistant", "text": "What horizon? … converged: tilt toward 0.0, intl 15%."},
]


def _patch_history(monkeypatch: pytest.MonkeyPatch, turns: list[dict[str, str]]) -> None:
    monkeypatch.setattr(encode_mod, "load_recent_history", lambda sid, **kw: turns)


def _patch_encode_llm(monkeypatch: pytest.MonkeyPatch, fn: object) -> None:
    # ``propose_profile`` imports call_llm_structured FUNCTION-LOCALLY (the
    # module-level import dragged the ~10s llm transport chain into the
    # Positioning panel's render path — Phase-5 verifier fix 5), so the patch
    # targets the source module the lazy import resolves against.
    monkeypatch.setattr("llm.structured.call_llm_structured", fn)


def test_propose_profile_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_history(monkeypatch, _THREAD)
    payload = {
        "profile": {
            "target_growth_tilt": 0.0,
            "sleeves": {"intl": 0.15},
            "sector_targets": [{"sector": "Technology", "target_weight": 0.35, "band": 0.05}],
            "life_circumstances": ["kid on the way"],
        },
        "summary": "Neutral growth tilt; build a 15% intl sleeve; Tech toward 35%.",
        "rationale": "Owner said less mega-cap growth; converged on tilt 0.0 and intl 15%.",
    }
    captured: dict[str, object] = {}

    def fake_structured(prompt: str, **kw: object) -> object:
        captured["prompt"] = prompt
        captured["purpose"] = kw.get("purpose")
        return payload

    _patch_encode_llm(monkeypatch, fake_structured)
    proposal = propose_profile(
        tmp_path / "missing.db", tmp_path, session_id="s1", sector_vocabulary=["Technology"]
    )
    assert captured["purpose"] == "positioning_encode"
    assert "Emit ONLY dimensions the owner actually expressed" in str(captured["prompt"])
    assert proposal.profile.sleeves == {"intl": 0.15}
    assert proposal.summary.startswith("Neutral growth tilt")
    assert proposal.session_id == "s1"
    # Everything is a diff vs 'no active profile'.
    assert any(d.fieldname == "sleeves" for d in proposal.diffs)


def test_propose_profile_invalid_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_history(monkeypatch, _THREAD)
    _patch_encode_llm(
        monkeypatch, lambda *a, **k: {"profile": {"sleeves": {"crypto": 0.5}}, "summary": "x"}
    )
    with pytest.raises(EncodeError, match="failed validation"):
        propose_profile(tmp_path / "m.db", tmp_path, session_id="s1", sector_vocabulary=[])


def test_propose_profile_empty_conversation_and_empty_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_history(monkeypatch, [])
    with pytest.raises(EncodeError, match="no conversation"):
        propose_profile(tmp_path / "m.db", tmp_path, session_id="s1", sector_vocabulary=[])
    _patch_history(monkeypatch, _THREAD)
    _patch_encode_llm(monkeypatch, lambda *a, **k: {"profile": {}, "summary": "x"})
    with pytest.raises(EncodeError, match="hasn't expressed any quantitative target"):
        propose_profile(tmp_path / "m.db", tmp_path, session_id="s1", sector_vocabulary=[])


# ---------------------------------------------------------------------------
# Approval — owner-wins re-validation from the form
# ---------------------------------------------------------------------------


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
        "VALUES (?, ?, 'coach', '2026-07-10T12:00:00')",
        ("Less growth, more intl value.", profile.model_dump_json()),
    )
    conn.commit()
    conn.close()
    return db


def test_panel_renders_default_and_intent_states(tmp_path: Path) -> None:
    # No DB / no cache → default-to-book card, empty history, coach present.
    html = render_positioning_panel(tmp_path / "missing.db", tmp_path)
    assert "defaulting to current book" in html
    # Wave B (B6): the section header names WHICH coach.
    assert '<h3 class="k-well-title">Positioning coach</h3>' in html
    assert "pos-chat-form" in html and "/api/positioning/coach" in html
    assert "Propose targets from this conversation" in html
    assert "grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr))" in html
    assert "repeat(2, minmax(var(--grid-card-lg), 1fr))" not in html
    # With a saved intent → versioned card + narrative + dimensions.
    db = _intent_db(tmp_path)
    card = render_active_target_card(db, tmp_path)
    assert "intent v1" in card
    assert "target -0.10" in card
    assert "Sleeve · intl" in card
    assert "Less growth, more intl value." in card


def test_approval_form_round_trips_proposal_values(tmp_path: Path) -> None:
    from positioning.encode import ProposedProfile

    proposal = ProposedProfile(
        profile=PositioningProfile(
            target_growth_tilt=0.0,
            sector_targets=[SectorTarget(sector="Technology", target_weight=0.35, band=0.05)],
            sleeves={"intl": 0.15},
        ),
        summary="Neutral tilt; intl 15%.",
        rationale="From the conversation.",
        session_id="s9",
    )
    html = render_approval_form(proposal)
    assert 'name="session_id" value="s9"' in html
    assert 'name="target_growth_tilt"' in html and 'value="0"' in html
    assert 'name="sector_0"' in html and 'value="Technology"' in html
    assert 'name="sector_target_0"' in html and 'value="35"' in html
    assert 'name="sleeve_intl"' in html and 'value="15"' in html
    assert '<h3 class="k-well-title">Proposed targets' in html
    assert '<h4 class="k-well-title">Sector targets' in html
    assert '<h4 class="k-well-title">Narrative' in html
    assert "Approve" in html


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
    monkeypatch.setattr(
        panel, "read_materialized_fit_meta", lambda root: {"book": {"degraded": list(raw)}}
    )
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

    monkeypatch.setattr(panel, "read_materialized_fit_meta", lambda root: {"book": {}})
    card = panel.render_active_target_card(tmp_path / "missing.db", tmp_path)
    assert "pos-degraded" not in card
    assert "context degraded" not in card
