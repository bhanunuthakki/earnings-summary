"""The governed ETF 'role in portfolio' one-pager (purpose etf_role_synthesis).

LLM-maximalist where semantics matter, deterministic everywhere else: every
number the model sees is already computed (fund score factors, fit factors,
look-through overlap + country rollup, what-if deltas, the positioning
target) — the model's ONLY job is the judgment call those numbers point at:
what role would this fund actually play in THIS book?

Cache discipline: the deterministic payload is the cache key
(``llm_artifact_store`` input sha). :func:`generate_role_synthesis`
pre-checks the current artifact's sha and SKIPS the LLM call entirely when
the inputs haven't moved — so the Stage 0f tail call is free on quiet days
and one Sonnet call per ETF per data change otherwise. Output is
schema-validated (:class:`report.etf_models.EtfRoleSynthesis`) before
persisting; the render path only ever reads the artifact.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from llm_artifact_store import UpsertRequest, compute_input_sha256, read_current, upsert
from report.etf_models import EtfRoleSynthesis

PURPOSE = "etf_role_synthesis"
PROMPT_VERSION = "v1"

_PROMPT = """You are the owner's portfolio analyst. Below is the complete
DETERMINISTIC workup for an ETF the owner is evaluating for THEIR book —
fund score factors, portfolio-fit factors, look-through ownership overlap,
country exposure, before/after what-if math, and the owner's saved
positioning target (when present). Every number is real; do not invent any.

Write the role-in-portfolio synthesis:
- role_summary: <=120 words, plain language — what role this fund would
  actually play in THIS book (not a generic fund description).
- what_it_adds: up to 4 short bullets — the exposures/benefits the book does
  not already have.
- overlap_caution: one sentence when the look-through overlap or factor
  crowding warrants it, else null.
- verdict: one of closes_target_gap | diversifier | redundant |
  style_crowding | insufficient_data. Use closes_target_gap ONLY when a
  saved positioning target exists and the fund demonstrably serves it;
  insufficient_data when the workup below is mostly degraded/missing.
