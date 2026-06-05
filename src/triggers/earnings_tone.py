"""Earnings-tone sensor — full implementation (PR-N8).

Fires after a new earnings transcript lands locally. The sensor compares
the new transcript against the prior 4 quarters via a single LLM call,
producing a structured shift list that the morning driver (PR-N9) lifts
into alerts + queued actions.

Two halves landed in two PRs so the cheap arrival-detection half could
review in isolation from the expensive LLM diff pass:

  * **PR-N8a** — real ``scan()`` over ``transcripts`` × ``documents``,
    feature-flagged-off ``should_fire()``, raising
    ``build_alert`` / ``draft_actions``.

  * **PR-N8 (this PR)** — flips ``_FEATURE_ENABLED``, implements
    ``build_alert`` via the Jinja2 prompt template + ``call_llm``
    + the artifact-store cache, and implements ``draft_actions``
    that maps each LLM-flagged shift into one or more
    :class:`QueuedActionDraft` rows (``earnings_prep_append`` for every
    shift; ``thesis_update`` for high-confidence directional shifts;
    ``bear_append`` for softer / dropped-topic shifts that touch a
    registered Tier-1 KPI).

"Landed locally" is keyed off ``documents.fetched_at`` rather than
``transcripts.call_date``: ``call_date`` is the day of the call;
``fetched_at`` is the day we ingested it. The user could be away from
the desk on the day of the call — they want an alert when *we have* the
transcript, not when the company said the words.

Failure mode: ``scan()`` is best-effort (missing tables / sqlite errors
→ ``[]``). ``build_alert`` fails loudly — it is on the primary-write
path and a silent skip would leave the alert un-emitted with no audit
trail. The morning driver catches ``EarningsToneLLMError`` per candidate
so one stuck ticker doesn't break the fan-out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

import jinja2

from alerts.store import compute_signature_sha
from llm.anchors import (
    compose_anchor_block,
    load_bear_anchor,
    load_ir_anchor,
    load_thesis_anchor,
)
from llm.prompt_versions import prompt_version_for
from llm.style import NUMBER_FORMATTING_BLOCK
from llm_artifact_store import (
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import JSON_FENCE_RE, call_llm
from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)

log = logging.getLogger(__name__)

# PR-N8 flipped this on after wiring the LLM diff pass below. The
# morning driver still needs to honor it (read-only consumers can
# always check the flag), but it now also expresses "the trigger is
# fully implemented" — not just "the slice is gated".
_FEATURE_ENABLED = True

# Detection window: transcripts whose source document was fetched in
# the last 24 hours count as "just landed". The morning driver runs
# daily, so a 24h window catches one quarter's worth of new transcripts
# without re-firing on yesterday's batch.
_ARRIVAL_WINDOW = timedelta(hours=24)

# How many prior-quarter transcripts to feed the LLM for the tone diff.
# The template (``_prompts/earnings_tone_diff.txt``) hard-codes the "prior 4"
# framing in the task instructions; keep this in sync.
_PRIOR_TRANSCRIPT_LIMIT = 4

# Artifact-store purpose key. Must match the entry in
# ``llm_artifact_store.FACT_DEPENDENT_PURPOSES`` so a fact-side
# restatement marks the diff dirty via the existing mark-dirty chain.
_ARTIFACT_PURPOSE = "earnings_tone_diff"

# Prompt-template path; loaded once at module import. The template
# itself constrains the LLM to JSON-only output and lays out the
# {summary, shifts[], no_material_shifts_detected} shape.
_PROMPT_TEMPLATE_PATH = Path(__file__).parent / "_prompts" / "earnings_tone_diff.txt"

# Prompt-version key for the cache + the calibration A/B dimension, sourced from
# the central registry (the single bump-point). Bump the entry there when the
# template body changes materially (re-using ``compute_input_sha256`` would be
# too fragile — minor whitespace tweaks would invalidate the cache).
_PROMPT_VERSION = prompt_version_for(_ARTIFACT_PURPOSE)

# Direction values that warrant promoting a shift to a ``thesis_update``
# action. Pure ``ambiguous``-typed shifts don't pass the bar — they go
# only into ``earnings_prep_append`` so the user revisits next call.
_DIRECTIONS_WARRANTING_THESIS_UPDATE: frozenset[str] = frozenset(
    {"softer", "firmer", "new_topic", "dropped_topic"}
)

# Direction values that warrant appending the shift to the bear case,
# IFF the shift names a registered Tier-1 KPI. The intuition: only
# *adverse* tonal moves (softer guidance, a topic management dropped)
# qualify as bear-case ammunition; a firmer / new positive topic is
# upside, not downside.
_DIRECTIONS_WARRANTING_BEAR_APPEND: frozenset[str] = frozenset({"softer", "dropped_topic"})

# Confidence floor for promoting a shift to ``thesis_update``. Below
# this, the shift is recorded only as ``earnings_prep_append`` so the
# user re-evaluates next call rather than committing it to the ledger.
_THESIS_UPDATE_CONFIDENCE_FLOOR = 0.6

# Retry hint prepended to the user prompt on a second attempt when the
# first LLM response failed to parse as JSON. call_llm has no separate
# system-prompt channel, so the instruction rides on the user prompt.
_JSON_RETRY_PREAMBLE = (
    "IMPORTANT: Your previous response was not valid JSON. "
    "Return only the JSON object specified at the end of this prompt. "
    "No markdown fences. No commentary. No prefatory prose.\n\n"
)


class EarningsToneLLMError(RuntimeError):
    """Raised when the earnings-tone LLM diff pass cannot be completed.

    Surfaces in two cases: (a) the LLM returned non-JSON on both the
    first attempt AND the retry; (b) the parsed JSON is structurally
    wrong (missing ``summary`` / ``shifts``). The morning driver in
    PR-N9 catches this per candidate so one stuck ticker doesn't break
    the fan-out across the watchlist.
    """


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """True iff ``table`` exists. Returns False on any sqlite error."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _format_threshold(threshold: datetime) -> str:
    """Format a datetime for sqlite TEXT-stored DATETIME comparison.

    The repo's ``documents.fetched_at`` lands as the default sqlalchemy
    representation — ``'YYYY-MM-DD HH:MM:SS[.ffffff]'`` — so string
    compare against the same shape is correct.
    """
    return threshold.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Transcript text loaders
