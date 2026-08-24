"""Render-side "key metrics" bubble row for the DIY picker
(directives/key_metrics_picker.md).

Builds the clickable preselect chips shown above the Explore picker's
``<select multiple>`` set. Two sources, merged here:

* **Tier-graded baseline (always, NO LLM, NO cache)** — the tier-1/tier-2 KPI
  grading in ``kpi_definitions.threshold_tier`` (the thesis breakers). Pulled
  straight from the DB for the selected tickers.
* **LLM picks (cache read only)** — ``data/key_metrics/{TICKER}.json``, written
  on the ``--enable-llm`` build by ``src/compute/key_metrics.py``. Read here as
  plain JSON — NO heavy import on the render path (mirrors how
  ``report.sections.p3_data`` reads the peer_selection cache). Absent / malformed
  cache → the tier-graded baseline alone.

Every bubble token is validated against the live picker catalog so a click
always maps to a real ``<option>``; the bubble's domain prefix (``fin:`` /
``kpi:``) routes it to the right ``<select>`` in the panel's inline JS.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal, cast

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Combined ceiling on the rendered row — tier baseline first, then LLM picks
# fill the remainder. Keeps the row to one scannable line, not a wall of chips.
_MAX_BUBBLES = 12

_TIER_TITLE = {
    "tier_1_break": "Tier-1 thesis KPI (a breach breaks the thesis)",
    "tier_2_monitor": "Tier-2 monitored KPI",
}


@dataclass(frozen=True)
class KeyMetricBubble:
    token: str
    label: str
    source: Literal["tier", "llm"]
    title: str  # tooltip text


# ---------------------------------------------------------------------------
# data gathering
# ---------------------------------------------------------------------------


def _catalog_index(
    catalog: dict[str, list[dict[str, object]]],
) -> tuple[set[str], dict[str, str]]:
    """``(valid_tokens, label_by_token)`` from the picker catalog — the closed
    set of tokens a bubble may reference (so every chip maps to an option)."""
    valid: set[str] = set()
    labels: dict[str, str] = {}
    for entries in catalog.values():
        for entry in entries:
            token = entry.get("token")
            if not isinstance(token, str) or not token:
                continue
            valid.add(token)
            label = entry.get("label")
            if isinstance(label, str) and label:
                labels[token] = label
    return valid, labels


def tier_graded_baseline(db_path: Path, tickers: list[str]) -> list[tuple[str, str]]:
    """``(token, tier)`` for every tier-1/tier-2 graded KPI (with ≥1 fact) across
    the selected tickers, tier-1 first then by how many tickers carry it. Tokens
    are ``kpi:<name>``. Best-effort: a missing DB/table yields ``[]``.

    Reads only ``threshold_tier`` (a long-standing column) — safe on a DB that
    has not yet migrated the capture-program ``definition_origin`` column."""
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    if not symbols or not db_path.exists():
        return []
    marks = ",".join("?" * len(symbols))
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT kd.name AS name, kd.threshold_tier AS tier,
                   COUNT(DISTINCT kf.ticker) AS n
            FROM kpi_definitions kd
            JOIN kpi_facts kf ON kf.kpi_definition_id = kd.id
            WHERE kd.threshold_tier IN ('tier_1_break', 'tier_2_monitor')
              AND kf.ticker IN ({marks})
            GROUP BY kd.name, kd.threshold_tier
            ORDER BY CASE kd.threshold_tier WHEN 'tier_1_break' THEN 0 ELSE 1 END,
                     n DESC, name ASC
            """,
            tuple(symbols),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[tuple[str, str]] = []
    for name, tier, _n in rows:
        if name:
            out.append((f"kpi:{name}", str(tier)))
    return out


def load_llm_picks(db_path: Path, ticker: str) -> list[tuple[str, str]]:
    """``(token, why)`` from the cached LLM picks for ``ticker``, read straight
    from ``data/key_metrics/<T>.json`` (no heavy import on the render path).
    ``[]`` on any miss / malformed cache (degrade to the tier-graded baseline)."""
    path = db_path.parent / "key_metrics" / f"{ticker.upper()}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    metrics = cast("dict[str, object]", raw).get("metrics")
    if not isinstance(metrics, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in cast("list[object]", metrics):
        if not isinstance(entry, dict):
            continue
        rec = cast("dict[str, object]", entry)
        token = rec.get("token")
        if not isinstance(token, str) or not token:
            continue
        why = rec.get("why")
        out.append((token, why.strip() if isinstance(why, str) else ""))
    return out


def key_metric_bubbles(
    db_path: Path,
    tickers: list[str],
    catalog: dict[str, list[dict[str, object]]],
    *,
    max_bubbles: int = _MAX_BUBBLES,
) -> list[KeyMetricBubble]:
    """Merge the tier-graded baseline with the cached LLM picks into the bubble
    row: tier-graded first (deterministic, always shown), then LLM picks that
    aren't already present. Deduped by token, every token validated against the
    live catalog, capped at ``max_bubbles``."""
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    valid_tokens, label_by_token = _catalog_index(catalog)
    bubbles: list[KeyMetricBubble] = []
    used: set[str] = set()

    def _label(token: str) -> str:
        if token in label_by_token:
            return label_by_token[token]
        # Fall back to the token's leaf (after the domain prefix).
        return token.split(":", 1)[-1] if ":" in token else token

    for token, tier in tier_graded_baseline(db_path, symbols):
        if token in used or token not in valid_tokens:
            continue
        used.add(token)
        bubbles.append(
            KeyMetricBubble(
                token=token,
                label=_label(token),
                source="tier",
                title=_TIER_TITLE.get(tier, "Graded thesis KPI"),
            )
        )
        if len(bubbles) >= max_bubbles:
            return bubbles

    for ticker in symbols:
        for token, why in load_llm_picks(db_path, ticker):
            if token in used or token not in valid_tokens:
                continue
            used.add(token)
            bubbles.append(
                KeyMetricBubble(
                    token=token,
                    label=_label(token),
                    source="llm",
                    title=why or "Key metric for this business",
                )
            )
            if len(bubbles) >= max_bubbles:
                return bubbles
    return bubbles


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_key_metrics_inner(bubbles: list[KeyMetricBubble], tickers: list[str]) -> str:
    """The inner HTML of the ``#vx-keymetrics`` container: a label + a chip per
    bubble. Empty string when there are no bubbles (the container collapses via
    ``:empty``). Returned bare so the ``?fragment=keymetrics`` route can swap it
    in on a ticker change."""
    if not bubbles:
        return ""
    symbols = [t.strip().upper() for t in tickers if t.strip()]
    scope = ", ".join(symbols[:4]) + ("…" if len(symbols) > 4 else "")
    label = f"Key metrics &middot; {escape(scope)}" if scope else "Key metrics"
    chips: list[str] = []
    for b in bubbles:
        tone = " k-chip-accent" if b.source == "llm" else ""
        chips.append(
            f'<button type="button" class="k-chip km-chip{tone}" '
            f'data-km-token="{escape(b.token)}" title="{escape(b.title)}">'
            f"{escape(b.label)}</button>"
        )
    return (
        f'<span class="vx-km-label">{label}</span><span class="vx-km-chips">{"".join(chips)}</span>'
    )
