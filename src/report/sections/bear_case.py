"""§7 Bear case — strategically deep, structured, LLM-driven.

First checks the on-disk cache (`data/bear_case/<TICKER>.json`); if the file
exists and is younger than `cache_ttl_days`, parses it and returns without
re-calling the LLM — regardless of `enable_llm`, so a non-LLM build (the
default) still surfaces a previously-generated bear case instead of blanking
the section. When the cache misses and `enable_llm` is False, returns
LLM_PENDING with the prompt template embedded for review. Otherwise
assembles inputs from the upstream sections, calls llm_client.generate_bear_case,
caches the raw JSON, and parses into FailureMode rows. A transient empty or
unparseable LLM response — OR a transient call failure (timeout, non-zero exit,
both Claude + Gemini momentarily down) — degrades to a MISSING_DATA section that
names the reason (loud, not a silent stub) so one flaky §7 call can't abort the
whole multi-section brief build; re-running retries. Genuine hard stops still
surface per the repo's "fail loudly" rule: a monthly budget cap
(LLMBudgetExceeded) or a missing CLI (LLMSetupError) propagate — see
``is_hard_stop`` in src/llm/cli.py.

The on-disk cache existed pre-Phase-A as a sidecar for cross-section anchoring
(load_bear_anchor); this module now also READS from it for cost containment.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

from llm_client import generate_bear_case, is_hard_stop, load_ir_anchor
from report.models import (
    BearCaseSection,
    EarningsSection,
    FailureMode,
    FinancialsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
)
from report.render_clock import render_now
from report.sections._common import budget_gate, budget_skip_missing, missing
from report.sections._ts_signals import (
    format_signals_as_prompt_block,
    load_all_signals,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

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
    force_budget_bypass: bool = False,
    conn: sqlite3.Connection | None = None,
) -> BearCaseSection:
    # Serve a valid on-disk cache regardless of enable_llm so a non-LLM build
    # (the default) still surfaces a previously-generated bear case instead of
    # blanking the section. Mirrors company_description's unconditional cache
    # read; force_refresh bypasses the cache to force a fresh LLM call.
    if not force_refresh:
        cached = _read_cache(ticker, repo_root, cache_ttl_days)
        if cached is not None:
            return cached

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

    skip = budget_gate("bear_case", "Bear case (§7)", repo_root, bypass=force_budget_bypass)
    if skip is not None:
        return BearCaseSection(
            status=SectionStatus.BUDGET_SKIPPED,
            budget_skip=skip,
            missing=budget_skip_missing(ticker, "bear_case", skip),
        )

    try:
        response_text = generate_bear_case(
            ticker=ticker,
            thesis=thesis.thesis_full or "",
            break_conditions=thesis.break_conditions,
            last_quarter_summaries=_last_summaries(earnings, n=4),
            financials_table_md=_financials_md(financials),
            segments_table_md=_segments_md(segments),
            kpi_status_md=_kpi_status_md(thesis),
            ticker_specific_md=_ticker_specific_md(ticker, repo_root),
            ts_signals_md=_ts_signals_md(ticker, repo_root, conn=conn),
            ir_anchor_md=load_ir_anchor(repo_root, ticker),
            repo_root=repo_root,
        )
    except Exception as exc:
        # A hard LLM CALL exception (distinct from the empty/unparseable
        # *response* handled in the parse guard below). Genuine hard stops — a
        # monthly budget cap (LLMBudgetExceeded) or the CLI not being installed
        # (LLMSetupError) — must PROPAGATE so the build fails loudly; re-running
        # won't help. A transient operational failure (timeout, non-zero exit,
        # both Claude + Gemini momentarily down) degrades to a loud MISSING_DATA
        # banner so one flaky §7 call can't abort every other section + the
        # render and leave the daily cron with no artifact at all.
        if is_hard_stop(exc):
            raise
        log.error("bear_case LLM call failed for %s: %s: %s", ticker, type(exc).__name__, exc)
        return _degraded_bear(
            ticker,
            "The bear-case LLM call failed operationally (timeout, or both the "
            "Claude CLI and the Gemini fallback were momentarily unavailable). "
            "Re-run to retry.",
        )
    # Cache the parsed JSON so other LLM calls (per-quarter summary, news,
    # pairwise SayDo) can cross-pollinate the analyst's named bear failure
    # modes via load_bear_anchor() — see llm_client.py.
    _cache_bear_response(ticker, repo_root, response_text)
    try:
        return _parse_response(response_text)
    except (json.JSONDecodeError, ValueError) as exc:
        # A transient empty / unparseable LLM response must fail at SECTION
        # scope, not abort the whole multi-section build: one flaky §7 call
        # would otherwise cost every other section + the render, and leave the
        # daily cron with no artifact at all. "Fail loudly" still holds — we
        # surface a MISSING_DATA banner naming the reason, not a silent stub —
        # but the rest of the brief renders and a re-run retries.
        log.error(
            "bear_case parse failed for %s (%d-char response): %s",
            ticker,
            len(response_text or ""),
            exc,
        )
        return _degraded_bear(
            ticker,
            "The bear-case LLM call returned an empty or unparseable "
            "response (usually transient). Re-run to retry.",
        )


def _degraded_bear(ticker: str, detail: str) -> BearCaseSection:
    """Loud MISSING_DATA banner for a transient §7 failure — an empty /
    unparseable response OR a transient call exception. Names the reason so the
    failure is visible (not a silent stub); a re-run retries. Hard stops
    (budget / setup) never reach here — they propagate (see ``is_hard_stop``)."""
    return BearCaseSection(
        status=SectionStatus.MISSING_DATA,
        missing=missing(
            stage="SYNTHESIZE(bear_case_llm)",
            fix_command=(
                f"python execution/build_artifacts.py --ticker {ticker.upper()} --enable-llm"
            ),
            detail=detail,
        ),
    )


def _read_cache(ticker: str, repo_root: Path, cache_ttl_days: int) -> BearCaseSection | None:
    """Read data/bear_case/<TICKER>.json if it exists and is younger than TTL.

    Returns a parsed BearCaseSection on hit, None on miss / stale / unreadable.
    """
    path = repo_root / "data" / "bear_case" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None
    if render_now() - mtime > timedelta(days=cache_ttl_days):
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
        payload = _loads_first_json_object(response_text)
    except (json.JSONDecodeError, ValueError):
        return
    out_dir = repo_root / "data" / "bear_case"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}.json"
    try:
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return


def _ts_signals_md(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Render every persisted time-series signal for the ticker as a
    Disconfirmation-candidates block.

    Bear case wants the broadest coverage — any decelerating trend,
    anomaly, or sustained inflection is a potential thesis-disconfirming
    pattern worth engaging with — so we pull ALL signals (financial + KPI
    + segment) rather than enumerate a metric list. Empty string when no
    rows exist for the ticker so the prompt stays clean.
    """
    signals = load_all_signals(ticker, repo_root=repo_root, conn=conn)
    return format_signals_as_prompt_block(signals, heading="Time-Series Disconfirmation Candidates")


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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
            lines.append(f'- **{kind}** — {value_str} by {period} — "{excerpt_short}"')
        else:
            lines.append(f'- **{kind}** — {period} — "{excerpt_short}"')
    return "\n".join(lines)