# --------------------------------------------------------------------------

# In-file boundary markers used to split a reconstructed transcript into
# (prepared_remarks, qa) text. Mirrors the regex patterns in
# ``report/sections/earnings.py`` so the trigger's split agrees with the
# section renderer's view of the same transcript.
_CALLSTREET_QA_HEADER = re.compile(r"\n\s*QUESTION\s+AND\s+ANSWER\s+SECTION\b", re.IGNORECASE)
_OPERATOR_FIRST_QUESTION = re.compile(
    r"first\s+question\s+(?:is\s+from|comes\s+from|will\s+come\s+from)",
    re.IGNORECASE,
)


def _reconstruct_transcript_text(conn: sqlite3.Connection, transcript_id: int) -> str:
    """Reassemble the full transcript text from ``transcript_segments``.

    Joins segments in ``seq`` order with one speaker-header line per
    turn so the operator's "first question is from X" boundary marker
    survives in the reconstructed body. Returns "" when the transcript
    has no segments (legacy ingest without speaker parsing).
    """
    try:
        rows = conn.execute(
            "SELECT speaker, text FROM transcript_segments "
            + "WHERE transcript_id = ? ORDER BY seq",
            (transcript_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "earnings_tone_segment_load_failed",
                "transcript_id": transcript_id,
                "error": str(exc),
            }
        )
        return ""
    parts: list[str] = []
    for speaker_raw, text_raw in rows:
        speaker = str(speaker_raw) if speaker_raw is not None else "Speaker"
        text = str(text_raw) if text_raw is not None else ""
        if not text.strip():
            continue
        parts.append(f"\n{speaker}:\n{text.strip()}\n")
    return "".join(parts)


