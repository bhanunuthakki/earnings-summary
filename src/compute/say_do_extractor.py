"""Automated LLM extraction of forward-looking commitments from transcripts.

Closes the manifest-handoff loop in compute/say_do.py: instead of a human (or
in-session LLM) hand-authoring the manifest JSON, this module assembles a
prompt from the transcript text + the ticker's canonical KPI catalog, calls
the LLM through the governed ``call_llm`` entry point (purpose
``saydo_commitment_extract``), validates the JSON response against the
existing CommitmentInput schema, and returns a manifest ready for
persist_manifest.

Design choices:
  - Reuses the existing CommitmentInput / CommitmentExtractionManifest models
    (no schema duplication).
  - Constrains the LLM to canonical kpi_definitions.name values for the
    ticker — out-of-catalog names are dropped so the matcher's JOIN actually
    fires later. Without this, LLMs hallucinate KPI names like
    "Q4 revenue growth" that don't match any kpi_definitions row.
  - injects ticker, period_made, transcript_segment_id from caller context
    (LLM doesn't need to know them).
  - LLM call is injected as a parameter so tests can stub it out.
  - A transcript that yields ZERO commitments is a normal, common outcome —
    it is recorded in ``commitment_scan_log`` (0129) so the daily backfill
    doesn't re-scan the same transcript forever (this exact loop was burning
    ~$25/day of anonymous Sonnet calls before the marker existed).
  - Tickers with an EMPTY kpi_definitions catalog are excluded from the
    pending set entirely: the prompt would instruct the model to return
    ``{"commitments": []}``, so the call's outcome is predetermined and the
    LLM spend is pure waste. They become eligible again the moment a KPI
    catalog is seeded (no scan marker is written for the skip).
  - An unusable LLM response raises ``CommitmentParseError`` (after one
    retry-with-feedback) instead of degrading to an empty manifest — an
    empty manifest is indistinguishable from a legitimate "no commitments
    in this call" and would poison the scan log (the silent-empty
    pathology, directives/llm_calls.md).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field, ValidationError

from compute.say_do import CommitmentExtractionManifest, CommitmentInput
from compute.thesis_evaluator import Comparator
from models.facts import Unit

log = logging.getLogger(__name__)

# Soft cap on transcript text passed to the LLM. The aggregator transcripts
# we're seeing are 20-50K chars; sonnet-4-6 has plenty of context, but bigger
# inputs aren't worth the latency for our purpose.
MAX_TRANSCRIPT_CHARS = 60_000

# Strip ``` fences the LLM sometimes adds despite explicit instructions.
_FENCE_RX = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Re-ask preamble when the first response is unusable (mirrors
# llm.structured._RETRY_PREAMBLE; local copy because our llm_call boundary is
# an injected plain-text callable, not call_llm itself).
_RETRY_PREAMBLE = (
    "IMPORTANT: your previous response was not the valid JSON requested. "
    "Return ONLY the JSON specified in the prompt — no markdown fences, "
    "no commentary, no prefatory prose.\n\n"
)


class CommitmentParseError(ValueError):
    """The LLM returned unusable JSON (bad syntax or top-level shape).

    Raised instead of returning an empty manifest: an empty manifest means
    "the model read the transcript and found no commitments", which is a
    persistable outcome — a parse failure is not, and must never be recorded
    as a clean scan."""


@dataclass(frozen=True)
class TranscriptContext:
    """All ticker/period context the LLM does NOT see, but the persister needs."""

    ticker: str
    period_made: datetime
    transcript_segment_id: int


class _LLMCommitment(BaseModel):
    """The shape the LLM returns. Relaxed — we promote to CommitmentInput
    after injecting ticker/period_made/segment_id from TranscriptContext.
    """

    kpi_name: str = Field(min_length=1, max_length=200)
    comparator: Comparator
    target_value: Decimal
    unit: Unit
    period_target: datetime
    narrative: str = Field(min_length=1, max_length=1000)


class _LLMResponse(BaseModel):
    """Top-level shape we expect the model to return."""

    commitments: list[_LLMCommitment] = Field(default_factory=list[_LLMCommitment])


def fetch_kpi_catalog(conn: sqlite3.Connection, ticker: str) -> list[tuple[str, str]]:
    """Return [(name, unit), ...] for the ticker's kpi_definitions.

    The LLM picks from this catalog so kpi_name matches what the matcher
    will JOIN on later. Tickers with no catalog get an empty list — the
    extractor still runs but the LLM is told there are no valid KPIs and
    is expected to return zero commitments.
    """
    cur = conn.execute(
        "SELECT name, unit FROM kpi_definitions WHERE UPPER(ticker) = ? ORDER BY name",
        (ticker.upper(),),
    )
    return [(row["name"], row["unit"]) for row in cur.fetchall()]


def fetch_transcript_text_and_segment(
    conn: sqlite3.Connection, transcript_id: int
) -> tuple[str, int, datetime] | None:
    """Return (text, segment_id, period_end) for the transcript's longest segment, or None."""
    cur = conn.execute(
        "SELECT t.period_end, ts.id AS segment_id, ts.text "
        "FROM transcripts t JOIN transcript_segments ts ON ts.transcript_id = t.id "
        "WHERE t.id = ? ORDER BY length(ts.text) DESC LIMIT 1",
        (transcript_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    period_end = row["period_end"]
    if isinstance(period_end, str):
        period_end = datetime.fromisoformat(period_end)
    return (row["text"], int(row["segment_id"]), period_end)


def scan_log_available(conn: sqlite3.Connection) -> bool:
    """True when the commitment_scan_log table (migration 0129) exists.

    Selection degrades gracefully on a pre-0129 DB (hand-built test fixtures,
    a prod DB awaiting migration) — with a WARNING, because without the log
    every zero-commitment transcript is re-scanned daily at full LLM cost."""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'commitment_scan_log'"
    )
    available = cur.fetchone() is not None
    if not available:
        log.warning(
            "commitment_scan_log table missing (migration 0129 not applied) — "
            "zero-commitment transcripts WILL be re-scanned on every run"
        )
    return available


def transcripts_pending_extraction(
    conn: sqlite3.Connection, ticker: str | None = None
) -> list[tuple[int, str, datetime]]:
    """Return [(transcript_id, ticker, period_end), ...] for transcripts worth
    an extraction LLM call. Optional --ticker filter.

    A transcript is pending iff ALL of:
      - it has no management_commitments rows yet;
      - it has no commitment_scan_log row (i.e. never scanned — a recorded
        zero-commitment scan is a real outcome, not a retry candidate);
      - its ticker has at least one kpi_definitions row (an empty catalog
        makes the call's outcome predetermined: the prompt tells the model
        to return no commitments)."""
    sql = (
        "SELECT t.id, t.ticker, t.period_end FROM transcripts t "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM management_commitments mc "
        "  JOIN transcript_segments ts ON ts.id = mc.transcript_segment_id "
        "  WHERE ts.transcript_id = t.id"
        ") "
        "AND EXISTS ("
        "  SELECT 1 FROM kpi_definitions k WHERE UPPER(k.ticker) = UPPER(t.ticker)"
        ")"
    )
    if scan_log_available(conn):
        sql += (
            " AND NOT EXISTS (  SELECT 1 FROM commitment_scan_log l WHERE l.transcript_id = t.id)"
        )
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " AND UPPER(t.ticker) = ?"
        params = (ticker.upper(),)
    sql += " ORDER BY t.ticker, t.period_end DESC"
    cur = conn.execute(sql, params)
    out: list[tuple[int, str, datetime]] = []
    for row in cur.fetchall():
        period_end = row["period_end"]
        if isinstance(period_end, str):
            period_end = datetime.fromisoformat(period_end)
        out.append((int(row["id"]), row["ticker"], period_end))
    return out


def record_scan(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    n_extracted: int,
    prompt_version: str | None = None,
) -> None:
    """Persist "this transcript was scanned" so zero-commitment transcripts
    are never re-scanned. No-op (with the scan_log_available warning) on a
    pre-0129 DB. ``prompt_version`` is recorded so a future prompt bump can
    invalidate old scans by deleting rows with a stale version."""
    if not scan_log_available(conn):
        return
    scanned_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
    conn.execute(
        "INSERT INTO commitment_scan_log (transcript_id, scanned_at, n_extracted, prompt_version) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(transcript_id) DO UPDATE SET "
        "  scanned_at = excluded.scanned_at, "
        "  n_extracted = excluded.n_extracted, "
        "  prompt_version = excluded.prompt_version",
        (transcript_id, scanned_at, n_extracted, prompt_version),
    )
    conn.commit()


def build_extraction_prompt(
    *, ticker: str, transcript_text: str, kpi_catalog: list[tuple[str, str]], period_made: datetime
) -> str:
    """Assemble the LLM prompt. KPI catalog is constrained to this ticker's defs."""
    text = transcript_text[:MAX_TRANSCRIPT_CHARS]
    truncated = len(transcript_text) > MAX_TRANSCRIPT_CHARS

    catalog_lines = "\n".join(f"  - {name}  (unit: {unit})" for name, unit in kpi_catalog)
    if not kpi_catalog:
        catalog_lines = "  (no KPIs defined for this ticker — return commitments: [])"

    period_made_iso = period_made.date().isoformat()
    truncation_note = (
        f"\n\nNOTE: transcript was truncated to first {MAX_TRANSCRIPT_CHARS:,} characters."
        if truncated
        else ""
    )

    return f"""You are an equity research analyst extracting forward-looking quantitative commitments from an earnings call transcript.

TICKER: {ticker.upper()}
CALL DATE (period_made): {period_made_iso}

YOUR TASK
Identify only quantitative, forward-looking statements that constitute a commitment management is making about a future quarter or year. Examples that QUALIFY:
  - "We expect Q1 revenue to grow at least 15% YoY"  -> comparator=ge, target_value=15, unit=percent
  - "Operating margin should be around 30% next quarter" -> comparator=eq, target_value=30, unit=percent
  - "Capex will not exceed $20B for the year"          -> comparator=le, target_value=20, unit=actual

Examples that DO NOT qualify (skip them):
  - Backward-looking results ("Q4 revenue grew 12%")
  - Vague qualitative ("we expect strength continuing")
  - Analyst questions or third-party comments

VALID KPI NAMES (use ONLY these — out-of-catalog names will be DROPPED):
{catalog_lines}

OUTPUT FORMAT
Return ONLY valid JSON, no prose, no markdown fences, with this exact shape:

{{
  "commitments": [
    {{
      "kpi_name": "<MUST match exactly one name from the catalog above>",
      "comparator": "<one of: lt, le, gt, ge, eq>",
      "target_value": "<numeric, no units, no commas>",
      "unit": "<one of: percent, actual, ratio, count, basis_points, bps>",
      "period_target": "<YYYY-MM-DD — the END of the calendar quarter management is guiding for>",
      "narrative": "<verbatim or near-verbatim quote from the transcript, max 500 chars>"
    }}
  ]
}}

If no qualifying commitments are found, return: {{"commitments": []}}

TRANSCRIPT:
---
{text}
---{truncation_note}
"""


def parse_llm_response(
    json_text: str,
    *,
    context: TranscriptContext,
) -> CommitmentExtractionManifest:
    """Parse the LLM's JSON output into a typed manifest.

    - Strips markdown fences if present.
    - Raises CommitmentParseError when the text isn't JSON or the top-level
      shape fails Pydantic — a malformed response is a model failure, not a
      "no commitments" result, and must stay distinguishable from one.
    - Drops individual commitments whose downstream CommitmentInput
      construction fails (e.g. unparseable target_value).
    """
    cleaned = _FENCE_RX.sub("", json_text).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise CommitmentParseError(f"LLM response is not valid JSON: {e}") from e

    try:
        response = _LLMResponse.model_validate(payload)
    except ValidationError as e:
        raise CommitmentParseError(f"LLM response failed schema validation: {e}") from e

    commitments: list[CommitmentInput] = []
    for raw in response.commitments:
        try:
            commitments.append(
                CommitmentInput(
                    ticker=context.ticker.upper(),
                    period_made=context.period_made,
                    transcript_segment_id=context.transcript_segment_id,
                    period_target=raw.period_target,
                    kpi_name=raw.kpi_name,
                    comparator=raw.comparator,
                    target_value=raw.target_value,
                    unit=raw.unit,
                    narrative=raw.narrative,
                )
            )
        except (ValidationError, InvalidOperation) as e:
            log.warning(
                "Dropping commitment (kpi_name=%r) due to validation error: %s",
                raw.kpi_name,
                e,
            )
    return CommitmentExtractionManifest(commitments=commitments)


def extract_for_transcript(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    llm_call: Callable[[str], str],
) -> CommitmentExtractionManifest:
    """Orchestrator: pull transcript, build prompt, call LLM, parse, return manifest.

    `llm_call` is injected so tests can stub the LLM. Production callers pass
    a governed wrapper around ``call_llm(..., purpose="saydo_commitment_extract",
    ticker=...)`` — never the raw private CLI helper, which bypasses the
    purpose ledger, budgets and model routing.

    Behavior notes:
      - Empty KPI catalog ⇒ returns an empty manifest WITHOUT calling the LLM
        (the prompt would instruct the model to return nothing). Normal
        selection excludes these tickers already; this guard covers direct
        --transcript-id invocations.
      - Unusable LLM response ⇒ ONE retry with explicit feedback, then
        CommitmentParseError. Callers must not record a scan for a transcript
        that raised."""
    cur = conn.execute("SELECT t.ticker FROM transcripts t WHERE t.id = ?", (transcript_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"transcript_id={transcript_id} not found")
    ticker = row["ticker"]

    transcript_data = fetch_transcript_text_and_segment(conn, transcript_id)
    if transcript_data is None:
        raise ValueError(f"transcript_id={transcript_id} has no transcript_segments rows")
    text, segment_id, period_end = transcript_data
    catalog = fetch_kpi_catalog(conn, ticker)
    if not catalog:
        log.info(
            "transcript_id=%d ticker=%s has no KPI catalog — skipping LLM call "
            "(outcome is predetermined: zero commitments)",
            transcript_id,
            ticker,
        )
        return CommitmentExtractionManifest(commitments=[])

    prompt = build_extraction_prompt(
        ticker=ticker,
        transcript_text=text,
        kpi_catalog=catalog,
        period_made=period_end,
    )
    context = TranscriptContext(
        ticker=ticker,
        period_made=period_end,
        transcript_segment_id=segment_id,
    )
    response_text = llm_call(prompt)
    try:
        return parse_llm_response(response_text, context=context)
    except CommitmentParseError as first_exc:
        log.warning(
            "transcript_id=%d ticker=%s: unusable LLM response, retrying with feedback: %s",
            transcript_id,
            ticker,
            first_exc,
        )
    retry_text = llm_call(_RETRY_PREAMBLE + prompt)
    return parse_llm_response(retry_text, context=context)
