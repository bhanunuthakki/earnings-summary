"""Read/write API for the llm_artifacts table.

Pattern:
  - ``read_current(ticker, purpose, fiscal_period=None, scope='ticker')`` →
    the most recent non-superseded artifact, or None.
  - ``upsert(...)`` → idempotent insert. When the existing row's input_sha256
    matches the new one, returns the existing row's id without inserting
    (cache hit). When it differs, marks the prior superseded and inserts a
    new row, preserving history. Returns the new row's id.
  - ``mark_dirty(ticker, purposes, reason)`` → flips dirty=1 on a set of
    current artifacts. Called from the brief_dirty trigger chain (Phase 1.6).
  - ``drain_dirty(limit)`` → returns the next batch of artifacts that need
    regeneration. Used by the daily drain cron (Phase 8).

The module is best-effort against missing DB / missing table — the LLM call
that produced the row must never fail because the store can't write.

JSON columns (source_doc_ids, parent_artifact_ids) are stored as TEXT
because SQLite's JSON1 extension isn't guaranteed across environments.
Helpers serialize / deserialize transparently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from db_paths import resolve_db_path

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Artifact:
    """One artifact row as the public API sees it. JSON columns are decoded."""

    id: int
    ticker: str | None
    scope: str
    purpose: str
    fiscal_period: str | None
    content_md: str | None
    content_json: object | None  # decoded from JSON, may be dict/list/None
    input_sha256: str
    output_sha256: str | None
    model: str | None
    prompt_version: str
    generated_at: datetime
    expires_at: datetime | None
    superseded_by_id: int | None
    dirty: bool
    dirty_reason: str | None
    source_doc_ids: list[int] = field(default_factory=list)
    parent_artifact_ids: list[int] = field(default_factory=list)
    llm_call_id: int | None = None


@dataclass(slots=True)
class UpsertRequest:
    """Inputs to upsert(). Composes deterministically into the input_sha256
    cache key so any caller can be cache-correct without knowing the hash
    function. Add inputs here only when they actually affect the output —
    spurious inputs invalidate the cache unnecessarily."""

    ticker: str | None
    purpose: str
    scope: str = "ticker"
    fiscal_period: str | None = None
    content_md: str | None = None
    content_json: object | None = None
    model: str | None = None
    prompt_version: str = "v1"
    # Inputs that determine the cache key, hashed in order:
    cache_inputs: list[bytes | str] = field(default_factory=list)
    # Provenance — NOT hashed; these are descriptive metadata for the brief.
    source_doc_ids: list[int] = field(default_factory=list)
    parent_artifact_ids: list[int] = field(default_factory=list)
    expires_at: datetime | None = None
    llm_call_id: int | None = None


def compute_input_sha256(*, prompt_version: str, cache_inputs: list[bytes | str]) -> str:
    """Deterministic cache-key hash. Caller passes the prompt_version explicitly
    and a list of all inputs that contribute to the output. The hash is order-
    sensitive so the caller controls reproducibility."""
    h = hashlib.sha256()
    h.update(b"v=" + prompt_version.encode("utf-8") + b"\n")
    for chunk in cache_inputs:
        b = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
        h.update(b"\n")
    return h.hexdigest()


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    raw_src = row["source_doc_ids"]
    raw_par = row["parent_artifact_ids"]
    src_ids: list[int] = []
    par_ids: list[int] = []
    if raw_src:
        try:
            decoded = json.loads(raw_src)
            if isinstance(decoded, list):
                src_ids = [int(v) for v in decoded if isinstance(v, (int, float))]
        except (json.JSONDecodeError, TypeError):
            pass
    if raw_par:
        try:
            decoded = json.loads(raw_par)
            if isinstance(decoded, list):
                par_ids = [int(v) for v in decoded if isinstance(v, (int, float))]
        except (json.JSONDecodeError, TypeError):
            pass

    json_blob_raw = row["content_json"]
    content_json: object | None = None
    if json_blob_raw:
        try:
            content_json = json.loads(json_blob_raw)
        except json.JSONDecodeError:
            content_json = None

    return Artifact(
        id=int(row["id"]),
        ticker=row["ticker"],
        scope=row["scope"],
        purpose=row["purpose"],
        fiscal_period=row["fiscal_period"],
        content_md=row["content_md"],
        content_json=content_json,
        input_sha256=row["input_sha256"],
        output_sha256=row["output_sha256"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        generated_at=_parse_dt(row["generated_at"]),
        expires_at=_parse_dt(row["expires_at"]) if row["expires_at"] else None,
        superseded_by_id=row["superseded_by_id"],
        dirty=bool(row["dirty"]),
        dirty_reason=row["dirty_reason"],
        source_doc_ids=src_ids,
        parent_artifact_ids=par_ids,
        llm_call_id=row["llm_call_id"],
    )


def _parse_dt(v: object) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def read_current(
    *,
    ticker: str | None,
    purpose: str,
    fiscal_period: str | None = None,
    scope: str = "ticker",
    db_path: Path | str | None = None,
) -> Artifact | None:
    """Read the most recent non-superseded artifact for the scope tuple.
    Returns None when no artifact exists (e.g. first-run for this ticker)
    or when DB / table is unavailable."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT * FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ? AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
              AND superseded_by_id IS NULL
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (ticker, scope, purpose, fiscal_period),
        ).fetchone()
        return _row_to_artifact(row) if row else None
    except sqlite3.Error as exc:
        # Best-effort read: _open() already guards a missing table, but a table
        # that exists with a *drifted* schema (a legacy DB predating a column
        # add, or a partial test fixture) would otherwise raise here and crash
        # the render path. Degrade to "no cached artifact" instead — the caller
        # recomputes or shows the empty state, same as a genuine cache miss.
        log.warning({"event": "artifact_read_current_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def upsert(
    req: UpsertRequest,
    *,
    db_path: Path | str | None = None,
) -> tuple[int | None, bool]:
    """Idempotent upsert.

    Returns (artifact_id, was_cache_hit):
      - was_cache_hit=True  → existing row's input_sha256 matches; existing id
                              returned, no new row inserted.
      - was_cache_hit=False → either no prior row (insert) or prior input_sha256
                              differs (supersede + insert). New id returned.

    On DB unavailability returns (None, False) — caller must treat as "go
    ahead, compute the artifact" (the LLM call still produces output, just
    no persistence).
    """
    conn = _open(db_path)
    if conn is None:
        return (None, False)
    try:
        conn.row_factory = sqlite3.Row
        input_sha = compute_input_sha256(
            prompt_version=req.prompt_version, cache_inputs=req.cache_inputs
        )

        existing = conn.execute(
            """
            SELECT id, input_sha256, dirty FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ? AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
              AND superseded_by_id IS NULL
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (req.ticker, req.scope, req.purpose, req.fiscal_period),
        ).fetchone()

        # Cache hit — same input hash AND not marked dirty. The dirty flag wins
        # over hash equality because a trigger may have flagged the row even
        # though inputs didn't change (e.g. prompt_version bump applied
        # retroactively to the table).
        if existing is not None and existing["input_sha256"] == input_sha and not existing["dirty"]:
            return (int(existing["id"]), True)

        # Either no existing row, hash drift, or dirty — insert a new row and
        # supersede any prior current row.
        content_json_str = (
            json.dumps(req.content_json, ensure_ascii=False)
            if req.content_json is not None
            else None
        )
        output_sha = (
            hashlib.sha256((req.content_md or content_json_str or "").encode("utf-8")).hexdigest()
            if (req.content_md or content_json_str)
            else None
        )
        effective_expires_at = req.expires_at or default_expires_at(req.purpose)
        cur = conn.execute(
            """
            INSERT INTO llm_artifacts(
              ticker, scope, purpose, fiscal_period,
              content_md, content_json,
              input_sha256, output_sha256, model, prompt_version,
              generated_at, expires_at,
              superseded_by_id, dirty, dirty_reason,
              source_doc_ids, parent_artifact_ids, llm_call_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,NULL,?,?,?)
            """,
            (
                req.ticker,
                req.scope,
                req.purpose,
                req.fiscal_period,
                req.content_md,
                content_json_str,
                input_sha,
                output_sha,
                req.model,
                req.prompt_version,
                datetime.now(UTC).isoformat(),
                effective_expires_at.isoformat() if effective_expires_at else None,
                json.dumps(req.source_doc_ids) if req.source_doc_ids else None,
                json.dumps(req.parent_artifact_ids) if req.parent_artifact_ids else None,
                req.llm_call_id,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        if existing is not None:
            conn.execute(
                "UPDATE llm_artifacts SET superseded_by_id = ? WHERE id = ?",
                (new_id, int(existing["id"])),
            )
        conn.commit()
        return (new_id, False)
    except sqlite3.Error as exc:
        log.warning({"event": "artifact_upsert_failed", "error": str(exc)})
        return (None, False)
    finally:
        conn.close()


# Purposes whose output depends on financial_facts / kpi_facts /
# segment_periods state. When upstream facts change for a ticker (SEC ingest,
# FMP refresh, manual correction), these artifacts are marked dirty so the
# stale cache is not served.
#
# Two refresh paths consume this set, by purpose family:
#   * Brief-side purposes (bear_case, valuation_basis, qa_topics, …) are
#     regenerated by the drain executor — see _PURPOSE_TO_REGENERATOR in
#     execution/refresh_dirty_artifacts.py, which must cover exactly those.
#   * Trigger/news-side purposes (earnings_tone_diff, kpi_inflection_context,
#     saydo_due_context, material_news_classification, news_structuring) have
#     NO standalone drain regenerator — recomputing them is a side effect of the
#     daily trigger scan / news fetch, which already honor the dirty flag. The
#     drain intentionally classifies these as "refreshed by the daily scan"
#     rather than warning "no regenerator". Their TTLs (above) only let the
#     expiry sweep reap orphans.
FACT_DEPENDENT_PURPOSES: tuple[str, ...] = (
    "bear_case",
    "company_description",
    "filing_intelligence",
    "valuation_basis",
    "saydo_filter",
    "exec_comp_alignment",
    "qa_topics",
    # Earnings-tone diff caches the LLM comparison of a new transcript
    # against the prior 4 quarters' transcripts. Keyed by transcript ids,
    # so a fact-side restatement of the *same* transcript doesn't move
    # the key — but the trigger framework re-runs on every transcript
    # arrival anyway. Inclusion here participates in the existing
    # mark-dirty chain so a restatement that touches financial_facts
    # (which the comparison anchor reads) invalidates the cached diff.
    "earnings_tone_diff",
    # KPI-inflection context: the optional "why it matters" line attached to a
    # kpi_inflection alert. Keyed by (kpi_name, period_end, factual core,
    # thesis anchor). A fact-side restatement that moves the KPI series should
    # invalidate the cached prose, so it joins the mark-dirty chain here.
    "kpi_inflection_context",
    # SayDo-due context: the optional "what it means for the thesis" line
    # attached to a saydo_due alert. Keyed by (kpi_name, period_target, factual
    # core, thesis anchor). A fact-side restatement that moves the realized
    # value should invalidate the cached prose, so it joins the chain here.
    "saydo_due_context",
    # Material-news classification: the batched LLM materiality scoring of a
    # ticker's recent headlines against its thesis anchor. Keyed by (ticker,
    # sorted news ids, anchor sha). A fact-side restatement that moves the
    # thesis anchor should invalidate the cached scoring, so it joins the chain.
    "material_news_classification",
    # News structuring: the WebSearch->structured-rows extraction (the
    # FMP-independent news feed). Keyed by (ticker, utc date, anchor sha), so the
    # date key already bounds staleness to 24h and the anchor sha re-invalidates
    # on an anchor change at read time. Joining the chain makes it consistent
    # with its four trigger/news siblings: a fact-side restatement that moves the
    # thesis anchor proactively dirties the cached extraction rather than waiting
    # for the next day's date-key rollover.
    "news_structuring",
)


# Default TTL per purpose. The artifact's `expires_at` is set to now+TTL at
# upsert time when the caller doesn't pass one explicitly, and the drain
# loop treats `expires_at < now` as a soft dirty signal so cached LLM
# outputs don't live forever even if no fact change ever flips brief_dirty.
# Tuned by data sensitivity:
#   bear_case / company_description: company narrative changes slowly →
#     30 days
#   qa_topics / saydo_filter / exec_comp_alignment / valuation_basis:
#     anchored to the most recent quarter → 14 days
#   filing_intelligence: anchored to a specific 10-K/10-Q → 60 days
# A purpose not in this table gets no default TTL (caller must opt in).
_DEFAULT_TTL_DAYS: dict[str, int] = {
    "bear_case": 30,
    "company_description": 30,
    "qa_topics": 14,
    "saydo_filter": 14,
    "exec_comp_alignment": 14,
    "valuation_basis": 14,
    "filing_intelligence": 60,
    # SayDo pairs are anchored to a specific (prev_q, curr_q) tuple so they
    # don't age in the usual sense. The TTL is a safety net so that a stale
    # cached pair gets re-attempted if the operator hasn't touched the
    # ticker for a while. anchor_sha + summary_sha already drive the
    # primary invalidation via cache_inputs.
    "saydo_pair": 60,
    # Earnings-tone diff is anchored to a specific transcript-id tuple
    # (current + prior 4). The TTL acts as a safety net so a months-old
    # cached diff is re-attempted even if the trigger never re-fires;
    # the transcript-id key drives the primary invalidation.
    "earnings_tone_diff": 30,
    # KPI-inflection context is anchored to a (kpi_name, period_end) tuple.
    # The TTL is a safety net; the input_sha256 (factual core + anchor) drives
    # the primary invalidation when the underlying numbers move.
    "kpi_inflection_context": 30,
    # SayDo-due context is anchored to a (kpi_name, period_target) tuple. The
    # TTL is a safety net; the input_sha256 (factual core + anchor) drives the
    # primary invalidation when the realized value moves.
    "saydo_due_context": 30,
    # Material-news classification is anchored to a (ticker, news-id-set,
    # anchor) tuple via input_sha256. The TTL is a safety net so a stale cached
    # scoring is re-attempted even if the same news batch lingers; the news-id
    # set drives the primary invalidation when new stories land.
    "material_news_classification": 30,
    # News structuring is keyed by (ticker, utc date, anchor sha), so a fresh
    # entry is created each day and yesterday's is never read again. A short TTL
    # lets the drain's expiry sweep reap those daily orphans rather than letting
    # them accumulate in llm_artifacts forever; the date key drives the primary
    # daily invalidation.
    "news_structuring": 7,
}


def default_expires_at(purpose: str, *, now: datetime | None = None) -> datetime | None:
    """Compute the canonical expires_at for an artifact of the given purpose.

    Returns None for purposes without a TTL policy — caller may still set
    expires_at explicitly on the UpsertRequest. now is injectable for tests.
    """
    days = _DEFAULT_TTL_DAYS.get(purpose)
    if days is None:
        return None
    now = now if now is not None else datetime.now(UTC)
    return now + timedelta(days=days)


def mark_artifacts_dirty_for_fact_change(
    *,
    ticker: str,
    reason: str,
    db_path: Path | str | None = None,
    purposes: tuple[str, ...] | list[str] = FACT_DEPENDENT_PURPOSES,
) -> int:
    """Mark every fact-dependent artifact for `ticker` dirty.

    Called from the brief-rebuild path whenever upstream facts may have
    changed: the daily worker before each rebuild, the SEC silent-staleness
    detector, and any future "facts restated" trigger. Without this chain
    the LLM artifact cache stays valid even though the inputs to the prompt
    have shifted underneath it.

    Returns the count of rows actually flipped from clean→dirty.
    """
    return mark_dirty(
        ticker=ticker,
        purposes=list(purposes),
        reason=reason,
        db_path=db_path,
    )


def mark_dirty(
    *,
    ticker: str,
    purposes: list[str],
    reason: str,
    db_path: Path | str | None = None,
) -> int:
    """Flip dirty=1 on all current artifacts matching (ticker, purpose IN ...).
    Returns the count of rows updated. Best-effort — returns 0 on DB error."""
    if not purposes:
        return 0
    conn = _open(db_path)
    if conn is None:
        return 0
    try:
        placeholders = ",".join("?" * len(purposes))
        cur = conn.execute(
            f"""
            UPDATE llm_artifacts
            SET dirty = 1, dirty_reason = ?
            WHERE ticker = ?
              AND purpose IN ({placeholders})
              AND superseded_by_id IS NULL
              AND dirty = 0
            """,
            (reason, ticker, *purposes),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as exc:
        log.warning({"event": "artifact_mark_dirty_failed", "error": str(exc)})
        return 0
    finally:
        conn.close()


def drain_dirty(
    *,
    limit: int = 50,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> list[Artifact]:
    """Return up to `limit` artifacts that need regeneration.

    An artifact is considered due when:
      * dirty=1 (an upstream change flipped it), OR
      * expires_at < now (TTL has elapsed since the row was generated)

    Both paths land in the same drain queue so callers don't have to run
    two separate sweeps. Caller regenerates them via the purpose-specific
    generator + upsert; dirty=0 + a fresh expires_at are written on the new
    row.
    """
    conn = _open(db_path)
    if conn is None:
        return []
    now_iso = (now if now is not None else datetime.now(UTC)).isoformat()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM llm_artifacts
            WHERE superseded_by_id IS NULL
              AND (
                dirty = 1
                OR (expires_at IS NOT NULL AND expires_at < ?)
              )
            ORDER BY generated_at ASC
            LIMIT ?
            """,
            (now_iso, int(limit)),
        ).fetchall()
        return [_row_to_artifact(r) for r in rows]
    except sqlite3.Error as exc:
        # Best-effort: a drifted schema degrades to "nothing to drain" rather
        # than crashing the drain cron (see read_current for rationale).
        log.warning({"event": "artifact_drain_dirty_failed", "error": str(exc)})
        return []
    finally:
        conn.close()


def history(
    *,
    ticker: str,
    purpose: str,
    fiscal_period: str | None = None,
    scope: str = "ticker",
    limit: int = 20,
    db_path: Path | str | None = None,
) -> list[Artifact]:
    """Return all artifacts for the scope tuple (current + superseded), newest
    first. Powers "show me the evolution of META's bear case" queries."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM llm_artifacts
            WHERE COALESCE(ticker,'') = COALESCE(?, '')
              AND scope = ? AND purpose = ?
              AND COALESCE(fiscal_period, '') = COALESCE(?, '')
            ORDER BY generated_at DESC
            LIMIT ?
            """,
            (ticker, scope, purpose, fiscal_period, int(limit)),
        ).fetchall()
        return [_row_to_artifact(r) for r in rows]
    except sqlite3.Error as exc:
        # Best-effort: a drifted schema degrades to empty history rather than
        # crashing the query surface (see read_current for rationale).
        log.warning({"event": "artifact_history_failed", "error": str(exc)})
        return []
    finally:
        conn.close()


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Open a connection or return None when the DB or table is unavailable.
    Best-effort pattern matches llm_call_ledger so the LLM pipeline never
    fails on telemetry."""
    try:
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        # Verify table exists — graceful return otherwise
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        if cur is None:
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError) as exc:
        log.debug({"event": "artifact_store_open_failed", "error": str(exc)})
        return None