def _split_prepared_qa(text: str, has_qa_section: bool | None) -> tuple[str, str]:
    """Split a reconstructed transcript into (prepared_remarks, qa).

    Detection ladder:
      1. CallStreet ``QUESTION AND ANSWER SECTION`` header (rare in
         reconstructed text — these are typically dropped during
         ingest, but match if present).
      2. Operator "first question comes from X" phrase (the most
         reliable boundary marker in reconstructed segment text).
      3. Fall back to ``has_qa_section``: True → entire body is Q&A
         (no detectable split point); False / None → entire body is
         prepared remarks.

    Both return values are non-None strings; either may be "".
    """
    if not text.strip():
        return "", ""

    cs_match = _CALLSTREET_QA_HEADER.search(text)
    if cs_match is not None:
        return text[: cs_match.start()].strip(), text[cs_match.end() :].strip()

    op_match = _OPERATOR_FIRST_QUESTION.search(text)
    if op_match is not None:
        return text[: op_match.start()].strip(), text[op_match.start() :].strip()

    if has_qa_section is True:
        return "", text.strip()
    return text.strip(), ""


def _load_transcript_text(conn: sqlite3.Connection, transcript_id: int) -> tuple[str, str]:
    """Return ``(prepared_remarks, qa)`` for the given transcript.

    Empty strings when the transcript has no segments. The ``has_qa_section``
    tri-state (migration 0019) drives the fallback when no in-body
    boundary marker is detectable.
    """
    text = _reconstruct_transcript_text(conn, transcript_id)
    if not text.strip():
        return "", ""

    has_qa: bool | None = None
    try:
        row = conn.execute(
            "SELECT has_qa_section FROM transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row[0] is not None:
        has_qa = bool(row[0])
    return _split_prepared_qa(text, has_qa)


def _load_prior_transcripts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    current_period_end: str,
    limit: int = _PRIOR_TRANSCRIPT_LIMIT,
) -> list[dict[str, object]]:
    """Load up to ``limit`` quarterly transcripts strictly prior to ``current_period_end``.

    Returns one dict per prior transcript with fields the prompt template
    expects: ``transcript_id``, ``fiscal_period_type``, ``fiscal_period``,
    ``prepared_remarks``, ``qa``. Ordered oldest-first to match the
    template's "chronological, oldest first" framing.

    Filters to ``fiscal_period_type IN ('Q1','Q2','Q3','Q4')`` — earnings
    transcripts are quarterly artifacts; H1 / FY entries (if any sneak in)
    would muddy the tone comparison.
    """
    try:
        rows = conn.execute(
            "SELECT id, fiscal_period_type, period_end FROM transcripts "
            + "WHERE ticker = ? "
            + "AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
            + "AND period_end < ? "
            + "ORDER BY period_end DESC LIMIT ?",
            (ticker, current_period_end, int(limit)),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "earnings_tone_prior_load_failed",
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return []

    out: list[dict[str, object]] = []
    for row in rows:
        transcript_id_raw, fpt_raw, pe_raw = row[0], row[1], row[2]
        if (
            not isinstance(transcript_id_raw, int)
            or not isinstance(fpt_raw, str)
            or not isinstance(pe_raw, str)
        ):
            continue
        prepared, qa = _load_transcript_text(conn, transcript_id_raw)
        out.append(
            {
                "transcript_id": transcript_id_raw,
                "fiscal_period_type": fpt_raw,
                "fiscal_period": pe_raw[:4],
                "prepared_remarks": prepared,
                "qa": qa,
            }
        )
    # Oldest first — template instructs "chronological per quarter, oldest first".
    out.reverse()
    return out


# --------------------------------------------------------------------------
# Prompt rendering + LLM call
# --------------------------------------------------------------------------


def _load_prompt_template() -> jinja2.Template:
    """Load and parse the diff template. Module-level cached via lru-cache
    semantics would help on repeated calls, but the morning driver invokes
    the trigger once per ticker per day — the saving doesn't pay back the
    indirection. Re-load is also robust to in-test edits."""
    env = jinja2.Environment(autoescape=False, keep_trailing_newline=True)
    return env.from_string(_PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8"))


def _format_period_label(fiscal_period_type: str, fiscal_period: str) -> str:
    """Render a period as ``"Q1-2026"`` (matches the template's citation example)."""
    return f"{fiscal_period_type}-{fiscal_period}"


def _render_prompt(
    *,
    ticker: str,
    fiscal_period_type: str,
    fiscal_period: str,
    thesis_anchor_block: str,
    current_prepared_remarks: str,
    current_qa: str,
    prior_transcripts: list[dict[str, object]],
) -> str:
    """Render the diff-prompt body with the supplied context."""
    template = _load_prompt_template()
    prior_periods = [
        _format_period_label(cast("str", p["fiscal_period_type"]), cast("str", p["fiscal_period"]))
        for p in prior_transcripts
    ]
    return template.render(
        ticker=ticker,
        fiscal_period_type=fiscal_period_type,
        fiscal_period=fiscal_period,
        prior_periods=prior_periods,
        thesis_anchor_block=thesis_anchor_block or "(no thesis anchor on file)",
        number_formatting_block=NUMBER_FORMATTING_BLOCK,
        current_prepared_remarks=current_prepared_remarks,
        current_qa=current_qa,
        prior_transcripts=prior_transcripts,
    )


def _sha256_text(text: str) -> str:
    """Deterministic sha256 over UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_cache_inputs(
    *,
    ticker: str,
    current_transcript_id: int,
    prior_transcript_ids: list[int],
    anchor_text: str,
    template_text: str,
) -> list[bytes | str]:
    """Compose the deterministic cache-input list for ``compute_input_sha256``.

    Order is part of the contract — changing it invalidates every cached
    diff. Each input is a single string; the helper inside the store
    length-prefixes them so two adjacent strings can't collide via
    boundary trickery.
    """
    return [
        f"ticker={ticker}",
        f"current={current_transcript_id}",
        "priors=" + ",".join(str(t) for t in sorted(prior_transcript_ids)),
        "anchor_sha=" + _sha256_text(anchor_text),
        "template_sha=" + _sha256_text(template_text),
    ]


def _strip_json_fence(raw: str) -> str:
    """Strip optional ``​```json ... ``​``` fences the LLM occasionally adds."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        return JSON_FENCE_RE.sub("", stripped).strip()
    return stripped


def _parse_diff_response(raw: str) -> dict[str, object]:
    """Parse + structurally validate the LLM JSON response.

    Returns a dict with keys ``summary`` (str), ``shifts`` (list), and
    ``no_material_shifts_detected`` (bool). Raises ``ValueError`` on any
    parse / shape failure — the caller's retry policy decides whether to
    re-prompt or surface.
    """
    payload = json.loads(_strip_json_fence(raw))
    if not isinstance(payload, dict):
        raise ValueError(
            f"earnings_tone_diff response is not a JSON object: got {type(payload).__name__}"
        )
    obj = cast("dict[str, object]", payload)
    summary = obj.get("summary")
    shifts = obj.get("shifts")
    flag = obj.get("no_material_shifts_detected")
    if not isinstance(summary, str):
        raise ValueError("earnings_tone_diff response missing 'summary' string")
    if not isinstance(shifts, list):
        raise ValueError("earnings_tone_diff response missing 'shifts' list")
    if not isinstance(flag, bool):
        raise ValueError(
            "earnings_tone_diff response missing 'no_material_shifts_detected' boolean"
        )
    return {
        "summary": summary,
        "shifts": cast("list[object]", shifts),
        "no_material_shifts_detected": flag,
    }


def _call_llm_with_retry(prompt: str, *, ticker: str) -> dict[str, object]:
    """Call the LLM and parse the response. Retries once on parse failure.

    A clean JSON-shape failure (parse OK, but missing required keys) also
    counts as a "bad response" and triggers the retry; the morning driver
    catches the final ``EarningsToneLLMError`` if both attempts fail.
    """
    try:
        raw_first = call_llm(prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker)
    except Exception as exc:
        raise EarningsToneLLMError(
            f"earnings_tone_diff LLM call failed (initial attempt): {exc}"
        ) from exc
    try:
        return _parse_diff_response(raw_first)
    except ValueError as first_exc:
        log.warning(
            {
                "event": "earnings_tone_diff_parse_failed_retrying",
                "ticker": ticker,
                "error": str(first_exc),
            }
        )

    retry_prompt = _JSON_RETRY_PREAMBLE + prompt
    try:
        raw_retry = call_llm(retry_prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker)
    except Exception as exc:
        raise EarningsToneLLMError(
            f"earnings_tone_diff LLM call failed (retry attempt): {exc}"
        ) from exc
    try:
        return _parse_diff_response(raw_retry)
    except ValueError as retry_exc:
        raise EarningsToneLLMError(
            f"earnings_tone_diff LLM returned malformed JSON twice: {retry_exc}"
        ) from retry_exc


# --------------------------------------------------------------------------
# Action drafters
# --------------------------------------------------------------------------


def _shift_topic(shift: dict[str, object]) -> str:
    """Read a non-empty ``topic`` out of a shift dict, or "" on miss."""
    topic = shift.get("topic")
    return topic.strip() if isinstance(topic, str) else ""


def _shift_direction(shift: dict[str, object]) -> str:
    """Read the direction string out of a shift dict, or "" on miss."""
    direction = shift.get("direction")
    return direction.strip() if isinstance(direction, str) else ""


def _shift_confidence(shift: dict[str, object]) -> float:
    """Read confidence as a float in [0, 1]. Returns 0.0 on miss / non-numeric."""
    confidence = shift.get("confidence")
    if isinstance(confidence, bool):  # bool is an int subclass; reject explicitly
        return 0.0
    if isinstance(confidence, (int, float)):
        return float(confidence)
    return 0.0


def _shift_related_kpi(shift: dict[str, object]) -> str | None:
    """Read ``related_thesis_kpi`` if non-empty / non-null, else None."""
    kpi = shift.get("related_thesis_kpi")
    return kpi.strip() if isinstance(kpi, str) and kpi.strip() else None


def _draft_actions_for_shift(
    shift: dict[str, object], memo_text: str | None
) -> list[QueuedActionDraft]:
    """Map one LLM-emitted shift into ``QueuedActionDraft`` rows.

    Always emits an ``earnings_prep_append``; conditionally emits a
    ``thesis_update`` (directional + confident enough) and a
    ``bear_append`` (adverse + touches a registered Tier-1 KPI).
    """
    topic = _shift_topic(shift)
    direction = _shift_direction(shift)
    if not topic or not direction:
        # Malformed shift — skip rather than queue a useless action. The
        # outer parse already validated overall shape; a per-shift miss
        # here is degraded-quality output, not a hard error.
        return []

    out: list[QueuedActionDraft] = [
        QueuedActionDraft(
            action_kind="earnings_prep_append",
            payload={
                "body": f"Re-check: {topic} (direction: {direction})",
                "source_shift_topic": topic,
            },
        )
    ]

    confidence = _shift_confidence(shift)
    if (
        direction in _DIRECTIONS_WARRANTING_THESIS_UPDATE
        and confidence >= _THESIS_UPDATE_CONFIDENCE_FLOOR
    ):
        out.append(
            QueuedActionDraft(
                action_kind="thesis_update",
                payload={
                    "body": f"{topic} — {memo_text or ''}".strip(" —"),
                    "source_shift_topic": topic,
                    "confidence": confidence,
                },
            )
        )

    related_kpi = _shift_related_kpi(shift)
    if direction in _DIRECTIONS_WARRANTING_BEAR_APPEND and related_kpi is not None:
        out.append(
            QueuedActionDraft(
                action_kind="bear_append",
                payload={
                    "body": f"Material tone shift on {related_kpi}: {topic}",
                    "source_shift_topic": topic,
                    "related_kpi": related_kpi,
                },
            )
        )
    return out


# --------------------------------------------------------------------------
# Path helpers — repo root resolution
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    """Resolve the project root from this module's location.

    ``src/triggers/earnings_tone.py`` → parents[2] = project root.
    Used to address ``micro_thesis/holdings/<TICKER>.json`` (for the
    thesis anchor) and ``data/portfolio.db`` (transitively, by
    ``load_thesis_anchor``'s statistical-patterns block).
    """
    return Path(__file__).resolve().parents[2]


def _db_path() -> Path:
    """Resolve the portfolio DB path. Used by ``build_alert`` to open
    the connection it needs for transcript loading + artifact-store
    caching (the Protocol does not pass a connection in)."""
    from db import DB_PATH

    return Path(DB_PATH)


# --------------------------------------------------------------------------
# Sensor implementation
# --------------------------------------------------------------------------


class EarningsToneTrigger:
    """``Cadence.ON_EARNINGS`` sensor over the ``transcripts`` table."""

    kind: ClassVar[str] = "earnings_tone"
    cadence: ClassVar[Cadence] = Cadence.ON_EARNINGS

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]:
        """Emit one candidate per fresh transcript arrival in the last 24h.

        Joins ``transcripts`` to ``documents`` so the freshness gate is
        ``documents.fetched_at`` (when we ingested it), not
        ``transcripts.call_date`` (which is often NULL at ingest and
        reflects the call itself, not arrival).

        Returns ``[]`` when either table is missing, the query errors, or
        no transcript arrived in window.
        """
        if not _has_table(db, "transcripts") or not _has_table(db, "documents"):
            log.debug(
                {
                    "event": "earnings_tone_scan_skipped",
                    "ticker": ticker,
                    "reason": "transcripts_or_documents_table_missing",
                }
            )
            return []

        now = datetime.now(UTC).replace(tzinfo=None)
        threshold = _format_threshold(now - _ARRIVAL_WINDOW)
        try:
            row = db.execute(
                "SELECT t.id, t.fiscal_period_type, t.period_end, d.fetched_at"
                + " FROM transcripts t"
                + " JOIN documents d ON d.id = t.document_id"
                + " WHERE t.ticker = ? AND d.fetched_at >= ?"
                + " ORDER BY d.fetched_at DESC"
                + " LIMIT 1",
                (ticker, threshold),
            ).fetchone()
        except sqlite3.Error as exc:
            log.debug(
                {
                    "event": "earnings_tone_scan_query_failed",
                    "ticker": ticker,
                    "error": str(exc),
                }
            )
            return []

        if row is None:
            return []

        transcript_id_raw, fiscal_period_type_raw, period_end_raw, fetched_at_raw = (
            row[0],
            row[1],
            row[2],
            row[3],
        )
        if (
            not isinstance(transcript_id_raw, int)
            or not isinstance(fiscal_period_type_raw, str)
            or not isinstance(period_end_raw, str)
            or not isinstance(fetched_at_raw, str)
        ):
            log.debug(
                {
                    "event": "earnings_tone_scan_row_malformed",
                    "ticker": ticker,
                }
            )
            return []

        fiscal_period = period_end_raw[:4]  # calendar year of period end
        key = f"{ticker}:{fiscal_period_type_raw}:{fiscal_period}"
        candidate = TriggerCandidate(
            ticker=ticker,
            kind=self.kind,
            key=key,
            evidence={
                "transcript_id": transcript_id_raw,
                "fiscal_period": fiscal_period,
                "fiscal_period_type": fiscal_period_type_raw,
                "published_at": fetched_at_raw,
                "period_end": period_end_raw,
            },
            computed_at=now,
        )
        return [candidate]

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool:
        """Gate firing on the feature flag + a truthy candidate.

        The morning driver in PR-N9 layers its own gates on top (dedup
        on ``signature_sha`` via ``find_by_signature``, dismissal-set
        membership) — this method speaks only to whether the sensor is
        wired and the candidate is non-empty.
        """
        _ = user_state
        return _FEATURE_ENABLED and bool(candidate)

    def signature_key_evidence(self, candidate: TriggerCandidate) -> Mapping[str, object]:
        """Key the dedup hash on the transcript id alone.

        Two scans for the same ticker against the same transcript MUST
        produce identical signatures; fiscal-period labels and the
        ``fetched_at`` timestamp derive from the same transcript and would
        only inflate the hash surface. ``build_alert`` keys on the exact
        same dict — see :meth:`build_alert`.
        """
        return {"transcript_id": candidate.evidence["transcript_id"]}

    def build_alert(
        self,
        candidate: TriggerCandidate,
        anchor: ThesisAnchor | None,
    ) -> AlertDraft:
        """Run the LLM diff pass and assemble the alert draft.

        Cache-aware: re-running the trigger against the same transcript
        (and the same prior 4) is free after the first call lands in
        ``llm_artifacts``. The cache invalidates when any of the input
        ids, the thesis anchor, or the template body changes.

        Raises :class:`EarningsToneLLMError` when the LLM diff cannot be
        produced after one retry. The morning driver catches per-candidate
        so one stuck ticker doesn't break the fan-out.
        """
        _ = anchor  # markdown anchor is loaded from disk below; this dataclass twin is reserved for future use
        ticker = candidate.ticker
        evidence = candidate.evidence
        current_transcript_id = cast("int", evidence["transcript_id"])
        fiscal_period_type = cast("str", evidence["fiscal_period_type"])
        fiscal_period = cast("str", evidence["fiscal_period"])
        current_period_end = cast("str", evidence.get("period_end") or "")

        repo_root = _repo_root()
        db_path = _db_path()

        # Open a local connection. Protocol doesn't pass one in; the
        # store-side helpers each open their own anyway, so we manage
        # this connection only for the transcript loading window.
        conn = sqlite3.connect(str(db_path))
        try:
            current_prepared, current_qa = _load_transcript_text(conn, current_transcript_id)
            prior_transcripts = _load_prior_transcripts(
                conn,
                ticker=ticker,
                current_period_end=current_period_end,
            )
        finally:
            conn.close()

        prior_transcript_ids = [cast("int", p["transcript_id"]) for p in prior_transcripts]

        # Compose the full 3-block anchor (thesis + bear case + IR narrative),
        # not just the thesis. A tone shift that confirms a named bear hypothesis
        # — or that contradicts management's own IR framing — is the
        # highest-value signal here, and the model can only flag it if the bear
        # case and the IR narrative are in front of it. compose_anchor_block
        # omits whichever blocks are absent for this ticker.
        thesis_anchor_block = compose_anchor_block(
            load_thesis_anchor(repo_root, ticker),
            load_bear_anchor(repo_root, ticker),
            load_ir_anchor(repo_root, ticker),
        )
        template_text = _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")

        cache_inputs = _build_cache_inputs(
            ticker=ticker,
            current_transcript_id=current_transcript_id,
            prior_transcript_ids=prior_transcript_ids,
            anchor_text=thesis_anchor_block,
            template_text=template_text,
        )

        parsed = self._read_cached_or_call_llm(
            ticker=ticker,
            fiscal_period_type=fiscal_period_type,
            fiscal_period=fiscal_period,
            thesis_anchor_block=thesis_anchor_block,
            current_prepared_remarks=current_prepared,
            current_qa=current_qa,
            prior_transcripts=prior_transcripts,
            cache_inputs=cache_inputs,
            db_path=db_path,
        )

        fired_at = datetime.now(UTC).replace(tzinfo=None)
        evidence_obj: dict[str, object] = {
            "summary": parsed["summary"],
            "shifts": parsed["shifts"],
            "no_material_shifts_detected": parsed["no_material_shifts_detected"],
            "transcript_id": current_transcript_id,
            "prior_transcript_ids": prior_transcript_ids,
        }
        evidence_json = json.dumps(evidence_obj, sort_keys=True, ensure_ascii=False, default=str)
        signature_sha = compute_signature_sha(
            self.kind, ticker, self.signature_key_evidence(candidate)
        )
        memo_text = cast("str", parsed["summary"])

        return AlertDraft(
            trigger_kind=self.kind,
            ticker=ticker,
            fired_at=fired_at,
            evidence_json=evidence_json,
            signature_sha=signature_sha,
            memo_text=memo_text,
        )

    def draft_actions(
        self,
        alert: AlertDraft,
        candidate: TriggerCandidate,
    ) -> list[QueuedActionDraft]:
        """Derive queued-action drafts from an alert's shift list.

        Returns ``[]`` when the LLM flagged no material shifts. Otherwise
        emits one ``earnings_prep_append`` per shift, plus a
        ``thesis_update`` for high-confidence directional shifts and a
        ``bear_append`` for adverse shifts touching a registered Tier-1
        KPI. See the module-level direction / confidence constants for
        the exact policy.
        """
        _ = candidate
        try:
            payload = json.loads(alert.evidence_json)
        except json.JSONDecodeError as exc:
            log.warning(
                {
                    "event": "earnings_tone_draft_actions_bad_evidence_json",
                    "ticker": alert.ticker,
                    "error": str(exc),
                }
            )
            return []
        if not isinstance(payload, dict):
            return []
        payload_dict = cast("dict[str, object]", payload)
        if payload_dict.get("no_material_shifts_detected") is True:
            return []
        shifts_raw = payload_dict.get("shifts")
        if not isinstance(shifts_raw, list):
            return []
        out: list[QueuedActionDraft] = []
        for entry in cast("Iterable[object]", shifts_raw):
            if not isinstance(entry, dict):
                continue
            out.extend(_draft_actions_for_shift(cast("dict[str, object]", entry), alert.memo_text))
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_cached_or_call_llm(
        self,
        *,
        ticker: str,
        fiscal_period_type: str,
        fiscal_period: str,
        thesis_anchor_block: str,
        current_prepared_remarks: str,
        current_qa: str,
        prior_transcripts: list[dict[str, object]],
        cache_inputs: list[bytes | str],
        db_path: Path,
    ) -> dict[str, object]:
        """Return the parsed diff response, hitting the artifact-store cache when possible.

        The cache key is computed via ``compute_input_sha256(_PROMPT_VERSION,
        cache_inputs)``. A hit short-circuits the LLM call. A miss runs
        the LLM, then writes the response back via ``upsert``.
        """
        existing = read_current(ticker=ticker, purpose=_ARTIFACT_PURPOSE, db_path=db_path)
        new_sha = compute_input_sha256(prompt_version=_PROMPT_VERSION, cache_inputs=cache_inputs)
        if (
            existing is not None
            and not existing.dirty
            and existing.input_sha256 == new_sha
            and isinstance(existing.content_json, dict)
        ):
            cached = cast("dict[str, object]", existing.content_json)
            log.info(
                {
                    "event": "earnings_tone_diff_cache_hit",
                    "ticker": ticker,
                    "artifact_id": existing.id,
                }
            )
            return cached

        prompt = _render_prompt(
            ticker=ticker,
            fiscal_period_type=fiscal_period_type,
            fiscal_period=fiscal_period,
            thesis_anchor_block=thesis_anchor_block,
            current_prepared_remarks=current_prepared_remarks,
            current_qa=current_qa,
            prior_transcripts=prior_transcripts,
        )
        parsed = _call_llm_with_retry(prompt, ticker=ticker)
        upsert(
            UpsertRequest(
                ticker=ticker,
                purpose=_ARTIFACT_PURPOSE,
                content_json=parsed,
                cache_inputs=cache_inputs,
                prompt_version=_PROMPT_VERSION,
            ),
            db_path=db_path,
        )
        return parsed
