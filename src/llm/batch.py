"""Anthropic message-batches plumbing for off-hours bulk LLM work.

The interactive build path (build_artifacts, refresh_news, etc.) needs
synchronous LLM responses — analysts won't wait 24h for a brief. But the
overnight crons that produce SayDo pairs, decision audits, bear-case
gradings, and qa_topics regenerations all share a property: they run a
long list of independent prompts, each ~5-15s, with no inter-prompt
dependency. That's the canonical batch-API use case.

The Anthropic batch API trades latency (up to 24h) for cost (50% discount
on input + output tokens). For a portfolio with ~8 held names × 4 quarters
= ~32 SayDo pairs per quarterly cycle, that's roughly the difference
between a $4 monthly cost and a $2 monthly cost — modest in absolute terms
but the latency win (overnight batch vs blocking the morning report build
by 20-30 minutes) is the bigger lift.

This module provides the *preparation* side: convert a list of pair specs
into the Anthropic batches JSONL request format. The submission step
(`anthropic.messages.batches.create`) lives in the cron script that
invokes us — keeping the SDK boundary at the executable layer so the rest
of the codebase doesn't take a hard dependency on the Anthropic package
just to render a brief.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


# Model used for SayDo pair batch jobs. Matches the synchronous-path default
# (claude-sonnet-4-6) so the batched output is interchangeable with what the
# interactive build produces. Update both together.
SAYDO_BATCH_MODEL: str = "claude-sonnet-4-5-20250929"

# Per-message max_tokens. SayDo verdicts are typically 400-800 tokens. 2048
# is a comfortable ceiling without paying for over-allocation.
SAYDO_BATCH_MAX_TOKENS: int = 2048


@dataclass(frozen=True)
class BatchRequest:
    """One message-create request to be submitted as part of a batch.

    `custom_id` is the operator-supplied identifier the batch API echoes
    back on each result so the caller can match output to input. For SayDo
    pairs we use the canonical filename stem
    (SayDo_<TICKER>_Q<n>_<Y>_Q<n+1>_<Y>) so the result file landing
    matches what the synchronous path would have written.
    """

    custom_id: str
    prompt: str
    model: str = SAYDO_BATCH_MODEL
    max_tokens: int = SAYDO_BATCH_MAX_TOKENS


def to_jsonl_payload(req: BatchRequest) -> dict[str, object]:
    """Convert one BatchRequest to the Anthropic batches JSONL shape.

    The schema mirrors the SDK's
    `anthropic.types.message_create_params.MessageCreateParamsNonStreaming`
    expectations — caller passes the list of these dicts directly into
    `client.messages.batches.create(requests=[...])`.
    """
    return {
        "custom_id": req.custom_id,
        "params": {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": req.prompt,
                }
            ],
        },
    }


def write_jsonl(requests: Iterable[BatchRequest], out_path: Path) -> int:
    """Write a JSONL file at `out_path` with one request per line.

    Returns the count of requests written. Operator-friendly format —
    can be inspected with `wc -l`, diffed across days, piped to other
    tools.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for req in requests:
            fh.write(json.dumps(to_jsonl_payload(req)) + "\n")
            written += 1
    return written


def saydo_custom_id(
    *, ticker: str, prev_quarter: str, prev_year: int, curr_quarter: str, curr_year: int
) -> str:
    """Canonical custom_id for a SayDo pair batch request.

    Mirrors the .tmp filename the synchronous path writes so a batched run
    can drop results into the same path without filename collisions or
    custom-mapping logic.
    """
    return f"SayDo_{ticker}_{prev_quarter}_{prev_year}_{curr_quarter}_{curr_year}"
