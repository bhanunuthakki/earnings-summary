"""Tests for src/llm/batch.py — Anthropic message-batches request preparation.

The submission step (`anthropic.messages.batches.create`) lives in the cron
script and isn't covered here. These tests pin the JSONL shape so a future
SDK change forces a deliberate update rather than surprising the operator
with rejected batches at 2am.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.batch import (  # noqa: E402
    SAYDO_BATCH_MAX_TOKENS,
    SAYDO_BATCH_MODEL,
    BatchRequest,
    saydo_custom_id,
    to_jsonl_payload,
    write_jsonl,
)


def test_saydo_custom_id_matches_synchronous_filename_stem() -> None:
    """The batch path's custom_id must equal the .tmp filename stem the
    synchronous path writes — otherwise post-batch landing can't drop into
    the existing cache shape."""
    cid = saydo_custom_id(
        ticker="META",
        prev_quarter="Q3",
        prev_year=2025,
        curr_quarter="Q4",
        curr_year=2025,
    )
    assert cid == "SayDo_META_Q3_2025_Q4_2025"


def test_batch_request_jsonl_shape_matches_anthropic_schema() -> None:
    """Schema is fixed by the Anthropic API:
    {"custom_id": str, "params": {"model", "max_tokens", "messages"}}
    """
    req = BatchRequest(custom_id="SayDo_X", prompt="hello")
    payload = to_jsonl_payload(req)
    assert payload["custom_id"] == "SayDo_X"
    params = payload["params"]
    assert isinstance(params, dict)
    assert params["model"] == SAYDO_BATCH_MODEL
    assert params["max_tokens"] == SAYDO_BATCH_MAX_TOKENS
    messages = params["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0] == {"role": "user", "content": "hello"}


def test_write_jsonl_one_request_per_line(tmp_path: Path) -> None:
    requests = [
        BatchRequest(custom_id="SayDo_A", prompt="prompt A"),
        BatchRequest(custom_id="SayDo_B", prompt="prompt B"),
        BatchRequest(custom_id="SayDo_C", prompt="prompt C"),
    ]
    out = tmp_path / "batch.jsonl"
    written = write_jsonl(requests, out)
    assert written == 3
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    # Each line is parseable JSON with the expected custom_id.
    payloads = [json.loads(line) for line in lines]
    assert [p["custom_id"] for p in payloads] == ["SayDo_A", "SayDo_B", "SayDo_C"]


def test_write_jsonl_creates_parent_directory(tmp_path: Path) -> None:
    """The batch dir might not exist on first overnight cron tick — writer
    must mkdir(parents=True) without forcing the caller to do it."""
    nested = tmp_path / "fresh" / "subdir" / "batch.jsonl"
    written = write_jsonl([BatchRequest(custom_id="SayDo_X", prompt="x")], nested)
    assert written == 1
    assert nested.exists()


def test_batch_request_defaults_are_sane() -> None:
    """The default model + max_tokens must point at a current Claude model +
    a realistic SayDo verdict budget."""
    req = BatchRequest(custom_id="X", prompt="x")
    assert "claude" in req.model
    assert 500 <= req.max_tokens <= 8192


def test_pairwise_prompt_extracted_for_reuse() -> None:
    """The synchronous and batch paths must share the prompt body so the
    output is interchangeable. The extracted helper is the contract."""
    from llm_client import _build_pairwise_analysis_prompt

    prev = {"quarter": "Q1", "year": 2025, "text": "previous summary"}
    curr = {"quarter": "Q2", "year": 2025, "text": "current summary"}
    prompt = _build_pairwise_analysis_prompt(prev, curr, anchor_block="")
    # Spot-check the prompt is the SayDo prompt and contains both quarters.
    assert "Say-Do" in prompt or "Say" in prompt
    assert "Q1 2025" in prompt
    assert "Q2 2025" in prompt
    assert "previous summary" in prompt
    assert "current summary" in prompt
