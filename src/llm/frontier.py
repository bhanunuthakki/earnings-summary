"""Pareto-frontier research + the candidate_models overlay
(meta_eval_governance.md §10.1 — owner decision Q5b, PR3).

The static ``model_ladder.MODEL_LADDER`` is a hand-edited constant; OpenRouter
alone exposes hundreds of open-weight models nobody curated. This module makes
the candidate pool DATA-REFRESHED:

* ``model_frontier_research`` — a MONTHLY Opus+web purpose that re-verifies the
  cross-provider cost/performance frontier (Claude · Gemini · OpenRouter),
  surfaces newly-released / newly-cheap models, and scores each candidate's
  *promise* (cheap × plausibly-capable). It automates the manual
  ``/refresh-frontier`` restamp for the OPTIMIZER's pool; the checked-in ladder
  stays the price source of truth for its own seed rows.
* ``candidate_models`` — the overlay table research upserts into. Discovered
  rows AUTO-ENTER the TEST pool (owner decision): eligible for
  ``scope="model_eval"`` replay immediately. Production routing is untouched —
  a discovered model reaches production only through the full switch bar
  (streak + Wilson gate + tier minimums).
* ``merged_cheaper_candidates`` — the union the nominator/sweep consult:
  static ladder ∪ active discovered rows, cheaper-than-incumbent, cheapest
  first. Static call sites (``family_of`` etc.) keep reading the constant;
  discovered slugs still dispatch correctly via ``model_ladder.backend_for``'s
  slash-slug convention.

Isolation: research is meta machinery — ``scope="meta_eval"``,
``CAPTURE_DENYLIST``, excluded from the workload inventory (EVAL_SCOPES). It
reads *about* models; it never touches a production generation prompt (I2/I5).
Any research failure degrades to the existing pool — steering never stalls.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from llm.model_ladder import (
    CLAUDE,
    GEMINI,
    MODEL_LADDER,
    OPENROUTER,
    cheaper_candidates,
    model_rank,
)

log = logging.getLogger(__name__)

FRONTIER_PURPOSE = "model_frontier_research"

# Validation clamps for researched rows (fail-closed: a row outside these is
# dropped + logged, never upserted).
_MAX_RESEARCH_ROWS = 12
_MAX_USD_PER_MTOK = 1000.0
_FAMILIES = (CLAUDE, GEMINI, OPENROUTER)

# Instruction scaffold for model_frontier_research (v1 — prompt_versions).
FRONTIER_PROMPT = """\
You are refreshing the candidate-model frontier for an LLM cost optimizer.

Research CURRENT (verify with web search) general-availability API pricing and
capability signals for language models across three providers:
- Anthropic Claude tiers (family "claude"),
- Google Gemini tiers (family "gemini"),
- open-weight models served via OpenRouter (family "openrouter", ids are
  provider/model slugs like "deepseek/deepseek-chat").

Also flag any INDEPENDENT (non-Anthropic, non-Google) model that would make a
better cheap JUDGE than the incumbent (currently deepseek/deepseek-v4-flash).
Judging is this platform's measurement instrument — every model and prompt
promotion rests on judged verdicts — so a cheaper or sharper independent judge
is worth surfacing even when it is unsuitable as a production generator. Note
its context window (a judge prompt must never truncate) and any public
judge-agreement benchmark you can verify.

Goal: surface models that could serve as CHEAPER candidates for analytical /
extraction / classification workloads — especially newly released or newly
re-priced models, and the most promising handful of OpenRouter open-weight
models (do NOT list hundreds; pick the few worth testing).

The models already known to the optimizer (do not re-list them):
{known_ids}

