"""CLI-transport failure classification + the quota circuit breaker.

Born from the July-2026 incident (measured 2026-07-24): the platform's
``llm_calls`` error rate ran **72% for the month** (7,488 of 10,340 calls) vs
10% in June, in multi-day ~100% bands (Jul 7-13, Jul 20-23) separated by ~0%
days — the signature of the shared subscription's usage-limit window being
exhausted and resetting, not of random flakiness. Three transport defects made
the incident worse than it needed to be, all fixed around this module:

1. **The real reason was thrown away.** ``claude -p --output-format json``
   reports failures INSIDE the JSON envelope (``is_error`` / ``result`` /
   ``api_error_status``) — sometimes with exit 0. The ledger recorded either
   the generic ``CalledProcessError`` string or the *tail* of the envelope
   (the usage-stats block), so a quota exhaustion and a one-off CLI bug were
   indistinguishable after the fact. ``classify_cli_failure`` parses the
   envelope and returns a stable class + the actual message.

2. **Every doomed call burned a full subprocess.** Once the usage limit is
   reached, every subsequent call for hours spawns a CLI process that cannot
   succeed (and each then burned a fallback attempt / phantom ledger row).
   The breaker records "blocked until ~T" the moment a usage-limit failure is
   classified; until T, callers fail fast with ``LLMQuotaExhausted`` without
   spawning anything. State is a small JSON file next to the DB so ALL
   processes (pipeline, dashboard, ad-hoc scripts) share one breaker.

3. **No retry for genuinely transient failures.** A single 529/network blip
   fell straight to the (disabled) fallback and became a recorded error.
   ``retry_budget`` gives the per-class retry policy; ``llm.cli`` sleeps and
   re-spawns for those classes only.

Classification is substring-based over the extracted envelope/stderr text.
That is deliberate: the CLI's message wording is not a stable API, so the
matcher prefers the envelope's numeric ``api_error_status`` when present and
falls back to conservative phrase matching; anything unrecognized is
``unknown`` (retryable, never breaker-tripping — the breaker only ever
engages on a *positively identified* usage-limit failure).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

# Failure classes. Stable vocabulary — ledger error strings are prefixed
# "[<class>] ..." so measurement can GROUP BY class with a LIKE.
USAGE_LIMIT = "usage_limit"  # subscription window exhausted — breaker engages
RATE_LIMIT = "rate_limit"  # 429 — transient, retry with backoff
OVERLOADED = "overloaded"  # 5xx / network — transient, retry with backoff
AUTH = "auth"  # 401/403 / expired OAuth — operator-actionable, no retry
CONFIG = "config"  # 404 / bad model id — deterministic, no retry
TIMEOUT = "timeout"  # subprocess timeout — one retry (each attempt is costly)
MALFORMED = "malformed"  # unparseable/empty envelope — transient, retry
UNKNOWN = "unknown"  # anything else — treated as transient

# When a usage-limit message carries no parseable reset time, probe again after
# this many minutes. Small enough to self-heal quickly after a reset, large
# enough that a blocked day costs ~100 doomed probes instead of thousands.
DEFAULT_PROBE_MINUTES = int(os.environ.get("LLM_QUOTA_PROBE_MINUTES", "15"))

# Cap on how far in the future a parsed reset time is honored. A garbled epoch
# (wrong units, clock skew) must not brick the transport for a week.
MAX_BLOCK_HOURS = 8.0

_BREAKER_FILENAME = ".llm_quota_breaker.json"

# "Claude AI usage limit reached|1753305600" — the CLI's machine-readable form.
_EPOCH_RX = re.compile(r"\|(\d{10})\b")

# The api_error_status when it arrives EMBEDDED IN TEXT rather than as an
# envelope field — the parse-ValueError path renders it as "api_status=404"
# (llm_call_ledger.parse_claude_json_output). Found by live probe 2026-07-24:
# without this, a real 404 classified as MALFORMED and earned 3 pointless
# retries instead of CONFIG's immediate stop.
_TEXT_STATUS_RX = re.compile(r"api_status=(\d{3})\b")

_USAGE_PHRASES = (
    "usage limit reached",
    "usage limit",
    "limit will reset",
    "weekly limit",
    "5-hour limit",
    "out of extra usage",
)
_AUTH_PHRASES = (
    "authentication",
    "oauth token",
    "token has expired",
    "invalid api key",
    "please run /login",
    "not logged in",
    "credit balance",  # billing exhaustion is operator-actionable like auth
)
_OVERLOADED_PHRASES = (
    "overloaded",
    "internal server error",
    "connection error",
    "connection reset",
    "econnreset",
    "econnrefused",
    "etimedout",
    "fetch failed",
    "network",
    "socket hang up",
)
_RATE_PHRASES = ("rate limit", "rate_limit", "too many requests")


class LLMQuotaExhausted(RuntimeError):  # noqa: N818 — matches LLMBudgetExceeded's naming
    """The subscription usage window is exhausted (breaker engaged).

    NOT a hard stop (``llm.cli.is_hard_stop`` returns False): unlike
    ``LLMBudgetExceeded`` (an operator-set cap that degrading would mask), this
    is temporal and self-healing — pipelines should defer the item and retry
    next run, per the per-item degrade pattern. Measurement contexts are the
    exception: an eval/judge caller must ABORT on it (scoring under an
    exhausted quota measures the outage, not the subject) — see
    ``evals.rubric_judge``.
    """

    def __init__(self, message: str, *, blocked_until: datetime | None = None) -> None:
        super().__init__(message)
        self.blocked_until = blocked_until


@dataclass(frozen=True, slots=True)
class FailureInfo:
    """One classified transport failure."""

    kind: str
    detail: str  # the actual reason text (envelope `result` when available)
    retry_after: datetime | None = None  # naive-UTC; only set for usage_limit

    @property
    def ledger_prefix(self) -> str:
        return f"[{self.kind}]"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _extract_envelope_error(stdout: str) -> tuple[str | None, int | None]:
    """(result_message, api_error_status) from a CLI JSON envelope, if stdout
    is one. Returns (None, None) for non-JSON output."""
    try:
        payload: object = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    obj = cast("dict[str, object]", payload)
    result = obj.get("result")
    status = obj.get("api_error_status")
    return (
        result if isinstance(result, str) and result.strip() else None,
        int(status) if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def _failure_text(exc: BaseException) -> tuple[str, int | None]:
    """The most informative text a failure carries, plus the API status when
    the envelope names one.

    Priority: envelope ``result`` (the actual reason) > stderr > stdout HEAD >
    the exception message. The old code kept the stdout TAIL, which for a JSON
    envelope is the usage-stats block — the one part guaranteed to say nothing.
    """
    stdout = str(getattr(exc, "stdout", None) or "")
    stderr = str(getattr(exc, "stderr", None) or "").strip()
    if stdout.strip():
        result, status = _extract_envelope_error(stdout.strip())
        if result:
            return result, status
    if stderr:
        return stderr[:600], None
    if stdout.strip():
        return stdout.strip()[:600], None  # HEAD, not tail
    return str(exc)[:600], None


def _parse_reset_time(text: str) -> datetime | None:
    """Reset timestamp from a usage-limit message, when machine-readable.

    Only the epoch form (``...|1753305600``) is trusted — prose forms
    ("resets 3pm") are timezone-ambiguous and a wrong parse would block
    healthy hours. Unparseable ⇒ None ⇒ the caller uses the probe interval.
    """
    m = _EPOCH_RX.search(text)
    if not m:
        return None
    try:
        ts = datetime.fromtimestamp(int(m.group(1)), tz=UTC).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return None
    now = _now()
    if ts <= now:
        return None  # already past — stale message; probe normally
    if ts > now + timedelta(hours=MAX_BLOCK_HOURS):
        return now + timedelta(hours=MAX_BLOCK_HOURS)  # clamp garbled far-future stamps
    return ts


def classify_cli_failure(exc: BaseException) -> FailureInfo:
    """Classify one Claude-CLI call failure into a stable class + real reason."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return FailureInfo(TIMEOUT, f"subprocess timeout after {exc.timeout}s")

    text, status = _failure_text(exc)
    lowered = text.lower()
    if status is None:
        m = _TEXT_STATUS_RX.search(text)
        if m:
            status = int(m.group(1))

    # The envelope's numeric status is the most reliable signal when present.
    if status is not None:
        if status == 429:
            # 429 is ambiguous: plain rate limiting vs the subscription window.
            if any(p in lowered for p in _USAGE_PHRASES):
                return FailureInfo(USAGE_LIMIT, text[:600], retry_after=_parse_reset_time(text))
            return FailureInfo(RATE_LIMIT, text[:600])
        if status in (401, 403):
            return FailureInfo(AUTH, text[:600])
        if status == 404 or status == 400:
            return FailureInfo(CONFIG, text[:600])
        if status >= 500:
            return FailureInfo(OVERLOADED, text[:600])

    if any(p in lowered for p in _USAGE_PHRASES):
        return FailureInfo(USAGE_LIMIT, text[:600], retry_after=_parse_reset_time(text))
    if any(p in lowered for p in _AUTH_PHRASES):
        return FailureInfo(AUTH, text[:600])
    if any(p in lowered for p in _RATE_PHRASES):
        return FailureInfo(RATE_LIMIT, text[:600])
    if any(p in lowered for p in _OVERLOADED_PHRASES):
        return FailureInfo(OVERLOADED, text[:600])
    if (
        isinstance(exc, ValueError)
        or "empty `result`" in lowered
        or "did not return json" in lowered
    ):
        return FailureInfo(MALFORMED, text[:600])
    return FailureInfo(UNKNOWN, text[:600])


