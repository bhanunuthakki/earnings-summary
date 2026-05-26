"""Synthesis lenses — cross-section LLM analytical artifacts.

The system has rich per-section LLM rendering (bear case, news, summary,
exec-comp alignment). What it has been MISSING is cross-cutting synthesis:
the LLM reading across sections, across quarters, across tickers to find
patterns no single section can name.

Each lens here is a focused prompt that reads a specific slice of the data
and produces an analyst-grade artifact. All lenses share:
  - One generic runner (run_lens)
  - llm_artifacts cache (purpose='lens:<name>', input_sha256 dedup)
  - Optional rendering integration via the lens name
  - Markdown output (renderers consume directly; no per-lens schema)

The 9 lenses shipped here are NOT just decorations — each was chosen because
it answers a specific analytical question the per-section pipeline cannot:

  thesis_drift_qoq    — How did this quarter's print engage with the named
                         failure modes from the prior bear case?
  five_min_reread     — What changed since last brief, what action do I
                         recommend, what would change my mind?
  cross_portfolio     — Cross-ticker patterns: convergence clusters,
                         capital-allocation reads, this-week's-deeper-look.
  bull_case           — What would have to happen for this to work
                         spectacularly? Forces the LLM to name conditions
                         that would justify a 3-5x return, not just hold.
  reverse_dcf         — At today's price, what is the market implying?
                         Where does that diverge from the thesis?
  catalyst_calendar   — Per-holding upcoming catalysts + bingo card for
                         what to listen for.
  filing_diff         — Year-over-year 10-K Item 1A diff narrative: what
                         management says has changed about how they see
                         the business.
  footnote_anomaly    — Item 8 footnote read: new related-party trxs,
                         contingent liability shifts, off-balance-sheet
                         appearances, restatements.
  underweighted_facts — Per-quarter: 5 things consensus is missing.

Each lens has:
  - LENS_NAME           (slug)
  - LENS_MODEL          (sonnet vs opus per analytical depth needed)
  - build_context()     (loads inputs, returns dict + cache_inputs list)
  - PROMPT_TEMPLATE     (markdown with .format() placeholders)
  - SCOPE               ('ticker' default, 'portfolio' for cross-ticker)

Run via execution/run_lens.py --ticker GOOG --lens thesis_drift_qoq
Or run all per-ticker lenses: execution/run_lens.py --ticker GOOG --all
Or run portfolio lenses:        execution/run_lens.py --lens cross_portfolio
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, cast

from llm_artifact_store import (
    Artifact,
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import call_llm

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LensContext:
    """The materialized inputs for one lens invocation."""

    ticker: str | None
    template_kwargs: dict[str, str]
    cache_inputs: list[bytes | str]
    source_doc_ids: list[int]
    parent_artifact_ids: list[int]


@dataclass(slots=True)
class Lens:
    """A single analytical synthesis prompt + its context-loading contract."""

    name: str
    model: str
    scope: str  # 'ticker' or 'portfolio'
    prompt_template: str
    build_context: Callable[[str | None, Path], LensContext | None]


# ===========================================================================
# Generic runner
# ===========================================================================


def run_lens(
    lens: Lens,
    *,
    ticker: str | None,
    repo_root: Path,
    force: bool = False,
) -> Artifact | None:
    """Run one lens. Cached via llm_artifacts; returns the artifact (cached or fresh).

    Returns None when:
      - the lens cannot build its context (insufficient input data)
      - DB unavailable
      - LLM call fails outright (logged; doesn't raise)
    """
    db_path = repo_root / "data" / "portfolio.db"
    purpose = f"lens:{lens.name}"

    try:
        ctx = lens.build_context(ticker, repo_root)
    except Exception as exc:  # noqa: BLE001 — defensive across context loaders
        log.warning(
            {"event": "lens_context_build_failed", "lens": lens.name, "ticker": ticker, "error": str(exc)}
        )
        return None
    if ctx is None:
        log.debug(
            {"event": "lens_context_empty", "lens": lens.name, "ticker": ticker, "reason": "insufficient_data"}
        )
        return None

    # Cache hit check — bypass on force=True
    if not force:
        existing = read_current(
            ticker=ctx.ticker,
            purpose=purpose,
            scope=lens.scope,
            db_path=db_path,
        )
        if existing is not None and not existing.dirty:
            new_sha = compute_input_sha256(
                prompt_version="v1", cache_inputs=ctx.cache_inputs
            )
            if new_sha == existing.input_sha256:
                log.info({"event": "lens_cache_hit", "lens": lens.name, "ticker": ticker, "artifact_id": existing.id})
                return existing

    try:
        prompt = lens.prompt_template.format(**ctx.template_kwargs)
    except KeyError as exc:
        log.warning(
            {"event": "lens_template_missing_key", "lens": lens.name, "ticker": ticker, "key": str(exc)}
        )
        return None

    try:
        content = call_llm(
            prompt,
            purpose=purpose,
            ticker=ctx.ticker,
            scope=lens.scope,
            model=lens.model,
        )
    except Exception as exc:  # noqa: BLE001 — record + return
        log.warning(
            {"event": "lens_llm_call_failed", "lens": lens.name, "ticker": ticker, "error": str(exc)}
        )
        return None

    artifact_id, _ = upsert(
        UpsertRequest(
            ticker=ctx.ticker,
            purpose=purpose,
            scope=lens.scope,
            content_md=content,
            cache_inputs=ctx.cache_inputs,
            model=lens.model,
            source_doc_ids=ctx.source_doc_ids,
            parent_artifact_ids=ctx.parent_artifact_ids,
        ),
        db_path=db_path,
    )
    if artifact_id is None:
        return None
    return read_current(
        ticker=ctx.ticker, purpose=purpose, scope=lens.scope, db_path=db_path
    )


def read_lens_artifact(
    *, ticker: str | None, lens_name: str, scope: str = "ticker", repo_root: Path
) -> Artifact | None:
    """Public read of a cached lens artifact. Used by renderers."""
    return read_current(
        ticker=ticker,
        purpose=f"lens:{lens_name}",
        scope=scope,
        db_path=repo_root / "data" / "portfolio.db",
    )


# ===========================================================================
# Shared context helpers — keep individual lens loaders concise
# ===========================================================================


def _read_holdings_json(ticker: str, repo_root: Path) -> dict[str, object]:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_prior_bear_case(ticker: str, repo_root: Path) -> Artifact | None:
    return read_current(
        ticker=ticker.upper(),
        purpose="bear_case",
        db_path=repo_root / "data" / "portfolio.db",
    )


def _load_recent_summaries(ticker: str, repo_root: Path, n: int = 4) -> list[tuple[str, str]]:
    """Returns [(quarter_label, summary_text), ...] newest first, up to n."""
    tmp = repo_root / ".tmp"
    out: list[tuple[str, str]] = []
    if not tmp.exists():
        return out
    candidates: list[tuple[str, Path]] = []
    for p in tmp.glob(f"{ticker.upper()}_Q*_*_summary.txt"):
        candidates.append((p.stem, p))
    # Sort newest first by filename (which encodes Q + year)
    candidates.sort(key=lambda t: t[0], reverse=True)
    for stem, p in candidates[:n]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            out.append((stem, text))
        except OSError:
            continue
    return out


def _load_recent_insider_transactions(
    ticker: str, repo_root: Path, days: int = 180
) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='insider_transactions'"
            ).fetchone()
            is None
        ):
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """
            SELECT transaction_date, insider_name, insider_title, transaction_type,
                   shares, transaction_value, is_10b5_1
            FROM insider_transactions
            WHERE ticker = ? AND transaction_date >= ?
            ORDER BY transaction_date DESC
            LIMIT 200
            """,
            (ticker.upper(), cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_predictions(ticker: str, repo_root: Path) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
            ).fetchone()
            is None
        ):
            return []
        rows = conn.execute(
            """
            SELECT source_kind, made_at, target_period, prediction_md,
                   kpi_name, target_value, realized_value, outcome
            FROM predictions
            WHERE ticker = ?
            ORDER BY made_at DESC
            LIMIT 30
            """,
            (ticker.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_dcf(ticker: str, repo_root: Path) -> dict[str, object] | None:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT npv_per_share, live_price, over_under_pct, mos_bar_used,
                   wacc, terminal_growth, fcf_margin, base_revenue, revenue_growths_json,
                   valuation_date
            FROM dcf_runs
            WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '')
            ORDER BY valuation_date DESC LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _load_latest_financials_snapshot(ticker: str, repo_root: Path, n_periods: int = 8) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT period_end, line_item, value
            FROM financial_facts
            WHERE ticker = ? AND fiscal_period_type IN ('Q1','Q2','Q3','Q4')
            ORDER BY period_end DESC
            LIMIT 200
            """,
            (ticker.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _summarize_insiders(rows: list[dict[str, object]], max_items: int = 25) -> str:
    if not rows:
        return "(no recent insider activity)"
    lines: list[str] = []
    for r in rows[:max_items]:
        dt = str(r.get("transaction_date") or "")[:10]
        name = str(r.get("insider_name") or "")
        title = str(r.get("insider_title") or "")
        kind = str(r.get("transaction_type") or "")
        sh = float(r.get("shares") or 0)
        val = r.get("transaction_value")
        val_str = (
            f"${float(val) / 1e6:.1f}M"
            if val is not None and float(val) >= 1e6
            else f"${float(val) / 1e3:.0f}K"
            if val is not None
            else "?"
        )
        rule = " [10b5-1]" if bool(r.get("is_10b5_1")) else ""
        lines.append(
            f"- {dt} · {name} ({title or '?'}) · {kind.replace('_', ' ')} · {sh:,.0f} sh · {val_str}{rule}"
        )
    return "\n".join(lines)


def _summarize_predictions(rows: list[dict[str, object]], max_items: int = 20) -> str:
    if not rows:
        return "(no recorded predictions)"
    lines: list[str] = []
    for r in rows[:max_items]:
        made = str(r.get("made_at") or "")[:10]
        tgt = str(r.get("target_period") or "")[:10] if r.get("target_period") else "-"
        kpi = str(r.get("kpi_name") or "?")
        outc = str(r.get("outcome") or "pending")
        narr = str(r.get("prediction_md") or "")[:140]
        lines.append(
            f"- {made} → {tgt} · {kpi} · {outc.upper()} · {narr}"
        )
    return "\n".join(lines)


def _thesis_block(ticker: str, repo_root: Path) -> str:
    h = _read_holdings_json(ticker, repo_root)
    thesis = str(h.get("thesis") or "")
    kpis = h.get("tier_1_kpis") or []
    kpi_lines: list[str] = []
    if isinstance(kpis, list):
        for k in kpis:
            if isinstance(k, dict):
                name = k.get("name")
                bc = k.get("break_condition")
                if isinstance(name, str):
                    kpi_lines.append(f"- **{name}** — breaks if {bc or '?'}")
    parts: list[str] = []
    if thesis.strip():
        parts.append(f"**Thesis:**\n{thesis.strip()}")
    if kpi_lines:
        parts.append("**Tier-1 KPIs:**\n" + "\n".join(kpi_lines))
    return "\n\n".join(parts) if parts else "(no holdings JSON for this ticker)"


def _sha8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


# ===========================================================================
# 1. thesis_drift_qoq — quarter-over-quarter thesis evolution
# ===========================================================================


_PROMPT_THESIS_DRIFT = """You are a senior buy-side analyst writing the quarter-over-quarter thesis
evolution memo for {ticker}. This is NOT a fresh bear case; it is the
*update* between two points in time, narrating how the named failure modes
held up to evidence.

**The thesis (anchor):**
{thesis_block}

**Prior bear case (failure modes the analyst was tracking):**
{prior_bear_case}

**This quarter's earnings summary (newest first):**
{recent_summaries}

**Predictions outcomes since the prior bear case (mgmt commitments,
risk-factor materialization, prior bear-case hypotheses):**
{predictions}

Produce a 350-500 word memo with three sections:

## 1. Failure modes that got MORE probable
List 1-3 prior failure modes whose probability went UP this quarter. For
each: NAME the specific number / disclosure / data point that drove the
shift. No hand-waving.

## 2. Failure modes that got LESS probable (or refuted)
Same shape. NAME the refuting evidence. If a hypothesis was outright
refuted, say so plainly — it's data even when it's negative.

## 3. NEW failure mode this quarter surfaced
What's the new risk that wasn't on the radar last quarter? Be specific: a
mechanism, a leading indicator, a refutation criterion. If nothing new
surfaced, say "no new failure modes — thesis is converging."

Voice: senior analyst writing for themselves. Opinion-bearing, terse,
non-consensus where the data supports it. No restating the thesis verbatim;
the reader already knows it. NO restating the prior failure modes verbatim;
they're the input. Focus on the DELTA.
"""


def _ctx_thesis_drift(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    prior = _load_prior_bear_case(ticker, repo_root)
    if prior is None or not (prior.content_md or prior.content_json):
        return None  # no prior bear case to drift against
    summaries = _load_recent_summaries(ticker, repo_root, n=4)
    if not summaries:
        return None
    predictions = _load_predictions(ticker, repo_root)
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "prior_bear_case": (
                (prior.content_md or json.dumps(prior.content_json or {}, indent=2))[:6000]
            ),
            "recent_summaries": "\n\n---\n\n".join(
                f"### {q}\n{txt[:3000]}" for q, txt in summaries
            ),
            "predictions": _summarize_predictions(predictions),
        },
        cache_inputs=[
            ticker,
            prior.input_sha256,
            *(f"{q}:{_sha8(txt)}" for q, txt in summaries),
            _sha8(json.dumps(predictions, default=str, sort_keys=True)),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[prior.id],
    )


# ===========================================================================
# 2. five_min_reread — what-changed + recommended action
# ===========================================================================


_PROMPT_FIVE_MIN_REREAD = """You are an analyst writing a 5-minute reread brief for {ticker}. The user
already knows the thesis. They want THREE things in 250-400 words:

**Thesis anchor:**
{thesis_block}

**DCF snapshot:**
{dcf_summary}

**Latest earnings summary (most recent quarter):**
{latest_summary}

**Recent insider activity (last 90d):**
{insider_activity}

**Predictions outcomes (last 12mo):**
{predictions}

Produce a memo with EXACTLY these three sections:

## 1. What changed
List 2-4 specific changes since the analyst last looked. Each must name a
concrete number, disclosure, or insider event. Sort by analytical
importance, NOT chronological.

## 2. Recommended action
ONE of: ADD <N%> / HOLD / TRIM <N%> / SELL. Pick a percentage size (of
current position) when ADD or TRIM. Justify the size in one sentence using
the DCF over/under, the trigger ladder thresholds, and what the recent
data tells you. Be opinionated — vague "watch for now" is not allowed
unless genuinely no thesis-relevant data has moved.

## 3. What would change my mind
2-3 specific data points that, if disclosed in the next 1-2 quarters,
would flip the recommendation. Concrete and falsifiable: "Cloud revenue
growth dropping below 25% YoY for 2 consecutive quarters" not "Cloud
disappointing."

Voice: terse, opinion-bearing, decision-oriented. The reader is making a
capital allocation choice in the next 5 minutes. Help them.
"""


def _ctx_five_min_reread(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    dcf = _load_dcf(ticker, repo_root)
    summaries = _load_recent_summaries(ticker, repo_root, n=1)
    insiders = _load_recent_insider_transactions(ticker, repo_root, days=90)
    predictions = _load_predictions(ticker, repo_root)
    if not summaries and not dcf and not insiders:
        return None

    dcf_summary = "(no DCF run)"
    if dcf:
        ou = float(dcf.get("over_under_pct") or 0) * 100 if dcf.get("over_under_pct") else 0.0
        npv = dcf.get("npv_per_share")
        live = dcf.get("live_price")
        mos = dcf.get("mos_bar_used")
        dcf_summary = (
            f"NPV/share: ${float(npv or 0):.0f} · Live: ${float(live or 0):.0f} · "
            f"Over/Under: {ou:+.1f}% · MoS bar: {float(mos or 0) * 100:.0f}% · "
            f"As of: {dcf.get('valuation_date')}"
        )
    latest_summary = summaries[0][1][:4000] if summaries else "(no recent earnings summary)"

    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "dcf_summary": dcf_summary,
            "latest_summary": latest_summary,
            "insider_activity": _summarize_insiders(insiders),
            "predictions": _summarize_predictions(predictions, max_items=10),
        },
        cache_inputs=[
            ticker,
            _sha8(dcf_summary),
            _sha8(latest_summary),
            _sha8(_summarize_insiders(insiders)),
            _sha8(_summarize_predictions(predictions)),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 3. bull_case — mirror of bear_case
# ===========================================================================


_PROMPT_BULL_CASE = """You are writing a structural BULL case for {ticker}. The system already has
a strong bear case. Your job here is the inverse: what WOULD have to happen
for this to work spectacularly — say, 3-5x over 5 years?

**Thesis:**
{thesis_block}

**DCF snapshot:**
{dcf_summary}

**Recent earnings summary:**
{latest_summary}

Produce a 300-450 word memo with three sections:

## 1. The asymmetric upside thesis
ONE paragraph naming the specific mechanism that, if it plays out, produces
a 3-5x return. NOT "AI is big" — concrete: which segment, which margin
inflection, which TAM expansion, which competitive moat that compounds.
Quantify the upside path.

## 2. The conditions that have to hold
List 3-5 specific conditions that have to be true for #1 to play out. Each
should be falsifiable and trackable. Avoid generic "execution" — name the
KPI that would confirm progress on each condition.

## 3. Why the market is missing it
ONE paragraph: WHY isn't this priced in today? Is it disclosure-driven
(genuinely hidden in filings)? Is it analytical bias (legacy framework
mismatch)? Is it patience-driven (timeline mismatch with sell-side cycles)?
Be specific about the source of the mispricing.

Voice: contrarian-credible. Not "buy this stock" promotional — analyst who
has examined the bear case and is naming what would falsify it on the
upside. Most underweighted by consensus.
"""


def _ctx_bull_case(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    summaries = _load_recent_summaries(ticker, repo_root, n=1)
    dcf = _load_dcf(ticker, repo_root)
    dcf_summary = "(no DCF run)"
    if dcf:
        npv = dcf.get("npv_per_share")
        live = dcf.get("live_price")
        ou = (float(dcf.get("over_under_pct") or 0)) * 100 if dcf.get("over_under_pct") else 0.0
        dcf_summary = (
            f"NPV/share: ${float(npv or 0):.0f} · Live: ${float(live or 0):.0f} · "
            f"Over/Under: {ou:+.1f}%"
        )
    if not summaries and not dcf:
        return None
    latest = summaries[0][1][:3000] if summaries else "(no recent earnings summary)"
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "dcf_summary": dcf_summary,
            "latest_summary": latest,
        },
        cache_inputs=[ticker, _sha8(latest), _sha8(dcf_summary)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 4. reverse_dcf — market-implied narrative
# ===========================================================================


_PROMPT_REVERSE_DCF = """You are reverse-engineering the market-implied narrative for {ticker}.
The analyst's DCF says fair value is one number; the market is trading at
another. Your job: tell the analyst what the market is *implicitly
betting* and where that diverges from the thesis.

**Analyst's DCF assumptions:**
{dcf_assumptions}

**Current price + DCF over/under:**
{dcf_summary}

**Analyst's thesis:**
{thesis_block}

**Latest earnings summary:**
{latest_summary}

Produce a 250-400 word memo with three sections:

## 1. What the market is implying
At the current price, what combination of (revenue growth × FCF margin ×
terminal multiple) is the market pricing in? Solve approximately — name
the implied 5-year revenue CAGR, the implied steady-state FCF margin, and
the implied exit multiple. Compare each to the analyst's DCF.

## 2. The market's implicit thesis
What is the market *betting* that the analyst's DCF doesn't capture (or
that the analyst rejects)? Examples: "Market is pricing in faster Cloud
margin expansion than the DCF assumes" or "Market is pricing in a Search
TAM that contracts faster than the DCF allows for." Be SPECIFIC about
which assumption the market is more aggressive on.

## 3. Where the edge is
If the analyst's thesis is right, what specific data point in the next
1-4 quarters will start to close the gap? OR if the market is right and
the analyst is wrong, what would the analyst have to update?

Voice: analytical, quantitative-where-possible, edge-naming. The output
should help the analyst decide: is my edge real, or am I pricing the same
thing differently than the market?
"""


def _ctx_reverse_dcf(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    dcf = _load_dcf(ticker, repo_root)
    if not dcf:
        return None
    summaries = _load_recent_summaries(ticker, repo_root, n=1)
    assumptions = json.dumps(
        {
            "npv_per_share": dcf.get("npv_per_share"),
            "wacc": dcf.get("wacc"),
            "terminal_growth": dcf.get("terminal_growth"),
            "fcf_margin": dcf.get("fcf_margin"),
            "base_revenue": dcf.get("base_revenue"),
            "revenue_growths": dcf.get("revenue_growths_json"),
            "as_of": dcf.get("valuation_date"),
        },
        default=str,
        indent=2,
    )
    ou = (float(dcf.get("over_under_pct") or 0)) * 100 if dcf.get("over_under_pct") else 0.0
    dcf_summary = (
        f"NPV/share: ${float(dcf.get('npv_per_share') or 0):.0f} · "
        f"Live: ${float(dcf.get('live_price') or 0):.0f} · "
        f"Over/Under: {ou:+.1f}%"
    )
    latest = summaries[0][1][:2500] if summaries else "(none)"
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "dcf_assumptions": assumptions,
            "dcf_summary": dcf_summary,
            "thesis_block": _thesis_block(ticker, repo_root),
            "latest_summary": latest,
        },
        cache_inputs=[ticker, _sha8(assumptions), _sha8(latest)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 5. underweighted_facts — 5 things consensus is missing
# ===========================================================================


_PROMPT_UNDERWEIGHTED = """You are writing the "5 underweighted facts" memo for {ticker} this quarter.
Sell-side reports the headline numbers and management's narrative. Your job:
surface 5 specific data points from this quarter that the consensus is
mispricing or ignoring.

**Thesis:**
{thesis_block}

**Latest earnings summary:**
{latest_summary}

**Latest financials snapshot (last 8Q line items):**
{financials_snapshot}

Produce a numbered list of 5 underweighted facts. For each:
- **The fact** (one sentence, with the specific number / disclosure)
- **Why consensus is missing it** (one sentence — disclosure pattern, framework
  mismatch, sell-side coverage gap, timing)
- **Why it matters for the thesis** (one sentence linking to the tier-1
  KPIs or a named failure mode)

Format: tight, scannable bullets. No more than 80 words per item.
The 5 items together should add up to a thesis the consensus does not yet
hold. The ranking matters — #1 is the highest-conviction underweighted
read.
"""


def _ctx_underweighted(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    summaries = _load_recent_summaries(ticker, repo_root, n=1)
    if not summaries:
        return None
    financials = _load_latest_financials_snapshot(ticker, repo_root)
    fin_summary = (
        "\n".join(
            f"- {r['period_end'][:10]} · {r['line_item']}: {float(r['value'] or 0):,.0f}"
            for r in financials[:40]
        )
        if financials
        else "(no financial facts available)"
    )
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "latest_summary": summaries[0][1][:4000],
            "financials_snapshot": fin_summary,
        },
        cache_inputs=[
            ticker,
            _sha8(summaries[0][1]),
            _sha8(fin_summary),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 6. catalyst_calendar — upcoming events + bingo card
# ===========================================================================


_PROMPT_CATALYST_CALENDAR = """You are building the catalyst calendar for {ticker} for the next 90 days.
For each upcoming catalyst, produce a one-paragraph pre-event brief.

**Thesis:**
{thesis_block}

**Latest earnings summary:**
{latest_summary}

**Recent predictions (mgmt commitments + bear-case hypotheses + risks):**
{predictions}

Produce a memo:

## Upcoming catalysts (next 90d)
For each catalyst you can infer from the data (next earnings call, scheduled
investor days, regulatory rulings, drug readouts, contract decisions, etc.):

### [Date estimate] · [Catalyst type]
- **What to watch:** 2-3 SPECIFIC numbers / disclosures / language signals
  that would meaningfully update the thesis.
- **Named failure modes engaged:** which prior bear-case hypotheses will
  this catalyst test? Cite by name.
- **Bingo card:** 2-3 things a credible analyst would ask in Q&A but
  consensus probably won't.

If there are no clear catalysts in the data, say so plainly and identify
the EARLIEST inferable signal (e.g. monthly subscriber disclosures, weekly
flight data, etc.).
"""


def _ctx_catalyst_calendar(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    summaries = _load_recent_summaries(ticker, repo_root, n=1)
    predictions = _load_predictions(ticker, repo_root)
    if not summaries:
        return None
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "latest_summary": summaries[0][1][:3500],
            "predictions": _summarize_predictions(predictions),
        },
        cache_inputs=[ticker, _sha8(summaries[0][1])],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 7. filing_diff_narrative — year-over-year 10-K Item 1A diff
# ===========================================================================


_PROMPT_FILING_DIFF = """You are narrating the year-over-year 10-K Item 1A risk-factor changes for
{ticker}. The schema's `risk_factors` table has new/removed/reworded
markers from automated diffing; your job is to read those and tell the
analyst what management is actually SIGNALING with the changes.

**Thesis:**
{thesis_block}

**Risk factor diffs (current 10-K vs prior 10-K):**
{risk_diffs}

Produce a 250-400 word memo with these sections:

## What was ADDED
Which new risks did management feel obligated to disclose this year? For
each meaningful addition, name the specific business mechanism it reveals.

## What was REMOVED
What risks did management feel safe dropping? Removals are sometimes more
informative than additions — they signal what management thinks has been
de-risked.

## What was REWORDED — and how
The wording delta matters. "Material adverse impact" → "could affect
results" is a softening; the reverse is a hardening. Call these out
explicitly with the before/after phrasing.

## Net read
ONE paragraph: what is management *implicitly saying* about how they see
the business evolving? Is the risk profile getting more concentrated? Are
new categories emerging (regulatory, technological, geographic)? Does the
language tone match management's commentary on calls?

If no risk_factors data exists yet, return a one-line "no Item 1A diff
data ingested yet — run `python execution/extract_risk_factors.py --ticker
{ticker}`."
"""


def _ctx_filing_diff(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_factors'"
            ).fetchone()
            is None
        ):
            return None
        rows = conn.execute(
            """
            SELECT fiscal_year, ordinal, heading, body_md, category, vs_prior_year, reword_diff_md
            FROM risk_factors WHERE ticker = ?
            ORDER BY fiscal_year DESC, ordinal ASC
            LIMIT 200
            """,
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    diff_block = "\n".join(
        f"- [{r['fiscal_year']}#{r['ordinal']}] {r['vs_prior_year'] or 'unchanged'} · "
        f"{r['category'] or '?'} · {r['heading'] or '(no heading)'}: "
        f"{(r['reword_diff_md'] or r['body_md'] or '')[:200]}"
        for r in rows[:60]
    )
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "risk_diffs": diff_block,
        },
        cache_inputs=[ticker, _sha8(diff_block)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 8. footnote_anomaly — Item 8 footnote read
# ===========================================================================


_PROMPT_FOOTNOTE_ANOMALY = """You are scanning {ticker}'s Item 8 financial-statement footnotes for the
signals 99% of analysts ignore: related-party transactions, contingent
liabilities, off-balance-sheet items, lease commitments, restatements,
stock-based-comp footnote shifts.

**Thesis:**
{thesis_block}

**Footnote facts extracted from recent filings:**
{footnotes}

Produce a 250-400 word memo. For each anomaly worth surfacing (up to 5):

### [Fact type] · [Period]
- **The disclosure** (one sentence, with the specific amount + counterparty
  if available)
- **Why it's anomalous** (vs. last year, vs. peer norm, vs. management
  commentary)
- **Read for the thesis** (one sentence: is this a yellow flag, a real
  signal, or normal-course noise?)

If no anomalies surface, say so. If footnote_facts has no data for this
ticker yet, return a one-line "no footnote data ingested — run
`python execution/extract_footnotes.py --ticker {ticker}`."
"""


def _ctx_footnote_anomaly(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='footnote_facts'"
            ).fetchone()
            is None
        ):
            return None
        rows = conn.execute(
            """
            SELECT period_end, fact_type, description_md, amount, currency, counterparty, status
            FROM footnote_facts WHERE ticker = ?
            ORDER BY period_end DESC LIMIT 80
            """,
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    block = "\n".join(
        f"- {str(r['period_end'])[:10]} · {r['fact_type']} · "
        f"{float(r['amount'] or 0):,.0f} {r['currency'] or ''} · "
        f"{r['counterparty'] or '?'} · {r['status'] or '?'} · "
        f"{(r['description_md'] or '')[:200]}"
        for r in rows
    )
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": _thesis_block(ticker, repo_root),
            "footnotes": block,
        },
        cache_inputs=[ticker, _sha8(block)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# 9. cross_portfolio — weekly cross-ticker synthesis
# ===========================================================================


_PROMPT_CROSS_PORTFOLIO = """You are writing the weekly cross-portfolio synthesis memo. The analyst
holds 11 portfolio names + tracks 63 watchlist names. Your job: find
cross-ticker patterns no per-ticker brief can see.

**Portfolio holdings + latest data:**
{portfolio_summary}

**Recent insider activity (cross-ticker, last 30d):**
{insider_summary}

**Predictions outcomes (cross-ticker, last 90d):**
{predictions_summary}

Produce a 500-700 word memo with these sections:

## This week's most-look name
ONE holding that warrants the deepest look this week. Be specific about
WHY — what data point moved, what hypothesis was tested, what action
might be warranted. NOT the worst-performing or best-performing — the
one where the analytical picture changed most.

## Thematic convergence clusters
Identify 1-3 clusters of holdings exposed to the same thesis driver. For
each cluster: name the holdings, name the shared driver, and name the
joint downside if the driver fails. Examples: "GOOG / META / AMZN — capex
absorption" or "JPM / HDB / IBN — credit cycle exposure."

## Capital allocation suggestions
Across the portfolio, what 1-3 SIZE adjustments would the data suggest?
"ADD 0.5% to META — bear-case failure mode #2 was refuted this quarter" or
"TRIM 1% from NVDA — insider cluster + DCF +25% over fair." Be specific.

## What I'd want to spend more time on
ONE specific analytical question worth a deeper investigation this week.

Voice: portfolio manager writing to themselves — terse, opinion-bearing,
linking specific data points to capital allocation. No hedging language
without a specific reason.
"""


def _ctx_cross_portfolio(ticker: str | None, repo_root: Path) -> LensContext | None:
    # ticker is None for portfolio scope; load all portfolio holdings
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        tickers = [
            r[0]
            for r in conn.execute(
                "SELECT ticker FROM tracked_companies WHERE archived_at IS NULL AND list_type = 'portfolio' ORDER BY ticker"
            )
        ]
        # Per-ticker snapshot: thesis, dcf, latest bear case head, recent insider count
        port_lines: list[str] = []
        for t in tickers:
            dcf = conn.execute(
                "SELECT npv_per_share, live_price, over_under_pct FROM dcf_runs "
                "WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '') "
                "ORDER BY valuation_date DESC LIMIT 1",
                (t,),
            ).fetchone()
            insiders = conn.execute(
                "SELECT COUNT(*) FROM insider_transactions "
                "WHERE ticker = ? AND transaction_date >= date('now', '-30 days') "
                "AND transaction_type IN ('open_market_buy','open_market_sell') "
                "AND is_10b5_1 = 0",
                (t,),
            ).fetchone()
            bear = conn.execute(
                "SELECT content_md FROM llm_artifacts "
                "WHERE ticker = ? AND purpose = 'bear_case' AND superseded_by_id IS NULL "
                "ORDER BY generated_at DESC LIMIT 1",
                (t,),
            ).fetchone()
            h = _read_holdings_json(t, repo_root)
            thesis = str(h.get("thesis") or "")[:200]
            ou_str = (
                f"{float(dcf[2]) * 100:+.1f}%" if dcf and dcf[2] is not None else "-"
            )
            bear_head = (
                str(bear[0] or "")[:300].replace("\n", " ") if bear else "(no bear case cached)"
            )
            port_lines.append(
                f"### {t}\n"
                f"- Thesis: {thesis}\n"
                f"- DCF over/under: {ou_str}\n"
                f"- Discretionary insider trades last 30d: {int(insiders[0]) if insiders else 0}\n"
                f"- Latest bear case head: {bear_head}\n"
            )
        port_summary = "\n".join(port_lines) if port_lines else "(no portfolio holdings)"
        insider_rows = conn.execute(
            """
            SELECT it.ticker, it.transaction_date, it.insider_name, it.insider_title,
                   it.transaction_type, it.transaction_value
            FROM insider_transactions it
            INNER JOIN tracked_companies tc ON tc.ticker = it.ticker
            WHERE tc.list_type = 'portfolio' AND tc.archived_at IS NULL
              AND it.transaction_date >= date('now', '-30 days')
              AND it.transaction_type IN ('open_market_buy','open_market_sell')
              AND it.is_10b5_1 = 0
            ORDER BY it.transaction_value DESC NULLS LAST
            LIMIT 30
            """
        ).fetchall()
        insider_summary = "\n".join(
            f"- {r[0]} · {str(r[1])[:10]} · {r[2]} ({r[3] or '?'}) · "
            f"{r[4].replace('_', ' ')} · ${float(r[5] or 0) / 1e6:.1f}M"
            for r in insider_rows
        ) or "(no discretionary insider activity)"
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
            ).fetchone()
            is not None
        ):
            pred_rows = conn.execute(
                """
                SELECT p.ticker, p.source_kind, p.outcome, COUNT(*) FROM predictions p
                INNER JOIN tracked_companies tc ON tc.ticker = p.ticker
                WHERE tc.list_type = 'portfolio'
                  AND p.evaluated_at >= date('now', '-90 days')
                GROUP BY p.ticker, p.source_kind, p.outcome
                """
            ).fetchall()
            predictions_summary = "\n".join(
                f"- {r[0]} · {r[1]} · {r[2]} × {r[3]}" for r in pred_rows
            ) or "(no graded predictions in last 90d)"
        else:
            predictions_summary = "(predictions table not yet populated)"
    finally:
        conn.close()

    return LensContext(
        ticker=None,
        template_kwargs={
            "portfolio_summary": port_summary,
            "insider_summary": insider_summary,
            "predictions_summary": predictions_summary,
        },
        cache_inputs=[
            "portfolio",
            _sha8(port_summary),
            _sha8(insider_summary),
            _sha8(predictions_summary),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


# ===========================================================================
# Registry
# ===========================================================================


LENSES: dict[str, Lens] = {
    "thesis_drift_qoq": Lens(
        name="thesis_drift_qoq",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_THESIS_DRIFT,
        build_context=_ctx_thesis_drift,
    ),
    "five_min_reread": Lens(
        name="five_min_reread",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_FIVE_MIN_REREAD,
        build_context=_ctx_five_min_reread,
    ),
    "bull_case": Lens(
        name="bull_case",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_BULL_CASE,
        build_context=_ctx_bull_case,
    ),
    "reverse_dcf": Lens(
        name="reverse_dcf",
        model="claude-opus-4-7",  # quantitative reverse-engineering benefits from Opus
        scope="ticker",
        prompt_template=_PROMPT_REVERSE_DCF,
        build_context=_ctx_reverse_dcf,
    ),
    "underweighted_facts": Lens(
        name="underweighted_facts",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_UNDERWEIGHTED,
        build_context=_ctx_underweighted,
    ),
    "catalyst_calendar": Lens(
        name="catalyst_calendar",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_CATALYST_CALENDAR,
        build_context=_ctx_catalyst_calendar,
    ),
    "filing_diff_narrative": Lens(
        name="filing_diff_narrative",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_FILING_DIFF,
        build_context=_ctx_filing_diff,
    ),
    "footnote_anomaly": Lens(
        name="footnote_anomaly",
        model="claude-sonnet-4-6",
        scope="ticker",
        prompt_template=_PROMPT_FOOTNOTE_ANOMALY,
        build_context=_ctx_footnote_anomaly,
    ),
    "cross_portfolio_synthesis": Lens(
        name="cross_portfolio_synthesis",
        model="claude-opus-4-7",  # cross-portfolio benefits from Opus's wider sector knowledge
        scope="portfolio",
        prompt_template=_PROMPT_CROSS_PORTFOLIO,
        build_context=_ctx_cross_portfolio,
    ),
}


def list_lenses_for_ticker() -> list[str]:
    """Lenses that operate on a single ticker."""
    return sorted(name for name, lens in LENSES.items() if lens.scope == "ticker")


def list_portfolio_lenses() -> list[str]:
    """Lenses that operate on the whole portfolio."""
    return sorted(name for name, lens in LENSES.items() if lens.scope == "portfolio")
