"""The Ledger — coaching core (Phase 0).

Turns the static seed into a *thought partner*: given a context (a ticker and/or
a contemplated action), it surfaces the owner's own standing musings, the themes
the name belongs to, the falsifiers he committed to, and the behavioral patterns
most likely to bite — i.e. it replays the owner's own words at the moment they're
relevant.

The behavioral lens is a transparent keyword ruleset (v0). It is deliberately the
seam that later phases refine: as the owner logs predictions and outcomes, the
pattern tags become learned and the surfacing gets calibrated to where his
judgment is sharp vs. biased. See directives/ledger_seed_2026_06.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ledger.models import Ledger, Musing, Theme

# Classify a musing into behavioral pattern categories by keyword.
_PATTERN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sell_winners_early": ("too early", "sold too early", "trimmed the monopoly", "4x", "winners"),
    "leap_overlay": ("leap", "far-otm", "convex", "options overlay", "option"),
    "washout_catalyst": ("washout", "catalyst test", "value trap", "no catalyst", "priced-in floor"),
    "instrument_selection": ("instrument", "vehicle", "index not", "t-bills", "dry powder", "sgov"),
    "rationalization": ("rationalization", "market is whack", "diversification", "diversify"),
    "concentration": ("concentration", "factor risk", "correlated", "co-move", "doubled"),
}

# Map a contemplated action to the pattern categories worth surfacing.
_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "sell": ("sell_winners_early", "leap_overlay", "rationalization"),
    "trim": ("sell_winners_early", "leap_overlay", "instrument_selection"),
    "buy": ("washout_catalyst", "concentration"),
    "add": ("washout_catalyst", "concentration", "rationalization"),
    "initiate": ("washout_catalyst", "concentration"),
    "pass": ("washout_catalyst",),
}


def patterns_of(musing: Musing) -> set[str]:
    body = musing.body.lower()
    return {p for p, kws in _PATTERN_KEYWORDS.items() if any(k in body for k in kws)}


@dataclass
class Coaching:
    """The thought-partner payload for a given (ticker, action) context."""

    ticker: Optional[str]
    action: Optional[str]
    themes: list[Theme] = field(default_factory=list)
    ticker_musings: list[Musing] = field(default_factory=list)
    behavioral_flags: list[Musing] = field(default_factory=list)
    falsifiers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.themes
            or self.ticker_musings
            or self.behavioral_flags
            or self.falsifiers
            or self.notes
        )


class LedgerCoach:
    """Replays the owner's own seed back at him, in context."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def themes_for(self, ticker: str) -> list[Theme]:
        t = ticker.strip().upper()
        return [th for th in self.ledger.themes if t in th.tickers]

    def musings_for(self, ticker: Optional[str]) -> list[Musing]:
        """Ticker-specific musings, or the cross-cutting (global) ones if ticker is None."""
        if not ticker:
            return [m for m in self.ledger.musings if m.ticker is None]
        t = ticker.strip().upper()
        return [m for m in self.ledger.musings if m.ticker == t]

    def falsifiers_for(self, ticker: str) -> list[str]:
        t = ticker.strip().upper()
        return [
            d.falsifier
            for d in self.ledger.decisions
            if d.ticker == t and d.falsifier and not d.falsifier.lower().startswith("n/a")
        ]

    def behavioral_flags(self, action: Optional[str], ticker: Optional[str] = None) -> list[Musing]:
        """Cross-cutting musings whose pattern matches the contemplated action."""
        if not action:
            return []
        categories = set(_ACTION_PATTERNS.get(action.strip().lower(), ()))
        if not categories:
            return []
        return [
            m
            for m in self.ledger.musings
            if m.ticker is None and (patterns_of(m) & categories)
        ]

    def advise(self, ticker: Optional[str] = None, action: Optional[str] = None) -> Coaching:
        """The top-level thought-partner call."""
        themes = self.themes_for(ticker) if ticker else []
        ticker_musings = self.musings_for(ticker) if ticker else []
        flags = self.behavioral_flags(action, ticker)
        falsifiers = self.falsifiers_for(ticker) if ticker else []

        notes: list[str] = []
        for theme in themes:
            desc = theme.description.lower()
            if any(k in desc for k in ("concentrat", "correlat", "factor")):
                notes.append(
                    f"Concentration watch: {ticker} sits in '{theme.title}', "
                    "a factor-correlated cluster - size the book-level exposure, not just the name."
                )

        return Coaching(
            ticker=ticker.strip().upper() if ticker else None,
            action=action.strip().lower() if action else None,
            themes=themes,
            ticker_musings=ticker_musings,
            behavioral_flags=flags,
            falsifiers=falsifiers,
            notes=notes,
        )
