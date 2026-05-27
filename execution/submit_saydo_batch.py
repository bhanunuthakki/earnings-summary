"""Submit a pre-built SayDo Anthropic message-batches JSONL and drop results
into `.tmp/SayDo_*.txt`.

Companion to `execution/build_saydo_pairs.py --prepare-batch`. The prep step
writes a JSONL of pending SayDo pair requests into
`.tmp/saydo_batch/saydo_batch_<TIMESTAMP>.jsonl`. This script:

  1. Submits the JSONL to `client.messages.batches.create(requests=[...])`.
  2. Polls `client.messages.batches.retrieve(batch_id)` with backoff
     (30→60→120→300s, capped) until `processing_status == 'ended'`.
  3. Streams `client.messages.batches.results(batch_id)`, writing each
     succeeded result's assistant text to `.tmp/<custom_id>.txt`.
  4. Records one ledger row per result via `llm_call_ledger.record_call`
     (cost computed at the 50% batch discount).
  5. Surfaces errored / canceled / expired results to
     `.tmp/saydo_batch/errors_<batch_id>.json` for operator inspection
     rather than silent drop.

Pre-flight cost gate: estimates `n_requests * 7000 * 0.5 * $3/Mtok` against
the monthly `pairwise_analysis` cap (read from `llm_budgets`). Hard-halts
when the projected cost would push the month-to-date spend over cap.

Usage:
    python execution/submit_saydo_batch.py                  # most recent JSONL
    python execution/submit_saydo_batch.py --jsonl .tmp/saydo_batch/saydo_batch_X.jsonl
    python execution/submit_saydo_batch.py --dry-run        # validate + budget check, no submit
    python execution/submit_saydo_batch.py --force-overwrite

Out of scope: this script is SayDo-specific. If bear_case / qa_topics later
adopt the batch path, refactor the polling + ledger + cost-gate spine into a
generic `src/llm/batch_submit.py` module rather than copy-pasting this file.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from llm_budget import check_budget, current_month_spend  # noqa: E402
from llm_call_ledger import LlmCallRecord, record_call, sha256_text  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# Anthropic pricing for Sonnet 4.5/4.6 (input / output) per token.
# Used to compute per-result cost from the usage block on each batched
# message (the batch API does NOT return a per-result total_cost_usd field).
# Update together with SAYDO_BATCH_MODEL in src/llm/batch.py if pricing shifts.
INPUT_PRICE_PER_TOKEN: float = 3.0 / 1_000_000
OUTPUT_PRICE_PER_TOKEN: float = 15.0 / 1_000_000
# Batch-tier discount applied uniformly to input + output tokens.
BATCH_DISCOUNT: float = 0.5

# Pre-flight cost estimate: 7000 tokens/pair is a conservative upper bound
# for the SayDo prompt (anchor block + 2 summaries ≈ 5K tokens; output ≤ 2K
# capped by SAYDO_BATCH_MAX_TOKENS). Input-only estimate per the gate spec —
# output cost is tracked post-hoc on ledger rows.
PREFLIGHT_INPUT_TOKENS_PER_PAIR: int = 7000

# Poll cadence (seconds). Batches typically complete in 1-20 min for this
# workload but can take up to 24h on Anthropic's side — keep the cap modest
# so the operator sees regular heartbeats but doesn't burn watch-time.
POLL_BACKOFF_SECONDS: tuple[int, ...] = (30, 60, 120, 300)


# ---------------------------------------------------------------------------
# Anthropic SDK boundary — Protocols so tests can inject a fake client
# without depending on the Anthropic SDK's import at test-collection time.
# ---------------------------------------------------------------------------


class _BatchesAPI(Protocol):
    def create(self, *, requests: list[dict[str, object]]) -> Any: ...
    def retrieve(self, batch_id: str) -> Any: ...
    def results(self, batch_id: str) -> Iterable[Any]: ...


class _MessagesAPI(Protocol):
    @property
    def batches(self) -> _BatchesAPI: ...


class _AnthropicClient(Protocol):
    @property
    def messages(self) -> _MessagesAPI: ...


def _default_client_factory() -> _AnthropicClient:
    """Lazy import so the script (and its tests) don't require the SDK to be
    installed unless a real submission is attempted."""
    from anthropic import Anthropic

    return cast("_AnthropicClient", Anthropic())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    _sync_db_to_repo(repo_root)
    jsonl_path = args.jsonl.resolve() if args.jsonl else _find_latest_jsonl(repo_root)
    if jsonl_path is None or not jsonl_path.exists():
        print(
            json.dumps({
                "error": "no_jsonl_found",
                "looked_in": str((repo_root / ".tmp" / "saydo_batch").resolve()),
            }),
            file=sys.stderr,
        )
        return 1

    summary = submit_and_collect(
        jsonl_path,
        repo_root=repo_root,
        dry_run=args.dry_run,
        force_overwrite=args.force_overwrite,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") != "halted_cost_gate" else 2


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Path to JSONL produced by build_saydo_pairs.py --prepare-batch. "
        "Default: newest .tmp/saydo_batch/saydo_batch_*.jsonl under --repo-root.",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate JSONL + check budget + skip submission. No API calls made.",
    )
    p.add_argument(
        "--force-overwrite",
        action="store_true",
        help="Re-write .tmp/<custom_id>.txt even if it already exists. Default is "
        "skip-when-present so the synchronous path's prior writes aren't clobbered.",
    )
    return p.parse_args()


def _sync_db_to_repo(repo_root: Path) -> None:
    """Same pattern as build_artifacts._sync_db_to_repo — point db module
    paths at the chosen --repo-root so the budget reader + ledger writer
    hit the right portfolio.db."""
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def _find_latest_jsonl(repo_root: Path) -> Path | None:
    batch_dir = repo_root / ".tmp" / "saydo_batch"
    if not batch_dir.exists():
        return None
    candidates = sorted(batch_dir.glob("saydo_batch_*.jsonl"))
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Core (testable)
# ---------------------------------------------------------------------------


def submit_and_collect(
    jsonl_path: Path,
    *,
    repo_root: Path,
    dry_run: bool = False,
    force_overwrite: bool = False,
    client_factory: Callable[[], _AnthropicClient] = _default_client_factory,
    sleep: Callable[[float], None] = time.sleep,
    poll_backoff: tuple[int, ...] = POLL_BACKOFF_SECONDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Submit a SayDo batch JSONL and write results back to `.tmp/`.

    Returns the JSON summary dict the CLI emits to stdout. Injects
    `client_factory` + `sleep` + `now` so tests can drive the full state
    machine without real API calls or wall-clock waits.

    `dry_run=True` short-circuits before submission — useful for the
    operator's "what would this cost?" check.
    """
    tmp_dir = repo_root / ".tmp"
    batch_dir = tmp_dir / "saydo_batch"

    requests = _load_jsonl(jsonl_path)
    # Idempotent filtering: skip requests whose target file already exists
    # so a re-run after a partial sync-path write doesn't double-pay.
    pending = _filter_existing(requests, tmp_dir, force_overwrite=force_overwrite)
    skipped = len(requests) - len(pending)

    gate = _check_cost_gate(len(pending), repo_root=repo_root, now=now())
    if not gate["allowed"]:
        return {
            "status": "halted_cost_gate",
            "jsonl": str(jsonl_path),
            "requests_total": len(requests),
            "requests_pending": len(pending),
            "requests_skipped_existing": skipped,
            "cost_gate": gate,
        }

    if dry_run:
        return {
            "status": "dry_run_ok",
            "jsonl": str(jsonl_path),
            "requests_total": len(requests),
            "requests_pending": len(pending),
            "requests_skipped_existing": skipped,
            "estimated_cost_usd": gate["projected_cost_usd"],
            "cost_gate": gate,
        }

    if not pending:
        # Nothing to submit — every request already has a result file. Don't
        # hit the API; return a clean "noop" so cron paths don't false-error.
        return {
            "status": "noop",
            "jsonl": str(jsonl_path),
            "requests_total": len(requests),
            "requests_pending": 0,
            "requests_skipped_existing": skipped,
        }

    client = client_factory()
    payloads = [_request_to_payload(r) for r in pending]
    batch = client.messages.batches.create(requests=payloads)
    batch_id = cast("str", batch.id)
    print(
        f"[submit_saydo_batch] submitted batch_id={batch_id} "
        f"requests={len(pending)} (skipped_existing={skipped})",
        file=sys.stderr,
    )

    final_batch = _poll_until_ended(
        client, batch_id, sleep=sleep, poll_backoff=poll_backoff
    )

    counts = _write_results(
        client,
        batch_id,
        tmp_dir=tmp_dir,
        batch_dir=batch_dir,
        force_overwrite=force_overwrite,
        now=now,
        request_index={cast("str", r["custom_id"]): r for r in pending},
    )

    request_counts = _request_counts_dict(final_batch)
    return {
        "status": "ended" if request_counts.get("errored", 0) == 0 else "ended_with_errors",
        "batch_id": batch_id,
        "submitted": len(pending),
        "succeeded": counts["succeeded"],
        "failed": counts["errored"],
        "errored": counts["errored"],
        "expired": counts["expired"],
        "canceled": counts["canceled"],
        "output_dir": str(tmp_dir),
        "total_cost_usd_est": counts["total_cost_usd"],
        "requests_skipped_existing": skipped,
        "request_counts_final": request_counts,
    }


