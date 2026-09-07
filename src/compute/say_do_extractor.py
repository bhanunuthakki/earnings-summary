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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from compute.management_indicators import (
    IndicatorRecurrence,
    IndicatorScope,
    ManagementIndicatorInput,
    validate_indicator_source_binding,
)
from compute.say_do import CommitmentExtractionManifest, CommitmentInput
from compute.thesis_evaluator import Comparator
from models.facts import Unit
from pipeline.commitment_scan_receipts import (
    CommitmentScanCoverageState,
    ObservedTranscriptSegment,
    TranscriptScanBinding,
    TranscriptSegmentVersion,
    append_commitment_scan_receipt,
    commitment_scan_coverage,
    observe_transcript_segment,
    scan_receipt_schema_available,
)
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

    model_config = ConfigDict(extra="forbid")

    kpi_name: str = Field(min_length=1, max_length=200)
    comparator: Comparator
    target_value: Decimal
    unit: Unit
    period_target: datetime
    narrative: str = Field(min_length=1, max_length=1000)

    @field_validator("target_value", mode="before")
    @classmethod
    def _reject_boolean_target(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("target_value must be numeric, not boolean")
        return value


class _LLMManagementIndicator(BaseModel):
    """Novel source measurement that must remain outside the KPI catalog."""

    model_config = ConfigDict(extra="forbid")

    raw_label: str = Field(min_length=1, max_length=256)
    value: Decimal
    unit: Unit
    scope: IndicatorScope = IndicatorScope.UNSPECIFIED
    recurrence: IndicatorRecurrence = IndicatorRecurrence.UNKNOWN
    source_excerpt: str = Field(min_length=1, max_length=2000)

    @field_validator("value", mode="before")
    @classmethod
    def _reject_boolean_value(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("value must be numeric, not boolean")
        return value


class _LLMResponse(BaseModel):
    """Top-level shape we expect the model to return."""

    model_config = ConfigDict(extra="forbid")

    commitments: list[_LLMCommitment]
    novel_indicators: list[_LLMManagementIndicator]


class TranscriptExtractionManifest(CommitmentExtractionManifest):
    """Commitments plus unpromoted, source-bound novel indicators."""

    indicators: list[ManagementIndicatorInput] = Field(
        default_factory=list[ManagementIndicatorInput]
    )


class TranscriptScanResult(TranscriptExtractionManifest):
    """Complete multi-segment result with extraction-produced coverage evidence."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    observed_segments: tuple[ObservedTranscriptSegment, ...]


@dataclass(frozen=True)
class _TranscriptSegmentInput:
    text: str
    source: TranscriptSegmentVersion


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
    """Return the longest segment for compatibility with legacy callers."""
    segments = fetch_transcript_segments(conn, transcript_id)
    return max(segments, key=lambda item: len(item[0])) if segments else None


def fetch_transcript_segments(
    conn: sqlite3.Connection, transcript_id: int
) -> list[tuple[str, int, datetime]]:
    """Return every selected transcript segment in deterministic source order."""
    return [
        (
            item.text,
            item.source.segment_id,
            datetime.fromisoformat(item.source.period_end),
        )
        for item in _fetch_transcript_segment_inputs(conn, transcript_id)
    ]


def _fetch_transcript_segment_inputs(
    conn: sqlite3.Connection, transcript_id: int
) -> tuple[_TranscriptSegmentInput, ...]:
    transcripts = selected_transcripts_relation(conn).sql
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(transcript_segments)")}
    speaker = "ts.speaker" if "speaker" in columns else "NULL"
    time_start = "ts.time_code_start" if "time_code_start" in columns else "NULL"
    time_end = "ts.time_code_end" if "time_code_end" in columns else "NULL"
    rows = conn.execute(
        "SELECT t.id,t.document_id,t.period_end,ts.id,ts.seq,"
        + speaker
        + ","
        + time_start
        + ","
        + time_end
        + ",ts.text "
        f"FROM {transcripts} AS t JOIN transcript_segments AS ts ON ts.transcript_id=t.id "  # nosec B608
        "WHERE t.id=? ORDER BY ts.seq,ts.id",
        (transcript_id,),
    ).fetchall()
    return tuple(
        _TranscriptSegmentInput(
            text=str(row[8]),
            source=observe_transcript_segment(
                transcript_id=int(row[0]),
                source_document_id=int(row[1]),
                period_end=row[2],
                segment_id=int(row[3]),
                sequence=int(row[4]),
                speaker=row[5],
                time_code_start=row[6],
                time_code_end=row[7],
                text=str(row[8]),
            ),
        )
        for row in rows
    )


def _segment_source_metadata(conn: sqlite3.Connection, segment_id: int) -> tuple[int, str | None]:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(transcript_segments)")}
    speaker = "ts.speaker" if "speaker" in columns else "NULL"
    row = conn.execute(
        "SELECT tr.document_id, " + speaker + " "  # nosec B608
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


def scan_log_schema_available(conn: sqlite3.Connection) -> bool:
    """Return whether the full completion-marker schema is writable."""

    if not scan_log_available(conn):
        return False
    table_info = conn.execute("PRAGMA table_info(commitment_scan_log)").fetchall()
    columns = {str(row[1]) for row in table_info}
    if not {"id", "transcript_id", "scanned_at", "n_extracted", "prompt_version"} <= columns:
        return False
    unique_targets = {
        tuple(
            str(column[0])
            for column in conn.execute("SELECT name FROM pragma_index_info(?)", (str(index[1]),))
        )
        for index in conn.execute("PRAGMA index_list(commitment_scan_log)")
        if bool(index[2]) and not bool(index[4])
    }
    return ("transcript_id",) in unique_targets


def transcripts_pending_extraction(
    conn: sqlite3.Connection, ticker: str | None = None
) -> list[tuple[int, str, datetime]]:
    """Return pending transcripts, using typed coverage on the active schema.

    The historical output/log heuristic remains only as read compatibility for
    pre-receipt schemas. Current automatic writers share the immutable coverage
    classifier through ``transcripts_without_scan_receipt``.
    """
    if scan_receipt_schema_available(conn):
        return transcripts_without_scan_receipt(conn, ticker=ticker)
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


def transcripts_without_scan_receipt(
    conn: sqlite3.Connection, ticker: str | None = None
) -> list[tuple[int, str, datetime]]:
    """Return current transcripts lacking a valid receipt for today's prompt."""

    transcripts = selected_transcripts_relation(conn)
    sql = f"SELECT t.id,t.ticker,t.period_end FROM {transcripts} t WHERE 1=1"  # nosec B608
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " AND UPPER(t.ticker)=?"
        params = (ticker.upper(),)
    sql += " ORDER BY t.ticker,t.period_end DESC"
    rows = conn.execute(sql, params).fetchall()
    receipts_available = scan_receipt_schema_available(conn)
    if receipts_available:
        from llm.prompt_versions import prompt_version_for

        current_prompt_version = prompt_version_for("saydo_commitment_extract")
    else:
        current_prompt_version = ""
    pending: list[tuple[int, str, datetime]] = []
    for row in rows:
        transcript_id = int(row["id"])
        if receipts_available:
            coverage = commitment_scan_coverage(
                conn,
                transcript_id=transcript_id,
                prompt_version=current_prompt_version,
            )
            already_scanned = coverage.state not in {
                CommitmentScanCoverageState.SOURCE_CHANGED_MISSING,
                CommitmentScanCoverageState.NEVER_SCANNED_MISSING,
            }
        elif scan_log_available(conn):
            already_scanned = (
                conn.execute(
                    "SELECT 1 FROM commitment_scan_log WHERE transcript_id=?",
                    (transcript_id,),
                ).fetchone()
                is not None
            )
        else:
            already_scanned = False
        if already_scanned:
            continue
        period_end = row["period_end"]
        pending.append(
            (
                transcript_id,
                str(row["ticker"]),
                datetime.fromisoformat(period_end) if isinstance(period_end, str) else period_end,
            )
        )
    return pending


def record_scan(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    n_extracted: int,
    prompt_version: str | None = None,
    commitment_ids: Sequence[int] = (),
    management_indicator_ids: Sequence[int] = (),
    observed_segments: Sequence[ObservedTranscriptSegment] | None = None,
    expected_binding: TranscriptScanBinding | None = None,
) -> None:
    """Atomically persist the mutable scan index and exact immutable receipt.

    Current writers require both active schemas and typed segment observations;
    legacy databases remain readable but cannot manufacture new completion.
    """
    if not scan_log_schema_available(conn):
        raise RuntimeError("commitment scan log schema is unavailable")
    if not scan_receipt_schema_available(conn):
        raise RuntimeError("commitment scan segment manifest schema is unavailable")
    if prompt_version is None:
        raise ValueError("prompt_version is required for an immutable scan receipt")
    if n_extracted != len(commitment_ids) + len(management_indicator_ids):
        raise ValueError("n_extracted does not match the exact scan output identities")
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
    append_commitment_scan_receipt(
        conn,
        transcript_id=transcript_id,
        prompt_version=prompt_version,
        commitment_ids=commitment_ids,
        management_indicator_ids=management_indicator_ids,
        observed_segments=observed_segments,
        expected_binding=expected_binding,
    )


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
    - Rejects the entire segment response when any output is invalid. Partial
      parsing cannot be represented as successful segment coverage.
    """
    cleaned = _FENCE_RX.sub("", json_text).strip()

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload_object: dict[str, object] = {}
        for key, value in pairs:
            if key in payload_object:
                raise ValueError(f"duplicate JSON key: {key}")
            payload_object[key] = value
        return payload_object

    try:
        payload = json.loads(cleaned, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as e:
        raise CommitmentParseError(f"LLM response is not valid JSON: {e}") from e
    except ValueError as e:
        raise CommitmentParseError(f"LLM response is not valid JSON: {e}") from e

    try:
        response = _LLMResponse.model_validate(payload)
    except ValidationError as e:
        raise CommitmentParseError(f"LLM response failed schema validation: {e}") from e

    if response.novel_indicators and context.source_doc_id is None:
        raise CommitmentParseError("novel indicators require a transcript source document")
    try:
        commitments = [
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
            for raw in response.commitments
        ]
        indicators = [
            ManagementIndicatorInput(
                ticker=context.ticker.upper(),
                transcript_segment_id=context.transcript_segment_id,
                raw_label=raw.raw_label,
                value=raw.value,
                unit=raw.unit,
                scope=raw.scope,
                recurrence=raw.recurrence,
                # The persistence boundary derives the speaker from this
                # exact anchor (or a unique segment for legacy callers).
                speaker=None,
                source_excerpt=raw.source_excerpt,
            )
            for raw in response.novel_indicators
        ]
    except ValidationError as exc:
        raise CommitmentParseError(
            f"LLM response failed promoted output validation: {exc}"
        ) from exc
    return TranscriptExtractionManifest(commitments=commitments, indicators=indicators)


def extract_for_transcript(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    llm_call: Callable[[str], str],
) -> TranscriptScanResult:
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

    transcript_segments = _fetch_transcript_segment_inputs(conn, transcript_id)
    if not transcript_segments:
        raise ValueError(f"transcript_id={transcript_id} has no transcript_segments rows")
    oversized = next(
        (item for item in transcript_segments if len(item.text) > MAX_TRANSCRIPT_CHARS), None
    )
    if oversized is not None:
        raise CommitmentParseError(
            f"transcript_id={transcript_id} segment_id={oversized.source.segment_id} exceeds "
            f"the {MAX_TRANSCRIPT_CHARS:,}-character complete-coverage limit"
        )
    catalog = fetch_kpi_catalog(conn, ticker)
    commitments: list[CommitmentInput] = []
    indicators: list[ManagementIndicatorInput] = []
    observed_segments: list[ObservedTranscriptSegment] = []
    for segment in transcript_segments:
        text = segment.text
        source = segment.source
        segment_id = source.segment_id
        period_end = datetime.fromisoformat(source.period_end)
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
            source_doc_id=source.source_document_id,
            speaker=source.speaker,
        )

        def parse_and_validate(
            response: str, *, segment_context: TranscriptContext = context
        ) -> TranscriptExtractionManifest:
            manifest = parse_llm_response(response, context=segment_context)
            try:
                for indicator in manifest.indicators:
                    validate_indicator_source_binding(conn, indicator=indicator)
            except ValueError as exc:
                raise CommitmentParseError(
                    f"novel indicator source evidence failed exact segment binding: {exc}"
                ) from exc
            return manifest

        response_text = llm_call(prompt)
        try:
            manifest = parse_and_validate(response_text)
        except CommitmentParseError as first_exc:
            validation_error = str(first_exc)
            log.warning(
                "transcript_id=%d segment_id=%d ticker=%s: unusable LLM response, retrying with feedback: %s",
                transcript_id,
                segment_id,
                ticker,
                validation_error,
            )
            retry_text = llm_call(
                _RETRY_PREAMBLE
                + f"VALIDATION ERROR: {validation_error}\n"
                + "Copy each novel indicator source_excerpt exactly from the transcript.\n\n"
                + prompt
            )
            manifest = parse_and_validate(retry_text)
        commitments.extend(manifest.commitments)
        indicators.extend(manifest.indicators)
        commitment_count = len(manifest.commitments)
        indicator_count = len(manifest.indicators)
        observed_segments.append(
            ObservedTranscriptSegment(
                source=source,
                disposition=(
                    "parsed_no_output"
                    if commitment_count + indicator_count == 0
                    else "parsed_with_output"
                ),
                commitment_count=commitment_count,
                indicator_count=indicator_count,
            )
        )
    return TranscriptScanResult(
        commitments=commitments,
        indicators=indicators,
        observed_segments=tuple(observed_segments),
    )
