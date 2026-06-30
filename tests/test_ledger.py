"""Phase-0 tests for The Ledger: the seed loads and the coaching lens fires."""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger.coach import LedgerCoach
from ledger.models import Ledger

SEED = Path(__file__).resolve().parents[1] / "data" / "ledger_seed" / "seed.json"


@pytest.fixture
def ledger() -> Ledger:
    return Ledger.from_seed(SEED)


def test_seed_loads_with_expected_shape(ledger: Ledger) -> None:
    assert len(ledger.decisions) == 22
    assert len(ledger.musings) == 13
    assert len(ledger.themes) == 10
    assert ledger.as_of  # non-empty


def test_theme_lookup_groups_the_name(ledger: Ledger) -> None:
    slugs = {t.slug for t in LedgerCoach(ledger).themes_for("NU")}
    assert "latam-fintech-commerce" in slugs


def test_sell_surfaces_sell_winners_and_leap_patterns(ledger: Ledger) -> None:
    # The signature coaching moment: contemplating a sell should replay the
    # owner's "I sell winners too early" musing and his LEAP-overlay fix.
    c = LedgerCoach(ledger).advise("NVDA", "sell")
    bodies = " ".join(m.body.lower() for m in c.behavioral_flags)
    assert "too early" in bodies
    assert "leap" in bodies


def test_buy_surfaces_catalyst_test(ledger: Ledger) -> None:
    c = LedgerCoach(ledger).advise("MELI", "buy")
    bodies = " ".join(m.body.lower() for m in c.behavioral_flags)
    assert "catalyst test" in bodies or "washout" in bodies


def test_falsifiers_present_for_tracked_name(ledger: Ledger) -> None:
    fals = LedgerCoach(ledger).falsifiers_for("NU")
    assert fals and any("npl" in f.lower() for f in fals)


def test_ticker_specific_musing_surfaces(ledger: Ledger) -> None:
    c = LedgerCoach(ledger).advise("NVDA")
    assert any(m.ticker == "NVDA" for m in c.ticker_musings)


def test_concentration_note_for_correlated_cluster(ledger: Ledger) -> None:
    c = LedgerCoach(ledger).advise("MELI", "add")
    assert any("concentration watch" in n.lower() for n in c.notes)
