"""The Opus nominator — the optimizer's monthly steering call
(meta_eval_governance.md §1.2 + §10 owner decisions, PR3).

Reads the deterministic workload inventory (PR1) + the merged candidate
frontier (static ladder + frontier-research overlay) + recent verdicts, and
returns RANKED nominations with rationale — the judgment layer the
deterministic leverage score can't provide: risk tiering, family grouping,
new-model awareness, prompt-experiment nominations, and (owner decision Q2)
EXCLUSIONS ("stop testing X — three KEEP streaks, no headroom").

Fail-closed validation (the ``key_metrics`` closed-vocabulary pattern):
* every purpose must exist in the supplied inventory — unknown → dropped + logged;
* every candidate must be in ``merged_cheaper_candidates(incumbent,
  include_openrouter=True)`` — the nominator can NEVER nominate a lateral or
  more expensive model; a validated nomination IS the OpenRouter opt-in act;
* a statically-RISKY purpose (``RISK_NOTES`` / ``advisor_*``) can only be
  tiered *risky* — the nominator may tighten, never loosen;
* exclusions carry a TTL (``EXCLUSION_TTL_DAYS``) and are overridden by the
  rotation floor (``MAX_UNSWEPT_DAYS``) so a bad exclusion self-heals and
  nothing is silently starved of measurement (§10 Q2 guards).

If the call fails or validates to zero rows, nominations degrade to the
deterministic top-K by ``headroom_usd_30d`` (``source='deterministic_fallback'``)
— the loop never stalls on its own steering call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from llm.frontier import frontier_sha, merged_cheaper_candidates, promise_of
from llm.workload_inventory import PurposeWorkload, build_workload_inventory, risk_note_for

log = logging.getLogger(__name__)

NOMINATOR_PURPOSE = "optimizer_nominator"

KIND_MODEL_DOWNGRADE = "model_downgrade"
KIND_PROMPT_EXPERIMENT = "prompt_experiment"
KIND_EXCLUDE = "exclude"
_KINDS = (KIND_MODEL_DOWNGRADE, KIND_PROMPT_EXPERIMENT, KIND_EXCLUDE)
_TIERS = ("safe", "candidate", "risky")

MAX_NOMINATIONS = 8
# Owner-decision Q2 guards: an exclusion expires after this TTL, and a purpose
# unswept for MAX_UNSWEPT_DAYS is force-reincluded regardless of an exclusion.
EXCLUSION_TTL_DAYS = 60
MAX_UNSWEPT_DAYS = 90
# Monthly cadence (the weekly orchestrator re-nominates when the newest run is
# older than this OR the candidate frontier changed).
NOMINATION_CADENCE_DAYS = 28

_INVENTORY_TOP_N = 40
_VERDICTS_PER_PAIR = 3

# Instruction scaffold for optimizer_nominator (v1 — prompt_versions).
NOMINATOR_PROMPT = """\
You are the steering layer of an LLM cost optimizer ("keep the incumbent unless a
cheaper model is PROVABLY at parity"). Below is the current workload inventory
(per-purpose 30d production cost, incumbent model, deterministic headroom), the
candidate-model frontier (id, family, $/MTok prices — indicative, for ranking),
recent per-candidate verdicts, and static risk notes.

Nominate what the weekly sweep should test next. Ranked, highest priority first.
Kinds:
- "model_downgrade": test cheaper candidate models for a purpose. Prefer high
  headroom, group purposes sharing a prompt scaffold, and re-test tiers when the
  frontier gained a new model. Candidates MUST come from the frontier list and
  MUST be cheaper than the purpose's incumbent.
- "prompt_experiment": the incumbent keeps winning on substance but bleeds
  format/conciseness facets, or eval scores are mediocre — nominate a prompt A/B
  instead of a model swap. (Web-scoped purposes are eligible here even though
  they are downgrade-ineligible.)
- "exclude": stop testing a purpose for a while (e.g. repeated KEEP streaks and
  no headroom). Use sparingly — exclusions expire automatically after
  {ttl_days} days and a rotation floor re-includes anything unswept too long.

Risk tiers: "safe" | "candidate" | "risky". A purpose carrying a static risk
note is ALWAYS "risky" (you may tighten a tier, never loosen one). Risky
purposes need larger samples — suggest min_n 16 for them.

Respond with ONLY a JSON object:
{{"nominations": [
  {{"purpose": "<purpose>",
   "kind": "model_downgrade" | "prompt_experiment" | "exclude",
   "priority": <1 = highest>,
   "candidates": ["<model id>", ...],   // empty for prompt_experiment/exclude
   "why": "<one line>",
   "risk_tier": "safe" | "candidate" | "risky",
   "suggested_min_n": <int>}},
  ...
]}}
At most {max_nominations} nominations.

=== WORKLOAD INVENTORY (top {top_n} by cost; production scopes only) ===
{inventory_json}

=== CANDIDATE FRONTIER (prices are indicative, for ranking) ===
{frontier_json}

=== RECENT VERDICTS (newest first per pair; CANDIDATE_ERRORED = infra, excluded) ===
{verdicts_json}

=== STATIC RISK NOTES (hints; your tier may only be stricter) ===
{risk_json}
"""

StructCall = Callable[..., object]


@dataclass(frozen=True, slots=True)
class Nomination:
    """One validated nomination row (persisted to ``optimizer_nominations``)."""

    purpose: str
    kind: str
    priority: int
    incumbent_model: str
    candidates: tuple[str, ...]
    rationale: str
    risk_tier: str
    suggested_min_n: int | None
    source: str
    headroom_usd_30d: float = 0.0
    cost_usd_30d: float = 0.0
    calls_30d: int = 0
    expires_at: str | None = None
    row_id: int | None = None


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def _recent_pair_verdicts(db_path: Path) -> dict[str, dict[str, list[str]]]:
    """purpose -> candidate -> newest-first verdicts (capped), CANDIDATE_ERRORED
    excluded (infra, not quality)."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_table(conn, "model_eval_verdicts"):
                return {}
            rows = conn.execute(
                """
                SELECT purpose, candidate, verdict FROM model_eval_verdicts
                WHERE verdict != 'CANDIDATE_ERRORED'
                ORDER BY recorded_at DESC, id DESC
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("_recent_pair_verdicts: %s", exc)
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for purpose, candidate, verdict in rows:
        bucket = out.setdefault(str(purpose), {}).setdefault(str(candidate), [])
        if len(bucket) < _VERDICTS_PER_PAIR:
            bucket.append(str(verdict))
    return out


def build_nominator_prompt(db_path: Path, inventory: list[PurposeWorkload]) -> str:
    """The compact JSON steering document (§1.2's input)."""
    top = inventory[:_INVENTORY_TOP_N]
    inventory_rows = [
        {
            "purpose": w.purpose,
            "incumbent": w.incumbent_model,
            "calls_30d": w.calls_30d,
            "cost_usd_30d": round(w.cost_usd_30d, 2),
            "headroom_usd_30d": round(w.headroom_usd_30d, 2),
            "distinct_prompts": w.distinct_prompts_30d,
            "web_scoped": w.web_scoped,
            "eval_modes": list(w.eval_modes),
            "last_verdicts": list(w.last_verdicts),
        }
        for w in top
    ]
    from llm.frontier import load_candidate_models
    from llm.model_ladder import MODEL_LADDER

    frontier_rows = [
        {
            "model_id": mid,
            "family": c.family,
            "in_usd_mtok": c.input_usd_per_mtok,
            "out_usd_mtok": c.output_usd_per_mtok,
        }
        for mid, c in sorted(MODEL_LADDER.items())
    ] + [
        {
            "model_id": mid,
            "family": c.family,
            "in_usd_mtok": c.input_usd_per_mtok,
            "out_usd_mtok": c.output_usd_per_mtok,
            "promise": c.promise,
            "discovered": True,
        }
        for mid, c in sorted(load_candidate_models(db_path).items())
        if mid not in MODEL_LADDER
    ]
    verdicts = _recent_pair_verdicts(db_path)
    top_purposes = {w.purpose for w in top}
    verdicts_trim = {p: v for p, v in verdicts.items() if p in top_purposes}
    risk = {w.purpose: note for w in top if (note := risk_note_for(w.purpose))}
    return NOMINATOR_PROMPT.format(
        ttl_days=EXCLUSION_TTL_DAYS,
        max_nominations=MAX_NOMINATIONS,
        top_n=_INVENTORY_TOP_N,
        inventory_json=json.dumps(inventory_rows, ensure_ascii=False),
        frontier_json=json.dumps(frontier_rows, ensure_ascii=False),
        verdicts_json=json.dumps(verdicts_trim, ensure_ascii=False),
        risk_json=json.dumps(risk, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Validation (fail-closed, closed vocabulary)
# ---------------------------------------------------------------------------


def _validate_rows(
    payload: object,
    db_path: Path,
    inventory_by_purpose: dict[str, PurposeWorkload],
    *,
    max_nominations: int,
) -> list[Nomination]:
    if not isinstance(payload, dict):
        return []
    rows_raw = cast("dict[str, object]", payload).get("nominations")
    if not isinstance(rows_raw, list):
        return []
    out: list[Nomination] = []
    for entry_obj in cast("list[object]", rows_raw):
        if not isinstance(entry_obj, dict):
            continue
        entry = cast("dict[str, object]", entry_obj)
        purpose = entry.get("purpose")
        kind = entry.get("kind")
        if not isinstance(purpose, str) or purpose not in inventory_by_purpose:
            log.info(
                {
                    "event": "optimizer_nomination_rejected",
                    "reason": "unknown_purpose",
                    "row": str(entry)[:200],
                }
            )
            continue
        if not isinstance(kind, str) or kind not in _KINDS:
            log.info(
                {"event": "optimizer_nomination_rejected", "reason": "bad_kind", "purpose": purpose}
            )
            continue
        work = inventory_by_purpose[purpose]
        # Candidates: closed vocabulary — the cheaper-than-incumbent frontier.
        # A validated nomination is the OpenRouter opt-in act (§6).
        candidates: tuple[str, ...] = ()
        if kind == KIND_MODEL_DOWNGRADE:
            if work.web_scoped:
                log.info(
                    {
                        "event": "optimizer_nomination_rejected",
                        "reason": "web_scoped_downgrade",
                        "purpose": purpose,
                    }
                )
                continue
            allowed = set(
                merged_cheaper_candidates(db_path, work.incumbent_model, include_openrouter=True)
            )
            raw_cands = entry.get("candidates")
            picked = [
                c
                for c in (cast("list[object]", raw_cands) if isinstance(raw_cands, list) else [])
                if isinstance(c, str) and c in allowed
            ]
            if not picked:
                log.info(
                    {
                        "event": "optimizer_nomination_rejected",
                        "reason": "no_valid_candidates",
                        "purpose": purpose,
                    }
                )
                continue
            candidates = tuple(dict.fromkeys(picked))  # dedup, order-preserving
        tier_raw = entry.get("risk_tier")
        tier = tier_raw if isinstance(tier_raw, str) and tier_raw in _TIERS else "candidate"
        if risk_note_for(purpose):
            tier = "risky"  # static note wins: tighten-only
        pr_raw = entry.get("priority")
        priority = int(pr_raw) if isinstance(pr_raw, (int, float)) else len(out) + 1
        min_n_raw = entry.get("suggested_min_n")
        suggested = max(1, min(64, int(min_n_raw))) if isinstance(min_n_raw, (int, float)) else None
        if tier == "risky" and (suggested is None or suggested < 16):
            suggested = 16
        why_raw = entry.get("why")
        rationale = why_raw.strip()[:400] if isinstance(why_raw, str) else ""
        expires = (
            (datetime.now(UTC) + timedelta(days=EXCLUSION_TTL_DAYS))
            .replace(tzinfo=None)
            .isoformat()
            if kind == KIND_EXCLUDE
            else None
        )
        out.append(
            Nomination(
                purpose=purpose,
                kind=kind,
                priority=priority,
                incumbent_model=work.incumbent_model,
                candidates=candidates,
                rationale=rationale or "(no rationale)",
                risk_tier=tier,
                suggested_min_n=suggested,
                source="opus",
                headroom_usd_30d=work.headroom_usd_30d,
                cost_usd_30d=work.cost_usd_30d,
                calls_30d=work.calls_30d,
                expires_at=expires,
            )
        )
    out.sort(key=lambda n: (n.priority, -n.headroom_usd_30d))
    return out[:max_nominations]


def _deterministic_fallback(
    db_path: Path, inventory: list[PurposeWorkload], *, max_nominations: int
) -> list[Nomination]:
    """Top-K by headroom, candidates from the merged frontier (top 3 cheapest).
    Rotation-aware: pairs untested longest float up within equal headroom via
    the inventory's ordering; promise breaks candidate ties."""
    out: list[Nomination] = []
    for w in inventory:
        if len(out) >= max_nominations:
            break
        if w.headroom_usd_30d <= 0 or w.web_scoped:
            continue
        allowed = merged_cheaper_candidates(db_path, w.incumbent_model, include_openrouter=True)
        if not allowed:
            continue
        ranked = sorted(allowed, key=lambda m: -promise_of(db_path, m))[:3]
        tier = "risky" if risk_note_for(w.purpose) else "candidate"
        out.append(
            Nomination(
                purpose=w.purpose,
                kind=KIND_MODEL_DOWNGRADE,
                priority=len(out) + 1,
                incumbent_model=w.incumbent_model,
                candidates=tuple(ranked),
                rationale=f"deterministic fallback: headroom ${w.headroom_usd_30d:.2f}/30d",
                risk_tier=tier,
                suggested_min_n=16 if tier == "risky" else None,
                source="deterministic_fallback",
                headroom_usd_30d=w.headroom_usd_30d,
                cost_usd_30d=w.cost_usd_30d,
                calls_30d=w.calls_30d,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Persistence + reads
# ---------------------------------------------------------------------------


def _persist_run(db_path: Path, run_id: str, nominations: list[Nomination]) -> int:
    """Expire prior pending rows, insert the new run. Best-effort (steering
    telemetry never raises); returns rows written."""
    if not nominations:
        return 0
    now = _now_iso()
    sha = frontier_sha(db_path)
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_table(conn, "optimizer_nominations"):
                log.warning("optimizer_nominations table absent — run not persisted")
                return 0
            conn.execute(
                "UPDATE optimizer_nominations SET status='expired', updated_at=? "
                "WHERE status='pending'",
                (now,),
            )
            for nom in nominations:
                conn.execute(
                    """
                    INSERT INTO optimizer_nominations
                        (nomination_run_id, purpose, kind, priority, headroom_usd_30d,
                         cost_usd_30d, calls_30d, incumbent_model, candidates_json,
                         rationale, risk_tier, suggested_min_n, source, ladder_sha,
                         status, expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        run_id,
                        nom.purpose,
                        nom.kind,
                        nom.priority,
                        nom.headroom_usd_30d,
                        nom.cost_usd_30d,
                        nom.calls_30d,
                        nom.incumbent_model,
                        json.dumps(list(nom.candidates)),
                        nom.rationale,
                        nom.risk_tier,
                        nom.suggested_min_n,
                        nom.source,
                        sha,
                        nom.expires_at,
                        now,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("_persist_run failed: %s", exc)
        return 0
    return len(nominations)


def run_nominator(
    db_path: Path, *, struct: StructCall | None = None, max_nominations: int = MAX_NOMINATIONS
) -> list[Nomination]:
    """One nomination pass: inventory → Opus (or fallback) → validate → persist.
    Returns the persisted nominations (empty when there is nothing to steer)."""
    inventory = build_workload_inventory(db_path)
    if not inventory:
        log.info("nominator: empty inventory — nothing to nominate")
        return []
    by_purpose = {w.purpose: w for w in inventory}
    nominations: list[Nomination] = []
    struct_fn: StructCall
    if struct is None:
        from llm.structured import call_llm_structured

        struct_fn = call_llm_structured
    else:
        struct_fn = struct
    try:
        payload = struct_fn(
            build_nominator_prompt(db_path, inventory),
            purpose=NOMINATOR_PURPOSE,
            scope="meta_eval",
            expect="object",
            required_keys=("nominations",),
        )
        nominations = _validate_rows(payload, db_path, by_purpose, max_nominations=max_nominations)
    except Exception as exc:
        # StructuredParseError, setup, budget, transport — the steering call
        # degrades identically: the deterministic fallback keeps the loop alive.
        log.warning(
            "nominator call failed (%s: %s) — deterministic fallback",
            type(exc).__name__,
            str(exc)[:200],
        )
    if not nominations:
        nominations = _deterministic_fallback(db_path, inventory, max_nominations=max_nominations)
    run_id = uuid.uuid4().hex
    written = _persist_run(db_path, run_id, nominations)
    log.info(
        "nominator: %d nomination(s) persisted (source=%s)",
        written,
        nominations[0].source if nominations else "-",
    )
    return nominations


def newest_run_info(db_path: Path) -> tuple[str, str] | None:
    """(created_at, ladder_sha) of the newest nomination run, or None."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_table(conn, "optimizer_nominations"):
                return None
            row = conn.execute(
                "SELECT created_at, ladder_sha FROM optimizer_nominations "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (str(row[0]), str(row[1])) if row else None


def nomination_run_due(db_path: Path, *, cadence_days: int = NOMINATION_CADENCE_DAYS) -> bool:
    """True when the newest run is absent, older than the cadence, or was made
    against a different candidate frontier (a new model landing is exactly when
    re-nomination pays)."""
    info = newest_run_info(db_path)
    if info is None:
        return True
    created_at, sha = info
    if sha != frontier_sha(db_path):
        return True
    try:
        age = datetime.now(UTC).replace(tzinfo=None) - datetime.fromisoformat(created_at)
    except ValueError:
        return True
    return age > timedelta(days=cadence_days)


def pending_nominations(
    db_path: Path, *, kinds: tuple[str, ...] = (KIND_MODEL_DOWNGRADE,)
) -> list[Nomination]:
    """Pending nominations of the given kinds, priority order."""
    if not db_path.exists():
        return []
    placeholders = ",".join("?" * len(kinds))
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if not _has_table(conn, "optimizer_nominations"):
                return []
            rows = conn.execute(
                f"""
                SELECT id, purpose, kind, priority, incumbent_model, candidates_json,
                       rationale, risk_tier, suggested_min_n, source, headroom_usd_30d,
                       cost_usd_30d, calls_30d, expires_at
                FROM optimizer_nominations
                WHERE status = 'pending' AND kind IN ({placeholders})
                ORDER BY priority, id
                """,
                kinds,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("pending_nominations: %s", exc)
        return []
    out: list[Nomination] = []
    for r in rows:
        try:
            cands_obj: object = json.loads(str(r["candidates_json"] or "[]"))
        except json.JSONDecodeError:
            cands_obj = []
        cands = tuple(
            c
            for c in (cast("list[object]", cands_obj) if isinstance(cands_obj, list) else [])
            if isinstance(c, str)
        )
        out.append(
            Nomination(
                purpose=str(r["purpose"]),
                kind=str(r["kind"]),
                priority=int(r["priority"]),
                incumbent_model=str(r["incumbent_model"]),
                candidates=cands,
                rationale=str(r["rationale"]),
                risk_tier=str(r["risk_tier"]),
                suggested_min_n=int(r["suggested_min_n"])
                if r["suggested_min_n"] is not None
                else None,
                source=str(r["source"]),
                headroom_usd_30d=float(r["headroom_usd_30d"] or 0.0),
                cost_usd_30d=float(r["cost_usd_30d"] or 0.0),
                calls_30d=int(r["calls_30d"] or 0),
                expires_at=str(r["expires_at"]) if r["expires_at"] is not None else None,
                row_id=int(r["id"]),
            )
        )
    return out


def excluded_purposes(db_path: Path) -> set[str]:
    """Purposes with a live (pending, unexpired) exclusion — MINUS anything the
    rotation floor force-reincludes (no graded verdict within MAX_UNSWEPT_DAYS):
    a bad exclusion can defer measurement, never kill it (§10 Q2)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    excluded: set[str] = set()
    for nom in pending_nominations(db_path, kinds=(KIND_EXCLUDE,)):
        if nom.expires_at is None:
            continue
        try:
            if datetime.fromisoformat(nom.expires_at) <= now:
                continue  # TTL elapsed — self-healed
        except ValueError:
            continue
        excluded.add(nom.purpose)
    if not excluded:
        return excluded
    # Rotation floor: force-reinclude anything unswept too long.
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_table(conn, "model_eval_verdicts"):
                return excluded
            floor = (now - timedelta(days=MAX_UNSWEPT_DAYS)).isoformat()
            rows = conn.execute(
                "SELECT purpose, MAX(recorded_at) FROM model_eval_verdicts GROUP BY purpose"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return excluded
    last_swept = {str(p): str(ts) for p, ts in rows if ts is not None}
    return {
        p
        for p in excluded
        if p in last_swept and last_swept[p] >= floor  # recently measured → excludable
    }


def mark_nomination(db_path: Path, row_id: int, status: str) -> None:
    """Sweep bookkeeping: pending → swept | skipped. Best-effort."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_table(conn, "optimizer_nominations"):
                return
            conn.execute(
                "UPDATE optimizer_nominations SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now_iso(), row_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug("mark_nomination skipped: %s", exc)
