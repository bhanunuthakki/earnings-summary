"""Material-news sensor — final slice (PR-N15), closing the 5-trigger taxonomy.

``Cadence.ON_NEWS`` sensor that asks, for each recent story on a holding:
*is this development material to the thesis?* That question is irreducibly an
LLM judgment — there is no deterministic "materiality" the way KPI inflection
has a deterministic changepoint. So this trigger is **LLM-dependent**, the same
shape as ``earnings_tone`` and the inverse of ``kpi_inflection`` /
``saydo_due`` (which fire their deterministic verdict even when the LLM is
down).

Two consequences fall out of that:

  * **Degrade, never fabricate.** If the LLM is unavailable, ``scan()`` logs
    and returns ``[]`` — no material-news alerts that day. It does NOT raise
    (the driver would only log a ``scan_failed``) and it does NOT invent
    materiality. A failed classification means "we couldn't judge today",
    not "nothing happened" and certainly not "everything is material".

  * **The materiality veto lives in ``scan()``.** ``build_alert`` cannot veto —
    the Protocol requires it to return an ``AlertDraft`` — so the filter must
    happen earlier. ``scan()`` classifies every recent headline in ONE batched
    LLM call, then emits a candidate *only* for stories scoring
    ``relevance >= _RELEVANCE_THRESHOLD``. Immaterial stories never become
    candidates, so the driver never fires them.

Cost is bounded: one batched call per ticker per run (up to
``_MAX_STORIES_PER_SCAN`` headlines), cached in ``llm_artifacts`` keyed on
(ticker, sorted news ids, anchor sha). NOTE: this scan-time LLM cost is not
gated by the driver's ``--max-cost-usd`` (that cap gates ``build_alert``, which
here is a cheap deterministic assembly of the judgment ``scan`` already made).
That is an accepted tradeoff — the batch is bounded and cached.

The news table
--------------
This sensor reads a ``news`` table with the column contract centralized in the
``_NEWS_*`` constants below: a per-ticker association column, a headline, a url,
a published-at datetime, and an optional snippet. No migration creates that
table today — news in this repo is currently WebSearch-driven free markdown
rendered at brief time, not a structured per-story store. Until a news loader
lands (a separate slice), the table is simply absent and ``scan()`` returns
``[]`` (the ``_has_table`` guard). The sensor is wired, typed, and tested
against a fixture ``news`` table so it begins producing alerts the moment a real
loader populates the contract — aligning the column names is then a one-line
change.

Failure mode summary: ``scan()`` is best-effort — a missing news table, a
sqlite error, an absent DB path, or an LLM failure all degrade to ``[]``.
``build_alert`` is deterministic from the stored classification and issues no
LLM call, so the alert body is stable and cheap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

from alerts.store import compute_signature_sha
from llm.anchors import (
    compose_anchor_block,
    load_bear_anchor,
    load_ir_anchor,
    load_priors_anchor,
    load_thesis_anchor,
)
from llm.prompt_versions import prompt_version_for
from llm.untrusted import spotlight
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

# Recency window: stories whose published-at is within this many hours of the
# run count as "recent". The morning driver runs daily, so 24h catches one
# day's news without re-classifying yesterday's batch.
_NEWS_RECENCY_HOURS = 24

# Hard cap on headlines fed to a single classification call — bounds the batch
# so one noisy ticker can't blow the prompt budget.
_MAX_STORIES_PER_SCAN = 15

# Relevance floor (0.0-1.0) at which a classified story becomes a candidate.
# The LLM scores every headline; this code-side cutoff is the materiality veto.
_RELEVANCE_THRESHOLD = 0.6

# News-table column contract. No migration creates this table yet (see module
# docstring); the names live here so a future news loader can align with a
# one-line change rather than a scattered find-and-replace.
_NEWS_TABLE = "news"
_NEWS_COL_ID = "id"
_NEWS_COL_TICKER = "ticker"
_NEWS_COL_HEADLINE = "headline"
_NEWS_COL_URL = "url"
_NEWS_COL_PUBLISHED_AT = "published_at"
_NEWS_COL_SNIPPET = "snippet"

# Built once at import from the column constants above. The table / column
# identifiers are module constants (never user input) so there is no injection
# surface — same pattern as the f-string SQL in ``alerts.store``.
_NEWS_SELECT_SQL = (
    f"SELECT {_NEWS_COL_ID}, {_NEWS_COL_HEADLINE}, {_NEWS_COL_URL}, "
    f"{_NEWS_COL_PUBLISHED_AT}, {_NEWS_COL_SNIPPET} "
    f"FROM {_NEWS_TABLE} "
    f"WHERE {_NEWS_COL_TICKER} = ? AND {_NEWS_COL_PUBLISHED_AT} >= ? "
    f"ORDER BY {_NEWS_COL_PUBLISHED_AT} DESC LIMIT ?"
)

# Artifact-store purpose for the cached batch classification. Must match the
# entry in ``llm_artifact_store.FACT_DEPENDENT_PURPOSES`` so a fact-side
# restatement participates in the existing mark-dirty chain.
_ARTIFACT_PURPOSE = "material_news_classification"
# Prompt-version key for the cache + the calibration A/B dimension, sourced from
# the central registry (the single bump-point). Bump the entry there when the
# classification prompt body changes materially.
_PROMPT_VERSION = prompt_version_for(_ARTIFACT_PURPOSE)

# queued_actions.action_kind values this trigger emits. Material news is
# informational — it never proposes a bear_append or sizing_update; the user
# decides direction.
_ACTION_THESIS_UPDATE = "thesis_update"
_ACTION_EARNINGS_PREP = "earnings_prep_append"

# Retry hint prepended to the user prompt on a second attempt when the first
# response failed to parse as JSON. call_llm has no separate system-prompt
# channel, so the instruction rides on the user prompt (mirrors earnings_tone).
_JSON_RETRY_PREAMBLE = (
    "IMPORTANT: Your previous response was not valid JSON. "
    "Return only the JSON array specified at the end of this prompt. "
    "No markdown fences. No commentary. No prefatory prose.\n\n"
)


class MaterialNewsLLMError(RuntimeError):
    """Raised internally when the batch classification cannot be produced.

    Surfaces when the LLM call fails outright, or returns non-array JSON on
    both the first attempt and the retry. ``scan()`` catches this and returns
    ``[]`` — material news has no deterministic fallback, so a failed
    classification is "no alerts today", never a fabricated one.
    """


@dataclass(frozen=True, slots=True)
class _NewsStory:
    """One row from the ``news`` table, narrowed to the fields the sensor uses."""

    news_id: int
    headline: str
    url: str
    published_at: str
    snippet: str | None


@dataclass(frozen=True, slots=True)
class _Score:
    """The LLM's materiality judgment for one headline."""

    relevance: float
    why_material: str


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------


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


