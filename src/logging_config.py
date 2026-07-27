"""Structured logging + correlation IDs (sre-4, 2026-06-18 hardening refresh).

The repo's ~191 freeform ``logging.basicConfig`` / logger call sites produce
inconsistent, hard-to-correlate output. ``configure_logging`` installs ONE
structured formatter on the root logger so every module's
``logging.getLogger(__name__)`` output is uniform and greppable, and a
contextvar-backed **correlation id** rides every record — bound once per HTTP
request (``comments_server``) or per pipeline run so a single operation's log
lines can be stitched together after the fact.

Opt-in at the entrypoint: call ``configure_logging()`` from a ``main()``.
Existing ``getLogger(__name__)`` call sites need no change — they inherit the
root config. Idempotent, so multiple entrypoints can call it safely. Tests do
NOT call it (it would fight pytest's log capture); the per-request correlation
binder is harmless without it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar

from log_redact import redact

__all__ = [
    "JsonLogFormatter",
    "RedactingFormatter",
    "configure_logging",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
]

_CORRELATION_ID: ContextVar[str] = ContextVar("correlation_id", default="-")
_configured = False


def new_correlation_id() -> str:
    """Generate, bind, and return a fresh short correlation id for this context."""
    cid = uuid.uuid4().hex[:12]
    _CORRELATION_ID.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    _CORRELATION_ID.set(cid)


def get_correlation_id() -> str:
    return _CORRELATION_ID.get()


class _CorrelationFilter(logging.Filter):
    """Stamp the current contextvar correlation id onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Assign via __dict__ so the new attribute is visible to %()s formatting
        # without tripping pyright (unknown LogRecord attr) or ruff (setattr-const).
        record.__dict__["correlation_id"] = _CORRELATION_ID.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """One JSON object per log line: ts, level, logger, correlation_id, msg.

    ``msg`` is the rendered message (many call sites log a dict — its repr is
    kept verbatim). Exceptions fold into an ``exc`` field; ``default=str`` keeps
    a stray non-serializable value from crashing the log call.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class RedactingFormatter(logging.Formatter):
    """Plain-text formatter that masks credentials in messages and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(*, level: int | str | None = None, json_format: bool | None = None) -> None:
    """Install the structured root logging config. Idempotent.

    ``level`` defaults to env ``LOG_LEVEL`` (else ``INFO``). ``json_format``
    defaults to env ``LOG_FORMAT == "json"`` (else a plain correlated line —
    friendlier in a local terminal). Replaces the root handlers so a prior
    ``basicConfig`` does not double-emit.
    """
    global _configured
    if _configured:
        return
    resolved_level: int | str = level if level is not None else os.environ.get("LOG_LEVEL", "INFO")
    use_json = json_format if json_format is not None else os.environ.get("LOG_FORMAT") == "json"

    handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_CorrelationFilter())
    if use_json:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            RedactingFormatter(
                "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
            )
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)
    _configured = True