# ---------------------------------------------------------------------------
# JSONL load + idempotent filter
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse the JSONL, validating each line has the shape `to_jsonl_payload`
    produces. Surfaces malformed lines as ValueError so the operator catches
    a busted JSONL before paying for an API round-trip."""
    out: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{i} malformed JSON: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{i} expected JSON object, got {type(row).__name__}")
            row_typed = cast("dict[str, object]", row)
            cid = row_typed.get("custom_id")
            params = row_typed.get("params")
            if not isinstance(cid, str) or not isinstance(params, dict):
                raise ValueError(
                    f"{path}:{i} missing required keys custom_id+params; got {list(row_typed.keys())}"
                )
            out.append(row_typed)
    return out


def _filter_existing(
    requests: list[dict[str, object]], tmp_dir: Path, *, force_overwrite: bool
) -> list[dict[str, object]]:
    if force_overwrite:
        return requests
    out: list[dict[str, object]] = []
    for r in requests:
        cid = cast("str", r["custom_id"])
        target = tmp_dir / f"{cid}.txt"
        if target.exists():
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Cost gate
# ---------------------------------------------------------------------------


def _estimate_input_cost_usd(n_requests: int) -> Decimal:
    """User-spec estimate: n × 7000 tokens × 50% discount × $3/Mtok input.

    Output-token cost is realized post-hoc on the ledger; the gate only
    fronts input cost since output is bounded by SAYDO_BATCH_MAX_TOKENS and
    much smaller in practice."""
    raw = float(n_requests) * PREFLIGHT_INPUT_TOKENS_PER_PAIR
    discounted = raw * BATCH_DISCOUNT
    return Decimal(str(discounted * INPUT_PRICE_PER_TOKEN)).quantize(Decimal("0.0001"))


def _check_cost_gate(
    n_requests: int, *, repo_root: Path, now: datetime
) -> dict[str, object]:
    """Decide whether the projected batch cost would push pairwise_analysis
    over its monthly cap. Returns a dict; `allowed=False` halts submission.

    Fails open when the budget table isn't present — fresh repos or pre-
    migration installs shouldn't break the overnight cron, the budget guard
    is best-effort observability layered on top of a working pipeline.
    """
    db_path = repo_root / "data" / "portfolio.db"
    projected = _estimate_input_cost_usd(n_requests)
    spend = current_month_spend("pairwise_analysis", db_path=db_path, now=now)
    check = check_budget("pairwise_analysis", db_path=db_path, now=now)
    if check.cap <= 0:
        # No cap configured — allow.
        return {
            "allowed": True,
            "projected_cost_usd": str(projected),
            "current_spend_usd": str(spend),
            "cap_usd": str(check.cap),
            "headroom_pct": check.headroom_pct,
            "reason": None,
        }
    projected_total = spend + projected
    if projected_total > check.cap:
        return {
            "allowed": False,
            "projected_cost_usd": str(projected),
            "current_spend_usd": str(spend),
            "projected_total_usd": str(projected_total),
            "cap_usd": str(check.cap),
            "headroom_pct": check.headroom_pct,
            "reason": (
                f"pairwise_analysis spend ${spend} + projected ${projected} "
                f"= ${projected_total} would exceed monthly cap ${check.cap}"
            ),
        }
    return {
        "allowed": True,
        "projected_cost_usd": str(projected),
        "current_spend_usd": str(spend),
        "cap_usd": str(check.cap),
        "headroom_pct": check.headroom_pct,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Submission helpers
# ---------------------------------------------------------------------------


def _request_to_payload(req: dict[str, object]) -> dict[str, object]:
    """Strip JSONL row down to the shape `messages.batches.create` accepts.
    Both fields are validated upstream by `_load_jsonl`."""
    return {"custom_id": req["custom_id"], "params": req["params"]}


def _poll_until_ended(
    client: _AnthropicClient,
    batch_id: str,
    *,
    sleep: Callable[[float], None],
    poll_backoff: tuple[int, ...],
) -> Any:
    """Block on the batch's processing_status until it leaves `in_progress`.

    Anthropic terminal states for the BATCH-level status (not individual
    requests) are `ended` and (transitionally) `canceling`. Cancellation
    initiated by an operator transitions through `canceling` → `ended`, so
    we only short-circuit on `ended`.
    """
    poll_idx = 0
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = cast("str", batch.processing_status)
        counts = _request_counts_dict(batch)
        print(
            f"[submit_saydo_batch] poll batch_id={batch_id} status={status} "
            f"processing={counts.get('processing', 0)} "
            f"succeeded={counts.get('succeeded', 0)} "
            f"errored={counts.get('errored', 0)}",
            file=sys.stderr,
        )
        if status == "ended":
            return batch
        delay = poll_backoff[min(poll_idx, len(poll_backoff) - 1)]
        sleep(float(delay))
        poll_idx += 1


def _request_counts_dict(batch: Any) -> dict[str, int]:
    """Coerce the SDK's MessageBatchRequestCounts (a pydantic model) into a
    plain dict so the summary JSON serializes cleanly. Tests can also pass
    a plain dict here."""
    rc = batch.request_counts
    if isinstance(rc, dict):
        return cast("dict[str, int]", rc)
    return {
        "processing": getattr(rc, "processing", 0),
        "succeeded": getattr(rc, "succeeded", 0),
        "errored": getattr(rc, "errored", 0),
        "canceled": getattr(rc, "canceled", 0),
        "expired": getattr(rc, "expired", 0),
    }


# ---------------------------------------------------------------------------
# Result writing
# ---------------------------------------------------------------------------


def _write_results(
    client: _AnthropicClient,
    batch_id: str,
    *,
    tmp_dir: Path,
    batch_dir: Path,
    force_overwrite: bool,
    now: Callable[[], datetime],
    request_index: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Stream results, write succeeded text + ledger rows, collect errors.

    Returns aggregated counts + cost. Idempotent on file writes per the
    `force_overwrite` flag — the synchronous path may have raced ahead and
    written a target between prepare-batch and submit.
    """
    counts = {"succeeded": 0, "errored": 0, "expired": 0, "canceled": 0, "skipped_exists": 0}
    total_cost = Decimal("0")
    error_records: list[dict[str, object]] = []

    for item in client.messages.batches.results(batch_id):
        cid = cast("str", item.custom_id)
        result = item.result
        result_type = cast("str", result.type)

        if result_type == "succeeded":
            target = tmp_dir / f"{cid}.txt"
            if target.exists() and not force_overwrite:
                counts["skipped_exists"] += 1
                continue
            text = _extract_assistant_text(result.message)
            target.write_text(text, encoding="utf-8")
            counts["succeeded"] += 1
            cost = _record_batched_call(
                custom_id=cid,
                message=result.message,
                request=request_index.get(cid),
                response_text=text,
                now=now,
            )
            total_cost += cost
        elif result_type == "errored":
            counts["errored"] += 1
            err = getattr(result, "error", None)
            error_records.append({
                "custom_id": cid,
                "type": "errored",
                "error": _coerce_to_dict(err),
            })
        elif result_type == "expired":
            counts["expired"] += 1
            error_records.append({"custom_id": cid, "type": "expired"})
        elif result_type == "canceled":
            counts["canceled"] += 1
            error_records.append({"custom_id": cid, "type": "canceled"})
        else:
            # Unknown variant — record without crashing.
            error_records.append({
                "custom_id": cid,
                "type": f"unknown:{result_type}",
            })

    if error_records:
        batch_dir.mkdir(parents=True, exist_ok=True)
        errors_path = batch_dir / f"errors_{batch_id}.json"
        errors_path.write_text(
            json.dumps(error_records, indent=2, default=str), encoding="utf-8"
        )

    return {
        "succeeded": counts["succeeded"],
        "errored": counts["errored"],
        "expired": counts["expired"],
        "canceled": counts["canceled"],
        "skipped_exists": counts["skipped_exists"],
        "total_cost_usd": str(total_cost),
    }


