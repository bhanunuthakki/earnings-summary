"""portfolio_macro_stress lens — portfolio-wide scenario stress digest.

Takes a Scenario object, computes the per-holding beta × shock grid
across the portfolio, and emits a cross-name read-through with hedge
clusters, thesis-breaking exposures, and capital-allocation actions.

Driven via the dedicated `run_portfolio_macro_stress_lens` runner — the
scenario_obj parameter precludes use of the generic dispatcher.
"""

from __future__ import annotations

import logging
import sqlite3
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
    format_shocks,
    read_holdings_json,
    sha8,
)

log = logging.getLogger(__name__)


_PROMPT_PORTFOLIO_MACRO_STRESS = """You are writing the portfolio-wide stress digest for scenario
"{scenario_title}". The analyst owns 11 portfolio names plus tracks a
watchlist; your job is to surface the cross-name read-throughs.

**Scenario:**
- Description: {scenario_description}
- Historical analog: {scenario_analog}
- Shocks:
{scenario_shocks_block}

**Per-holding sensitivity grid (beta × shock = implied weekly-return move):**
{stress_grid}

**Portfolio composition (thesis + DCF over/under per holding):**
{portfolio_summary}

Produce a 500-700 word digest with these sections:

## 1. Most-exposed names — in size order
List the 3-5 holdings with the largest mechanical impact (positive OR
negative). For each, ONE sentence on direction + magnitude AND ONE
sentence on whether the impact aligns with the thesis (i.e. is this
exposure intentional or accidental?).

## 2. Hedge / offset clusters
Are there names whose exposures cancel? Name the clusters. Are there
unhedged concentrations (3+ names exposed the same direction)? Call them
out.

## 3. Thesis-breaking exposures
For each of the top 3 most-exposed names, name the SPECIFIC tier-1 KPI
that the scenario would press on. Is the bear-case failure mode for that
name engaged by this scenario?

## 4. Capital allocation actions
1-3 specific position-size changes the scenario suggests. "TRIM 1% from
X to reduce concentration in commodity exposure" / "ADD 0.5% to Y — the
scenario, if it plays out, refutes the bear-case failure mode #2."
Concrete %s and named names.

## 5. What I'd want to monitor
1-2 leading indicators that would tell me the scenario is starting to
play out (before it shows in prices).

Voice: portfolio manager talking to themselves. Terse, opinion-bearing,
linking macro shocks to capital allocation. No "should consider monitoring"
filler — either commit to a view or say "no action — exposure is in line
with the thesis." That's also a view.
"""


def _ctx_portfolio_macro_stress(*, scenario_obj: object, repo_root: Path) -> LensContext | None:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    try:
        from macro_store import fetch_sensitivities  # noqa: PLC0415
    except ImportError:
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
        port_lines: list[str] = []
        grid_lines: list[str] = []
        for t in tickers:
            dcf = conn.execute(
                "SELECT npv_per_share, live_price, over_under_pct FROM dcf_runs "
                "WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '') "
                "ORDER BY valuation_date DESC LIMIT 1",
                (t,),
            ).fetchone()
            dcf_ou = cast("float | None", dcf[2]) if dcf is not None else None
            ou_str = f"{(dcf_ou or 0.0) * 100:+.1f}%" if dcf_ou is not None else "-"
            h = read_holdings_json(t, repo_root)
            thesis = str(h.get("thesis") or "")[:200]
            port_lines.append(f"### {t}\n- Thesis: {thesis}\n- DCF over/under: {ou_str}")
            sens = fetch_sensitivities(ticker=t, db_path=db)
            beta_by_series = {s.series_id: s.beta for s in sens}
            stress_pieces: list[str] = []
            for shock in getattr(scenario_obj, "shocks", ()):
                sid = getattr(shock, "series_id", None)
                if sid is None:
                    continue
                beta = beta_by_series.get(sid)
                if beta is None:
                    stress_pieces.append(f"{sid}: β n/a")
                    continue
                # Implied return contribution = beta × shock_return.
                # `magnitude` is in pct or bps depending on unit; normalize to
                # a return-style decimal for a rough mechanical estimate.
                unit = getattr(shock, "unit", "pct")
                mag = float(getattr(shock, "magnitude", 0.0))
                direction = getattr(shock, "direction", "up")
                sign = 1.0 if direction == "up" else -1.0
                if unit == "pct":
                    shock_ret = sign * (mag / 100.0)
                elif unit == "bps":
                    shock_ret = sign * (mag / 10000.0)
                elif unit == "absolute":
                    shock_ret = sign * mag
                else:
                    shock_ret = sign * mag
                impact = beta * shock_ret * 100  # in %
                stress_pieces.append(f"{sid}: β={beta:+.2f}→{impact:+.1f}%")
            grid_lines.append(f"- **{t}** · " + " · ".join(stress_pieces))
    finally:
        conn.close()

    scenario_id = str(getattr(scenario_obj, "id", "scenario"))
    portfolio_summary = "\n\n".join(port_lines) if port_lines else "(no portfolio holdings)"
    stress_grid = (
        "\n".join(grid_lines) if grid_lines else "(no sensitivities computed for any holding)"
    )
    return LensContext(
        ticker=None,
        template_kwargs={
            "scenario_title": str(getattr(scenario_obj, "title", scenario_id)),
            "scenario_description": str(getattr(scenario_obj, "description", "")),
            "scenario_analog": str(getattr(scenario_obj, "historical_analog", "—")),
            "scenario_shocks_block": format_shocks(scenario_obj),
            "portfolio_summary": portfolio_summary,
            "stress_grid": stress_grid,
        },
        cache_inputs=[
            scenario_id,
            sha8(format_shocks(scenario_obj)),
            sha8(portfolio_summary),
            sha8(stress_grid),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


def run_portfolio_macro_stress_lens(
    *,
    scenario_obj: object,
    repo_root: Path,
    force: bool = False,
) -> Artifact | None:
    """Public entry point — portfolio-wide macro stress digest."""
    scenario_id = str(getattr(scenario_obj, "id", "scenario"))
    purpose = f"lens:portfolio_macro_stress:{scenario_id}"
    model = "claude-opus-4-7"
    db_path = repo_root / "data" / "portfolio.db"

    ctx = _ctx_portfolio_macro_stress(scenario_obj=scenario_obj, repo_root=repo_root)
    if ctx is None:
        log.debug({"event": "portfolio_macro_stress_context_empty", "scenario": scenario_id})
        return None
    effective_cache_inputs = ctx.cache_inputs + [style_block_cache_token()]
    if not force:
        existing = read_current(ticker=None, purpose=purpose, scope="portfolio", db_path=db_path)
        if existing is not None and not existing.dirty:
            new_sha = compute_input_sha256(prompt_version="v1", cache_inputs=effective_cache_inputs)
            if new_sha == existing.input_sha256:
                log.info({"event": "portfolio_macro_stress_cache_hit", "scenario": scenario_id})
                return existing
    try:
        prompt = _PROMPT_PORTFOLIO_MACRO_STRESS.format(**ctx.template_kwargs)
    except KeyError as exc:
        log.warning({"event": "portfolio_macro_stress_template_key", "key": str(exc)})
        return None
    try:
        content = call_llm(
            compose_brief_prompt(prompt),
            purpose=purpose,
            ticker=None,
            scope="portfolio",
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "portfolio_macro_stress_llm_failed", "error": str(exc)})
        return None
    artifact_id, _ = upsert(
        UpsertRequest(
            ticker=None,
            purpose=purpose,
            scope="portfolio",
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
    return read_current(ticker=None, purpose=purpose, scope="portfolio", db_path=db_path)
