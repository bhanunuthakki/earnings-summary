"""Per-ticker data hygiene rules.

For 15-25 tracked companies a year, raw FMP/SEC segment data has too many
ticker-specific quirks (duplicate XBRL tag variants, deprecated segment
names, unit oddities) for a one-size-fits-all extractor. Rather than baking
ticker logic into compute/, we read it from each ticker's holdings JSON
under a `data_rules` block, merged with project-wide defaults.

Holdings-JSON shape (all fields optional):

    "data_rules": {
      "segment_name_aliases": {
        "Old Name": "Canonical Name",   // rename
        "Garbage Bucket": null          // drop entirely
      },
      "segment_denylist_patterns": ["[Abstract]"],
      "current_price_override": 173.45
    }

Global defaults catch SEC XBRL artifacts that are universal noise.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

_GLOBAL_DENYLIST_PATTERNS: list[str] = [
    "[Abstract]",
    "Reconciling items",
    "Operating Segments",
    "Segment Reporting",
]


class TickerRules(BaseModel):
    """Rules merged from holdings JSON + project defaults."""

    segment_name_aliases: dict[str, str | None] = Field(default_factory=dict)
    segment_denylist_patterns: list[str] = Field(default_factory=list)
    current_price_override: float | None = None

    def canonicalize_segment(self, name: str) -> str | None:
        """Apply alias mapping, then denylist. Returns None if the segment should be dropped."""
        normalized = name.strip()
        if normalized in self.segment_name_aliases:
            renamed = self.segment_name_aliases[normalized]
            if renamed is None:
                return None
            normalized = renamed
        for pattern in self.segment_denylist_patterns:
            if pattern in normalized:
                return None
        return normalized


def load_rules(ticker: str, repo_root: Path) -> TickerRules:
    """Load per-ticker rules from holdings JSON, merged with global defaults.

    Tickers without a holdings file get the global defaults only — every
    company gets baseline cleanup of universal SEC XBRL artifacts.
    """
    rules_block = _read_rules_block(ticker, repo_root)
    aliases = _coerce_aliases(rules_block.get("segment_name_aliases"))
    denylist_user = _coerce_str_list(rules_block.get("segment_denylist_patterns"))
    price_override = rules_block.get("current_price_override")
    if not isinstance(price_override, (int, float)) and price_override is not None:
        price_override = None

    merged_denylist = list(_GLOBAL_DENYLIST_PATTERNS) + denylist_user
    return TickerRules(
        segment_name_aliases=aliases,
        segment_denylist_patterns=merged_denylist,
        current_price_override=price_override,
    )


def _read_rules_block(ticker: str, repo_root: Path) -> dict[str, object]:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        holdings = json.load(f)
    rules = holdings.get("data_rules") or {}
    if not isinstance(rules, dict):
        return {}
    return rules


def _coerce_aliases(raw: object) -> dict[str, str | None]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str | None] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if v is None:
            out[k] = None
        elif isinstance(v, str):
            out[k] = v
    return out


def _coerce_str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, str)]
