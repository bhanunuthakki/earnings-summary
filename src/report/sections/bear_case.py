"""§7 Bear case — strategically deep, structured, LLM-driven.

When `enable_llm` is False (default for dev runs), returns LLM_PENDING with
the prompt template embedded for review. When True, first checks the on-disk
cache (`data/bear_case/<TICKER>.json`); if the file exists and is younger than
`cache_ttl_days`, parses it and returns without re-calling the LLM. Otherwise
assembles inputs from the upstream sections, calls llm_client.generate_bear_case,
caches the raw JSON, and parses into FailureMode rows. Any LLM / parsing error
surfaces — no silent stub on failure (per repo's "fail loudly" rule).

The on-disk cache existed pre-Phase-A as a sidecar for cross-section anchoring
(load_bear_anchor); this module now also READS from it for cost containment.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import cast

from llm_client import generate_bear_case
from report.models import (
    BearCaseSection,
    EarningsSection,
    FailureMode,
    FinancialsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
)
from report.sections._common import missing

log = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_DAYS = 7


def build(
    ticker: str,
    repo_root: Path,
    enable_llm: bool,
    thesis: ThesisSection,
    financials: FinancialsSection,
    segments: SegmentsSection,
    earnings: EarningsSection,
    cache_ttl_days: int = DEFAULT_CACHE_TTL_DAYS,
    force_refresh: bool = False,
) -> BearCaseSection:
    if not enable_llm:
        return BearCaseSection(
            status=SectionStatus.LLM_PENDING,
            missing=missing(
                stage="SYNTHESIZE(bear_case_llm)",
                fix_command=(
                    f"python execution/build_artifacts.py --ticker {ticker.upper()} --enable-llm"
                ),
                detail=(
                    "Pass --enable-llm to populate this section. "
                    "Calls go through the Claude Code CLI (Sonnet 4.6) and bill against your "
                    "Pro/Max subscription; GEMINI_API_KEY is only the automatic fallback when the CLI fails."
                ),
            ),
        )

    if thesis.status == SectionStatus.MISSING_DATA or not thesis.thesis_full:
        return BearCaseSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(holdings)",
                fix_command=f"create micro_thesis/holdings/{ticker.upper()}.json",
                detail="Bear case requires a thesis + break conditions to ground itself.",
            ),
        )

    if not force_refresh:
        cached = _read_cache(ticker, repo_root, cache_ttl_days)
        if cached is not None:
            return cached

    response_text = generate_bear_case(
        ticker=ticker,
        thesis=thesis.thesis_full or "",
        break_conditions=thesis.break_conditions,
        last_quarter_summaries=_last_summaries(earnings, n=4),
        financials_table_md=_financials_md(financials),
        segments_table_md=_segments_md(segments),
        kpi_status_md=_kpi_status_md(thesis),
        ticker_specific_md=_ticker_specific_md(ticker, repo_root),
    )
    # Cache the parsed JSON so other LLM calls (per-quarter summary, news,
    # pairwise SayDo) can cross-pollinate the analyst's named bear failure
    # modes via load_bear_anchor() — see llm_client.py.
    _cache_bear_response(ticker, repo_root, response_text)
    return _parse_response(response_text)


def _read_cache(
    ticker: str, repo_root: Path, cache_ttl_days: int
) -> BearCaseSection | None:
    """Read data/bear_case/<TICKER>.json if it exists and is younger than TTL.

    Returns a parsed BearCaseSection on hit, None on miss / stale / unreadable.
    """
    path = repo_root / "data" / "bear_case" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    if datetime.now(timezone.utc) - mtime > timedelta(days=cache_ttl_days):
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return _parse_response(raw)
    except (json.JSONDecodeError, ValueError):
        # Corrupt cache — let the caller re-fetch.
        return None


def _cache_bear_response(ticker: str, repo_root: Path, response_text: str) -> None:
    """Persist the LLM's raw JSON to data/bear_case/<TICKER>.json. Best-effort:
    a write failure does not break the report build (the bear case section
    still renders from the parsed in-memory payload)."""
    try:
        payload = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text.strip(), flags=re.MULTILINE))
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    out_dir = repo_root / "data" / "bear_case"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}.json"
    try:
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return


def _ticker_specific_md(ticker: str, repo_root: Path) -> str:
    """Concatenate any per-ticker enhancement JSONs + investor-deck targets
    into prompt-ready markdown.

    Two sources, both optional:

      1. Per-ticker research JSONs at `data/ticker_specific/<TICKER>/*.json`
         (extract_nvo_patent_timeline.py and friends). Inlined verbatim as
         JSON code blocks. Used for evidence the LLM should ground on (patent
         expiries, drug pipeline milestones, regulatory readouts).

      2. Strategic targets the LLM extracted from investor decks
         (`strategic_targets` table). Rendered as a bulleted "## Strategic
         Targets" block so the bear case's failure-mode analysis can refute
         management's on-record commitments, not just project an LLM prior.

    Returns an empty string when neither source has content.
    """
    parts: list[str] = []

    targets_block = _strategic_targets_md(ticker, repo_root)
    if targets_block:
        parts.append(targets_block)

    base = repo_root / "data" / "ticker_specific" / ticker.upper()
    if base.exists():
        for path in sorted(base.glob("*.json")):
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            parts.append(f"### {path.stem}\n\n```json\n{body}\n```")

    return "\n\n".join(parts)


def _strategic_targets_md(ticker: str, repo_root: Path) -> str:
    """Render strategic_targets rows for `ticker` as a markdown block.

    Returns '' when the table is missing (fresh repo / migration not run),
    when the ticker has no rows, or on any read error — the section degrades
    gracefully so a missing DB doesn't break the report.
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            present = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategic_targets'"
            ).fetchone()
            if present is None:
                return ""
            rows = conn.execute(
                """
                SELECT target_kind, target_value, target_unit, target_period,
                       target_currency, narrative_excerpt
                FROM strategic_targets
                WHERE ticker = ?
                ORDER BY target_period, target_kind, id
                """,
                (ticker.upper(),),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "strategic_targets_read_failed",
                "ticker": ticker,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return ""
    if not rows:
        return ""

    lines: list[str] = [
        "## Strategic Targets",
        "",
        "Management's on-record multi-year commitments (extracted from investor decks):",
        "",
    ]
    for kind, value, unit, period, currency, excerpt in rows:
        value_str = _format_target_value(value, unit, currency)
        excerpt_short = (excerpt or "").strip()
        if len(excerpt_short) > 160:
            excerpt_short = excerpt_short[:157] + "..."
        if value_str:
            lines.append(
                f"- **{kind}** — {value_str} by {period} — \"{excerpt_short}\""
            )
        else:
            lines.append(f"- **{kind}** — {period} — \"{excerpt_short}\"")
    return "\n".join(lines)


def _format_target_value(
    value: object, unit: object, currency: object
) -> str:
    """Compose '50000 USD_M', '30%', etc. Returns '' for qualitative rows."""
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    unit_str = str(unit) if isinstance(unit, str) else ""
    if unit_str == "%":
        return f"{v:g}%"
    # Drop trailing .0 on whole numbers (50000.0 → 50000) for readability.
    formatted = f"{v:g}"
    suffix = unit_str if unit_str and unit_str != "qualitative" else ""
    return f"{formatted} {suffix}".strip()


def _parse_response(text: str) -> BearCaseSection:
    """LLM occasionally wraps JSON in ``` fences; strip and parse."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError(f"Bear case response was not a JSON object: {type(payload).__name__}")

    failure_modes_raw = payload.get("failure_modes") or []
    failure_modes = [FailureMode(**_coerce_failure_mode(fm)) for fm in failure_modes_raw]
    return BearCaseSection(
        status=SectionStatus.OK,
        failure_modes=failure_modes,
        most_underweighted=_str_or_none(payload.get("most_underweighted")),
        out_of_scope_flags=[
            s for s in (payload.get("out_of_scope_flags") or []) if isinstance(s, str)
        ],
    )


def _coerce_failure_mode(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"failure_mode must be an object; got {type(raw).__name__}")
    keys = (
        "hypothesis",
        "evidence_in_data",
        "leading_indicator",
        "quantitative_impact",
        "refutation_criteria",
    )
    return {k: str(raw.get(k, "")) for k in keys}


def _str_or_none(v: object) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def _last_summaries(earnings: EarningsSection, n: int) -> list[str]:
    """Pull the most recent N full LLM summaries for the prompt."""
    cards = (list(earnings.digest_quarters) + list(earnings.full_quarters))[-n:]
    return [c.summary_md for c in cards if c.summary_md]


def _financials_md(financials: FinancialsSection) -> str:
    if financials.status == SectionStatus.MISSING_DATA:
        return "(not yet extracted)"
    out = StringIO()
    out.write(
        "| Line item | "
        + " | ".join(financials.quarter_labels)
        + " | QoQ | YoY | 1Y CAGR | 3Y CAGR |\n"
    )
    out.write("|" + "|".join(["---"] * (len(financials.quarter_labels) + 5)) + "|\n")
    for li in financials.line_items:
        cells = [li.line_item] + [_fmt(v, li.digits) for v in li.values]
        cells.extend(
            _pct(g)
            for g in (li.growth.qoq, li.growth.yoy, li.growth.cagr_1y_ttm, li.growth.cagr_3y_ttm)
        )
        out.write("| " + " | ".join(cells) + " |\n")
    return out.getvalue()


def _segments_md(segments: SegmentsSection) -> str:
    if segments.status == SectionStatus.MISSING_DATA:
        return "(not yet extracted)"
    out = StringIO()
    for label, group in (
        ("Revenue by product", segments.revenue_by_product),
        ("Operating income", segments.operating_income),
    ):
        if not group:
            continue
        out.write(f"\n**{label}**\n")
        out.write("| Segment | " + " | ".join(segments.quarter_labels) + " | YoY |\n")
        out.write("|" + "|".join(["---"] * (len(segments.quarter_labels) + 2)) + "|\n")
        for s in group:
            cells = [s.segment_name] + [_fmt(v, 0) for v in s.values] + [_pct(s.growth.yoy)]
            out.write("| " + " | ".join(cells) + " |\n")
    return out.getvalue()


def _kpi_status_md(thesis: ThesisSection) -> str:
    out = StringIO()
    out.write("| Tier | KPI | Break condition | Current status |\n|---|---|---|---|\n")
    for k in thesis.kpi_ledger:
        out.write(f"| {k.tier} | {k.name} | {k.break_condition or '—'} | {k.current_status} |\n")
    return out.getvalue()


def _fmt(v: float | None, digits: int) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


# Kept for documentation parity with v0 — the real prompt is in llm_client.
PROMPT_TEMPLATE = cast(str, generate_bear_case.__doc__) or ""
