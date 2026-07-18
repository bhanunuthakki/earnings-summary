"""In-report chatbot session — context assembly + streaming LLM call + thread storage + apply-diff.

Backed by the canonical Claude CLI subprocess wrapper. Streams tokens via
`claude -p --output-format stream-json` so the frontend can render
incrementally. Threads are persisted to
`data/report_chats/<TICKER>/<YYYY-MM-DD>.json` per-report (matches comment
lifetime preference).

The chatbot has filesystem read access (per user recommendation Q3) scoped
to `data/`, `micro_thesis/`, `.tmp/`, `transcripts/` — exposed as a small
tool the LLM can invoke ("look up NU's actual Q2 2025 NPL ratio from the
transcript") via `--allowedTools Read`. The CLI handles the tool loop;
this module just sets the system prompt and harvests the final text.

Public surface (consumed by execution/comments_server.py):

  build_chat_response.load_thread(repo_root, ticker, report_date) -> list[ThreadTurn]
  build_chat_response.stream_response(repo_root, ticker, report_date, user_message)
      -> generator yielding dicts: {type: "delta"|"final"|"diff_proposal"|"error", ...}
  apply_chat_diff(repo_root, ticker, report_date, diff, dry_run) -> dict
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# We intentionally import the renderer + builder lazily inside functions to
# avoid pulling the entire report graph at module-import time (the renderer
# imports a lot of submodules).

ChatRole = Literal["user", "assistant", "system"]


class ChatTurn(BaseModel):
    role: ChatRole
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    proposed_diff: dict[str, object] | None = None


class ChatStore(BaseModel):
    ticker: str
    report_date: date
    thread: list[ChatTurn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _chat_path(repo_root: Path, ticker: str, report_date: date) -> Path:
    out = repo_root / "data" / "report_chats" / ticker.upper()
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{report_date.isoformat()}.json"


def load_thread(repo_root: Path, ticker: str, report_date: date) -> list[ChatTurn]:
    path = _chat_path(repo_root, ticker, report_date)
    if not path.exists():
        return []
    try:
        store = ChatStore.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return store.thread


def save_thread(repo_root: Path, ticker: str, report_date: date, thread: list[ChatTurn]) -> None:
    path = _chat_path(repo_root, ticker, report_date)
    store = ChatStore(ticker=ticker.upper(), report_date=report_date, thread=thread)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# System-prompt assembly from ReportSpec
# ---------------------------------------------------------------------------


def _compact_report_context(repo_root: Path, ticker: str) -> str:
    """Build a compact text snapshot of the report's analytical content.

    Pulls from on-disk caches rather than re-running the section builders
    so the chat is cheap (no DB queries, no LLM calls)."""
    bits: list[str] = []

    # Thesis
    holdings = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if holdings.exists():
        try:
            payload = cast("dict[str, object]", json.loads(holdings.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            payload = {}
        thesis = payload.get("thesis") or payload.get("thesis_full")
        if isinstance(thesis, str):
            bits.append(f"### THESIS\n{thesis}\n")
        rules = payload.get("business_model_rules")
        if isinstance(rules, list):
            ledger_bits = []
            for r in rules:
                if isinstance(r, dict):
                    narrative = r.get("narrative")
                    if isinstance(narrative, str):
                        ledger_bits.append(f"- {narrative}")
            if ledger_bits:
                bits.append("### THESIS-BREAKERS\n" + "\n".join(ledger_bits))
        kpis = payload.get("tier_1_kpis")
        if isinstance(kpis, list):
            kpi_lines = []
            for k in kpis:
                if isinstance(k, dict) and isinstance(k.get("name"), str):
                    kpi_lines.append(
                        f"- **{k['name']}** — breaks if {k.get('break_condition') or '—'}"
                    )
            if kpi_lines:
                bits.append("### TIER-1 KPIs\n" + "\n".join(kpi_lines))

    # Bear case
    bear = repo_root / "data" / "bear_case" / f"{ticker.upper()}.json"
    if bear.exists():
        try:
            b = cast("dict[str, object]", json.loads(bear.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            b = {}
        underweighted = b.get("most_underweighted")
        if isinstance(underweighted, str):
            bits.append(f"### BEAR CASE — MOST UNDERWEIGHTED\n{underweighted[:1500]}\n")
        fms = b.get("failure_modes")
        if isinstance(fms, list):
            fm_lines = []
            for fm in fms[:5]:
                if isinstance(fm, dict) and isinstance(fm.get("hypothesis"), str):
                    fm_lines.append(f"- {fm['hypothesis']}")
            if fm_lines:
                bits.append("### NAMED FAILURE MODES\n" + "\n".join(fm_lines))

    # Valuation
    val = repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json"
    if val.exists():
        try:
            v = cast("dict[str, object]", json.loads(val.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            v = {}
        mult = v.get("multiple_name")
        display = v.get("current_value_display")
        verdict = v.get("rich_cheap_verdict")
        rationale = v.get("rationale")
        if mult:
            line = f"{mult}: {display or '—'}"
            if verdict:
                line += f" — {verdict}"
            bits.append(f"### VALUATION\n{line}")
            if isinstance(rationale, str):
                bits.append(f"Rationale: {rationale[:400]}")

    # Company description
    cd = repo_root / "data" / "company_description" / f"{ticker.upper()}.json"
    if cd.exists():
        try:
            c = cast("dict[str, object]", json.loads(cd.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            c = {}
        ep = c.get("elevator_pitch")
        if isinstance(ep, str):
            bits.append(f"### ELEVATOR PITCH\n{ep[:600]}")

    return "\n\n".join(bits) if bits else "(no cached context on file)"


def _prior_threads_context(
    repo_root: Path,
    ticker: str,
    report_date: date,
    max_turns: int = 6,
    char_cap: int = 1500,
) -> str:
    """Compact tail of the most recent EARLIER build's chat thread.

    Chat files live per (ticker, report_date); without this, every new report
    build starts the conversation amnesiac. Picking the latest file dated
    strictly before this report (ISO stems sort chronologically) gives the
    assistant the thread of what was discussed last build. Best-effort: any
    missing/corrupt file degrades to "" — chat must keep working on a
    first-ever build.
    """
    base = repo_root / "data" / "report_chats" / ticker.upper()
    if not base.is_dir():
        return ""
    try:
        candidates = sorted(p for p in base.glob("*.json") if p.stem < report_date.isoformat())
    except OSError:
        return ""
    if not candidates:
        return ""
    prior = candidates[-1]
    try:
        store = ChatStore.model_validate_json(prior.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    turns = [t for t in store.thread if t.role in ("user", "assistant") and t.text.strip()]
    if not turns:
        return ""
    lines: list[str] = []
    for t in turns[-max_turns:]:
        flat = " ".join(t.text.split())
        if len(flat) > 220:
            flat = flat[:217].rstrip() + "..."
        lines.append(f"[{t.role.upper()}] {flat}")
    block = (
        f"### PRIOR DISCUSSION (your chat on the {prior.stem} report — "
        f"continue from it, don't restart)\n" + "\n".join(lines)
    )
    return block if len(block) <= char_cap else block[:char_cap].rstrip() + "\n[...truncated]"


def _system_prompt(repo_root: Path, ticker: str, report_date: date) -> str:
    context = _compact_report_context(repo_root, ticker)

    # Durable analyst memory: open notes (questions / watch-items / assumptions
    # from analyst_notes) + the tail of the previous build's chat. Both are
    # best-effort — lazy import keeps module import light, and any failure
    # degrades to an absent section rather than a broken chat.
    memory_bits: list[str] = []
    try:
        from llm.anchors import load_priors_anchor

        priors = load_priors_anchor(repo_root, ticker)
    except Exception:
        priors = ""
    if priors:
        memory_bits.append(priors)
        memory_bits.append(
            "When your answer resolves one of the open questions above, say so "
            'explicitly ("this answers your open question about ...") so the '
            "analyst can mark it resolved. When evidence touches a watch-item "
            "or assumption, call that out by name."
        )
    # The owner's Worldview (standing Tenets) as soft priors — spotlight-wrapped
    # (Tenets distil from captured musings, so treat as untrusted content) and inert
    # until LEDGER_WORLDVIEW_ANCHOR is on. This site composes anchors by hand rather
    # than via compose_anchor_block, so wrap it here.
    try:
        from llm.anchors import load_worldview_anchor
        from llm.untrusted import spotlight

        raw_worldview = load_worldview_anchor(repo_root)
        worldview = (
            spotlight(raw_worldview, source="the investor's Worldview tenets")
            if raw_worldview
            else ""
        )
    except Exception:
        worldview = ""
    if worldview:
        memory_bits.append(worldview)
    # The owner's affirmed profile facts (capacity/appetite/behavioral) —
    # same hand-composed spotlight treatment as Worldview above, since this
    # site doesn't route through compose_anchor_block. Inert until at least
    # one fact is affirmed (§7.1 gated assertion) — no env flag needed.
    try:
        from llm.anchors import load_owner_profile_anchor
        from llm.untrusted import spotlight

        raw_owner_profile = load_owner_profile_anchor(repo_root)
        owner_profile = (
            spotlight(raw_owner_profile, source="the owner's affirmed profile facts")
            if raw_owner_profile
            else ""
        )
    except Exception:
        owner_profile = ""
    if owner_profile:
        memory_bits.append(owner_profile)
    prior_chat = _prior_threads_context(repo_root, ticker, report_date)
    if prior_chat:
        memory_bits.append(prior_chat)
    memory_block = ("\n\n" + "\n\n".join(memory_bits)) if memory_bits else ""

    return f"""You are an analyst assistant for {ticker}, embedded in the
