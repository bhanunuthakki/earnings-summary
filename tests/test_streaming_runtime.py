from __future__ import annotations

import queue
import threading
from collections.abc import Iterator

from server_runtime.streaming import drain_events


def _failing_events() -> Iterator[dict[str, object]]:
    yield {"type": "delta", "text": "safe"}
    raise RuntimeError("provider failed?api_key=secret-value")


def test_stream_errors_do_not_expose_exception_details() -> None:
    chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=8)
    drain_events(_failing_events(), chunks, threading.Event())
    assert chunks.get_nowait() == {"type": "delta", "text": "safe"}
    assert chunks.get_nowait() == {
        "type": "error",
        "error": "chat stream failed; retry the request",
    }
    assert chunks.get_nowait() is None


def test_cancelled_stream_stops_before_consuming_events() -> None:
    consumed: list[int] = []

    def events() -> Iterator[dict[str, object]]:
        consumed.append(1)
        yield {"type": "delta"}

    stop = threading.Event()
    stop.set()
    chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=2)
    drain_events(events(), chunks, stop)
    assert consumed == []
    assert chunks.get_nowait() is None
