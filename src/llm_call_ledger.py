"""Ledger writer for LLM calls.

One row per LLM invocation, written best-effort. If the DB is missing,
locked, or doesn't have the llm_calls table, the writer logs a warning and
returns — never raising. The LLM call that produced the row is too
expensive to fail because of a logging miss.

The writer resolves its DB via ``db_paths.resolve_db_path``: an explicit
``db_path`` parameter wins; then an active ``db_paths.db_path_context`` (how
``call_llm(db_path=...)`` scopes a call made against an explicit DB); then
``db.DB_PATH`` — so it automatically picks up whichever repo-root the caller
has set via ``_sync_db_to_repo`` (build_artifacts.py mutates db.DB_PATH when
--repo-root is passed). Tests can inject a path through ``db_path`` directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from db_paths import resolve_db_path

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LlmCallRecord:
    """One row's worth of LLM-call telemetry.

    Every field maps 1:1 to llm_calls columns. The writer treats None as SQL
    NULL — caller decides which columns to populate based on what's available
    from the underlying CLI / API response. ``cache_hit`` is the only required
    boolean (defaults to False); Phase 1 enables it for short-circuit returns.
    """

    called_at: datetime
    model: str
    prompt_sha256: str
    prompt_chars: int
    elapsed_ms: int
    cache_hit: bool = False
    purpose: str | None = None
    ticker: str | None = None
    scope: str | None = None
    response_sha256: str | None = None
    response_chars: int | None = None
    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate_usd: float | None = None
    fallback_used: str | None = None
    artifact_id: int | None = None
    error: str | None = None
    run_id: str | None = None
    # P0 prompt-registry identity (llm.prompt_registry, mig 0205). NULL = an
    # unmigrated call site passing a raw string — honest, never backfilled.
    template_id: str | None = None
    template_version: str | None = None
    template_vars_sha256: str | None = None


def sha256_text(text: str) -> str:
    """SHA-256 hex digest of a UTF-8 encoded string. The ledger's canonical
    cache-key function: same prompt → same sha → dedup candidate."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_claude_json_output(stdout: str) -> tuple[str, dict[str, object]]:
    """Parse the JSON envelope from ``claude -p --output-format json``.

    Returns (response_text, full_payload). The CLI wraps responses as e.g.::

      {"type":"result","subtype":"success","is_error":false,
       "result":"<the response>","total_cost_usd":0.07,
       "usage":{"input_tokens":..., "cache_creation_input_tokens":...,
                "cache_read_input_tokens":..., "output_tokens":...},
       ...}

    Raises ValueError when stdout isn't well-formed JSON, when ``is_error``
    is true, or when ``result`` is missing/not a string. Callers map this
    onto their existing failure path so the Gemini fallback still fires.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude CLI did not return JSON: {stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Claude CLI returned non-object JSON: {type(payload).__name__}")
    payload_dict = cast("dict[str, object]", payload)
    if payload_dict.get("is_error"):
        # Include the envelope's `result` HEAD — it carries the actual reason
        # ("usage limit reached...", "There's an issue with the selected
        # model...", auth prompts). Before this, the raised message named only
        # subtype/status, so the July-2026 quota incident was recorded as
        # thousands of indistinguishable generic errors.
        result_raw = payload_dict.get("result")
        result_head = result_raw[:300] if isinstance(result_raw, str) else ""
        raise ValueError(
            f"Claude CLI reported error: subtype={payload_dict.get('subtype')!r} "
            f"api_status={payload_dict.get('api_error_status')!r} result={result_head!r}"
        )
    result = payload_dict.get("result")
    if not isinstance(result, str):
        raise ValueError(
            f"Claude CLI JSON missing string `result`: keys={sorted(payload_dict.keys())}"
        )
    return result, payload_dict


def usage_from_json_meta(meta: dict[str, object]) -> dict[str, int | float | None]:
    """Extract the usage/cost fields the ledger captures from a CLI JSON
    envelope. Defensive — any missing field returns None, never raises.

    Returned keys match LlmCallRecord field names so the caller can splat
    this dict into the record constructor.
    """
    usage_raw = meta.get("usage")
    usage = cast("dict[str, object]", usage_raw) if isinstance(usage_raw, dict) else {}

    def _int(key: str) -> int | None:
        v = usage.get(key)
        if isinstance(v, bool):  # bool is a subclass of int in Python
            return None
        return int(v) if isinstance(v, (int, float)) else None

    cost = meta.get("total_cost_usd")
    cost_float = (
        float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    )

    return {
        "input_tokens": _int("input_tokens"),
        "cache_creation_input_tokens": _int("cache_creation_input_tokens"),
        "cache_read_input_tokens": _int("cache_read_input_tokens"),
        "output_tokens": _int("output_tokens"),
        "cost_estimate_usd": cost_float,
    }


_INSERT_SQL = """
INSERT INTO llm_calls(
    called_at, purpose, ticker, scope, model,
    prompt_sha256, response_sha256, prompt_chars, response_chars,
    input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
    output_tokens, elapsed_ms, cost_estimate_usd, cache_hit,
    fallback_used, artifact_id, error, run_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

# Extended INSERT carrying the P0 template-identity columns (mig 0205). The
# writer tries this first and falls back to the legacy SQL — WITH a warning —
# when the columns are absent (a pre-0205 DB). Silent fallback would be the
# silent-degradation class: template telemetry vanishing while everything
# looks migrated; the warning names the missing migration instead.
_INSERT_SQL_TEMPLATED = """
INSERT INTO llm_calls(
    called_at, purpose, ticker, scope, model,
    prompt_sha256, response_sha256, prompt_chars, response_chars,
    input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
    output_tokens, elapsed_ms, cost_estimate_usd, cache_hit,
    fallback_used, artifact_id, error, run_id,
    template_id, template_version, template_vars_sha256
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def record_call(record: LlmCallRecord, *, db_path: Path | str | None = None) -> int | None:
    """Best-effort write of one ledger row. Never raises.

    Returns the inserted row id on success, None on any failure. A None
    return is logged at WARNING and callers should treat it as informational
    only — the LLM call itself has already succeeded by the time this is
    invoked.

    ``db_path`` overrides the default resolution (used by tests). Production
    callers leave it None so the writer picks up whichever DB the surrounding
    pipeline has configured via ``db.DB_PATH``.
    """
    try:
        if record.purpose is None:
            # Attribution hygiene (2026-07-19 review: purpose=NULL rows had
            # accumulated $150+ of unattributable spend). Name the caller so
            # the next NULL identifies its own call site in the log — the row
            # still lands (telemetry must not be dropped over a label).
            import inspect

            caller = "?"
            try:
                frame = inspect.stack()[1]
                caller = f"{Path(frame.filename).name}:{frame.lineno}"
            except Exception:
                pass
            log.warning(
                {"event": "llm_call_missing_purpose", "model": record.model, "caller": caller}
            )
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            log.debug(
                {
                    "event": "llm_call_ledger_skip",
                    "reason": "db_missing",
                    "path": str(path),
                }
            )
            return None
        # 30s busy_timeout matches db.py's writer. This telemetry row races the
        # surrounding pipeline for portfolio.db's single WAL write lock; at 5s it
        # lost that race in bursts (morning pipeline holds the lock longer since
        # the #873 Diet/alert/ledger writes), logging "database is locked" and
        # dropping cost rows. 30s clears the burst without ever blocking the LLM
        # call itself (this runs after the call has already succeeded).
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            legacy_values = (
                record.called_at.isoformat(),
                record.purpose,
                record.ticker,
                record.scope,
                record.model,
                record.prompt_sha256,
                record.response_sha256,
                record.prompt_chars,
                record.response_chars,
                record.input_tokens,
                record.cache_creation_input_tokens,
                record.cache_read_input_tokens,
                record.output_tokens,
                record.elapsed_ms,
                record.cost_estimate_usd,
                1 if record.cache_hit else 0,
                record.fallback_used,
                record.artifact_id,
                record.error,
                record.run_id,
            )
            try:
                cur = conn.execute(
                    _INSERT_SQL_TEMPLATED,
                    (
                        *legacy_values,
                        record.template_id,
                        record.template_version,
                        record.template_vars_sha256,
                    ),
                )
            except sqlite3.OperationalError as exc:
                if "template" not in str(exc):
                    raise
                # Pre-0205 DB: the row still lands, but template telemetry is
                # dropped — say so loudly instead of degrading in silence.
                log.warning(
                    {
                        "event": "llm_call_ledger_template_cols_missing",
                        "hint": "run `alembic upgrade head` (mig 0205) to capture "
                        "template identity",
                    }
                )
                cur = conn.execute(_INSERT_SQL, legacy_values)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        # Best-effort: log + return None. The LLM call that produced this
        # record has already succeeded (or failed loudly elsewhere); a logging
        # miss must never escalate into a pipeline failure.
        log.warning(
            {
                "event": "llm_call_ledger_write_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "purpose": record.purpose,
                "ticker": record.ticker,
            }
        )
        return None
