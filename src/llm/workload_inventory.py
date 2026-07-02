"""Deterministic workload inventory — the leverage-ranked map of what the pareto
optimizer should test next (``directives/meta_eval_governance.md`` §1.1).

Pure ledger SQL: no LLM calls, no migration. For each PRODUCTION LLM purpose over a
trailing window it computes 30d cost/volume, the cheapest ladder candidate below the
incumbent, and a single LEVERAGE score

    headroom_usd_30d = cost_usd_30d * (1 - blended(cheapest_candidate)/blended(incumbent))

= "cost × volume × current-tier headroom" in one auditable number. A purpose already
at the ladder floor (or web-scoped, or with an unranked incumbent) scores 0.

This replaces two hand-maintained artifacts: the cost table in
``cheapest_model_routing.md`` §4, and the "$112/mo purpose ranks == $0.30/mo purpose"
blindness of ``run_model_eval_sweep._discover_active_purposes``. The Opus nominator
(§1.2, a later PR) reads this rollup; PR1 ships only this deterministic floor plus a
read-only CLI (``execution/report_workload_inventory.py``).

Incumbent resolution mirrors ``cli._model_for`` precedence WITHOUT importing ``db``
(this stays a pure read against an explicit ``db_path``): active ``model_pin_overrides``
row → ``LLM_MODELS`` code pin → ``DEFAULT_MODEL``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals.coverage import META_PURPOSES, eval_coverage
from llm.cli import DEFAULT_MODEL, LLM_MODELS
from llm.eval_scopes import EVAL_SCOPES
from llm.model_ladder import cheaper_candidates, model_cost

WINDOW_DAYS = 30
_VERDICTS_PER_PURPOSE = 6
# The #723 infra verdict — excluded from a purpose's last_verdicts (it's an
# infrastructure signal, not a quality result; see directives/model_eval_loop.md).
_CANDIDATE_ERRORED = "CANDIDATE_ERRORED"

# The RISKY purposes from cheapest_model_routing.md §5, moved here as a reviewable
# constant (meta_eval_governance.md §1.2). Each note is a HINT: a switch on one of
# these has silent-portfolio-harm blast radius, so the optimizer holds it to a higher
# bar (min_n 16 + Wilson gate + 3-family judging — §10 Q3/Q5a). ``advisor_*`` is a
# family (prefix); ``risk_note_for`` resolves the prefix.
RISK_NOTES: dict[str, str] = {
    "bear_case": "adversarial analytical reasoning; Gemini REJECTED 4/4 in the first sweep",
    "valuation_basis": "sector judgment; a wrong multiple is silent per-ticker harm",
    "material_news_classification": "alert veto; a false negative is a missed alert",
    "kpi_registry_auto_proposal": "drives alert thresholds; instruction-following-sensitive",
    "earnings_tone_diff": "alert trigger; high-stakes",
}
_RISKY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("advisor_", "portfolio advice; judgment tier (advisor_* family)"),
)


def risk_note_for(purpose: str) -> str | None:
    """The RISK_NOTES hint for a purpose: exact match, else a risky family prefix."""
    note = RISK_NOTES.get(purpose)
    if note is not None:
        return note
    for prefix, prefix_note in _RISKY_PREFIXES:
        if purpose.startswith(prefix):
            return prefix_note
    return None


@dataclass(frozen=True, slots=True)
class PurposeWorkload:
    """One production purpose's 30d workload + its leverage score.
    ``headroom_usd_30d`` is the rank key (meta_eval_governance.md §1.1)."""

    purpose: str
    incumbent_model: str
    calls_30d: int
    cost_usd_30d: float
    distinct_prompts_30d: int
    avg_prompt_chars: float
    web_scoped: bool
    cheapest_candidate: str | None
    headroom_usd_30d: float
    last_verdicts: tuple[str, ...] = ()
    eval_modes: tuple[str, ...] = ()
    budget_capped: bool = False


@dataclass(frozen=True, slots=True)
class _RawRow:
    """One GROUP BY purpose row straight from the ledger SQL (pre-enrichment)."""

    purpose: str
    calls: int
    cost_usd: float
    distinct_prompts: int
    avg_prompt_chars: float
    web_scoped: bool


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _window_start(window_days: int) -> str:
    """Naive-UTC cutoff (stored ``called_at`` is naive-UTC — repo convention). An
    aware ``+00:00`` stamp would compare lexicographically WRONG against naive rows."""
    return (datetime.now(UTC) - timedelta(days=window_days)).replace(tzinfo=None).isoformat()


def _load_purpose_rows(conn: sqlite3.Connection, *, window_days: int) -> list[_RawRow]:
    """Per production purpose: calls, cost, distinct prompt shapes, avg prompt size,
    and whether any call was web-scoped. Eval-machinery scopes are excluded so the
    optimizer never observes itself (EVAL_SCOPES / invariant I5)."""
    if not _has_table(conn, "llm_calls"):
        return []
    scopes = sorted(EVAL_SCOPES)
    placeholders = ",".join("?" * len(scopes))
    rows = conn.execute(
        f"""
        SELECT purpose,
               COUNT(*) AS calls,
               SUM(COALESCE(cost_estimate_usd, 0)) AS usd,
               COUNT(DISTINCT prompt_sha256) AS uniq,
               AVG(prompt_chars) AS avg_chars,
               MAX(CASE WHEN scope = 'web' THEN 1 ELSE 0 END) AS web
        FROM llm_calls
        WHERE called_at >= ?
          AND purpose IS NOT NULL
          AND (scope IS NULL OR scope NOT IN ({placeholders}))
        GROUP BY purpose
        """,
        (_window_start(window_days), *scopes),
    ).fetchall()
    out: list[_RawRow] = []
    for r in rows:
        purpose = str(r["purpose"])
        if purpose in META_PURPOSES:  # belt-and-braces: never rank the machinery (§1.7)
            continue
        out.append(
            _RawRow(
                purpose=purpose,
                calls=int(r["calls"] or 0),
                cost_usd=float(r["usd"] or 0.0),
                distinct_prompts=int(r["uniq"] or 0),
                avg_prompt_chars=float(r["avg_chars"] or 0.0),
                web_scoped=bool(r["web"]),
            )
        )
    return out


def _active_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    """purpose -> the model an active ``model_pin_overrides`` row routes it to."""
    if not _has_table(conn, "model_pin_overrides"):
        return {}
    rows = conn.execute(
        "SELECT purpose, model FROM model_pin_overrides WHERE active = 1"
    ).fetchall()
    return {str(r["purpose"]): str(r["model"]) for r in rows}


def _recent_verdicts(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """purpose -> the latest non-CANDIDATE_ERRORED verdict per candidate, newest
    first (capped). CANDIDATE_ERRORED excluded: infra, not quality (#723)."""
    if not _has_table(conn, "model_eval_verdicts"):
        return {}
    rows = conn.execute(
        """
        SELECT purpose, candidate, verdict
        FROM model_eval_verdicts
        WHERE verdict != ?
        ORDER BY purpose, recorded_at DESC, id DESC
        """,
        (_CANDIDATE_ERRORED,),
    ).fetchall()
    seen: dict[str, set[str]] = {}
    out: dict[str, list[str]] = {}
    for r in rows:
        purpose, candidate, verdict = str(r["purpose"]), str(r["candidate"]), str(r["verdict"])
        candidates_seen = seen.setdefault(purpose, set())
        if candidate in candidates_seen:
            continue
        candidates_seen.add(candidate)
        bucket = out.setdefault(purpose, [])
        if len(bucket) < _VERDICTS_PER_PURPOSE:
            bucket.append(verdict)
    return {purpose: tuple(verdicts) for purpose, verdicts in out.items()}


def _budget_capped_map(conn: sqlite3.Connection) -> dict[str, bool]:
    """purpose -> True when its budget ``on_exceed`` is not 'warn' (i.e. skip|block:
    a real cap, not a soft warning). Falls back to the ``hard_block`` bool on a
    pre-0066 DB. The '__default__' row is included (callers fall back to it)."""
    if not _has_table(conn, "llm_budgets"):
        return {}
    cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(llm_budgets)")}
    if "on_exceed" in cols:
        rows = conn.execute("SELECT purpose, on_exceed FROM llm_budgets").fetchall()
        return {str(r["purpose"]): (str(r["on_exceed"]) != "warn") for r in rows}
    if "hard_block" in cols:  # pre-0066 fallback
        rows = conn.execute("SELECT purpose, hard_block FROM llm_budgets").fetchall()
        return {str(r["purpose"]): bool(r["hard_block"]) for r in rows}
    return {}


def _eval_modes_map(db_path: Path) -> dict[str, tuple[str, ...]]:
    """purpose -> its eval modes (golden/audit/outcome/meta) from ``evals.coverage``."""
    return {row.purpose: row.modes for row in eval_coverage(db_path)}


def _leverage(incumbent: str, cost_usd_30d: float, *, web_scoped: bool) -> tuple[str | None, float]:
    """(cheapest auto-sweepable candidate, headroom_usd_30d).

    ``include_openrouter=False``: the OpenRouter axis is opt-in via a nomination, not
    part of the deterministic floor (§1.1/§6). Web-scoped purposes are downgrade-
    INELIGIBLE (candidates have no web tools) → headroom 0, though the cheapest
    price-candidate is still reported and the purpose stays A/B-eligible later.
    """
    inc = model_cost(incumbent)
    candidates = cheaper_candidates(incumbent, include_openrouter=False) if inc else []
    cheapest = candidates[0] if candidates else None
    if inc is None or cheapest is None or web_scoped or inc.blended_usd_per_mtok <= 0:
        return cheapest, 0.0
    cand = model_cost(cheapest)
    if cand is None:
        return cheapest, 0.0
    headroom = cost_usd_30d * (1.0 - cand.blended_usd_per_mtok / inc.blended_usd_per_mtok)
    return cheapest, max(0.0, headroom)


def build_workload_inventory(
    db_path: Path, *, window_days: int = WINDOW_DAYS
) -> list[PurposeWorkload]:
    """The leverage-ranked inventory from the ledger at ``db_path``, highest headroom
    first. Returns ``[]`` when the DB or ``llm_calls`` is absent (pure read; safe
    anywhere)."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        raw = _load_purpose_rows(conn, window_days=window_days)
        overrides = _active_overrides(conn)
        verdicts = _recent_verdicts(conn)
        budgets = _budget_capped_map(conn)
    finally:
        conn.close()
    modes = _eval_modes_map(db_path)
    default_capped = budgets.get("__default__", False)
    out: list[PurposeWorkload] = []
    for row in raw:
        incumbent = overrides.get(row.purpose) or LLM_MODELS.get(row.purpose) or DEFAULT_MODEL
        cheapest, headroom = _leverage(incumbent, row.cost_usd, web_scoped=row.web_scoped)
        out.append(
            PurposeWorkload(
                purpose=row.purpose,
                incumbent_model=incumbent,
                calls_30d=row.calls,
                cost_usd_30d=row.cost_usd,
                distinct_prompts_30d=row.distinct_prompts,
                avg_prompt_chars=row.avg_prompt_chars,
                web_scoped=row.web_scoped,
                cheapest_candidate=cheapest,
                headroom_usd_30d=headroom,
                last_verdicts=verdicts.get(row.purpose, ()),
                eval_modes=modes.get(row.purpose, ()),
                budget_capped=budgets.get(row.purpose, default_capped),
            )
        )
    out.sort(key=lambda w: (-w.headroom_usd_30d, -w.cost_usd_30d, w.purpose))
    return out


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def render_inventory_text(
    rows: list[PurposeWorkload], *, window_days: int = WINDOW_DAYS, max_rows: int = 60
) -> str:
    """Human-readable leverage table (pure — testable without a DB). ``max_rows=0``
    shows every row."""
    out: list[str] = [
        f"=== WORKLOAD INVENTORY - last {window_days}d (production scopes only) ===",
        "",
    ]
    if not rows:
        out.append("No production LLM purposes in the window (or llm_calls absent).")
        return "\n".join(out)
    total_cost = sum(r.cost_usd_30d for r in rows)
    total_headroom = sum(r.headroom_usd_30d for r in rows)
    out.append(
        f"{len(rows)} purposes | {_usd(total_cost)} production spend | "
        f"{_usd(total_headroom)} ranked model-downgrade headroom"
    )
    out.append("")
    header = (
        f"{'#':>3}  {'purpose':<30} {'incumbent':<24} {'-> cheapest':<24} "
        f"{'calls':>6} {'cost':>10} {'headroom':>10} {'uniq':>5}  flags"
    )
    out.append(header)
    out.append("-" * len(header))
    limit = max_rows if max_rows > 0 else len(rows)
    shown = rows[:limit]
    for i, r in enumerate(shown, start=1):
        cheapest = r.cheapest_candidate or "- (at floor)"
        flags: list[str] = []
        if r.web_scoped:
            flags.append("web")
        if r.budget_capped:
            flags.append("cap")
        if risk_note_for(r.purpose):
            flags.append("RISK")
        if r.eval_modes:
            flags.append("/".join(r.eval_modes))
        if r.last_verdicts:
            flags.append(f"[{','.join(r.last_verdicts[:3])}]")
        out.append(
            f"{i:>3}  {r.purpose[:30]:<30} {r.incumbent_model[:24]:<24} {cheapest[:24]:<24} "
            f"{r.calls_30d:>6} {_usd(r.cost_usd_30d):>10} {_usd(r.headroom_usd_30d):>10} "
            f"{r.distinct_prompts_30d:>5}  {' '.join(flags)}"
        )
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more (ranked by headroom); pass --max-rows 0 for all.")
    risky = [(r.purpose, note) for r in shown if (note := risk_note_for(r.purpose))]
    if risky:
        out.append("")
        out.append("RISKY purposes (higher switch bar - meta_eval_governance.md S10/Q3):")
        for purpose, note in risky:
            out.append(f"  {purpose}: {note}")
    return "\n".join(out)