def retry_budget(kind: str) -> int:
    """Total ATTEMPTS (not retries) allowed for a failure class.

    * transient (overloaded / rate-limit / malformed / unknown): 3 attempts —
      a blip should not become a recorded error + a doomed fallback;
    * timeout: 2 — each attempt already costs the full timeout window;
    * usage_limit / auth / config: 1 — deterministic until something outside
      this process changes; retrying is pure waste.
    """
    if kind in (OVERLOADED, RATE_LIMIT, MALFORMED, UNKNOWN):
        return int(os.environ.get("LLM_CLI_MAX_ATTEMPTS", "3"))
    if kind == TIMEOUT:
        return 2
    return 1


# ---------------------------------------------------------------------------
# The quota circuit breaker (cross-process, file-based)
# ---------------------------------------------------------------------------


def _breaker_path() -> Path | None:
    """Next to the resolved DB so every process sees the same breaker. None
    when no DB is resolvable (tests with no DB → breaker inert, calls proceed)."""
    try:
        from db_paths import resolve_db_path

        db = resolve_db_path(None)
    except Exception:
        return None
    if db is None:
        return None
    return Path(db).parent / _BREAKER_FILENAME


def quota_block_active(*, path: Path | None = None) -> datetime | None:
    """The breaker's ``blocked_until`` if it is in the future, else None.

    Fail-open on every read problem: a corrupt/unreadable breaker file must
    never block calls — the worst case is the pre-breaker behavior (doomed
    subprocess spawns), never a stuck-closed transport.
    """
    p = path if path is not None else _breaker_path()
    if p is None or not p.exists():
        return None
    try:
        payload: object = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        raw = cast("dict[str, object]", payload).get("blocked_until")
        if not isinstance(raw, str):
            return None
        until = datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return None
    return until if until > _now() else None


