"""Pareto-frontier candidate discovery + the candidate_models overlay
(meta_eval_governance.md §10.1 — owner decision Q5b, PR3; revised 2026-08-06).

The static ``model_ladder.MODEL_LADDER`` is a hand-edited constant; OpenRouter
alone exposes hundreds of open-weight models nobody curated. This module makes
the candidate pool DATA-REFRESHED, cheaply:

* ``run_frontier_research`` — pulls OpenRouter's public model catalog
  (``GET https://openrouter.ai/api/v1/models``, no auth, no LLM call, no
  tokens) and upserts the cheapest not-yet-known models into
  ``candidate_models``. This can run as often as useful — it costs an HTTP
  request, not a metered LLM call.

  An earlier revision of this module ran a MONTHLY Opus+``WebSearch`` pass and
  scored each candidate against the Artificial Analysis Intelligence Index.
  Both choices were wrong for this pipeline: the token-intensive search was
  unnecessary (OpenRouter's own catalog already gives exact prices, and
  Claude/Gemini pricing changes rarely enough to track by the existing manual
  cross-project ``/refresh-frontier`` restamp instead), and AA's index measures
  general reasoning/coding/agentic-tool-use — not finance-specific competence.

  This pipeline's actual finance benchmark is ``model_eval_verdicts``: judged
  output on real earnings-summary / DCF / thesis-tracking purposes
  (``backend_judge.py`` / ``model_eval.py``). A newly discovered candidate is
  UNSCORED until that loop grades it — same as any static-ladder model. No
  external index stands in for that judgment. See
  ``directives/meta_eval_governance.md`` §10.4.
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

Isolation: research reads *about* models, not from them — it never touches a
production generation prompt. Any research failure (network, malformed
response) degrades to the existing pool and returns 0 — steering never stalls.
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
    backend_for,
    cheaper_candidates,
    family_of,
    model_rank,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

FRONTIER_PURPOSE = "model_frontier_research"

OPENROUTER_MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
_CATALOG_TIMEOUT_SECONDS = 15

# Validation clamps for catalog rows (fail-closed: a row outside these is
# dropped + logged, never upserted).
_MAX_CATALOG_ROWS = 12
_MAX_USD_PER_MTOK = 1000.0

HttpGet = Callable[[str], object]


@dataclass(frozen=True, slots=True)
class CandidateModel:
    """One overlay row — a frontier-discovered (or manually added) candidate.

    ``promise`` defaults NEUTRAL (0.5) for a catalog-discovered row: at
    discovery time there is no capability signal to assign one from — this
    pipeline deliberately does not substitute a general benchmark for one.
    Real value accrues via ``model_eval_verdicts`` as the sweep actually tests
    the candidate on production purposes."""

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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
    models — they earned their place by being curated — and for freshly
    discovered ones, which carry no capability signal yet)."""
    row = load_candidate_models(db_path).get(model_id)
    return row.promise if row is not None else 0.5


def merged_backend_for(db_path: Path, model_id: str) -> str:
    """Resolve transport from the static ladder or active frontier overlay.

    Overlay rows are executable routing data, not just price metadata. Reject
    an unsupported recorded family instead of silently grading the model via
    Claude or another transport.
    """
    static_family = family_of(model_id)
    if static_family is not None:
        return static_family
    row = load_candidate_models(db_path).get(model_id)
    if row is None:
        return backend_for(model_id)
    if row.family not in {CLAUDE, GEMINI, OPENROUTER}:
        raise ValueError(f"unsupported candidate family {row.family!r} for model {model_id!r}")
    return row.family


def merged_rank(db_path: Path, model_id: str) -> float | None:
    """Blended $/MTok across the merged pool — static ladder first (the
    checked-in price is authoritative for its own rows), else the discovered
    overlay. None when the model is in neither."""
    static = model_rank(model_id)
    if static is not None:
        return static
    row = load_candidate_models(db_path).get(model_id)
    return row.blended_usd_per_mtok if row is not None else None


def merged_is_cheaper(db_path: Path, candidate_id: str, incumbent_id: str) -> bool:
    """Strictly-cheaper across the merged pool. False when either side is
    unpriced — unknown cost is not evidence of savings."""
    cand = merged_rank(db_path, candidate_id)
    inc = merged_rank(db_path, incumbent_id)
    if cand is None or inc is None:
        return False
    return cand < inc


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


