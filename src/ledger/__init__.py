"""The Ledger — a voice-first investment journaling + research thought-partner.

Phase 0 (this package): the persistent backbone — DECISIONS (what the owner did),
MUSINGS (how the owner thinks: standing, cross-cutting beliefs and biases), and
THEMES (the clusters that organize them) — loaded from the seed corpus, plus the
coaching lens that replays the owner's own words at the moment they're relevant.

Design goal (core, per the owner): become a better thought-partner and advisor
*over time*. The seed is the cold-start profile; later phases add capture
(voice/Telegram/Gmail), the conversational research loop, and calibration of the
owner's predictions against outcomes. See directives/ledger_seed_2026_06.md.
"""

from ledger.models import Decision, Ledger, Musing, Theme
from ledger.coach import Coaching, LedgerCoach

__all__ = [
    "Decision",
    "Ledger",
    "Musing",
    "Theme",
    "Coaching",
    "LedgerCoach",
]
