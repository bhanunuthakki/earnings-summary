"""macro_scenario lens — single-ticker macro-scenario read-through.

Takes a Scenario object (from src/macro_scenarios.py) plus a ticker, walks
through the mechanical first-order betas, second-order thesis effects,
DCF implications, and what to watch next.

Driven via the dedicated `run_macro_scenario_lens` runner (rather than
the generic `run_lens`) because it needs an extra runtime parameter —
the scenario_obj — that the generic dispatcher doesn't carry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from llm.style import compose_brief_prompt, style_block_cache_token
from llm_artifact_store import (
    Artifact,
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import call_llm

from ._shared import (
    LensContext,
    format_sensitivities,
    format_shocks,
    load_dcf,
    load_recent_summaries,
    sha8,
    thesis_block,
)

log = logging.getLogger(__name__)


_PROMPT_MACRO_SCENARIO = """You are writing a macro-scenario read-through memo for {ticker}.
The analyst is asking: "if scenario X plays out, what does it mean for THIS name?"

**Scenario:**
- Name: {scenario_title}
- Description: {scenario_description}
- Historical analog: {scenario_analog}
- Shocks:
{scenario_shocks_block}

**Thesis anchor:**
{thesis_block}

**DCF snapshot (current valuation context):**
{dcf_summary}

**Macro sensitivities (per-series beta of {ticker}'s weekly returns):**
{sensitivities_block}

**Latest earnings summary (recent management posture):**
{latest_summary}

Produce a 350-500 word memo with these four sections:

## 1. Mechanical first-order impact
Walk through the implied first-order price move using the betas above.
Be explicit about which shock × which beta you're applying. Cite the
direction (up/down) and rough magnitude (in %). NO false precision — round
to 5pp / 10% buckets. If a key shock has no beta data (n/a), say so.

## 2. Second-order effects on the thesis
For each shock, name the SPECIFIC line of the thesis that should move:
revenue mix, cost line, customer demand, capital allocation, FX
translation. Tie back to the tier-1 KPIs by name. NOT macro hand-waving.

## 3. What the DCF would look like under this scenario
Pick the 1-2 DCF inputs that would shift (revenue growth, FCF margin,
WACC, terminal growth) and re-anchor the over/under. Direction + size
only — don't re-run the model in your head; just NAME the move.

## 4. What I'd watch in the next 1-2 quarters
2-3 specific data points / disclosures that would confirm or refute the
scenario read-through. Concrete (not "watch margins" — "watch AWS
operating margin guide for FY '26").

Voice: senior portfolio manager. Quantitative-where-possible, opinion-
bearing on whether the scenario would or wouldn't break the thesis. No
hedging without a specific reason cited.
"""


def _ctx_macro_scenario(
    *,
    scenario_obj: object,
    ticker: str,
    repo_root: Path,
) -> LensContext | None:
    """Context loader for macro_scenario. Requires scenario_obj, ticker, repo_root."""
    ticker = ticker.upper()
    # Late import — keep macro_store optional at import time so the existing
    # lens runner doesn't acquire a hard dep on the new module.
    try:
        from macro_store import fetch_sensitivities  # noqa: PLC0415
    except ImportError:
        return None
    sens_objs = fetch_sensitivities(ticker=ticker, db_path=repo_root / "data" / "portfolio.db")
    sens_rows: list[dict[str, object]] = [
        {
            "series_id": s.series_id,
            "beta": s.beta,
            "r_squared": s.r_squared,
            "lookback_window_days": s.lookback_window_days,
        }
        for s in sens_objs
    ]
    dcf = load_dcf(ticker, repo_root)
    if dcf:
        ou_raw = cast("float | None", dcf.get("over_under_pct"))
        npv_raw = cast("float | None", dcf.get("npv_per_share"))
        live_raw = cast("float | None", dcf.get("live_price"))
        ou = (ou_raw or 0.0) * 100
        dcf_summary = (
            f"NPV/share: ${npv_raw or 0:.0f} · Live: ${live_raw or 0:.0f} · Over/Under: {ou:+.1f}%"
        )
    else:
        dcf_summary = "(no DCF run for this ticker)"
    summaries = load_recent_summaries(ticker, repo_root, n=1)
    latest = summaries[0][1][:2500] if summaries else "(no recent earnings summary)"
    scenario_id = str(getattr(scenario_obj, "id", "scenario"))
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "scenario_title": str(getattr(scenario_obj, "title", scenario_id)),
            "scenario_description": str(getattr(scenario_obj, "description", "")),
            "scenario_analog": str(getattr(scenario_obj, "historical_analog", "—")),
            "scenario_shocks_block": format_shocks(scenario_obj),
            "thesis_block": thesis_block(ticker, repo_root),
            "dcf_summary": dcf_summary,
            "sensitivities_block": format_sensitivities(sens_rows),
            "latest_summary": latest,
        },
        cache_inputs=[
            ticker,
            scenario_id,
            sha8(format_shocks(scenario_obj)),
            sha8(format_sensitivities(sens_rows)),
            sha8(dcf_summary),
            sha8(latest),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


def run_macro_scenario_lens(
    *,
    scenario_obj: object,
    ticker: str,
    repo_root: Path,
    force: bool = False,
) -> Artifact | None:
    """Public entry point — single-ticker macro scenario read-through."""
    scenario_id = str(getattr(scenario_obj, "id", "scenario"))
    purpose = f"lens:macro_scenario:{scenario_id}"
    model = "claude-sonnet-4-6"
    db_path = repo_root / "data" / "portfolio.db"

    ctx = _ctx_macro_scenario(scenario_obj=scenario_obj, ticker=ticker, repo_root=repo_root)
    if ctx is None:
        log.debug(
            {"event": "macro_scenario_context_empty", "ticker": ticker, "scenario": scenario_id}
        )
        return None
    effective_cache_inputs = ctx.cache_inputs + [style_block_cache_token()]
    if not force:
        existing = read_current(ticker=ctx.ticker, purpose=purpose, scope="ticker", db_path=db_path)
        if existing is not None and not existing.dirty:
            new_sha = compute_input_sha256(prompt_version="v1", cache_inputs=effective_cache_inputs)
            if new_sha == existing.input_sha256:
                log.info(
                    {"event": "macro_scenario_cache_hit", "ticker": ticker, "scenario": scenario_id}
                )
                return existing
    try:
        prompt = _PROMPT_MACRO_SCENARIO.format(**ctx.template_kwargs)
    except KeyError as exc:
        log.warning({"event": "macro_scenario_template_key", "key": str(exc)})
        return None
    try:
        content = call_llm(
            compose_brief_prompt(prompt),
            purpose=purpose,
            ticker=ctx.ticker,
            scope="ticker",
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "macro_scenario_llm_failed", "error": str(exc)})
        return None
    artifact_id, _ = upsert(
        UpsertRequest(
            ticker=ctx.ticker,
            purpose=purpose,
            scope="ticker",
            content_md=content,
            cache_inputs=effective_cache_inputs,
            model=model,
            source_doc_ids=ctx.source_doc_ids,
            parent_artifact_ids=ctx.parent_artifact_ids,
        ),
        db_path=db_path,
    )
    if artifact_id is None:
        return None
    return read_current(ticker=ctx.ticker, purpose=purpose, scope="ticker", db_path=db_path)