def _format_target_value(value: object, unit: object, currency: object) -> str:
    """Compose '50000 USD_M', '30%', etc. Returns '' for qualitative rows."""
    if not isinstance(value, (int, float, str)):
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


def _loads_first_json_object(text: str) -> dict[str, Any]:
    """Decode the first JSON object out of an LLM response.

    Tolerant of three things the model does intermittently: wrapping the JSON
    in ``` fences, prefixing a sentence of prose, and — the case that used to
    degrade bear cases to MISSING_DATA — appending commentary *after* the
    closing brace (a bare ``json.loads`` raises "Extra data: ... char N" on
    that trailing text). Strip fences, seek the first ``{``, then ``raw_decode``
    so anything trailing the object is ignored.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("bear case response contained no JSON object")
    payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(payload, dict):
        raise ValueError(f"Bear case response was not a JSON object: {type(payload).__name__}")
    return cast("dict[str, Any]", payload)


def _parse_response(text: str) -> BearCaseSection:
    """Parse the bear-case LLM response. See ``_loads_first_json_object`` for
    the fence/prose tolerance that keeps a chatty response from degrading the
    section to MISSING_DATA."""
    payload = _loads_first_json_object(text)

    raw_modes = payload.get("failure_modes")
    failure_modes_raw = cast("list[Any]", raw_modes) if isinstance(raw_modes, list) else []
    failure_modes = [FailureMode(**_coerce_failure_mode(fm)) for fm in failure_modes_raw]
    raw_flags = payload.get("out_of_scope_flags")
    flags = cast("list[Any]", raw_flags) if isinstance(raw_flags, list) else []
    return BearCaseSection(
        status=SectionStatus.OK,
        failure_modes=failure_modes,
        most_underweighted=_str_or_none(payload.get("most_underweighted")),
        out_of_scope_flags=[s for s in flags if isinstance(s, str)],
    )


def _coerce_failure_mode(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"failure_mode must be an object; got {type(raw).__name__}")
    values = cast("dict[object, object]", raw)
    keys = (
        "hypothesis",
        "evidence_in_data",
        "leading_indicator",
        "quantitative_impact",
        "refutation_criteria",
    )
    return {k: str(values.get(k, "")) for k in keys}


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