Respond with ONLY a JSON object:
{{"candidates": [
  {{"model_id": "<exact API/OpenRouter id>",
   "family": "claude" | "gemini" | "openrouter",
   "input_usd_per_mtok": <number>,
   "output_usd_per_mtok": <number>,
   "promise": <0..1 — cheap x plausibly-capable for analytical text work>,
   "source_url": "<pricing page you verified>",
   "notes": "<one line: what it is / why promising>"}},
  ...
]}}
Prices must be the provider's public list prices in USD per million tokens.
Omit anything you could not verify. An empty list is a valid answer.
"""

WebCall = Callable[..., str]


@dataclass(frozen=True, slots=True)
class CandidateModel:
    """One overlay row — a frontier-discovered (or manually added) candidate."""

    model_id: str
    family: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    promise: float

    @property
    def blended_usd_per_mtok(self) -> float:
        # Same 6:1 output-weighting as ModelCost — the shared rank key.
        return (6.0 * self.input_usd_per_mtok + self.output_usd_per_mtok) / 7.0


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def load_candidate_models(db_path: Path) -> dict[str, CandidateModel]:
    """Active overlay rows keyed by model id. Empty on missing DB/table (the
    pool degrades to the static ladder — never an error)."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if not _has_table(conn, "candidate_models"):
                return {}
            rows = conn.execute(
                "SELECT model_id, family, input_usd_per_mtok, output_usd_per_mtok, promise "
                "FROM candidate_models WHERE status = 'active'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("load_candidate_models: %s", exc)
        return {}
    out: dict[str, CandidateModel] = {}
    for r in rows:
        mid = str(r["model_id"])
        out[mid] = CandidateModel(
            model_id=mid,
            family=str(r["family"]),
            input_usd_per_mtok=float(r["input_usd_per_mtok"]),
            output_usd_per_mtok=float(r["output_usd_per_mtok"]),
            promise=float(r["promise"]),
        )
    return out


def promise_of(db_path: Path, model_id: str) -> float:
    """The overlay's promise score for a model (0.5 neutral for static-ladder
    models — they earned their place by being curated)."""
    row = load_candidate_models(db_path).get(model_id)
    return row.promise if row is not None else 0.5


def merged_cheaper_candidates(
    db_path: Path, incumbent: str, *, include_openrouter: bool = True
) -> list[str]:
    """Static ``cheaper_candidates`` ∪ active discovered rows strictly cheaper
    than the incumbent, cheapest first. The overlay never shadows a static id
    (the checked-in ladder is the price authority for its own rows)."""
    inc_rank = model_rank(incumbent)
    base = cheaper_candidates(incumbent, include_gemini=True, include_openrouter=include_openrouter)
    if inc_rank is None:
        return base
    extras: list[tuple[float, str]] = []
    for mid, cand in load_candidate_models(db_path).items():
        if mid in MODEL_LADDER or mid == incumbent:
            continue
        if not include_openrouter and cand.family == OPENROUTER:
            continue
        if cand.blended_usd_per_mtok < inc_rank:
            extras.append((cand.blended_usd_per_mtok, mid))
    if not extras:
        return base  # preserve the static ladder's own tie-breaking exactly
    # Stable merge: base keeps its relative order (the ladder's tie-breaks);
    # extras slot in by blended rank.
    ranked = [((model_rank(m) or 0.0), i, m) for i, m in enumerate(base)]
    ranked += [(blended, len(base) + j, mid) for j, (blended, mid) in enumerate(sorted(extras))]
    ranked.sort()
    return [m for _rank, _idx, m in ranked]


def frontier_sha(db_path: Path) -> str:
    """Fingerprint of the WHOLE candidate frontier: the static ladder + active
    overlay rows (ids + prices). A change is the re-nomination trigger."""
    from llm.model_ladder import ladder_sha

    parts = [ladder_sha()]
    for mid, cand in sorted(load_candidate_models(db_path).items()):
        parts.append(f"{mid}:{cand.family}:{cand.input_usd_per_mtok}:{cand.output_usd_per_mtok}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _validate_candidate(entry: dict[str, object], rejected: set[str]) -> CandidateModel | None:
    """Fail-closed row validation (closed vocabulary + price sanity). Ids in
    ``rejected`` — the static ladder (its prices live in code) plus rows already
    validated this response (dedup) — are dropped; overlay rows are NOT in the
    set, so a research pass can re-verify/re-price them."""
    mid = entry.get("model_id")
    fam = entry.get("family")
    if not isinstance(mid, str) or not mid.strip() or not isinstance(fam, str):
        return None
    mid = mid.strip()
    if fam not in _FAMILIES or mid in rejected:
        return None
    # Id shape must match the family it claims (an openrouter id is a slug).
    if fam == OPENROUTER and "/" not in mid:
        return None
    if fam == CLAUDE and not mid.startswith("claude"):
        return None
    if fam == GEMINI and not mid.startswith("gemini"):
        return None
    try:
        input_usd = float(cast("float | int | str", entry.get("input_usd_per_mtok")))
        output_usd = float(cast("float | int | str", entry.get("output_usd_per_mtok")))
    except (TypeError, ValueError):
        return None
    if not (0.0 < input_usd < _MAX_USD_PER_MTOK and 0.0 < output_usd < _MAX_USD_PER_MTOK):
        return None
    promise_raw = entry.get("promise")
    promise = float(promise_raw) if isinstance(promise_raw, (int, float)) else 0.5
    promise = min(1.0, max(0.0, promise))
    return CandidateModel(
        model_id=mid,
        family=fam,
        input_usd_per_mtok=input_usd,
        output_usd_per_mtok=output_usd,
        promise=promise,
    )


def run_frontier_research(
    db_path: Path,
    *,
    web_call: WebCall | None = None,
    run_id: str | None = None,
) -> int:
    """One monthly research pass: web-verify the frontier, validate fail-closed,
    upsert ``candidate_models`` (discovered rows auto-enter the TEST pool).
    Returns the number of rows upserted; ANY failure returns 0 with a warning —
    the loop proceeds on the existing pool (steering never stalls)."""
    rid = run_id or uuid.uuid4().hex
    # The prompt lists EVERYTHING tracked (static + overlay) to discourage
    # redundant listing; validation rejects only STATIC ladder ids — the
    # checked-in ladder is the price authority for its own rows, while overlay
    # rows may be RE-verified/re-priced by a later research pass (that refresh
    # is the point of the monthly cadence).
    tracked = set(MODEL_LADDER) | set(load_candidate_models(db_path))
    prompt = FRONTIER_PROMPT.format(known_ids=", ".join(sorted(tracked)))
    rejected: set[str] = set(MODEL_LADDER)
    if web_call is None:
        from llm.cli import call_llm_with_web

        web_call = call_llm_with_web
    try:
        raw = web_call(
            prompt,
            purpose=FRONTIER_PURPOSE,
            scope="meta_eval",
            run_id=rid,
        )
        from llm.structured import parse_json_payload

        payload = parse_json_payload(raw, expect="object", required_keys=("candidates",))
    except Exception as exc:
        log.warning(
            "frontier research failed (%s: %s) — keeping the existing pool",
            type(exc).__name__,
            str(exc)[:200],
        )
        return 0
    if not isinstance(payload, dict):
        return 0
    candidates_raw = cast("dict[str, object]", payload).get("candidates")
    if not isinstance(candidates_raw, list):
        return 0
    validated: list[CandidateModel] = []
    dropped = 0
    for entry_obj in cast("list[object]", candidates_raw)[:_MAX_RESEARCH_ROWS]:
        if not isinstance(entry_obj, dict):
            dropped += 1
            continue
        entry = cast("dict[str, object]", entry_obj)
        cand = _validate_candidate(entry, rejected)
        if cand is None:
            dropped += 1
            continue
        validated.append(cand)
        rejected.add(cand.model_id)  # in-response dedup
    if dropped:
        log.info("frontier research: %d row(s) dropped by validation", dropped)
    if not validated:
        return 0
    return _upsert_candidates(
        db_path,
        validated,
        research_run_id=rid,
        raw_entries=cast("list[object]", candidates_raw),
    )


def _upsert_candidates(
    db_path: Path,
    candidates: list[CandidateModel],
    *,
    research_run_id: str,
    raw_entries: list[object],
) -> int:
    """Best-effort write (telemetry-grade: a failed write logs, never raises)."""
    # source_url/notes ride along from the raw entries when present.
    extras: dict[str, tuple[str | None, str | None]] = {}
    for entry_obj in raw_entries:
        if not isinstance(entry_obj, dict):
            continue
        entry = cast("dict[str, object]", entry_obj)
        mid = entry.get("model_id")
        if not isinstance(mid, str):
            continue
        url = entry.get("source_url")
        notes = entry.get("notes")
        extras[mid.strip()] = (
            url if isinstance(url, str) else None,
            notes if isinstance(notes, str) else None,
        )
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_table(conn, "candidate_models"):
                log.warning("candidate_models table absent — research result not persisted")
                return 0
            for cand in candidates:
                url, notes = extras.get(cand.model_id, (None, None))
                conn.execute(
                    """
                    INSERT INTO candidate_models
                        (model_id, family, input_usd_per_mtok, output_usd_per_mtok,
                         promise, source, status, source_url, notes, research_run_id,
                         first_seen_at, verified_at)
                    VALUES (?, ?, ?, ?, ?, 'frontier_research', 'active', ?, ?, ?, ?, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        input_usd_per_mtok = excluded.input_usd_per_mtok,
                        output_usd_per_mtok = excluded.output_usd_per_mtok,
                        promise = excluded.promise,
                        source_url = excluded.source_url,
                        notes = excluded.notes,
                        research_run_id = excluded.research_run_id,
                        verified_at = excluded.verified_at
                    """,
                    (
                        cand.model_id,
                        cand.family,
                        cand.input_usd_per_mtok,
                        cand.output_usd_per_mtok,
                        cand.promise,
                        url,
                        notes,
                        research_run_id,
                        now,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("candidate_models upsert failed: %s", exc)
        return 0
    log.info("frontier research: %d candidate(s) upserted", len(candidates))
    return len(candidates)
