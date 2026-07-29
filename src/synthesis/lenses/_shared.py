"""Shared infrastructure for synthesis lenses.

Holds:
  - The `Lens` + `LensContext` dataclasses every lens registers with.
  - The generic `run_lens` runner that loads context → renders prompt →
    calls the LLM → upserts the artifact.
  - `read_lens_artifact` for renderers reading a cached artifact.
  - The `load_*` / `summarize_*` / `thesis_block` / `sha8` context
    helpers individual lenses compose.
  - `format_shocks` / `format_sensitivities` shared between the two
    macro-scenario lenses.

Per-lens prompt + context-loader pairs live in sibling modules; the
package `__init__` re-exports the public API + the LENSES registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from llm.style import compose_brief_prompt, style_block_cache_token
from llm.untrusted import spotlight
from llm_artifact_store import (
    Artifact,
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import call_llm
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from synthesis.grounded_numbers import (
    check_numeric_drift,
    grounding_footnote,
    load_grounded_numbers,
)

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
# Untrusted-input spotlighting (sec-llm; directives/llm_injection_threat_model.md §6)
# ===========================================================================

# Per-lens registry of template_kwargs whose VALUES carry untrusted external
# text — issuer filings (10-K risk factors, Item 8 footnotes, customer-
# concentration disclosures), earnings-call / IR summaries (.tmp briefs), and
# chained prior-LLM artifacts (bear-case heads re-embedded into a downstream
# prompt). Those values are spotlight()-wrapped before .format(), so an
# instruction injected into that content cannot hijack the lens prompt. Trusted
# kwargs (our computed numbers, DB rows, the operator's own thesis/KPIs) are NOT
# wrapped. EVERY generic lens in LENSES MUST appear here, even mapped to an empty
# frozenset — the guard in tests/test_lens_spotlight.py fails if a new lens is
# added unclassified, so this choke point can't be silently bypassed.
LENS_UNTRUSTED_KWARGS: dict[str, frozenset[str]] = {
    "bull_case": frozenset({"latest_summary"}),
    "catalyst_calendar": frozenset({"latest_summary"}),
    "cross_portfolio_synthesis": frozenset({"portfolio_summary"}),
    "customer_concentration_risk": frozenset({"concentrations_block", "anonymized_block"}),
    "filing_diff_narrative": frozenset({"risk_diffs"}),
    "five_min_reread": frozenset({"latest_summary"}),
    "footnote_anomaly": frozenset({"footnotes"}),
    "llm_calibration": frozenset(),
    "mgmt_credibility_score": frozenset({"recent_commitments"}),
    "reverse_dcf": frozenset({"latest_summary"}),
    "thesis_drift_qoq": frozenset({"prior_bear_case", "recent_summaries"}),
    "underweighted_facts": frozenset({"latest_summary"}),
}


def spotlight_template_kwargs(
    template_kwargs: dict[str, str], untrusted_keys: frozenset[str], *, lens_name: str
) -> dict[str, str]:
    """Return a copy of ``template_kwargs`` with each key in ``untrusted_keys``
    wrapped via :func:`spotlight`, marking that interpolated content as DATA, not
    instructions. Keys that are absent or blank are left as-is (spotlight already
    returns ``""`` for blank input). Used by the generic ``run_lens`` and by the
    macro entry points, which render their prompts outside ``run_lens``."""
    out = dict(template_kwargs)
    for key in untrusted_keys:
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = spotlight(
                val, source=f"{lens_name}:{key} (issuer/filing/chained-LLM content)"
            )
    return out


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
    except Exception as exc:
        log.warning(
            {
                "event": "lens_context_build_failed",
                "lens": lens.name,
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return None
    if ctx is None:
        log.debug(
            {
                "event": "lens_context_empty",
                "lens": lens.name,
                "ticker": ticker,
                "reason": "insufficient_data",
            }
        )
        return None

    # Include the style-block hash in cache_inputs so editing the global
    # NUMBER_FORMATTING_BLOCK auto-invalidates every cached lens artifact
    # on the next run — no per-lens prompt_version bump required.
    effective_cache_inputs = [*ctx.cache_inputs, style_block_cache_token()]

    # Cache hit check — bypass on force=True
    if not force:
        existing = read_current(
            ticker=ctx.ticker,
            purpose=purpose,
            scope=lens.scope,
            db_path=db_path,
        )
        if existing is not None and not existing.dirty:
            new_sha = compute_input_sha256(prompt_version="v1", cache_inputs=effective_cache_inputs)
            if new_sha == existing.input_sha256:
                log.info(
                    {
                        "event": "lens_cache_hit",
                        "lens": lens.name,
                        "ticker": ticker,
                        "artifact_id": existing.id,
                    }
                )
                return existing

    try:
        safe_kwargs = spotlight_template_kwargs(
            ctx.template_kwargs,
            LENS_UNTRUSTED_KWARGS.get(lens.name, frozenset()),
            lens_name=lens.name,
        )
        prompt = lens.prompt_template.format(**safe_kwargs)
    except KeyError as exc:
        log.warning(
            {
                "event": "lens_template_missing_key",
                "lens": lens.name,
                "ticker": ticker,
                "key": str(exc),
            }
        )
        return None

    try:
        content = call_llm(
            compose_brief_prompt(prompt),
            purpose=purpose,
            ticker=ctx.ticker,
            scope=lens.scope,
            model=lens.model,
        )
    except Exception as exc:
        log.warning(
            {
                "event": "lens_llm_call_failed",
                "lens": lens.name,
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return None

    # B8: drop leaked process narration ("Having exhausted my web budget… Here
    # is the brief:") before the artifact is cached — every downstream surface
    # serves content_md verbatim.
    from llm.postprocess import strip_llm_preamble

    content = strip_llm_preamble(content)

    # Grounding: if the model's prose restated a DCF/MoS figure that contradicts
    # the figures of record (dcf_runs), append a corrective footnote carrying the
    # canonical numbers and log the drift. Conservative — only fires on an explicit
    # fair-value / MoS claim, so a lens that never mentions the DCF is untouched.
    if ctx.ticker:
        grounded = load_grounded_numbers(ctx.ticker, repo_root)
        if grounded is not None and grounded.has_dcf():
            drifts = check_numeric_drift(content, grounded)
            if drifts:
                content += grounding_footnote(grounded, drifts)
                log.warning(
                    {
                        "event": "lens_numeric_drift",
                        "lens": lens.name,
                        "ticker": ctx.ticker,
                        "drifts": drifts,
                    }
                )

    artifact_id, _ = upsert(
        UpsertRequest(
            ticker=ctx.ticker,
            purpose=purpose,
            scope=lens.scope,
            content_md=content,
            cache_inputs=effective_cache_inputs,
            model=lens.model,
            source_doc_ids=ctx.source_doc_ids,
            parent_artifact_ids=ctx.parent_artifact_ids,
        ),
        db_path=db_path,
    )
    if artifact_id is None:
        return None
    return read_current(ticker=ctx.ticker, purpose=purpose, scope=lens.scope, db_path=db_path)


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


def read_holdings_json(ticker: str, repo_root: Path) -> dict[str, object]:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_prior_bear_case(ticker: str, repo_root: Path) -> Artifact | None:
    return read_current(
        ticker=ticker.upper(),
        purpose="bear_case",
        db_path=repo_root / "data" / "portfolio.db",
    )


def load_recent_summaries(ticker: str, repo_root: Path, n: int = 4) -> list[tuple[str, str]]:
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


def load_recent_insider_transactions(
    ticker: str, repo_root: Path, days: int = 180
) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
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


def load_predictions(ticker: str, repo_root: Path) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
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


# What a lens prompt says instead of numbers when the latest run is sanity-flagged.
DCF_FLAGGED_NOTE = (
    "(DCF model flagged as unreviewed outlier — fair value withheld pending model review)"
)


def load_dcf(ticker: str, repo_root: Path) -> dict[str, object] | None:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.row_factory = sqlite3.Row
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
        sanity_select = ", sanity_flag" if "sanity_flag" in cols else ""
        row = conn.execute(
            f"""
            SELECT npv_per_share, live_price, over_under_pct, mos_bar_used,
                   wacc, terminal_growth, fcf_margin, base_revenue, revenue_growths_json,
                   valuation_date{sanity_select}
            FROM dcf_runs
            WHERE ticker = ? AND (segment_name IS NULL OR segment_name = '')
            ORDER BY valuation_date DESC LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out.setdefault("sanity_flag", None)
        # A sanity-flagged run (migration 0182: |over_under| past the trust limit —
        # more likely a broken model than a mispricing) must not put numbers in an
        # LLM prompt: null the valuation fields centrally so no lens can quote them,
        # and keep the flag so formatters can say WHY the numbers are withheld.
        if out.get("sanity_flag"):
            for k in ("npv_per_share", "over_under_pct", "mos_bar_used"):
                out[k] = None
        return out
    finally:
        conn.close()


