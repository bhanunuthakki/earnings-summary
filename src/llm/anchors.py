"""
src/llm/anchors.py
------------------
Thesis & bear-case anchor block builders — shared context for analytical prompts.

Several prompts (per-quarter summary, pairwise SayDo, recent developments,
SayDo filter, event brief) promise "thesis-anchored analysis" but currently
have no thesis on hand. These helpers pull two small blocks of context that
can be appended to those prompts so the LLM has the pillars / KPIs / break
rules / non-consensus risks to anchor against. Both helpers are tolerant:
missing files → empty string, so watchlist/evaluation tickers (no thesis,
no cached bear case) still work and the prompt shape is unchanged.

Public API:
    load_thesis_anchor(repo_root, ticker) -> str
    load_bear_anchor(repo_root, ticker) -> str
    compose_anchor_block(thesis_anchor, bear_anchor) -> str
    ANCHOR_BLOCK_CHAR_CAP — hard cap on the assembled anchor blocks.

Extracted from src/llm_client.py during the llm subpackage split (PURE
refactor — zero behavior change).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

# Hard cap on the assembled anchor blocks so a verbose holdings JSON cannot
# blow the prompt budget on any single prompt. Trim is deterministic
# (truncation at the section boundary), not a smart compressor.
ANCHOR_BLOCK_CHAR_CAP = 3500

_HOLDINGS_DIRNAME = ("micro_thesis", "holdings")
_BEAR_CASE_CACHE_DIRNAME = ("data", "bear_case")


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively. Returns None
    for any read or parse failure so callers degrade to no-anchor mode."""
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _kpi_anchor_lines(payload: dict[str, object]) -> list[str]:
    """Render `tier_1_kpis` as one-line bullets: name + break condition."""
    raw = payload.get("tier_1_kpis")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        name = e.get("name")
        bc = e.get("break_condition")
        if not isinstance(name, str) or not name.strip():
            continue
        bc_text = bc.strip() if isinstance(bc, str) and bc.strip() else "—"
        lines.append(f"- **{name.strip()}** — breaks if {bc_text}")
    return lines


def _business_rule_anchor_lines(payload: dict[str, object]) -> list[str]:
    """Render quantitative `business_model_rules` as scannable bullets."""
    raw = payload.get("business_model_rules")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        narrative = e.get("narrative")
        if isinstance(narrative, str) and narrative.strip():
            lines.append(f"- {narrative.strip()}")
    return lines


# Canonical financial line items always worth statistically profiling — the
# Week-1 time-series layer runs detect_trend + detect_inflection on these
# so the LLM sees the numerical read, not just the raw rows. Stays a small
# fixed set so the anchor block doesn't balloon; per-ticker tier-1 KPIs
# are added on top via load_kpi_series when available.
_STATS_LINE_ITEMS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "free_cash_flow",
    "net_income",
)


def _stats_block_from_series(series_label: str, series: list[object]) -> str | None:
    """One-line markdown summary of a series: direction + slope + (inflection
    when present). Returns None when the series is too short to analyze."""
    from timeseries import detect_inflection, detect_trend
    if len(series) < 4:
        return None
    trend = detect_trend(cast("list[object]", series))
    if trend.get("insufficient_data"):
        return None
    direction = str(trend.get("direction") or "?")
    slope_pct = trend.get("slope_pct_of_mean")
    slope_str = (
        f"{float(cast('float', slope_pct)) * 100:+.1f}%/q"
        if isinstance(slope_pct, (int, float))
        else "—"
    )
    sig = " (sig)" if trend.get("statistical_significance") else ""
    line = f"- **{series_label}** — {direction} · slope {slope_str}{sig}"

    # Add inflection callout when the series is long enough
    if len(series) >= 8:
        infl = detect_inflection(cast("list[object]", series))
        if infl.get("inflection_period") and float(cast("float", infl.get("magnitude") or 0)) >= 1.0:
            line += f" · inflection {infl['inflection_period']} (delta={float(cast('float', infl['magnitude'])):.1f}sd)"
    return line


def _statistical_patterns_block(
    repo_root: Path, ticker: str, payload: dict[str, object]
) -> list[str]:
    """Run detect_trend + detect_inflection on the canonical line items and
    any per-ticker registered KPIs. Returns markdown lines, empty when no
    series load (missing DB, unknown ticker)."""
    try:
        from timeseries import load_financial_series, load_kpi_series
    except ImportError:
        return []

    lines: list[str] = []
    for line_item in _STATS_LINE_ITEMS:
        try:
            s = load_financial_series(ticker=ticker, line_item=line_item, repo_root=repo_root)
        except Exception as exc:  # never block anchor build
            log.debug({"event": "stats_load_financial_failed", "ticker": ticker, "lineitem": line_item, "error": str(exc)})
            continue
        if not s:
            continue
        rendered = _stats_block_from_series(line_item.replace("_", " "), cast("list[object]", s))
        if rendered:
            lines.append(rendered)

    # Per-ticker registered KPIs (from kpi_definitions). Tier-1 KPIs from the
    # holdings JSON would be ideal here but holdings names rarely match the
    # registry verbatim — use the registry as the source of truth.
    db_path = repo_root / "data" / "portfolio.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT name FROM kpi_definitions WHERE ticker = ? LIMIT 4",
                    (ticker.upper(),),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.debug({"event": "stats_kpi_def_lookup_failed", "error": str(exc)})
            rows = []
        for (kpi_name,) in rows:
            if not isinstance(kpi_name, str) or not kpi_name.strip():
                continue
            try:
                s_kpi = load_kpi_series(ticker=ticker, kpi_name=kpi_name, repo_root=repo_root)
            except Exception as exc:  # best-effort
                log.debug({"event": "stats_load_kpi_failed", "ticker": ticker, "kpi": kpi_name, "error": str(exc)})
                continue
            if not s_kpi:
                continue
            short_label = kpi_name if len(kpi_name) <= 60 else kpi_name[:57] + "…"
            rendered = _stats_block_from_series(short_label, cast("list[object]", s_kpi))
            if rendered:
                lines.append(rendered)

    # Reference payload so linters know it's intentional (reserved for
    # future use: cross-check tier-1 KPI names against the registry)
    _ = payload
    return lines


