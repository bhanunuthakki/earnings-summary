"""The Ledger — core data model (Phase 0).

Loads the Phase-0 seed corpus (data/ledger_seed/seed.json) into typed objects.
This is the persistent backbone the capture + coaching loops build on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _norm_ticker(value: Optional[str]) -> Optional[str]:
    """Normalize a ticker to upper-case, or None for a cross-cutting item."""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


@dataclass(frozen=True)
class Decision:
    """A thing the owner actually did (or chose not to do)."""

    ticker: str
    action: str  # buy | add | trim | sell | initiate | pass
    approx_date: str  # "YYYY-MM" or "Q_'YY" or "YYYY"
    conviction: str  # low | medium | high (or a %)
    rationale: str
    falsifier: str = ""


@dataclass(frozen=True)
class Musing:
    """A standing thought/belief/bias. ticker=None means it is cross-cutting."""

    body: str
    approx_date: str = ""
    ticker: Optional[str] = None


@dataclass(frozen=True)
class Theme:
    """A cross-cutting cluster grouping tickers and musings."""

    slug: str
    title: str
    description: str
    tickers: tuple[str, ...] = ()
    musing_refs: tuple[int, ...] = ()


@dataclass
class Ledger:
    """The owner's full corpus: decisions, musings, themes."""

    as_of: str
    decisions: list[Decision]
    musings: list[Musing]
    themes: list[Theme]

    @classmethod
    def from_seed(cls, path: str | Path) -> "Ledger":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        decisions = [
            Decision(
                ticker=_norm_ticker(d.get("ticker")) or "",
                action=(d.get("action") or "").strip().lower(),
                approx_date=d.get("approx_date") or "",
                conviction=(d.get("conviction") or "").strip().lower(),
                rationale=d.get("rationale") or "",
                falsifier=d.get("falsifier") or "",
            )
            for d in data.get("decisions", [])
        ]
        musings = [
            Musing(
                body=m.get("body") or "",
                approx_date=m.get("approx_date") or "",
                ticker=_norm_ticker(m.get("ticker")),
            )
            for m in data.get("musings", [])
        ]
        themes = [
            Theme(
                slug=t.get("slug") or "",
                title=t.get("title") or "",
                description=t.get("description") or "",
                tickers=tuple(
                    _norm_ticker(x) or "" for x in t.get("tickers", []) if _norm_ticker(x)
                ),
                musing_refs=tuple(int(i) for i in t.get("musing_refs", [])),
            )
            for t in data.get("themes", [])
        ]
        return cls(
            as_of=data.get("as_of", ""),
            decisions=decisions,
            musings=musings,
            themes=themes,
        )

    def tickers(self) -> set[str]:
        """Every ticker referenced by a decision, theme, or musing."""
        out = {d.ticker for d in self.decisions if d.ticker}
        for theme in self.themes:
            out.update(t for t in theme.tickers if t)
        for musing in self.musings:
            if musing.ticker:
                out.add(musing.ticker)
        return out

    def musing(self, index: int) -> Optional[Musing]:
        """Resolve a theme's musing_ref to the underlying Musing, safely."""
        if 0 <= index < len(self.musings):
            return self.musings[index]
        return None
