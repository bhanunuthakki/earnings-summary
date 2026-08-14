"""Env-gated capture of full LLM prompt/response exchanges.

The ``llm_calls`` ledger stores only the prompt/response **SHA** — deliberate,
because prompts embed thesis and IR content (see ``directives/llm_calls.md``).
But comparing the eval-gated Gemini backend against Claude across the *real*
production surface (every ``--enable-llm`` section, not a hand-built smoke set)
needs the actual prompt TEXT each section sends. This opt-in sink records the
full exchange when ``LLM_CAPTURE_DIR`` is set; it is **OFF by default** and
best-effort — a capture failure never breaks the LLM call.

Consumer: ``execution/compare_backends.py --from-capture`` replays the captured
Claude prompts through Gemini ONLY (the Claude response is already captured), so
a cross-purpose backend corpus costs zero extra Claude spend.

One JSONL line per exchange in a process-and-purpose shard,
``<LLM_CAPTURE_DIR>/capture_<YYYY-MM-DD>_<PID>_p<PURPOSE_SHA>.jsonl``::

    {captured_at, purpose, prompt_version, ticker, scope, model, backend, run_id,
     prompt, response, prompt_sha256}

Filters:
  * ``LLM_CAPTURE_DIR`` unset ⇒ capture is off (the common case).
  * ``LLM_CAPTURE_PURPOSES`` (csv) ⇒ capture only those purposes; unset ⇒ all
    (minus the denylist).
  * shards older than ``EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS`` (default 90)
    are pruned at most once per UTC day in each process.
  * The judge/eval purposes are NEVER captured — they read a corpus, and capturing
    them would feed the grader's own traffic back into the next comparison.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from stat import S_ISDIR

log = logging.getLogger(__name__)

LLM_CAPTURE_DIR_ENV = "LLM_CAPTURE_DIR"
LLM_CAPTURE_PURPOSES_ENV = "LLM_CAPTURE_PURPOSES"
CAPTURE_ARCHIVE_DIR_ENV = "EARNINGS_SUMMARY_CAPTURE_ARCHIVE_DIR"
CAPTURE_RETENTION_DAYS_ENV = "EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS"
DEFAULT_CAPTURE_RETENTION_DAYS = 90
DEFAULT_CAPTURE_MAX_BYTES = 1 << 30
_CAPTURE_FILE_RX = re.compile(r"^capture_(\d{4}-\d{2}-\d{2})(?:_\d+)?(?:_p[0-9a-f]{12})?\.jsonl$")
_WRITE_LOCK = threading.Lock()
_LAST_PRUNED_DAY: dict[Path, date] = {}

# Never capture eval/judge/steering traffic — it would pollute a comparison
# corpus with the grading calls that consume it (isolation invariant I4,
# meta_eval_governance.md §5). backend_compare_judge = the pairwise judge;
# eval_judge = the general LLM-evals harness's judge; case_difficulty_classify =
# the sweep sampler's difficulty classifier (its prompts EMBED captured
# production prompts — recapturing them would nest corpora).
CAPTURE_DENYLIST: frozenset[str] = frozenset(
    {
        "backend_compare_judge",
        "eval_judge",
        "case_difficulty_classify",
        "optimizer_nominator",
        "model_frontier_research",
        "query_criteria_derive",
        "prompt_variant_propose",
        "prompt_reflect_rewrite",
        "readme_update",
        "readme_update_judge",
    }
)


def capture_dir() -> Path | None:
    """The capture directory, or None when capture is disabled."""
    raw = os.environ.get(LLM_CAPTURE_DIR_ENV)
    if not raw or not raw.strip():
        return None
    return Path(raw.strip())


def default_capture_archive_dir(repo_root: Path) -> Path:
    """Private archive location used by harvesters and replay audits.

    Windows defaults outside the mirrored repository under LocalAppData. Other
    platforms retain the historical repo-local default for portable CI/dev use.
    """
    configured = (
        os.environ.get(CAPTURE_ARCHIVE_DIR_ENV, "").strip()
        or os.environ.get(LLM_CAPTURE_DIR_ENV, "").strip()
    )
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local_app_data:
        return Path(local_app_data) / "earnings-summary" / "llm_capture"
    return repo_root / "data" / "llm_capture"


def capture_purpose_suffix(purpose: str) -> str:
    """Stable filename-safe purpose partition for bounded replay scans."""
    partition = "lens:*" if purpose.startswith("lens:") else purpose
    return sha256(partition.encode("utf-8")).hexdigest()[:12]


def _purpose_allowlist() -> frozenset[str] | None:
    raw = os.environ.get(LLM_CAPTURE_PURPOSES_ENV)
    if not raw or not raw.strip():
        return None
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def should_capture(purpose: str | None) -> bool:
    """Whether this purpose's exchange should be captured right now."""
    if capture_dir() is None:
        return False
    if purpose is None:
        return False
    if purpose in CAPTURE_DENYLIST:
        return False
    allow = _purpose_allowlist()
    if allow is None:
        return True
    return purpose in allow


