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
  - Tickers with an EMPTY kpi_definitions catalog still scan for novel or
    one-off management indicators. They cannot yield catalog-backed
    commitments, but the retained staging observation is still useful.
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

from compute.management_indicators import (
    IndicatorRecurrence,
    IndicatorScope,
    ManagementIndicatorInput,
)
from compute.say_do import CommitmentExtractionManifest, CommitmentInput
from compute.thesis_evaluator import Comparator
from models.facts import Unit
from provenance.selection import selected_transcripts_relation

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
    source_doc_id: int | None = None
    speaker: str | None = None


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


class _LLMManagementIndicator(BaseModel):
    """Novel source measurement that must remain outside the KPI catalog."""

    raw_label: str = Field(min_length=1, max_length=256)
    value: Decimal
    unit: Unit
    scope: IndicatorScope = IndicatorScope.UNSPECIFIED
    recurrence: IndicatorRecurrence = IndicatorRecurrence.UNKNOWN
    source_excerpt: str = Field(min_length=1, max_length=2000)


class _LLMResponse(BaseModel):
    """Top-level shape we expect the model to return."""

    commitments: list[_LLMCommitment] = Field(default_factory=list[_LLMCommitment])
    novel_indicators: list[_LLMManagementIndicator] = Field(
        default_factory=list[_LLMManagementIndicator]
    )


class TranscriptExtractionManifest(CommitmentExtractionManifest):
    """Commitments plus unpromoted, source-bound novel indicators."""

    indicators: list[ManagementIndicatorInput] = Field(
        default_factory=list[ManagementIndicatorInput]
    )


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
    """Return (text, segment_id, period_end) for the longest segment, or None."""
    transcripts = selected_transcripts_relation(conn)
    cur = conn.execute(
        f"SELECT t.period_end, ts.id AS segment_id, ts.text "  # nosec B608 -- trusted internal SQL shape; values remain bound
        f"FROM {transcripts} t JOIN transcript_segments ts ON ts.transcript_id = t.id "
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


def _segment_source_metadata(conn: sqlite3.Connection, segment_id: int) -> tuple[int, str | None]:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(transcript_segments)")}
    speaker = "ts.speaker" if "speaker" in columns else "NULL"
    row = conn.execute(
        "SELECT tr.document_id, " + speaker + " "
        "FROM transcript_segments ts JOIN transcripts tr ON tr.id=ts.transcript_id "
        "WHERE ts.id=?",
        (segment_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"transcript_segment_id={segment_id} has no source document")
    return int(row[0]), str(row[1]).strip() if row[1] else None


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
      - it has no commitment_scan_log row (or is being re-run after a prompt
        version reset). Novel/one-off management indicators may be relevant
        even when the ticker has no existing KPI catalog, so catalog absence
        must not suppress a transcript scan."""
    transcripts = selected_transcripts_relation(conn)
    sql = (
        f"SELECT t.id, t.ticker, t.period_end FROM {transcripts} t "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM management_commitments mc "
        "  JOIN transcript_segments ts ON ts.id = mc.transcript_segment_id "
        "  WHERE ts.transcript_id = t.id"
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

VALID KPI NAMES (use ONLY these for `commitments` — do not create a new
catalog KPI name here):
{catalog_lines}

NOVEL / ONE-OFF MANAGEMENT INDICATORS
For a quantitative management-reported measurement that is not an exact valid
KPI name above, record it in `novel_indicators` instead of silently dropping
it. Include the raw label, value, unit, scope, whether management presented it
as recurring or one-off, and a short exact source excerpt. These are research
staging observations, NOT canonical KPIs and must never be included in
`commitments` unless they match a valid KPI name above.

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
  ],
  "novel_indicators": [
    {{
      "raw_label": "<management's label, preserving qualifiers>",
      "value": "<numeric, no units, no commas>",
      "unit": "<one of: actual, thousands, millions, billions, percent, ratio, bps, count>",
      "scope": "<one of: consolidated, segment, product, geography, unspecified>",
      "recurrence": "<one of: recurring, one_off, unknown>",
      "source_excerpt": "<exact supporting transcript excerpt, max 2,000 chars>"
    }}
  ]
}}

If neither category is found, return: {{"commitments": [], "novel_indicators": []}}

TRANSCRIPT:
---
{text}
---{truncation_note}
"""


def parse_llm_response(
    json_text: str,
    *,
    context: TranscriptContext,
) -> TranscriptExtractionManifest:
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
    indicators: list[ManagementIndicatorInput] = []
    if response.novel_indicators and context.source_doc_id is None:
        raise CommitmentParseError("novel indicators require a transcript source document")
    for raw in response.novel_indicators:
        try:
            indicators.append(
                ManagementIndicatorInput(
                    ticker=context.ticker.upper(),
                    transcript_segment_id=context.transcript_segment_id,
                    raw_label=raw.raw_label,
                    value=raw.value,
                    unit=raw.unit,
                    scope=raw.scope,
                    recurrence=raw.recurrence,
                    # The persistence boundary derives the speaker from the
                    # uniquely matched verbatim source segment.
                    speaker=None,
                    source_excerpt=raw.source_excerpt,
                )
            )
        except (ValidationError, InvalidOperation) as e:
            log.warning("Dropping novel management indicator due to validation error: %s", e)
    return TranscriptExtractionManifest(commitments=commitments, indicators=indicators)


def extract_for_transcript(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    llm_call: Callable[[str], str],
) -> TranscriptExtractionManifest:
    """Orchestrator: pull transcript, build prompt, call LLM, parse, return manifest.

    `llm_call` is injected so tests can stub the LLM. Production callers pass
    a governed wrapper around ``call_llm(..., purpose="saydo_commitment_extract",
    ticker=...)`` — never the raw private CLI helper, which bypasses the
    purpose ledger, budgets and model routing.

    Behavior notes:
      - Empty KPI catalog still scans: it cannot yield a catalog-backed
        commitment, but can yield a reviewable novel management indicator.
      - Unusable LLM response ⇒ ONE retry with explicit feedback, then
        CommitmentParseError. Callers must not record a scan for a transcript
        that raised."""
    transcripts = selected_transcripts_relation(conn)
    cur = conn.execute(
        f"SELECT t.ticker FROM {transcripts} t WHERE t.id = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (transcript_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"transcript_id={transcript_id} not found")
    ticker = row["ticker"]

    transcript_data = fetch_transcript_text_and_segment(conn, transcript_id)
    if transcript_data is None:
        raise ValueError(f"transcript_id={transcript_id} has no transcript_segments rows")
    text, segment_id, period_end = transcript_data
    source_doc_id, speaker = _segment_source_metadata(conn, segment_id)
    catalog = fetch_kpi_catalog(conn, ticker)

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
        source_doc_id=source_doc_id,
        speaker=speaker,
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
