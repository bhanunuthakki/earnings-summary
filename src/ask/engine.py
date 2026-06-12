"""The unified ask engine — one brain behind both chat surfaces.

Every conversational turn (Ask tab ``POST /api/ask``, report drawer
``POST /chat/<ticker>``) goes through :func:`respond_turn`, which routes it
to one of three paths and yields a single event-stream vocabulary:

  command    — deterministic slash commands (``ask.commands``): no LLM,
               instant reply. ``/discovery …``, ``/help``.
  data       — metric-shaped questions: NL → ViewSpec compile (fast model,
               budget-gated) → execute → rendered matrix/chart fragment.
               ``/view <q>`` forces this path; otherwise a failed compile
               falls back to the narrative path so the question still gets
               answered.
  narrative  — everything else: the claude-CLI chat path. Ticker scope
               runs the existing ``chat_session.stream_response`` (report
               context + per-report thread persistence); portfolio scope
               composes the pack's system context + the client-supplied
               thread tail over the shared transport.

Routing is deterministic and cheap (regexes + the metric catalog — no
model call), with narrative as the safe default: the narrative path can
answer a data question in prose (it has Read access to the fact caches),
but the data path can't answer a narrative question at all.

Event vocabulary (a superset of the report drawer's existing SSE frames —
old clients ignore unknown types):

  {type: "stage", stage: "compiling"|"running"|"answering", route, note?}
  {type: "delta", text}                  — incremental narrative tokens
  {type: "fragment", html, spec}         — a rendered data view
  {type: "final", text, route}           — once, on success
  {type: "diff_proposal", diff}          — narrative edit proposal
  {type: "error", error, code?}          — on failure
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, cast

import chat_session
from ask.commands import COMMAND_PREFIXES, run_chat_command
from ask.context import ContextPack, tracked_tickers
from ask.grounding import build_evidence_block, gather_evidence, used_citation_items
from ask.store import append_turn as _store_append_turn
from ask.store import load_recent_history as _store_load_history
from dispatch_registry import Registry
from viewspec.engine import execute_view, metric_catalog
from viewspec.render import render_view_fragment
from viewspec.spec import ViewSpecError

log = logging.getLogger(__name__)

Route = Literal["command", "data", "narrative"]
ROUTE_COMMAND: Route = "command"
ROUTE_DATA: Route = "data"
ROUTE_NARRATIVE: Route = "narrative"

_MAX_HISTORY_TURNS = 8
_MAX_HISTORY_CHARS = 1200

_TICKERISH_RX = re.compile(r"\b[A-Z][A-Z0-9.\-]{0,5}\b")

# Narrative markers win over data markers: "why did margins fall?" is an
# explanation question even though "margins" is chartable.
_NARRATIVE_RX = re.compile(
    r"\b(why|how come|how did|how does|how is|explain|should (i|we)|do you think|"
    r"your (take|view|read)|opinion|thesis|bear case|bull case|risks?|"
    r"what happened|what changed|guidance|transcript|management|"
    r"summari[sz]e|tell me about|describe|walk me through|quote)\b",
    re.IGNORECASE,
)

# Transform / cadence / comparison structure — the strong "chart this" signals.
_DATA_RX = re.compile(
    r"\b(yoy|y/y|qoq|cagr|growth|grew|margins?|as % of|% of revenue|"
    r"last \d+\s*(?:quarters?|years?|qtrs?|q)\b|quarterly|annual(?:ly)?|per quarter|"
    r"chart|plot|table|matrix|trend|vs\.?|versus|compared?(?:\s+(?:to|with))?|"
    r"decelerat\w*|accelerat\w*)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class AskTurn:
    """One inbound message, surface-agnostic."""

    text: str
    # Requested universe (the Ask tab's ticker box); empty → pack defaults.
    tickers: list[str] = field(default_factory=list[str])
    # The previous turn's view spec — "now annual" refines instead of restarting.
    context_spec: dict[str, object] | None = None
    # Client-supplied thread tail — used ONLY when session_id is absent (legacy /
    # first-turn fallback).  When session_id is set the engine loads history from
    # ask_turns instead, so the client can never supply a corrupted tail.
    history: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    # Server-side session for portfolio-scope persistence.  None → the engine
    # falls back to the client-supplied history tail (pre-S3 / ticker scope).
    session_id: str | None = None


def sanitize_history(raw: object) -> list[dict[str, str]]:
    """Validate a client-supplied thread tail: [{"role", "text"}], capped."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        item_d = cast("dict[str, object]", item)
        role = str(item_d.get("role") or "")
        text = str(item_d.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        out.append({"role": role, "text": text[:_MAX_HISTORY_CHARS]})
    return out[-_MAX_HISTORY_TURNS:]


def _hits_metric_label(text: str, labels: Collection[str]) -> bool:
    """Does the question name a metric that actually exists in the catalog
    for this universe? Grounded signal: "NU revenue" routes to data because
    "revenue" is plottable, not because of a hardcoded word list."""
    if not labels:
        return False
    low = " " + " ".join(re.sub(r"[^a-z0-9&%.\- ]+", " ", text.lower()).split()) + " "
    for raw in labels:
        label = " ".join(str(raw).lower().split())
        if len(label) < 3:
            continue
        if f" {label} " in low:
            return True
    return False


def _mentions_tracked_ticker(text: str, tracked: Collection[str]) -> bool:
    if not tracked:
        return False
    tracked_set = {t.upper() for t in tracked}
    return any(m.group(0) in tracked_set for m in _TICKERISH_RX.finditer(text))


def route_turn(
    text: str,
    *,
    has_context_spec: bool = False,
    metric_labels: Collection[str] = (),
    known_tickers: Collection[str] = (),
) -> Route:
    """Deterministic routing for one turn. Pure — no I/O, no model call."""
    t = text.strip()
    low = t.lower()
    if low == "/view" or low.startswith("/view "):
        return ROUTE_DATA
    if low.startswith(COMMAND_PREFIXES):
        return ROUTE_COMMAND
    if low.startswith("/"):
        return ROUTE_NARRATIVE  # unknown command → let the assistant explain
    if _NARRATIVE_RX.search(t):
        return ROUTE_NARRATIVE
    if _DATA_RX.search(t):
        return ROUTE_DATA
    if _hits_metric_label(t, metric_labels):
        return ROUTE_DATA
    # A bare tracked-ticker mention only signals data when it refines an
    # existing view ("add MELI"); standalone it reads as "tell me about X".
    if has_context_spec and _mentions_tracked_ticker(t, known_tickers):
        return ROUTE_DATA
    return ROUTE_NARRATIVE


def respond_turn(
    turn: AskTurn,
    pack: ContextPack,
    *,
    db_path: Path,
    repo_root: Path,
    registry: Registry | None = None,
) -> Iterator[dict[str, object]]:
    """Answer one turn as an event stream (see module docstring)."""
    text = turn.text.strip()
    if not text:
        yield {"type": "error", "error": "empty message"}
        return

    low_text = text.lower()
    forced_view = low_text == "/view" or low_text.startswith("/view ")
    if forced_view:
        text = text[len("/view") :].strip()
        if not text:
            yield {
                "type": "error",
                "error": 'usage: /view <question> — e.g. "/view NU revenue growth, last 8 quarters"',
            }
            return

    effective_tickers = [t.strip().upper() for t in turn.tickers if t.strip()] or list(
        pack.default_tickers
    )

    if forced_view:
        route: Route = ROUTE_DATA
    elif text.startswith("/"):
        route = route_turn(text)  # command/narrative — no catalog lookup needed
    else:
        catalog = metric_catalog(db_path, effective_tickers)
        labels = [
            str(e.get("label") or "")
            for domain in ("fin", "kpi", "seg")
            for e in catalog.get(domain, [])
        ]
        route = route_turn(
            text,
            has_context_spec=turn.context_spec is not None,
            metric_labels=labels,
            known_tickers=tracked_tickers(db_path),
        )

    if route == ROUTE_COMMAND:
        reply = run_chat_command(repo_root, text, registry) if registry is not None else None
        if reply is None:
            reply = "Commands aren't available on this surface — try /help in the report chat."
        yield {"type": "delta", "text": reply}
        yield {"type": "final", "text": reply, "route": ROUTE_COMMAND}
        return

    if route == ROUTE_DATA:
        yield from _data_events(
            text,
            turn,
            pack,
            db_path=db_path,
            repo_root=repo_root,
            effective_tickers=effective_tickers,
            forced=forced_view,
        )
        return

    yield from _narrative_events(
        text, turn, pack, repo_root=repo_root, db_path=db_path, emit_stage=True
    )


def _data_events(
    text: str,
    turn: AskTurn,
    pack: ContextPack,
    *,
    db_path: Path,
    repo_root: Path,
    effective_tickers: list[str],
    forced: bool,
) -> Iterator[dict[str, object]]:
    """Compile → execute → fragment. A failed compile degrades to the
    narrative path (the question still gets answered) unless /view forced
    data-only — then the compile error itself is the answer."""
    yield {"type": "stage", "stage": "compiling", "route": ROUTE_DATA}
    from viewspec.nl_compile import compile_nl_to_viewspec  # lazy: pulls llm.cli

    result = compile_nl_to_viewspec(
        text,
        db_path=db_path,
        context_tickers=effective_tickers,
        context_spec=turn.context_spec,
    )
    if result.status != "ok" or result.spec is None:
        message = result.message or "compile failed"
        if forced:
            yield {"type": "error", "error": message, "code": result.status}
            return
        yield {
            "type": "stage",
            "stage": "answering",
            "route": ROUTE_NARRATIVE,
            "note": "no chartable view — answering in prose",
        }
        yield from _narrative_events(
            text, turn, pack, repo_root=repo_root, db_path=db_path, emit_stage=False
        )
        return

    spec = result.spec
    yield {"type": "stage", "stage": "running", "route": ROUTE_DATA}
    try:
        view = execute_view(spec, db_path=db_path)
        fragment = render_view_fragment(view, include_chart=True)
    except ViewSpecError as exc:
        if forced:
            yield {"type": "error", "error": str(exc)}
            return
        yield {
            "type": "stage",
            "stage": "answering",
            "route": ROUTE_NARRATIVE,
            "note": "view failed — answering in prose",
        }
        yield from _narrative_events(
            text, turn, pack, repo_root=repo_root, db_path=db_path, emit_stage=False
        )
        return

    n_rows = len(view.rows)
    refined = " (refined the previous view)" if turn.context_spec else ""
    if n_rows == 0:
        other = "annual" if spec.cadence == "quarterly" else "quarterly"
        message = (
            f"0 series — no data came back for that view. "
            f'Try {other} cadence ("now {other}"), or different metrics/tickers.'
        )
    else:
        message = (
            f"{n_rows} series · {spec.transform} · {spec.cadence}, {spec.periods} periods{refined}"
        )

    spec_dict = spec.to_dict()
    # Persist BEFORE yielding: a consumer that stops at the final frame must
    # not strand the thread write inside a suspended generator.
    if pack.persist and pack.ticker and pack.report_date is not None:
        _persist_data_turn(repo_root, pack.ticker, pack.report_date, turn.text, message)
    # Portfolio-scope data turns: record the exchange so the session thread
    # stays continuous for the narrative path's history loading.
    if turn.session_id and pack.scope == "portfolio":
        data_label = f"{message} (rendered as a live data view)"
        try:
            _store_append_turn(
                session_id=turn.session_id, role="user", text=turn.text, db_path=db_path
            )
            _store_append_turn(
                session_id=turn.session_id, role="assistant", text=data_label, db_path=db_path
            )
        except Exception:
            log.warning({"event": "ask_store_data_turn_failed", "sid": turn.session_id})
    yield {"type": "fragment", "html": fragment, "spec": spec_dict}
    yield {"type": "final", "text": message, "route": ROUTE_DATA}


def _persist_data_turn(
    repo_root: Path,
    ticker: str,
    report_date: date,
    user_text: str,
    message: str,
) -> None:
    """Keep the per-report thread continuous when a data turn answers in the
    report drawer (fragments themselves aren't stored — the message line is
    enough for prior-thread context). Best-effort: the answer already
    rendered; a failed write must not break the stream."""
    try:
        thread = chat_session.load_thread(repo_root, ticker, report_date)
        thread.append(chat_session.ChatTurn(role="user", text=user_text))
        thread.append(
            chat_session.ChatTurn(
                role="assistant", text=f"{message} (rendered as a live data view)"
            )
        )
        chat_session.save_thread(repo_root, ticker, report_date, thread)
    except Exception:
        log.warning(
            {"event": "ask_persist_data_turn_failed", "ticker": ticker},
            exc_info=True,
        )


def _narrative_events(
    text: str,
    turn: AskTurn,
    pack: ContextPack,
    *,
    repo_root: Path,
    db_path: Path,
    emit_stage: bool,
) -> Iterator[dict[str, object]]:
    """The claude-CLI chat path, grounded (Ask v3). Ticker scope = the
    existing report session (its own system prompt + thread persistence);
    portfolio scope = the pack's system context + client-supplied history
    over the raw transport.

    Before the call, ``ask.grounding`` retrieves numbered evidence (facts /
    filing sections / transcript lines) for the question; when anything
    comes back it rides into the prompt under a cite-or-don't-claim
    contract (the answering stage notes how many sources), and the markers
    the answer actually used are resolved into a trailing
    ``{type: "citations", items: [...]}`` event (each item carries the
    /source/<doc_id> viewer href). No evidence → the turn runs exactly as
    before: same stage frame, no citations event."""
    scope_tickers = (
        [pack.ticker]
        if pack.scope == "ticker" and pack.ticker
        else ([t.strip().upper() for t in turn.tickers if t.strip()] or list(pack.default_tickers))
    )
    evidence = gather_evidence(
        text, repo_root=repo_root, db_path=db_path, scope_tickers=scope_tickers
    )
    evidence_block = build_evidence_block(evidence)
    if emit_stage:
        stage: dict[str, object] = {"type": "stage", "stage": "answering", "route": ROUTE_NARRATIVE}
        if evidence:
            stage["note"] = f"grounded on {len(evidence)} source{'s' if len(evidence) != 1 else ''}"
        yield stage

    if pack.scope == "ticker" and pack.ticker and pack.report_date is not None:
        # extra_context is passed only when evidence exists so monkeypatched
        # fakes with the original four-kwarg signature keep working.
        kwargs: dict[str, str] = {"extra_context": evidence_block} if evidence_block else {}
        final_text_t: str | None = None
        for event in chat_session.build_chat_response.stream_response(
            repo_root=repo_root,
            ticker=pack.ticker,
            report_date=pack.report_date,
            user_message=text,
            **kwargs,
        ):
            yield event
            if event.get("type") == "final":
                maybe = event.get("text")
                final_text_t = maybe if isinstance(maybe, str) else None
        if final_text_t is not None and evidence:
            used = used_citation_items(final_text_t, evidence)
            if used:
                yield {"type": "citations", "items": [item.chip_payload() for item in used]}
        return

    system_context = pack.system_context or "You are a portfolio research assistant."
    if evidence_block:
        system_context = system_context + "\n\n" + evidence_block

    # Server-side history: when the turn carries a session_id, load the stored
    # thread from ask_turns (authoritative) instead of trusting the client tail.
    if turn.session_id:
        server_hist = _store_load_history(turn.session_id, db_path=db_path)
        history = sanitize_history(server_hist)
        # Persist the user turn immediately so the audit trail is never missing
        # even if the assistant side errors.
        try:
            _store_append_turn(
                session_id=turn.session_id,
                role="user",
                text=text,
                db_path=db_path,
            )
        except Exception:
            log.warning({"event": "ask_store_user_turn_failed", "sid": turn.session_id})
    else:
        history = sanitize_history(turn.history)

    thread_text = "\n\n".join(f"[{h['role'].upper()}] {h['text']}" for h in history)
    full_prompt = (
        system_context
        + "\n\n---\n\nPRIOR THREAD:\n"
        + (thread_text or "(first turn)")
        + "\n\n---\n\nUSER:\n"
        + text
    )
    final_text: str | None = None
    for event in chat_session.stream_llm_text(full_prompt):
        kind = event.get("type")
        if kind == "final":
            final_text = cast("str", event["text"])
        else:
            yield event
            if kind == "error":
                return
    if final_text is None:  # defensive: transport always ends in final or error
        return
    yield {"type": "final", "text": final_text, "route": ROUTE_NARRATIVE}

    citations_payload: list[object] | None = None
    if evidence:
        used = used_citation_items(final_text, evidence)
        if used:
            citations_payload = [item.chip_payload() for item in used]
            yield {"type": "citations", "items": citations_payload}

    # Persist the assistant turn after a successful response.
    if turn.session_id:
        try:
            _store_append_turn(
                session_id=turn.session_id,
                role="assistant",
                text=final_text,
                citations=citations_payload,
                db_path=db_path,
            )
        except Exception:
            log.warning({"event": "ask_store_asst_turn_failed", "sid": turn.session_id})

    diff = chat_session.extract_diff(final_text)
    if diff is not None:
        yield {"type": "diff_proposal", "diff": diff}


def fold_events(events: Iterable[dict[str, object]]) -> dict[str, object]:
    """Collapse an event stream into the Ask tab's single-round-trip JSON
    payload — back-compatible with the pre-merge /api/ask contract
    (status/spec/fragment/message for views) plus ``kind``/``text`` for
    narrative and command answers."""
    fragment: dict[str, object] | None = None
    final: dict[str, object] | None = None
    diff: dict[str, object] | None = None
    error: dict[str, object] | None = None
    citations: list[object] | None = None
    notes: list[str] = []
    for ev in events:
        kind = ev.get("type")
        if kind == "fragment":
            fragment = ev
        elif kind == "final":
            final = ev
        elif kind == "diff_proposal":
            maybe = ev.get("diff")
            if isinstance(maybe, dict):
                diff = cast("dict[str, object]", maybe)
        elif kind == "citations":
            maybe_items = ev.get("items")
            if isinstance(maybe_items, list):
                citations = cast("list[object]", maybe_items)
        elif kind == "error" and error is None:
            error = ev
        elif kind == "stage":
            note = ev.get("note")
            if isinstance(note, str) and note:
                notes.append(note)
    if error is not None:
        code = error.get("code")
        status = code if code == "budget_skipped" else "error"
        return {"status": status, "message": str(error.get("error") or "failed")}
    if final is None:
        return {"status": "error", "message": "no answer produced"}
    final_text = str(final.get("text") or "")
    if fragment is not None:
        return {
            "status": "ok",
            "kind": "view",
            "spec": fragment.get("spec"),
            "fragment": fragment.get("html"),
            "message": final_text,
        }
    out: dict[str, object] = {
        "status": "ok",
        "kind": "command" if final.get("route") == ROUTE_COMMAND else "narrative",
        "text": final_text,
    }
    if notes:
        out["note"] = " · ".join(notes)
    if citations:
        out["citations"] = citations
    if diff is not None:
        out["diff"] = diff
    return out


__all__ = [
    "ROUTE_COMMAND",
    "ROUTE_DATA",
    "ROUTE_NARRATIVE",
    "AskTurn",
    "Route",
    "fold_events",
    "respond_turn",
    "route_turn",
    "sanitize_history",
]