def record_quota_exhausted(info: FailureInfo, *, path: Path | None = None) -> datetime:
    """Engage the breaker. Returns the blocked_until it wrote (or would have).

    Uses the classified reset time when the message carried one; else
    now + DEFAULT_PROBE_MINUTES. Atomic write (tmp + replace) — concurrent
    writers converge on either's value, both of which are valid."""
    until = info.retry_after or (_now() + timedelta(minutes=DEFAULT_PROBE_MINUTES))
    p = path if path is not None else _breaker_path()
    if p is None:
        return until
    payload = {
        "blocked_until": until.isoformat(),
        "reason": info.detail[:400],
        "set_at": _now().isoformat(),
    }
    try:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        log.warning(
            {
                "event": "llm_quota_breaker_engaged",
                "blocked_until": payload["blocked_until"],
                "reason": info.detail[:200],
            }
        )
    except OSError as exc:
        log.warning({"event": "llm_quota_breaker_write_failed", "error": str(exc)})
    return until


def clear_quota_block(*, path: Path | None = None) -> None:
    """Disengage after a successful call. Idempotent; failures are logged only
    (a stale file self-expires via its timestamp anyway)."""
    p = path if path is not None else _breaker_path()
    if p is None or not p.exists():
        return
    try:
        p.unlink()
        log.info({"event": "llm_quota_breaker_cleared"})
    except OSError as exc:
        log.warning({"event": "llm_quota_breaker_clear_failed", "error": str(exc)})