def load_latest_financials_snapshot(
    ticker: str, repo_root: Path, n_periods: int = 8
) -> list[dict[str, object]]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
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


def summarize_insiders(rows: list[dict[str, object]], max_items: int = 25) -> str:
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


def summarize_predictions(rows: list[dict[str, object]], max_items: int = 20) -> str:
    if not rows:
        return "(no recorded predictions)"
    lines: list[str] = []
    for r in rows[:max_items]:
        made = str(r.get("made_at") or "")[:10]
        tgt = str(r.get("target_period") or "")[:10] if r.get("target_period") else "-"
        kpi = str(r.get("kpi_name") or "?")
        outc = str(r.get("outcome") or "pending")
        narr = str(r.get("prediction_md") or "")[:140]
        lines.append(f"- {made} → {tgt} · {kpi} · {outc.upper()} · {narr}")
    return "\n".join(lines)


def thesis_block(ticker: str, repo_root: Path) -> str:
    h = read_holdings_json(ticker, repo_root)
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


def sha8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


# ===========================================================================
# Macro-scenario shared formatters — used by macro_scenario +
# portfolio_macro_stress lens modules
# ===========================================================================


def format_shocks(scenario_obj: object) -> str:
    """Format the scenario's shocks list as a markdown bullet block. Expects
    a Scenario from src/macro_scenarios.py — duck-typed so synthesis_lenses
    doesn't import from a sibling module at import-time."""
    shocks = getattr(scenario_obj, "shocks", ())
    if not shocks:
        return "(no shocks defined)"
    lines: list[str] = []
    for s in shocks:
        sid = getattr(s, "series_id", "?")
        unit = getattr(s, "unit", "pct")
        magnitude = getattr(s, "magnitude", 0.0)
        direction = getattr(s, "direction", "up")
        sign = "+" if direction == "up" else "-"
        if unit == "bps":
            lines.append(f"  - **{sid}** · {sign}{abs(magnitude):.0f} bps")
        elif unit == "pct":
            lines.append(f"  - **{sid}** · {sign}{abs(magnitude):.1f}%")
        elif unit == "absolute":
            lines.append(f"  - **{sid}** · to {magnitude:.2f}")
        else:
            lines.append(f"  - **{sid}** · {sign}{magnitude} ({unit})")
    return "\n".join(lines)


def format_sensitivities(sens_rows: list[dict[str, object]]) -> str:
    if not sens_rows:
        return "(no sensitivities computed — run `python execution/compute_macro_sensitivities.py --ticker <T>` first)"
    lines: list[str] = []
    for r in sens_rows:
        sid = r.get("series_id", "?")
        beta = r.get("beta")
        r_sq = r.get("r_squared")
        lb = r.get("lookback_window_days")
        beta_s = f"{cast('float', beta):+.3f}" if beta is not None else "?"
        rsq_s = f"{cast('float', r_sq):.2f}" if r_sq is not None else "?"
        lines.append(f"- **{sid}** · β={beta_s} · R²={rsq_s} · lookback={lb}d")
    return "\n".join(lines)
