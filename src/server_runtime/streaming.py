"""Bounded, cancellable bridge from blocking event iterators to HTTP streams."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator

from log_redact import redact

_LOGGER = logging.getLogger(__name__)
_PUT_TIMEOUT_SECONDS = 0.1


def _put(
    chunks: queue.Queue[dict[str, object] | None],
    item: dict[str, object] | None,
    stop: threading.Event,
    *,
    final: bool = False,
) -> bool:
    while not stop.is_set():
        try:
            chunks.put(item, timeout=_PUT_TIMEOUT_SECONDS)
            return True
        except queue.Full:
            continue
    if final:
        try:
            chunks.put_nowait(item)
            return True
        except queue.Full:
            pass
    return False


def drain_events(
    events: Iterator[dict[str, object]],
    chunks: queue.Queue[dict[str, object] | None],
    stop: threading.Event,
) -> None:
    """Pump events without unbounded memory growth or client-visible exceptions."""
    try:
        while not stop.is_set():
            try:
                chunk = next(events)
            except StopIteration:
                break
            if not _put(chunks, chunk, stop):
                break
    except Exception as exc:
        _LOGGER.error("chat stream failed: %s", redact(exc))
        _put(
            chunks,
            {"type": "error", "error": "chat stream failed; retry the request"},
            stop,
        )
    finally:
        _put(chunks, None, stop, final=True)
