"""Generic narrative streaming and diff extraction for durable Ask."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import cast

_DIFF_FENCE_RX = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def extract_diff(text: str) -> dict[str, object] | None:
    """Return a proposed diff from the bounded JSON fence, when present."""
    match = _DIFF_FENCE_RX.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    candidate = cast("dict[str, object]", payload).get("diff")
    return cast("dict[str, object]", candidate) if isinstance(candidate, dict) else None


def stream_llm_text(
    full_prompt: str,
    *,
    purpose: str = "ask_answer",
    allow_read: bool = False,
) -> Iterator[dict[str, object]]:
    """Stream through the canonical tool-free Ask policy seam.

    ``allow_read`` remains as a compatibility argument for existing callers,
    but is intentionally ignored: Ask retrieval is deterministic and the
    Claude subprocess must never receive file, shell, MCP, or plugin access.
    """
    from llm.cli import stream_llm

    del allow_read
    yield from stream_llm(
        full_prompt,
        purpose=purpose,
        scope="ask",
    )


__all__ = ["extract_diff", "stream_llm_text"]