def _default_http_get(url: str) -> object:
    import requests

    resp = requests.get(url, timeout=_CATALOG_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _extract_openrouter_row(entry: object) -> CandidateModel | None:
    """One row of OpenRouter's ``/models`` response -> a validated candidate,
    or None. Fail-closed: a malformed or nonsensical row is dropped, not
    coerced. OpenRouter reports per-token USD strings; converted to $/MTok
    here to match this pipeline's price basis."""
    if not isinstance(entry, dict):
        return None
    row = cast("dict[str, object]", entry)
    mid = row.get("id")
    if not isinstance(mid, str) or "/" not in mid:
        return None
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return None
    pricing_dict = cast("dict[str, object]", pricing)
    try:
        input_usd_per_token = float(cast("float | int | str", pricing_dict.get("prompt")))
        output_usd_per_token = float(cast("float | int | str", pricing_dict.get("completion")))
    except (TypeError, ValueError):
        return None
    input_usd = input_usd_per_token * 1_000_000.0
    output_usd = output_usd_per_token * 1_000_000.0
    # Excludes exactly-zero (free/rate-limited variants that don't reflect a
    # real production price signal) as well as nonsense/negative values.
    if not (0.0 < input_usd < _MAX_USD_PER_MTOK and 0.0 < output_usd < _MAX_USD_PER_MTOK):
        return None
    return CandidateModel(
        model_id=mid,
        family=OPENROUTER,
        input_usd_per_mtok=input_usd,
        output_usd_per_mtok=output_usd,
        promise=0.5,  # neutral — no capability signal at discovery time
    )


def run_frontier_research(
    db_path: Path,
    *,
    http_get: HttpGet | None = None,
    run_id: str | None = None,
) -> int:
    """One catalog-refresh pass: pull OpenRouter's public model list (no LLM,
    no tokens), keep the cheapest models not already known to the optimizer,
    and upsert them into ``candidate_models`` (discovered rows auto-enter the
    TEST pool). Returns the number of rows upserted; ANY failure (network,
    malformed response) returns 0 with a warning — the loop proceeds on the
    existing pool (steering never stalls)."""
    rid = run_id or uuid.uuid4().hex
    getter = http_get or _default_http_get
    try:
        payload = getter(OPENROUTER_MODELS_ENDPOINT)
    except Exception as exc:
        log.warning(
            "frontier catalog fetch failed (%s: %s) — keeping the existing pool",
            type(exc).__name__,
            str(exc)[:200],
        )
        return 0
    if not isinstance(payload, dict):
        return 0
    entries = cast("dict[str, object]", payload).get("data")
    if not isinstance(entries, list):
        return 0

    # Only STATIC ladder ids are off-limits — the checked-in ladder is the
    # price authority for its own rows. An already-discovered overlay row IS
    # eligible here, so a re-fetch can catch OpenRouter re-pricing it; that
    # refresh is the point of running this repeatedly.
    candidates: list[CandidateModel] = []
    dropped = 0
    for entry in cast("list[object]", entries):
        cand = _extract_openrouter_row(entry)
        if cand is None:
            dropped += 1
            continue
        if cand.model_id in MODEL_LADDER:
            continue
        candidates.append(cand)
    if dropped:
        log.info("frontier catalog: %d row(s) dropped by validation", dropped)
    if not candidates:
        return 0

    # Cheapest first, capped — "do not list hundreds, keep the few worth
    # testing" is now a deterministic sort instead of an LLM judgment call.
    # Already-discovered rows outside this window simply keep their last
    # known price rather than being evicted.
    candidates.sort(key=lambda c: c.blended_usd_per_mtok)
    selected = candidates[:_MAX_CATALOG_ROWS]
    return _upsert_candidates(db_path, selected, research_run_id=rid)


def _upsert_candidates(
    db_path: Path,
    candidates: list[CandidateModel],
    *,
    research_run_id: str,
) -> int:
    """Best-effort write (telemetry-grade: a failed write logs, never raises)."""
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    try:
        conn = connect_sqlite(
            db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            if not _has_table(conn, "candidate_models"):
                log.warning("candidate_models table absent — research result not persisted")
                return 0
            for cand in candidates:
                conn.execute(
                    """
                    INSERT INTO candidate_models
                        (model_id, family, input_usd_per_mtok, output_usd_per_mtok,
                         promise, source, status, source_url, notes,
                         research_run_id, first_seen_at, verified_at)
                    VALUES (?, ?, ?, ?, ?, 'frontier_research', 'active', ?, ?, ?, ?, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        input_usd_per_mtok = excluded.input_usd_per_mtok,
                        output_usd_per_mtok = excluded.output_usd_per_mtok,
                        source_url = excluded.source_url,
                        research_run_id = excluded.research_run_id,
                        verified_at = excluded.verified_at
                    """,
                    (
                        cand.model_id,
                        cand.family,
                        cand.input_usd_per_mtok,
                        cand.output_usd_per_mtok,
                        cand.promise,
                        f"https://openrouter.ai/models/{cand.model_id}",
                        "auto-discovered from the OpenRouter model catalog (cheapest not yet known)",
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
    log.info("frontier catalog: %d candidate(s) upserted", len(candidates))
    return len(candidates)