def _conn_db_path(conn: sqlite3.Connection) -> Path | None:
    """File path backing the 'main' schema of an open connection.

    ``scan()`` is handed a connection, but the artifact-store cache takes a
    ``db_path``; resolving it here lets the cache reach the *same* DB the
    driver opened. Returns None for an in-memory / unbacked connection or on
    any sqlite error — the caller then skips the cache and still classifies.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # (seq, name, file) — 'main' is the primary attached DB.
        if len(row) >= 3 and row[1] == "main":
            file_path = row[2]
            if isinstance(file_path, str) and file_path:
                return Path(file_path)
    return None


def _format_threshold(threshold: datetime) -> str:
    """Format a datetime for sqlite TEXT-stored datetime comparison.

    Matches the ``'YYYY-MM-DD HH:MM:SS'`` shape news rows are expected to
    store ``published_at`` in, so a lexical ``>=`` compare is also chronological.
    """
    return threshold.strftime("%Y-%m-%d %H:%M:%S")


def _load_recent_news(conn: sqlite3.Connection, ticker: str, *, now: datetime) -> list[_NewsStory]:
    """Recent stories for ``ticker``, most recent first, capped at the batch size.

    Returns ``[]`` when the news table is absent or the query errors — the
    sensor degrades to producing no candidates rather than raising.
    """
    if not _has_table(conn, _NEWS_TABLE):
        log.debug(
            {
                "event": "material_news_scan_skipped",
                "ticker": ticker,
                "reason": "news_table_missing",
            }
        )
        return []

    threshold = _format_threshold(now - timedelta(hours=_NEWS_RECENCY_HOURS))
    try:
        rows = conn.execute(_NEWS_SELECT_SQL, (ticker, threshold, _MAX_STORIES_PER_SCAN)).fetchall()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "material_news_scan_query_failed",
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return []

    stories: list[_NewsStory] = []
    for row in rows:
        news_id, headline, url, published_at, snippet = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
        )
        if (
            not isinstance(news_id, int)
            or not isinstance(headline, str)
            or not isinstance(url, str)
            or not isinstance(published_at, str)
        ):
            continue
        snippet_str = snippet if isinstance(snippet, str) and snippet.strip() else None
        stories.append(
            _NewsStory(
                news_id=news_id,
                headline=headline,
                url=url,
                published_at=published_at,
                snippet=snippet_str,
            )
        )
    return stories


# --------------------------------------------------------------------------
# Prompt / parse / LLM call
# --------------------------------------------------------------------------


def _build_classification_prompt(ticker: str, anchor_block: str, stories: list[_NewsStory]) -> str:
    """Compose the one-shot batch-classification prompt.

    When a thesis anchor is present it frames materiality against the analyst's
    own thesis + tier-1 KPIs; absent an anchor it falls back to a generic
    "material to an investor in {ticker}" judgment (per the spec: judge anyway,
    don't skip)."""
    if anchor_block.strip():
        framing = anchor_block.strip()
    else:
        framing = (
            f"(No thesis anchor on file for {ticker}. Judge each story by whether "
            f"it would change the investment case for a long-term investor in "
            f"{ticker} — a material development, not routine coverage, price "
            f"chatter, or analyst rating noise.)"
        )

    numbered: list[str] = []
    for idx, story in enumerate(stories):
        snippet = f"\n   Snippet: {story.snippet}" if story.snippet else ""
        numbered.append(f"{idx}. {story.headline}{snippet}")
    # Headlines/snippets arrive from external feeds (FMP, EDGAR, WebSearch
    # structuring) — spotlight them so injected "news" text cannot issue
    # instructions to the materiality classifier (news→trigger→alert chain;
    # directives/llm_injection_threat_model.md).
    headlines_block = spotlight(
        "\n".join(numbered),
        source="news headlines and snippets from external feeds",
    )

    return (
        f"You are screening recent news for a long-term investor in {ticker}.\n\n"
        f"{framing}\n\n"
        f"Recent headlines (numbered, most recent first):\n{headlines_block}\n\n"
        "For EACH numbered headline, score how material it is to the investment "
        "thesis above (or to a long-term holder of the stock generally, if no "
        "thesis was provided). Use 0.0 for noise / routine coverage / price "
        "chatter and 1.0 for a development that directly bears on the thesis or "
        "a tier-1 KPI.\n\n"
        "Return ONLY a JSON array with one object per headline, in this exact "
        "shape:\n"
        '[{"news_index": <int matching the number above>, '
        '"relevance": <float 0.0-1.0>, '
        '"why_material": "<one sentence; for low scores, why it is routine>"}]'
        "\n\nOutput the JSON array and nothing else — no markdown fences, no "
        "commentary, no prefatory prose."
    )


def _strip_json_fence(raw: str) -> str:
    """Strip optional ```json ... ``` fences the LLM occasionally adds."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        return JSON_FENCE_RE.sub("", stripped).strip()
    return stripped


def _parse_classification(raw: str) -> list[object]:
    """Parse the LLM response into a JSON array.

    Raises ``ValueError`` when the response is not valid JSON or not a JSON
    array — the caller's retry policy decides whether to re-prompt or surface a
    ``MaterialNewsLLMError``. An *empty* array is a valid response ("nothing
    material") and does NOT trigger a retry; per-entry malformations are
    tolerated downstream in ``_scores_from_payload`` (degraded quality, not a
    hard failure).
    """
    payload = json.loads(_strip_json_fence(raw))
    if not isinstance(payload, list):
        raise ValueError(
            f"material_news classification is not a JSON array: got {type(payload).__name__}"
        )
    return cast("list[object]", payload)


def _call_llm_with_retry(prompt: str, *, ticker: str) -> list[object]:
    """Call the LLM and parse the array. Retries once on a parse failure.

    A call that raises outright (network, budget cap) surfaces immediately as
    ``MaterialNewsLLMError`` — only a *parse* failure (the call returned, but
    the text wasn't a JSON array) gets the retry. Both attempts failing to
    parse also raises ``MaterialNewsLLMError``.
    """
    try:
        raw_first = call_llm(prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker)
    except Exception as exc:
        raise MaterialNewsLLMError(
            f"material_news classification LLM call failed (initial attempt): {exc}"
        ) from exc
    try:
        return _parse_classification(raw_first)
    except ValueError as first_exc:
        log.warning(
            {
                "event": "material_news_parse_failed_retrying",
                "ticker": ticker,
                "error": str(first_exc),
            }
        )

    retry_prompt = _JSON_RETRY_PREAMBLE + prompt
    try:
        raw_retry = call_llm(retry_prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker)
    except Exception as exc:
        raise MaterialNewsLLMError(
            f"material_news classification LLM call failed (retry attempt): {exc}"
        ) from exc
    try:
        return _parse_classification(raw_retry)
    except ValueError as retry_exc:
        raise MaterialNewsLLMError(
            f"material_news classification returned malformed JSON twice: {retry_exc}"
        ) from retry_exc


def _scores_from_payload(payload: list[object], *, n_stories: int) -> dict[int, _Score]:
    """Normalize a classification array into ``{news_index: _Score}``.

    Skips entries that aren't objects, carry a non-int / out-of-range index, or
    lack a numeric relevance — a malformed entry is degraded output, not a hard
    error. ``bool`` is rejected explicitly for both index and relevance because
    it is an ``int`` subclass.
    """
    out: dict[int, _Score] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        obj = cast("dict[str, object]", entry)
        idx = obj.get("news_index")
        relevance = obj.get("relevance")
        if not isinstance(idx, int) or isinstance(idx, bool):
            continue
        if not (0 <= idx < n_stories):
            continue
        if isinstance(relevance, bool) or not isinstance(relevance, (int, float)):
            continue
        why = obj.get("why_material")
        why_str = why.strip() if isinstance(why, str) else ""
        out[idx] = _Score(relevance=float(relevance), why_material=why_str)
    return out


# --------------------------------------------------------------------------
# Cache key + misc helpers
# --------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    """Deterministic sha256 over UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _classification_cache_inputs(
    *, ticker: str, news_ids: list[int], anchor_text: str
) -> list[bytes | str]:
    """Deterministic cache-input list for ``compute_input_sha256``.

    Keyed on (ticker, sorted news ids, anchor sha): the same batch of stories
    judged against the same thesis anchor is a cache hit. Order is part of the
    contract — changing it invalidates every cached classification.
    """
    return [
        f"ticker={ticker}",
        "news_ids=" + ",".join(str(n) for n in sorted(news_ids)),
        "anchor_sha=" + _sha256_text(anchor_text),
    ]


def _as_float(value: object) -> float:
    """Narrow a numeric evidence value to float. Fails loud on a non-numeric
    (rejecting bool, an int subclass) — ``scan()`` always stores
    ``relevance_score`` as a float, so a miss here is a contract bug."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise TypeError(f"expected numeric evidence value, got {type(value).__name__}")


def _compose_memo(ticker: str, headline: str, relevance: float, why_material: str) -> str:
    """Deterministic alert memo built entirely from the stored classification.

    No LLM involvement — the materiality judgment already happened in
    ``scan()``; this just renders it, so the alert body is stable and cheap.
    """
    pct = f"{relevance:.0%}"
    reason = why_material.strip().rstrip(".")
    if reason:
        return f"{ticker}: '{headline}' — material ({pct}) because {reason}."
    return f"{ticker}: '{headline}' — material ({pct})."


def _repo_root() -> Path:
    """``src/triggers/material_news.py`` -> parents[2] = project root. Used to
    address ``micro_thesis/holdings/<TICKER>.json`` via load_thesis_anchor."""
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Sensor implementation
# --------------------------------------------------------------------------


class MaterialNewsTrigger:
    """``Cadence.ON_NEWS`` sensor over the ``news`` table.

    LLM-dependent: ``scan()`` classifies recent headlines for materiality and
    emits a candidate only for stories above ``_RELEVANCE_THRESHOLD``. On any
    classification failure it returns ``[]`` rather than raising or fabricating.
    """

    kind: ClassVar[str] = "material_news"
    cadence: ClassVar[Cadence] = Cadence.ON_NEWS

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]:
        """Emit one candidate per recent story the LLM scores as material.

        Loads recent news, classifies the whole batch in one (cached) LLM call,
        and keeps only stories with ``relevance >= _RELEVANCE_THRESHOLD``.
        Returns ``[]`` when there is no recent news, the news table is missing,
        or the classification fails — never raises.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        stories = _load_recent_news(db, ticker, now=now)
        if not stories:
            return []

        db_path = _conn_db_path(db)
        anchor_block = self._load_anchor(ticker)

        try:
            scores = self._classify(
                ticker=ticker,
                stories=stories,
                anchor_block=anchor_block,
                db_path=db_path,
            )
        except MaterialNewsLLMError as exc:
            # The LLM-dependent contract: a failed classification degrades to
            # "no material-news alerts today". Log and return [] — do not raise,
            # do not fabricate materiality.
            log.warning(
                {
                    "event": "material_news_classification_failed",
                    "ticker": ticker,
                    "error": str(exc),
                }
            )
            return []

        candidates: list[TriggerCandidate] = []
        for idx, story in enumerate(stories):
            score = scores.get(idx)
            if score is None or score.relevance < _RELEVANCE_THRESHOLD:
                continue  # immaterial — the veto: never becomes a candidate
            evidence: dict[str, object] = {
                "news_id": story.news_id,
                "headline": story.headline,
                "url": story.url,
                "published_at": story.published_at,
                "relevance_score": score.relevance,
                "why_material": score.why_material,
            }
            candidates.append(
                TriggerCandidate(
                    ticker=ticker,
                    kind=self.kind,
                    key=f"{ticker}:news:{story.news_id}",
                    evidence=evidence,
                    computed_at=now,
                )
            )
        return candidates

    def should_fire(self, candidate: TriggerCandidate, user_state: UserStateContext) -> bool:
        """Defensive re-check of the relevance floor.

        ``scan()`` already filtered to ``relevance >= _RELEVANCE_THRESHOLD``;
        this guards against a hand-built candidate slipping below the bar.
        Driver-side dedup (``find_by_signature``) owns dismissed-alert
        suppression, so we don't re-check ``recent_dismissed_signatures`` here.
        """
        _ = user_state
        relevance = candidate.evidence.get("relevance_score")
        if isinstance(relevance, bool) or not isinstance(relevance, (int, float)):
            return False
        return float(relevance) >= _RELEVANCE_THRESHOLD

    def signature_key_evidence(self, candidate: TriggerCandidate) -> Mapping[str, object]:
        """Key the dedup hash on the news id alone so each story fires once.

        The ticker is carried separately by ``compute_signature_sha``'s
        ``ticker`` arg; the headline / url / score derive from the same story
        and would only inflate the hash surface. ``build_alert`` keys on the
        exact same dict.
        """
        return {"news_id": candidate.evidence["news_id"]}

    def build_alert(self, candidate: TriggerCandidate, anchor: ThesisAnchor | None) -> AlertDraft:
        """Assemble the alert deterministically from the stored classification.

        No LLM call here — the materiality judgment already happened in
        ``scan()`` and rides in ``candidate.evidence``. This keeps ``build_alert``
        cheap (so the driver's cost cap isn't a factor) and the alert body
        stable across re-runs.
        """
        _ = anchor  # driver passes None; this trigger has no markdown anchor to load
        ticker = candidate.ticker
        evidence = candidate.evidence

        news_id = evidence["news_id"]
        headline = str(evidence["headline"])
        url = str(evidence["url"])
        published_at = str(evidence.get("published_at") or "")
        relevance = _as_float(evidence["relevance_score"])
        why_material = str(evidence.get("why_material") or "")

        memo_text = _compose_memo(ticker, headline, relevance, why_material)

        evidence_obj: dict[str, object] = {
            "news_id": news_id,
            "url": url,
            "headline": headline,
            "published_at": published_at,
            "relevance_score": relevance,
            "why_material": why_material,
        }
        evidence_json = json.dumps(evidence_obj, sort_keys=True, ensure_ascii=False, default=str)
        signature_sha = compute_signature_sha(
            self.kind, ticker, self.signature_key_evidence(candidate)
        )
        fired_at = datetime.now(UTC).replace(tzinfo=None)

        return AlertDraft(
            trigger_kind=self.kind,
            ticker=ticker,
            fired_at=fired_at,
            evidence_json=evidence_json,
            signature_sha=signature_sha,
            memo_text=memo_text,
        )

    def draft_actions(
        self, alert: AlertDraft, candidate: TriggerCandidate
    ) -> list[QueuedActionDraft]:
        """Propose informational follow-ups for a material development.

        Material news is informational — it never proposes a ``bear_append`` or
        ``sizing_update`` (the user decides direction). Emits a
        ``thesis_update`` (incorporate the development) and an
        ``earnings_prep_append`` (watch management's Q&A handling of it next call
        — the user tracks management's responses to analysts, not their own).
        """
        _ = alert
        evidence = candidate.evidence
        headline = evidence.get("headline")
        news_id = evidence.get("news_id")
        if not isinstance(headline, str) or news_id is None:
            return []

        url = evidence.get("url")
        url_str = url if isinstance(url, str) else ""
        why = evidence.get("why_material")
        why_str = why.strip() if isinstance(why, str) else ""

        thesis_body = f"Incorporate development: '{headline}'."
        if why_str:
            thesis_body = f"{thesis_body} {why_str}"

        return [
            QueuedActionDraft(
                action_kind=_ACTION_THESIS_UPDATE,
                payload={
                    "body": thesis_body,
                    "news_url": url_str,
                    "news_id": news_id,
                },
            ),
            QueuedActionDraft(
                action_kind=_ACTION_EARNINGS_PREP,
                payload={
                    "body": f"Next call Q&A: watch how management addresses '{headline}'.",
                    "news_id": news_id,
                },
            ),
        ]

    # ------------------------------------------------------------------
    # Internal — anchor load + cache-aware classification
    # ------------------------------------------------------------------

    def _load_anchor(self, ticker: str) -> str:
        """Best-effort composed anchor markdown (thesis + bear + IR + analyst
        priors), or "" on any failure.

        The anchor frames the materiality judgment; its absence is fine (the
        prompt falls back to generic framing) so a load failure must never sink
        the scan. The bear case + IR narrative are included so a story that
        validates a bear hypothesis — or undercuts management's IR framing —
        scores material; the priors block does the same for a story that
        answers an open analyst question or hits a watch-item.
        compose_anchor_block omits whichever blocks are absent."""
        try:
            repo_root = _repo_root()
            return compose_anchor_block(
                load_thesis_anchor(repo_root, ticker),
                load_bear_anchor(repo_root, ticker),
                load_ir_anchor(repo_root, ticker),
                load_priors_anchor(repo_root, ticker),
            )
        except Exception as exc:  # anchor is optional; never block classification
            log.debug(
                {
                    "event": "material_news_anchor_load_failed",
                    "ticker": ticker,
                    "error": str(exc),
                }
            )
            return ""

    def _classify(
        self,
        *,
        ticker: str,
        stories: list[_NewsStory],
        anchor_block: str,
        db_path: Path | None,
    ) -> dict[int, _Score]:
        """Return ``{news_index: _Score}``, hitting the artifact-store cache when
        possible. A cache hit short-circuits the LLM call; a miss classifies and
        writes the result back. Raises ``MaterialNewsLLMError`` on LLM failure."""
        cache_inputs = _classification_cache_inputs(
            ticker=ticker,
            news_ids=[s.news_id for s in stories],
            anchor_text=anchor_block,
        )
        n_stories = len(stories)

        if db_path is not None:
            existing = read_current(ticker=ticker, purpose=_ARTIFACT_PURPOSE, db_path=db_path)
            new_sha = compute_input_sha256(
                prompt_version=_PROMPT_VERSION, cache_inputs=cache_inputs
            )
            if (
                existing is not None
                and not existing.dirty
                and existing.input_sha256 == new_sha
                and isinstance(existing.content_json, list)
            ):
                log.info(
                    {
                        "event": "material_news_cache_hit",
                        "ticker": ticker,
                        "artifact_id": existing.id,
                    }
                )
                return _scores_from_payload(
                    cast("list[object]", existing.content_json), n_stories=n_stories
                )

        prompt = _build_classification_prompt(ticker, anchor_block, stories)
        payload = _call_llm_with_retry(prompt, ticker=ticker)

        if db_path is not None:
            _ = upsert(
                UpsertRequest(
                    ticker=ticker,
                    purpose=_ARTIFACT_PURPOSE,
                    content_json=payload,
                    cache_inputs=cache_inputs,
                    prompt_version=_PROMPT_VERSION,
                ),
                db_path=db_path,
            )
        return _scores_from_payload(payload, n_stories=n_stories)
