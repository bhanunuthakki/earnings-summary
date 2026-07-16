"""LLM information-quality scoring for diet signals — the ingest-time pass.

The diet reading lane's low-quality filter was a static substring denylist
(``store._LOW_QUALITY_SOURCE_TOKENS``) — a keyword gate where semantics decide
(LLM-maximalist directive: "use LLM where semantics beat keyword/regex"). This
module is the intelligent replacement's WRITE side: a batched Haiku call
(purpose ``diet_source_quality``) scores news-backed diet signals 0..1 on
information quality — original reporting / primary-source disclosure high,
recycled recaps / screener-generated content-farm copy low — and stamps the
score onto ``signals.quality_score`` (alembic 0150) ONCE, at ingest.

Read-time filtering happens in ``store.load_diet_signals`` on the STORED
score, so the diet lane stays guard-safe: no decaying scorer import, no
wall-clock decay — a stored-field filter, exactly like the denylist it
supersedes. Rows the batch pass hasn't reached yet (NULL) fall back to the
denylist there (belt-and-braces).

Scheduling: runs as a follow-on step of ``execution/fetch_news.py`` (stage 0
of the 04:00 morning pipeline; also any ad-hoc fetch), capped at
``_MAX_ROWS_PER_RUN`` rows (= ``_MAX_ROWS_PER_RUN / _BATCH_SIZE`` calls) per
run. Registered in ``directives/llm_quota_scheduling.md``. Degradation follows
the fleet-wide per-item pattern (rule 3; reference: ``attach_conditions``): a
transient LLM failure or a twice-unparseable response leaves that batch's rows
NULL — tallied, retried next run — while hard stops (``llm.cli.is_hard_stop``:
budget cap, missing CLI) propagate loudly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import cast

from llm.cli import is_hard_stop
from llm.structured import StructuredParseError, call_llm_structured
from signals.store import NEWS_MIRRORED_TYPES

log = logging.getLogger(__name__)

PURPOSE = "diet_source_quality"

# Batching: one call scores up to _BATCH_SIZE rows; one run scores at most
# _MAX_ROWS_PER_RUN rows (5 Haiku calls) so a large unscored backlog drains
# over a few mornings instead of burning one run's quota.
_BATCH_SIZE = 20
_MAX_ROWS_PER_RUN = 100

_PROMPT = """You are a strict JSON grader of financial-news INFORMATION QUALITY.

Below are news-backed signals from an equity-research reading feed, one per
line as JSON: {{"id": ..., "firm": <publisher>, "headline": ...}}.

Score each 0.0-1.0 on how much NEW, decision-useful information a diligent
analyst gains by reading it:

- HIGH (0.7-1.0): original reporting; primary-source disclosure (company
  announcement, filing, regulator); a sell-side rating/estimate change from
  the issuing firm; substantive investigative or data-driven journalism.
- MID (0.4-0.7): straight wire coverage of a real event; competent secondary
  reporting that adds context.
- LOW (0.0-0.4): recycled recaps of others' reporting; screener/algorithm-
  generated copy; listicles ("3 stocks to buy"); price-move commentary with
  no new facts; promotional or SEO content-farm output.

Judge the SUBSTANCE (publisher + headline together), not the brand name
alone — a normally-weak publisher can carry a real scoop, and a strong wire
can syndicate filler.

Return ONLY a JSON array — no markdown fences, no commentary — with one
element per input line, preserving the ids:

[{{"id": <integer from the input>, "score": <number 0.0-1.0>}}, ...]

