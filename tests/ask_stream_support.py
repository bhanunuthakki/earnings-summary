"""Small helpers for exercising the sole production Ask transport in route tests."""

from __future__ import annotations

import json
from typing import cast

from ask.engine import fold_events


def parse_sse_events(response_text: str) -> list[dict[str, object]]:
    """Parse the data frames emitted by ``POST /api/ask/stream``."""
    return [
        cast("dict[str, object]", json.loads(line.removeprefix("data: ")))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def fold_sse_response(response_text: str) -> dict[str, object]:
    """Fold stream events for assertions that target the engine result shape."""
    events = parse_sse_events(response_text)
    session = next((event for event in events if event.get("type") == "session"), None)
    result = fold_events(event for event in events if event.get("type") != "session")
    if session is not None:
        result["session_id"] = session.get("session_id", "")
        if "session_revision" not in result and "session_revision" in session:
            result["session_revision"] = session["session_revision"]
    return result


__all__ = ["fold_sse_response", "parse_sse_events"]