- suggested_weight_band: like "2-4%", grounded in the what-if rows (which
  weight keeps the book's vol/Sharpe move sensible), or null.
- watch_items: up to 3 concrete things that would change the read (fee
  changes, overlap drift, factor loading decay, target changes).

WORKUP (deterministic):
{payload}

Respond with ONE JSON object:
{{"role_summary": "...", "what_it_adds": ["..."], "overlap_caution": "...|null",
"verdict": "...", "suggested_weight_band": "...|null", "watch_items": ["..."]}}"""


def assemble_payload(
    conn: sqlite3.Connection, repo_root: Path, db_path: Path, ticker: str
) -> dict[str, object]:
    """Everything the synthesis judges, from the materialized caches + DB —
    deterministic, absence-explicit, and the cache key verbatim."""
    from candidate_fit_cache import read_materialized_candidate_fit, read_materialized_fit_meta
    from etf_overlap import compute_lookthrough_overlap
    from etf_score_cache import (
        read_materialized_etf_loadings,
        read_materialized_etf_scores,
        read_materialized_etf_whatif,
    )
    from instrument_store import get_etf_profile
    from portfolio_weights import read_materialized_weights

    t = ticker.strip().upper()
    payload: dict[str, object] = {"ticker": t}

    try:
        profile = get_etf_profile(conn, t)
    except sqlite3.Error:
        profile = None
    payload["profile"] = (
        {
            "name": profile.name,
            "issuer": profile.issuer,
            "expense_ratio": profile.expense_ratio,
            "aum_usd_m": profile.aum_usd_m,
            "benchmark_index": profile.benchmark_index,
            "pe_ratio": profile.pe_ratio,
            "pb_ratio": profile.pb_ratio,
            "characteristics_source": profile.characteristics_source,
        }
        if profile is not None
        else "unavailable"
    )

    bd = read_materialized_etf_scores(repo_root).get(t)
    payload["score"] = bd.why if bd is not None else "unavailable"
    cf = read_materialized_candidate_fit(repo_root).get(t)
    payload["fit"] = cf.why if cf is not None else "unavailable"
    payload["fit_target"] = cf.fit_target if cf is not None else None
    payload["target_factors"] = (
        [f"{f.key} {f.multiplier:.2f} ({f.detail})" for f in cf.target_factors]
        if cf is not None
        else []
    )
    payload["loadings"] = [
        {"key": ld.key, "beta": round(ld.beta, 3), "r_squared": round(ld.r_squared, 3)}
        for ld in read_materialized_etf_loadings(repo_root).get(t, [])
    ]

    weights = read_materialized_weights(repo_root)
    ov = compute_lookthrough_overlap(conn, t, weights) if weights else None
    payload["overlap"] = (
        {
            "direct_overlap_pct": round(ov.direct_overlap_pct, 4),
            "top": [
                {
                    "sym": r.constituent,
                    "etf_w": round(r.etf_weight, 4),
                    "book_w": round(r.book_weight, 4),
                }
                for r in ov.top_overlaps
            ],
            "country_weights": {c: round(w, 4) for c, w in sorted(ov.country_weights.items())},
            "as_of": ov.as_of.isoformat(),
            "source": ov.source,
        }
        if ov is not None
        else "unavailable"
    )
    payload["whatif"] = read_materialized_etf_whatif(repo_root).get(t, "unavailable")

    meta = read_materialized_fit_meta(repo_root)
    payload["book"] = meta.get("book", "unavailable")
    payload["positioning_target"] = meta.get("target", "unavailable")
    return payload


def generate_role_synthesis(
    conn: sqlite3.Connection,
    repo_root: Path,
    db_path: Path,
    ticker: str,
    *,
    force: bool = False,
) -> tuple[int | None, str]:
    """Generate (or skip) the one-pager. Returns (artifact_id, status) where
    status ∈ {'generated', 'unchanged', 'error:<msg>'} — hard stops
    (budget/setup) PROPAGATE per the call-exception policy; transient LLM
    failures degrade to an error status so a scheduled tail call never kills
    the run."""
    from llm.cli import is_hard_stop
    from llm.structured import call_llm_structured

    t = ticker.strip().upper()
    payload = assemble_payload(conn, repo_root, db_path, t)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    sha = compute_input_sha256(prompt_version=PROMPT_VERSION, cache_inputs=[canonical])

    current = read_current(ticker=t, purpose=PURPOSE, scope="etf", db_path=db_path)
    if current is not None and current.input_sha256 == sha and not force:
        return current.id, "unchanged"  # inputs haven't moved — no LLM call at all

    try:
        raw = call_llm_structured(
            _PROMPT.format(payload=json.dumps(payload, indent=2, default=str)),
            purpose=PURPOSE,
            ticker=t,
            scope="etf",
            expect="object",
            required_keys=("role_summary", "verdict"),
            db_path=db_path,
        )
        synthesis = EtfRoleSynthesis.model_validate(raw)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        return None, f"error:{exc}"

    artifact_id, _hit = upsert(
        UpsertRequest(
            ticker=t,
            purpose=PURPOSE,
            scope="etf",
            content_json=synthesis.model_dump(),
            prompt_version=PROMPT_VERSION,
            cache_inputs=[canonical],
        ),
        db_path=db_path,
    )
    return artifact_id, "generated"


def read_role_synthesis(db_path: Path, ticker: str) -> EtfRoleSynthesis | None:
    """The current artifact decoded, or None (not generated yet / undecodable —
    the workup block shows its build hint)."""
    artifact = read_current(
        ticker=ticker.strip().upper(), purpose=PURPOSE, scope="etf", db_path=db_path
    )
    if artifact is None or artifact.content_json is None:
        return None
    try:
        return EtfRoleSynthesis.model_validate(artifact.content_json)
    except ValueError:
        return None


def synthesis_generated_at(db_path: Path, ticker: str) -> str | None:
    artifact = read_current(
        ticker=ticker.strip().upper(), purpose=PURPOSE, scope="etf", db_path=db_path
    )
    return artifact.generated_at.isoformat(timespec="minutes") if artifact is not None else None
