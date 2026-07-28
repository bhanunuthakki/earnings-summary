"""Canonical derived numbers for a ticker + a drift check over LLM prose.

The synthesis lenses are fed faithfully-formatted derived figures (DCF NPV/share,
MoS bar, over/under) but write free-form memos that can RESTATE those numbers
wrong — e.g. NU's 5-min-reread said "DCF fair value $55 / 0% MoS" while dcf_runs
holds ~$20.88 / 25%. This module is the single source of truth for those figures
plus a conservative check that flags prose claims contradicting them, so the
report can surface the figures of record next to (not instead of) the prose.

The check is deliberately conservative: it only matches a number that sits next
to an explicit DCF/fair-value/MoS keyword, so a lens that legitimately mentions
some other dollar figure is never flagged. It logs + annotates; it never rewrites
the model's prose.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# A $-figure asserted as the DCF/intrinsic/fair value, e.g. "fair value is $55",
# "DCF fair value $55", "NPV/share of $21". Keyword-anchored so unrelated dollar
# amounts in the prose are never matched.
_NPV_CLAIM_RX = re.compile(
    r"(?:dcf\s+(?:fair\s+)?value|fair\s+value|intrinsic\s+value|npv(?:/share)?)"
    r"[^\d$%]{0,25}\$?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
# A margin-of-safety percentage, e.g. "0% MoS", "25% margin of safety".
_MOS_CLAIM_RX = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:mos\b|margin[ -]of[ -]safety)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GroundedNumbers:
    """A ticker's figures of record, read from the latest consolidated dcf_runs.

    over_under_pct and mos_bar are decimals (0.25 == 25%), matching the
    convention snapshot._trigger_status reads (over_under > 0.20).
    """

    npv_per_share: float | None
    live_price: float | None
    over_under_pct: float | None
    mos_bar: float | None

    def has_dcf(self) -> bool:
        return self.npv_per_share is not None

    def dcf_line(self) -> str:
        """One-line canonical DCF anchor — the figures of record."""
        parts: list[str] = []
        if self.npv_per_share is not None:
            parts.append(f"NPV/share ${self.npv_per_share:.2f}")
        if self.live_price is not None:
            parts.append(f"price ${self.live_price:.2f}")
        if self.over_under_pct is not None:
            parts.append(f"over/under {self.over_under_pct * 100:+.0f}%")
        if self.mos_bar is not None:
            parts.append(f"MoS bar {self.mos_bar * 100:.0f}%")
        return " · ".join(parts) if parts else "(no DCF run)"


def load_grounded_numbers(ticker: str, repo_root: Path) -> GroundedNumbers | None:
    """Latest consolidated dcf_runs row as GroundedNumbers, or None when absent."""
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.row_factory = sqlite3.Row
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dcf_runs'"
        ).fetchone():
            return None
        row = conn.execute(
            """
            SELECT npv_per_share, live_price, over_under_pct, mos_bar_used
            FROM dcf_runs
            WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '')
            ORDER BY valuation_date DESC LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    def _f(v: float | int | None) -> float | None:
        return float(v) if v is not None else None

    return GroundedNumbers(
        npv_per_share=_f(row["npv_per_share"]),
        live_price=_f(row["live_price"]),
        over_under_pct=_f(row["over_under_pct"]),
        mos_bar=_f(row["mos_bar_used"]),
    )


def check_numeric_drift(
    text: str, gn: GroundedNumbers, *, npv_tol_frac: float = 0.15, mos_tol_pp: float = 5.0
) -> list[str]:
    """Return human-readable drift messages for DCF/MoS claims in `text` that
    contradict the figures of record. Empty list == grounded (or nothing claimed).

    A fair-value claim drifts when it's off the canonical NPV/share by more than
    `npv_tol_frac`; an MoS claim drifts when it's off the canonical bar by more
    than `mos_tol_pp` percentage points.
    """
    drifts: list[str] = []
    if gn.npv_per_share is not None and gn.npv_per_share > 0:
        for m in _NPV_CLAIM_RX.finditer(text):
            claimed = float(m.group(1))
            if abs(claimed - gn.npv_per_share) / gn.npv_per_share > npv_tol_frac:
                drifts.append(
                    f"states a DCF/fair value of ${claimed:g} but the figure of record "
                    f"is NPV/share ${gn.npv_per_share:.2f}"
                )
    if gn.mos_bar is not None:
        canonical_mos = gn.mos_bar * 100
        for m in _MOS_CLAIM_RX.finditer(text):
            claimed = float(m.group(1))
            if abs(claimed - canonical_mos) > mos_tol_pp:
                drifts.append(
                    f"states a margin of safety of {claimed:g}% but the MoS bar is "
                    f"{canonical_mos:.0f}%"
                )
    return drifts


def grounding_footnote(gn: GroundedNumbers, drifts: list[str]) -> str:
    """Markdown footnote appended to a lens whose prose drifted from the figures
    of record — surfaces the canonical numbers so the reader isn't misled."""
    lead = "; ".join(drifts)
    return (
        f"\n\n> **⚠ Grounding:** this section {lead}. "
        f"Figures of record (per `dcf_runs`): {gn.dcf_line()}."
    )
