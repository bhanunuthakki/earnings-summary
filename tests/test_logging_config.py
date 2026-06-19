"""Structured logging + correlation-id helpers (sre-4, 2026-06-18 refresh).

configure_logging() itself is NOT exercised here — it mutates the root logger
and would fight pytest's capture — so we test the pieces it installs: the JSON
formatter, the correlation contextvar helpers, and the filter that stamps the id.
"""

from __future__ import annotations

import json
import logging

import logging_config
from logging_config import (
    JsonLogFormatter,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)


def _record(msg: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="a.b", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None
    )


def test_correlation_id_helpers() -> None:
    cid = new_correlation_id()
    assert get_correlation_id() == cid
    assert len(cid) == 12
    set_correlation_id("manual-id")
    assert get_correlation_id() == "manual-id"


def test_filter_stamps_current_correlation_id() -> None:
    # vars()[...] reaches the module-private filter without tripping pyright
    # reportPrivateUsage (strict) or ruff B009.
    correlation_filter = vars(logging_config)["_CorrelationFilter"]()
    set_correlation_id("stamp-me")
    rec = _record("hello")
    assert correlation_filter.filter(rec) is True
    assert rec.__dict__["correlation_id"] == "stamp-me"


def test_json_formatter_emits_structured_line() -> None:
    set_correlation_id("cid-123")
    rec = _record("hello %s")
    rec.args = ("world",)
    rec.__dict__["correlation_id"] = get_correlation_id()
    payload = json.loads(JsonLogFormatter().format(rec))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "a.b"
    assert payload["correlation_id"] == "cid-123"
    assert payload["msg"] == "hello world"


def test_json_formatter_preserves_dict_message() -> None:
    # Many call sites log a dict (e.g. lens events) — its content must survive.
    rec = _record({"event": "lens_cache_hit", "lens": "bull_case"})
    payload = json.loads(JsonLogFormatter().format(rec))
    assert "lens_cache_hit" in payload["msg"]


def test_json_formatter_defaults_correlation_id_when_unset() -> None:
    # A record that never passed through the filter still formats (default "-").
    payload = json.loads(JsonLogFormatter().format(_record("x")))
    assert payload["correlation_id"] == "-"