workspace research report dated {report_date.isoformat()}. Answer the
analyst's questions using the cached report context below as the primary
source. When you need data that isn't in the context, use the Read tool
to look it up — you have read access to:

- `data/historical/fmp/<T>_*.json` — FMP financial data (segments,
  ratios, key_metrics, statements)
- `data/company_description/<T>.json`, `data/bear_case/<T>.json`,
  `data/valuation_basis/<T>.json` — cached LLM outputs
- `micro_thesis/holdings/<T>.json` — the analyst's thesis + KPIs
- `.tmp/<T>_Q<N>_<YYYY>_summary.txt`, `.tmp/<T>_Q<N>_<YYYY>_press_release_summary.txt`,
  `.tmp/<T>_Q<N>_<YYYY>_presentation_brief.txt` — per-quarter LLM summaries
- `transcripts/{{processed,raw}}/<T>_Q<N>_<YYYY>.txt` — raw call transcripts

CACHED REPORT CONTEXT (snapshot):

{context}{memory_block}

When the analyst asks you to **edit** something (e.g. "rewrite the thesis
assuming Mexico interchange caps at 1.5%", "drop this KPI", "tighten the
valuation rationale"), respond with both:

1. A natural-language explanation of what you'd change and why.
2. A JSON code-fenced block at the end of your message with the proposed
   diff in this exact shape:

```json
{{
  "diff": {{
    "target_file": "<repo-relative path>",
    "target_path": "<json-pointer or section name>",
    "old_value": "<verbatim text or value being replaced>",
    "new_value": "<the replacement>",
    "summary": "<one-sentence what changed>"
  }}
}}
```

The user can then choose to apply or reject. Don't propose a diff unless
the user explicitly asks for an edit.

When the analyst asks for a STANCE on this holding (buy/add/hold/trim/sell,
"what would you do", "should I size up") — do NOT give one here. Stances
exist only through the Socratic think-through, which asks THEIR read first
and records the result for outcome scoring. Point them to it:
http://localhost:7421/socratic/{ticker} (also under Portfolio -> Memos).
You may still discuss evidence freely — the restriction is on stances.

The NEW-NAME discovery queue (Research -> Discovery) has deterministic
chat commands the server handles directly (no LLM): "/discovery list",
"/discovery queue <T>", "/discovery dismiss <T>", "/discovery build <T>"
(an eval build is ~25 min + LLM spend; the command is the approval).
Mention these when the analyst asks about finding or evaluating new names.
Metric questions ("revenue growth last 8 quarters") render as live data
views automatically; "/view <question>" forces that path.
"""


# ---------------------------------------------------------------------------
# Streaming LLM call
# ---------------------------------------------------------------------------


_DIFF_FENCE_RX = None  # lazy compile


def _extract_diff(text: str) -> dict[str, object] | None:
    """Pull the optional ```json ... ``` diff block out of a chat response."""
    import re

    global _DIFF_FENCE_RX
    if _DIFF_FENCE_RX is None:
        _DIFF_FENCE_RX = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)
    m = _DIFF_FENCE_RX.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, object]", payload).get("diff") if "diff" in payload else None


# Public names — the ask engine extracts diffs from portfolio-scoped
# narrative answers it composes itself, and the S7 evidence loop rebuilds
# the report-drawer prompt for ledger-attributed follow-up calls
# (src/ask/engine.py).
extract_diff = _extract_diff
build_system_prompt = _system_prompt


def stream_llm_text(
    full_prompt: str, *, purpose: str = "ask_answer"
) -> Iterator[dict[str, object]]:
    """Low-level transport: stream one assembled prompt through the claude
    CLI. Yields {type: "delta", text} per chunk, then exactly one of
    {type: "final", text: "<full>"} or {type: "error", error: "..."}.
    No thread storage, no diff extraction — callers own session semantics
    (this module's `stream_response` for the per-report thread; the ask
    engine's portfolio scope composes its own prompt over it).

    The conversational answer is the most expensive LLM call in the repo, so —
    unlike before — it no longer rides the bare CLI default. It resolves its
    model through the model-downgrade / Gemini-promotion loop (``purpose``
    defaults to ``ask_answer``: ``_model_for`` consults ``model_pin_overrides``
    -> ``LLM_MODELS`` -> ``DEFAULT_MODEL``), enforces the per-purpose monthly
    budget (seeded soft/warn — an interactive answer is never hard-blocked
    mid-conversation), and records a best-effort ``llm_calls`` ledger row so it
    shows in Call Health like every other purpose. A purpose promoted to a
    Gemini model (which the CLI can't stream) degrades to a single buffered
    ``call_llm`` answer."""
    from llm.cli import (
        LLMBudgetExceeded,
        _enforce_budget_pre_call,  # pyright: ignore[reportPrivateUsage]
        _model_for,  # pyright: ignore[reportPrivateUsage]
    )
    from llm.model_ladder import GEMINI as _GEMINI_FAMILY
    from llm.model_ladder import family_of

    model = _model_for(purpose)

    # Per-purpose budget: ask_answer is seeded soft (warn), so this only raises
    # if an operator later hard-blocks it — then degrade with an explicit error
    # frame rather than a crashed stream.
    try:
        _enforce_budget_pre_call(purpose, force_budget_bypass=False)
    except LLMBudgetExceeded:
        yield {
            "type": "error",
            "error": "Ask monthly budget reached — raise the cap or wait for the reset.",
        }
        return

    # A Gemini-promoted purpose can't stream through `claude -p`; buffer one
    # answer through the canonical client (which owns the Gemini backend, its
    # operational fallback to Claude, and its own ledger row).
    if family_of(model) == _GEMINI_FAMILY:
        yield from _buffered_llm_answer(full_prompt, purpose=purpose)
        return

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        yield {"type": "error", "error": "claude CLI not found in PATH"}
        return

    # Filesystem read scope is enforced by --allowedTools Read; the LLM can
    # only read, not write. (Comments + chat writes go through Flask endpoints
    # we control, not through the CLI's Edit tool.) --model pins the resolved
    # downgrade-loop choice instead of the CLI's ambient default.
    cmd = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--output-format",
        "stream-json",
        # The conversational turn never --resume-s (the whole thread is
        # re-encoded into the prompt each turn), so persisting a CLI session
        # transcript is pure waste — skip the per-turn disk write. (L14.)
        "--no-session-persistence",
        "--allowedTools",
        "Read",
        "--verbose",
    ]
    started_at = datetime.now(UTC)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        yield {"type": "error", "error": f"failed to launch claude: {e}"}
        return

    assert proc.stdin and proc.stdout
    proc.stdin.write(full_prompt)
    proc.stdin.close()

    full_text_parts: list[str] = []
    result_meta: dict[str, object] | None = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            # The CLI emits a stream of `{type: ..., ...}` envelopes. We
            # extract assistant text deltas and capture the final `result`
            # envelope (token usage + cost) for the ledger row.
            if etype == "assistant":
                msg = event.get("message") or {}
                content = msg.get("content") or []
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        chunk = block.get("text") or ""
                        if chunk:
                            full_text_parts.append(chunk)
                            yield {"type": "delta", "text": chunk}
            elif etype == "result" and isinstance(event, dict):
                result_meta = cast("dict[str, object]", event)
    finally:
        proc.wait()

    full_text = "".join(full_text_parts).strip()
    if not full_text:
        stderr_text = (proc.stderr.read() if proc.stderr else "").strip()[:300]
        _record_stream_call(
            purpose, model, full_prompt, "", started_at, error=f"empty: {stderr_text[:160]}"
        )
        yield {"type": "error", "error": f"empty response (stderr: {stderr_text})"}
        return

    _record_stream_call(purpose, model, full_prompt, full_text, started_at, meta=result_meta)
    yield {"type": "final", "text": full_text}


def _buffered_llm_answer(full_prompt: str, *, purpose: str) -> Iterator[dict[str, object]]:
    """Non-streaming answer for a Gemini-promoted ask purpose: one ``call_llm``
    round-trip (its own backend selection + operational fallback + ledger row),
    surfaced as a single delta + final so streaming clients still render it."""
    from llm.cli import call_llm

    try:
        text = call_llm(full_prompt, purpose=purpose, scope="ask").strip()
    except Exception as exc:
        yield {"type": "error", "error": f"answer failed: {type(exc).__name__}"}
        return
    if not text:
        yield {"type": "error", "error": "empty response"}
        return
    yield {"type": "delta", "text": text}
    yield {"type": "final", "text": text}


def _record_stream_call(
    purpose: str,
    model: str,
    prompt: str,
    response: str,
    started_at: datetime,
    *,
    error: str | None = None,
    meta: dict[str, object] | None = None,
) -> None:
    """Best-effort ``llm_calls`` ledger row for the streamed conversational
    answer. The streaming transport bypasses ``call_llm``, so it records its
    own row (token usage + cost lifted from the CLI's final ``result``
    envelope when present). Never raises — a ledger miss must not break the
    answer that already streamed."""
    try:
        from llm_call_ledger import (
            LlmCallRecord,
            record_call,
            sha256_text,
            usage_from_json_meta,
        )

        usage: dict[str, int | float | None] = (
            usage_from_json_meta(meta) if isinstance(meta, dict) else {}
        )
        elapsed_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
        record_call(
            LlmCallRecord(
                called_at=started_at,
                model=model,
                prompt_sha256=sha256_text(prompt),
                prompt_chars=len(prompt),
                elapsed_ms=elapsed_ms,
                purpose=purpose,
                scope="ask",
                response_sha256=sha256_text(response) if response else None,
                response_chars=len(response) if response else None,
                error=error,
                input_tokens=cast("int | None", usage.get("input_tokens")),
                cache_creation_input_tokens=cast(
                    "int | None", usage.get("cache_creation_input_tokens")
                ),
                cache_read_input_tokens=cast("int | None", usage.get("cache_read_input_tokens")),
                output_tokens=cast("int | None", usage.get("output_tokens")),
                cost_estimate_usd=cast("float | None", usage.get("cost_estimate_usd")),
            )
        )
    except Exception as exc:
        log.debug({"event": "ask_stream_ledger_skipped", "error": f"{type(exc).__name__}: {exc}"})


def stream_response(
    repo_root: Path,
    ticker: str,
    report_date: date,
    user_message: str,
    extra_context: str = "",
) -> Iterator[dict[str, object]]:
    """Stream a chat response. Yields:
    {type: "delta", text: "<chunk>"}      — incremental tokens
    {type: "final", text: "<full>"}       — once at end
    {type: "diff_proposal", diff: {...}}  — if the response contained a diff block
    {type: "error", error: "..."}         — on failure

    ``extra_context`` is an optional turn-scoped block appended to the
    system prompt — the ask engine passes retrieved evidence (Ask v3
    grounding) through it. It is not persisted with the thread.
    """
    thread = load_thread(repo_root, ticker, report_date)
    thread.append(ChatTurn(role="user", text=user_message))

    system_prompt = _system_prompt(repo_root, ticker, report_date)
    if extra_context:
        system_prompt = system_prompt + "\n\n" + extra_context

    # Assemble the prior conversation as a single user message body. The
    # CLI runs single-turn, so we encode the thread into the prompt itself.
    thread_text = "\n\n".join(f"[{t.role.upper()}] {t.text}" for t in thread[:-1])
    user_block = thread[-1].text
    full_prompt = (
        system_prompt
        + "\n\n---\n\nPRIOR THREAD:\n"
        + (thread_text or "(first turn)")
        + "\n\n---\n\nUSER:\n"
        + user_block
    )

    full_text: str | None = None
    for event in stream_llm_text(full_prompt):
        kind = event.get("type")
        if kind == "final":
            full_text = cast("str", event["text"])
        else:
            yield event
            if kind == "error":
                return
    if full_text is None:  # defensive: transport always ends in final or error
        return

    diff = _extract_diff(full_text)
    assistant_turn = ChatTurn(role="assistant", text=full_text, proposed_diff=diff)
    thread.append(assistant_turn)
    save_thread(repo_root, ticker, report_date, thread)

    yield {"type": "final", "text": full_text}
    if diff is not None:
        yield {"type": "diff_proposal", "diff": diff}


# ---------------------------------------------------------------------------
# Apply-diff (Phase 4)
# ---------------------------------------------------------------------------


def _diff_identity(d: dict[str, object]) -> tuple[object, object, object, object]:
    """The identity of a proposed edit — which file/key it writes and the
    before/after values. Used to match an apply request against the model's
    stored proposal."""
    return (d.get("target_file"), d.get("target_path"), d.get("old_value"), d.get("new_value"))


def _matches_stored_proposal(diff: dict[str, object], thread: list[ChatTurn]) -> bool:
    """True iff ``diff`` matches an edit the assistant actually proposed in this
    thread. Blocks applying a request-supplied diff the model never proposed
    (e.g. a forged/CSRF apply call that swaps the target file or the value)."""
    incoming = _diff_identity(diff)
    return any(
        turn.role == "assistant"
        and turn.proposed_diff is not None
        and _diff_identity(turn.proposed_diff) == incoming
        for turn in thread
    )


def apply_chat_diff(
    repo_root: Path,
    ticker: str,
    report_date: date,
    diff: dict[str, object],
    dry_run: bool = False,
) -> dict[str, object]:
    """Apply a chatbot-proposed diff. Currently supports:

    - target_path is a top-level key in a JSON file (e.g. `"thesis"` in
      `micro_thesis/holdings/<T>.json`) — string-value replacement.
    - target_path is a deeper jq-style path "$.kpi_ledger[?(@.name=='X')]"
      — NOT supported yet; falls through with a soft error.

    Returns: `{"applied": bool, "summary": str, "path": str, "error": str | None}`.

    Refuses to write outside `data/`, `micro_thesis/`, or `.tmp/` — the
    filesystem scope the analyst edits via the chatbot. `directives/` is
    deliberately NOT writable: those are pipeline-control specs that get read
    back into later prompts, so a chat-driven write there is an
    injection-persistence vector, not a user edit.

    Proposal binding: the diff must match an edit the assistant actually
    proposed in this report's thread (keyed by ``report_date``). An apply
    request carrying a diff the model never proposed — a forged/CSRF call that
    swaps the target file or value — is refused.

    Optimistic-concurrency: when the proposal carries `old_value` (the schema
    always asks for it), the on-disk value must still match it — otherwise the
    proposal is stale (the analyst or a rebuild changed it since) and we refuse
    rather than silently clobber the newer value.
    """
    target_file = diff.get("target_file")
    target_path = diff.get("target_path")
    new_value = diff.get("new_value")
    summary = diff.get("summary") or "(no summary)"

    if not isinstance(target_file, str) or not isinstance(target_path, str):
        return {
            "applied": False,
            "error": "diff missing target_file or target_path",
            "summary": str(summary),
        }

    if not _matches_stored_proposal(diff, load_thread(repo_root, ticker, report_date)):
        return {
            "applied": False,
            "error": "diff does not match any edit proposed in this conversation",
            "summary": str(summary),
        }

    abs_target = (repo_root / target_file).resolve()
    if not _is_in_writable_scope(repo_root, abs_target):
        return {
            "applied": False,
            "error": f"target {target_file} outside writable scope",
            "summary": str(summary),
        }
    if not abs_target.exists():
        return {
            "applied": False,
            "error": f"target file does not exist: {target_file}",
            "summary": str(summary),
        }

    try:
        payload = json.loads(abs_target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {
            "applied": False,
            "error": f"could not parse target as JSON: {e}",
            "summary": str(summary),
        }

    if "." in target_path or "[" in target_path:
        return {
            "applied": False,
            "error": "nested target_path not yet supported",
            "summary": str(summary),
            "next_step": "use a top-level key for now",
        }
    if target_path not in payload:
        return {
            "applied": False,
            "error": f"key {target_path!r} not in {target_file}",
            "summary": str(summary),
        }

    # Optimistic-concurrency guard: refuse to overwrite if the on-disk value has
    # drifted from what the proposal expected to replace. Only enforced when the
    # proposal supplies `old_value` (always, per the prompt schema); a legacy
    # proposal that omits it falls back to the prior apply-anyway behavior.
    if "old_value" in diff and payload[target_path] != diff["old_value"]:
        return {
            "applied": False,
            "error": "value changed since the proposal — refusing to overwrite",
            "summary": str(summary),
            "old_preview": str(payload[target_path])[:120],
            "expected_preview": str(diff["old_value"])[:120],
        }

    if dry_run:
        return {
            "applied": False,
            "dry_run": True,
            "summary": str(summary),
            "old_preview": str(payload[target_path])[:120],
            "new_preview": str(new_value)[:120],
        }

    payload[target_path] = new_value
    abs_target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "applied": True,
        "summary": str(summary),
        "path": str(abs_target.relative_to(repo_root)),
    }


def _is_in_writable_scope(repo_root: Path, abs_target: Path) -> bool:
    """Restrict writes to the directories users normally edit via the
    chatbot. Keeps source code, CI configs, etc. out of bounds."""
    allowed = (
        repo_root / "data",
        repo_root / "micro_thesis",
        repo_root / ".tmp",
    )
    try:
        for base in allowed:
            base_resolved = base.resolve()
            try:
                abs_target.relative_to(base_resolved)
                return True
            except ValueError:
                continue
    except (OSError, RuntimeError):
        return False
    return False


# Expose the module-level functions on a small namespace the server can
# import as a single object — mirrors the public-surface comment in the
# docstring above.
class _BuildChatResponse:
    load_thread = staticmethod(load_thread)
    stream_response = staticmethod(stream_response)


build_chat_response = _BuildChatResponse()


__all__ = [
    "ChatStore",
    "ChatTurn",
    "apply_chat_diff",
    "build_chat_response",
    "build_system_prompt",
    "extract_diff",
    "load_thread",
    "save_thread",
    "stream_llm_text",
    "stream_response",
]


if __name__ == "__main__":  # smoke test
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--ticker", required=True)
    p.add_argument("--report-date", required=True, type=date.fromisoformat)
    p.add_argument("--message", required=True)
    p.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = p.parse_args()
    for ev in stream_response(
        args.repo_root.resolve(), args.ticker, args.report_date, args.message
    ):
        if ev.get("type") == "delta":
            sys.stdout.write(cast("str", ev["text"]))
            sys.stdout.flush()
        elif ev.get("type") == "diff_proposal":
            print("\n\n[DIFF PROPOSAL]", json.dumps(ev["diff"], indent=2))
        elif ev.get("type") == "error":
            print(f"\n[ERROR] {ev['error']}", file=sys.stderr)
    print()