def load_thesis_anchor(repo_root: Path, ticker: str) -> str:
    """Compose a compact thesis anchor for prompt injection. Empty string
    when no holdings JSON exists. Output is markdown, ~300-1500 chars.

    Since the Week-1 time-series layer landed, the anchor also carries a
    "Recent statistical patterns" subsection — detect_trend + detect_inflection
    over the canonical financial line items + any per-ticker registered KPIs.
    The subsection is best-effort: DB / loader failures degrade silently so
    the original thesis anchor still renders."""
    payload = _load_holdings_json(repo_root, ticker)
    if payload is None:
        return ""

    parts: list[str] = ["## THESIS ANCHOR (analyst's own framing of this name)"]

    # Recently-IPO'd issuers: tell the LLM the narrative source is the S-1,
    # not the 10-K — so it stops phrasing claims as "the company's most-recent
    # 10-K disclosed..." which would be factually wrong.
    data_anchor = payload.get("data_anchor")
    if isinstance(data_anchor, str) and data_anchor.strip().lower() == "s1":
        ipo_date = payload.get("ipo_date")
        ipo_suffix = (
            f" (IPO {ipo_date.strip()})"
            if isinstance(ipo_date, str) and ipo_date.strip()
            else ""
        )
        parts.append(
            f"\n**Narrative source:** S-1 / 424B prospectus{ipo_suffix} — "
            f"no 10-K filed yet. Phrase historical claims accordingly."
        )

    thesis = payload.get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        parts.append(f"\n**Thesis statement:**\n{thesis.strip()}")

    key_driver = payload.get("key_driver")
    if isinstance(key_driver, str) and key_driver.strip():
        parts.append(f"\n**Key driver tracked:** {key_driver.strip()}")

    kpi_lines = _kpi_anchor_lines(payload)
    if kpi_lines:
        parts.append("\n**Tier-1 KPIs (with break conditions):**")
        parts.extend(kpi_lines)

    rule_lines = _business_rule_anchor_lines(payload)
    if rule_lines:
        parts.append("\n**Quantitative thesis-breakers:**")
        parts.extend(rule_lines)

    # Best-effort statistical block. Wrapped in a try/except so any failure
    # in the timeseries layer (DB missing, scipy import error, etc.) can't
    # break the anchor for the dozens of prompts that depend on it.
    try:
        stats_lines = _statistical_patterns_block(repo_root, ticker, payload)
    except Exception as exc:  # anchor must keep rendering
        log.debug({"event": "statistical_patterns_block_failed", "ticker": ticker, "error": str(exc)})
        stats_lines = []
    if stats_lines:
        parts.append("\n**Recent statistical patterns (last 8-16 quarters):**")
        parts.extend(stats_lines)

    if len(parts) == 1:  # only the header — no usable content
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > ANCHOR_BLOCK_CHAR_CAP:
        assembled = assembled[:ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    return assembled


def load_bear_anchor(repo_root: Path, ticker: str) -> str:
    """Compose a compact bear-case anchor from the on-disk cache (written by
    the bear_case section after a successful LLM run). Returns the
    `most_underweighted` paragraph plus the top 3 failure-mode hypotheses so
    the per-quarter summary / news / SayDo can engage with the analyst's
    existing bear framing without re-running the bear case.

    Returns "" when no cache exists (no prior `--enable-llm` run) so the
    first-ever build of a ticker still works without circular dependency.
    """
    path = repo_root.joinpath(*_BEAR_CASE_CACHE_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return ""
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""

    parts: list[str] = ["## BEAR-CASE ANCHOR (from analyst's prior bear review)"]

    underweighted = payload.get("most_underweighted")
    if isinstance(underweighted, str) and underweighted.strip():
        parts.append(f"\n**Most underweighted by consensus:**\n{underweighted.strip()}")

    fms = payload.get("failure_modes")
    if isinstance(fms, list):
        hyps: list[str] = []
        for entry in cast("list[object]", fms)[:3]:
            if not isinstance(entry, dict):
                continue
            e = cast("dict[str, object]", entry)
            h = e.get("hypothesis")
            if isinstance(h, str) and h.strip():
                hyps.append(f"- {h.strip()}")
        if hyps:
            parts.append("\n**Named failure modes the analyst is tracking:**")
            parts.extend(hyps)

    if len(parts) == 1:
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > ANCHOR_BLOCK_CHAR_CAP:
        assembled = assembled[:ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    return assembled


def compose_anchor_block(thesis_anchor: str, bear_anchor: str) -> str:
    """Join thesis + bear anchors with a separator, omitting empties.
    Returns "" when both are empty so the caller can conditionally insert."""
    blocks = [b for b in (thesis_anchor, bear_anchor) if b.strip()]
    if not blocks:
        return ""
    return "\n\n---\n\n".join(blocks) + "\n\n---\n\n"