def _retention_days() -> int:
    raw = os.environ.get(CAPTURE_RETENTION_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_CAPTURE_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_CAPTURE_RETENTION_DAYS


def prune_capture_archive(
    directory: Path,
    *,
    retention_days: int = DEFAULT_CAPTURE_RETENTION_DAYS,
    today: datetime | None = None,
    strict: bool = False,
    require_directory: bool = False,
) -> int:
    """Delete only recognized capture shards older than ``retention_days``."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    current = today or datetime.now(UTC).replace(tzinfo=None)
    cutoff = current.date().toordinal() - retention_days
    deleted = 0
    if not _archive_directory_available(
        directory,
        strict=strict,
        require_directory=require_directory,
    ):
        return deleted
    for path in directory.glob("capture_*.jsonl"):
        match = _CAPTURE_FILE_RX.fullmatch(path.name)
        if match is None:
            continue
        try:
            file_day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            if file_day.toordinal() < cutoff:
                path.unlink()
                deleted += 1
        except OSError:
            if strict:
                raise
            continue
        except ValueError:
            continue
    return deleted


def _archive_directory_available(
    directory: Path,
    *,
    strict: bool,
    require_directory: bool,
) -> bool:
    try:
        mode = directory.stat().st_mode
    except FileNotFoundError:
        if strict and require_directory:
            raise
        return False
    except OSError:
        if strict:
            raise
        return False
    if S_ISDIR(mode):
        return True
    if strict:
        raise NotADirectoryError("capture archive root is not a directory")
    return False


def capture_archive_bytes(
    directory: Path,
    *,
    strict: bool = False,
    require_directory: bool = False,
) -> int:
    """Total bytes in recognized capture shards, ignoring unrelated files."""
    if not _archive_directory_available(
        directory,
        strict=strict,
        require_directory=require_directory,
    ):
        return 0
    total = 0
    for path in directory.glob("capture_*.jsonl"):
        if _CAPTURE_FILE_RX.fullmatch(path.name) is None:
            continue
        try:
            total += path.stat().st_size
        except OSError:
            if strict:
                raise
    return total


def _prune_expired(directory: Path, *, today: datetime) -> None:
    resolved = directory.resolve()
    prune_day = today.date()
    if _LAST_PRUNED_DAY.get(resolved) == prune_day:
        return
    prune_capture_archive(directory, retention_days=_retention_days(), today=today)
    _LAST_PRUNED_DAY[resolved] = prune_day


def capture_exchange(
    *,
    prompt: str,
    response: str,
    purpose: str | None,
    ticker: str | None,
    scope: str | None,
    model: str,
    run_id: str | None,
    backend: str = "claude",
    prompt_version: str | None = None,
) -> None:
    """Best-effort append of one prompt/response exchange to the capture log.

    A no-op unless ``LLM_CAPTURE_DIR`` is set and ``purpose`` passes the filter.
    Never raises — capture is telemetry, it must not block the LLM call (same
    contract as the ledger writes in ``llm.ledger``).
    """
    try:
        if not should_capture(purpose):
            return
        directory = capture_dir()
        if directory is None:  # re-checked for the type-narrower; should_capture covered it
            return
        directory.mkdir(parents=True, exist_ok=True)

        from llm.prompt_versions import prompt_version_for
        from llm_call_ledger import sha256_text

        record = {
            "purpose": purpose,
            "prompt_version": prompt_version or prompt_version_for(purpose or ""),
            "ticker": ticker,
            "scope": scope,
            "model": model,
            "backend": backend,
            "run_id": run_id,
            "prompt": prompt,
            "response": response,
            "prompt_sha256": sha256_text(prompt),
        }
        # Serialize the potentially large exchange before taking the write
        # lock. captured_at is injected while locked, so reverse file order
        # stays timestamp order for concurrent threads in this process.
        body = json.dumps(record, ensure_ascii=False)
        with _WRITE_LOCK:
            # Repo convention: naive-UTC stamps
            # (project_naive_utc_datetime_convention).
            now = datetime.now(UTC).replace(tzinfo=None)
            purpose_suffix = capture_purpose_suffix(purpose or "")
            path = directory / (
                f"capture_{now.strftime('%Y-%m-%d')}_{os.getpid()}_p{purpose_suffix}.jsonl"
            )
            _prune_expired(directory, today=now)
            line = (
                '{"captured_at":'
                + json.dumps(now.isoformat())
                + ","
                + body.removeprefix("{")
                + "\n"
            )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # best-effort: telemetry never blocks the call
        log.debug({"event": "llm_capture_failed", "error": str(exc)})