def _extract_assistant_text(message: Any) -> str:
    """Concatenate all text-block contents from a Message. Mirrors what
    the synchronous Claude CLI path returns — a single string."""
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(cast("str", block.text))
    return "".join(parts)


def _coerce_to_dict(obj: Any) -> object:
    """Best-effort coercion of an SDK error object to a JSON-serializable
    dict. Pydantic models expose .model_dump(); anything else falls back to
    str()."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return cast("dict[str, object]", obj.model_dump())
        except Exception:
            pass
    if isinstance(obj, dict):
        return cast("dict[str, object]", obj)
    return str(obj)


def _record_batched_call(
    *,
    custom_id: str,
    message: Any,
    request: dict[str, object] | None,
    response_text: str,
    now: Callable[[], datetime],
) -> Decimal:
    """Write one ledger row for a succeeded batched message and return the
    computed cost (Decimal). Best-effort: the ledger writer swallows DB
    errors so a missing portfolio.db doesn't break submission."""
    usage = getattr(message, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_creation = getattr(usage, "cache_creation_input_tokens", None)
    cache_read = getattr(usage, "cache_read_input_tokens", None)

    cost_float = _batched_cost_usd(input_tokens, output_tokens)

    params = cast("dict[str, object]", request.get("params", {})) if request else {}
    model = cast("str", message.model if hasattr(message, "model") else params.get("model", "")) or "unknown"
    prompt_text = _prompt_text_from_params(params)

    ticker = _ticker_from_custom_id(custom_id)

    record_call(
        LlmCallRecord(
            called_at=now(),
            model=model,
            prompt_sha256=sha256_text(prompt_text) if prompt_text else "0" * 64,
            prompt_chars=len(prompt_text),
            elapsed_ms=0,  # batch latency isn't per-result
            cache_hit=False,
            purpose="pairwise_analysis",
            ticker=ticker,
            scope=f"batch:saydo:{custom_id}",
            response_sha256=sha256_text(response_text),
            response_chars=len(response_text),
            input_tokens=input_tokens or None,
            cache_creation_input_tokens=cast("int | None", cache_creation),
            cache_read_input_tokens=cast("int | None", cache_read),
            output_tokens=output_tokens or None,
            cost_estimate_usd=cost_float,
            fallback_used=None,
        )
    )
    return Decimal(str(cost_float)).quantize(Decimal("0.0001"))


def _batched_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Cost at the 50% batch tier, computed locally — the SDK does not
    surface a per-result total_cost_usd on batched messages."""
    return (
        input_tokens * INPUT_PRICE_PER_TOKEN
        + output_tokens * OUTPUT_PRICE_PER_TOKEN
    ) * BATCH_DISCOUNT


def _prompt_text_from_params(params: dict[str, object]) -> str:
    """Reconstruct the user-message text from the batch params block, so the
    ledger's prompt_sha256 + prompt_chars match what build_saydo_pairs.py's
    synchronous path would have recorded."""
    messages_raw = params.get("messages")
    if not isinstance(messages_raw, list) or not messages_raw:
        return ""
    messages = cast("list[object]", messages_raw)
    first = messages[0]
    if not isinstance(first, dict):
        return ""
    content = cast("dict[str, object]", first).get("content")
    if isinstance(content, str):
        return content
    return ""


def _ticker_from_custom_id(custom_id: str) -> str | None:
    """Custom_id shape (from `saydo_custom_id`):
    `SayDo_<TICKER>_<prev_q>_<prev_y>_<curr_q>_<curr_y>`. Parse out the
    TICKER so the ledger row is filterable in the spend dashboard."""
    parts = custom_id.split("_")
    if len(parts) < 2 or parts[0] != "SayDo":
        return None
    return parts[1]


if __name__ == "__main__":
    raise SystemExit(main())