Signals:
{lines}
"""


def _build_prompt(rows: list[tuple[int, str | None, str]]) -> str:
    lines = "\n".join(
        json.dumps({"id": rid, "firm": firm, "headline": title[:300]}, ensure_ascii=False)
        for rid, firm, title in rows
    )
    return _PROMPT.format(lines=lines)


def _parse_scores(payload: object, valid_ids: set[int]) -> dict[int, float]:
    """Validate one batch response: keep only entries whose id is in THIS
    batch and whose score is a number in [0, 1]. Invalid entries are dropped
    with a log line (their rows stay NULL — rescored next run)."""
    out: dict[int, float] = {}
    if not isinstance(payload, list):
        return out
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            log.warning({"event": "diet_quality_entry_invalid", "reason": "not an object"})
            continue
        obj = cast("dict[str, object]", entry)
        rid = obj.get("id")
        score = obj.get("score")
        if isinstance(rid, bool) or not isinstance(rid, int) or rid not in valid_ids:
            log.warning({"event": "diet_quality_entry_invalid", "reason": "bad id", "id": rid})
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            log.warning({"event": "diet_quality_entry_invalid", "reason": "bad score", "id": rid})
            continue
        if not (0.0 <= float(score) <= 1.0):
            log.warning(
                {"event": "diet_quality_entry_invalid", "reason": "out of range", "id": rid}
            )
            continue
        out[rid] = float(score)
    return out


def _has_quality_column(conn: sqlite3.Connection) -> bool:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    except sqlite3.Error:
        return False
    return "quality_score" in cols


def score_unscored_signals(
    db_path: Path | str,
    *,
    max_rows: int = _MAX_ROWS_PER_RUN,
    batch_size: int = _BATCH_SIZE,
) -> dict[str, int]:
    """Score up to ``max_rows`` unscored news-backed diet signals; return a
    tally. Newest first, so the rows the panel is about to show are scored
    before backlog.

    Per-batch degradation (quota-scheduling rule 3): a ``StructuredParseError``
    or any other transient LLM failure leaves that batch's rows NULL (tallied,
    retried next run) and the loop continues; hard stops propagate. Best-effort
    against a missing DB / pre-0150 schema (tallied ``db_unavailable``).
    """
    tally = {"scored": 0, "deferred_transient": 0, "parse_failed": 0, "db_unavailable": 0}
    path = Path(db_path)
    if not path.exists():
        tally["db_unavailable"] = 1
        return tally
    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
    except sqlite3.Error:
        tally["db_unavailable"] = 1
        return tally
    try:
        if not _has_quality_column(conn):
            tally["db_unavailable"] = 1
            return tally
        marks = ",".join("?" for _ in NEWS_MIRRORED_TYPES)
        try:
            rows = conn.execute(
                f"SELECT id, firm, title FROM signals "
                f"WHERE quality_score IS NULL AND signal_type IN ({marks}) "
                "ORDER BY published_at DESC, id DESC LIMIT ?",
                (*sorted(NEWS_MIRRORED_TYPES), int(max_rows)),
            ).fetchall()
        except sqlite3.Error:
            tally["db_unavailable"] = 1
            return tally
        pending = [(int(r[0]), str(r[1]) if r[1] is not None else None, str(r[2])) for r in rows]
        for start in range(0, len(pending), int(batch_size)):
            batch = pending[start : start + int(batch_size)]
            valid_ids = {rid for rid, _firm, _title in batch}
            try:
                payload = call_llm_structured(
                    _build_prompt(batch), purpose=PURPOSE, expect="array", db_path=path
                )
            except StructuredParseError as exc:
                log.error(
                    {
                        "event": "diet_quality_parse_failed",
                        "batch_ids": sorted(valid_ids),
                        "error": str(exc),
                    }
                )
                tally["parse_failed"] += 1
                continue
            except Exception as exc:
                # Hard stops (budget cap / missing CLI) are configuration
                # problems — halt the whole pass loudly. Anything else is a
                # transient per-call failure: this batch stays NULL (picked up
                # by the next run's WHERE quality_score IS NULL) and the loop
                # moves on.
                if is_hard_stop(exc):
                    raise
                log.warning(
                    {
                        "event": "diet_quality_transient_failure",
                        "batch_ids": sorted(valid_ids),
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
                tally["deferred_transient"] += 1
                continue
            scores = _parse_scores(payload, valid_ids)
            for rid, score in scores.items():
                conn.execute(
                    "UPDATE signals SET quality_score = ? WHERE id = ? AND quality_score IS NULL",
                    (score, rid),
                )
            conn.commit()
            tally["scored"] += len(scores)
        return tally
    finally:
        conn.close()
