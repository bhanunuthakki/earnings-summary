"""Read-only research tools for an orchestrator agent, served over MCP stdio.

This is the Phase 1 harness kernel. The agent plans; these tools return the
evidence. Every tool reads through an existing owner under a read-only
connection: the roster (``pipeline.queries``), the earnings calendar
(``expected_earnings``), the owner thesis (``pipeline.cockpit_file_inputs``)
and the grounded evidence channels (``ask.grounding``). The LLM pack router is
never called, so a tool call spends no model quota and writes nothing.

One session ledger numbers every item handed to the agent. ``verify_draft``
binds each drafted claim to the cited ledger items with the memo verifier, and
``manifest`` returns the ledger for the saved artifact. Each item carries an
explicit status; no tool claims a decision-grade result.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Literal, cast

from pydantic import ValidationError

from ask.grounding import (
    NEED_DOC_KINDS,
    NEED_KINDS,
    EvidenceItem,
    EvidenceNeed,
    gather_requested_evidence,
)
from expected_earnings import last_reported_by_ticker, upcoming_by_ticker
from log_redact import redact
from models.companies import ListType
from pipeline.cockpit_file_inputs import read_holdings_payload
from pipeline.queries import tracked_companies_for_user
from research.memo_claim_support import (
    MemoClaimSupportError,
    MemoDerivedFigure,
    derived_figure_display,
    readable_source_text,
    verify_memo_analysis_text,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

SERVER_NAME = "earnings-summary-research"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
ROSTER_LIST_TYPES = frozenset({ListType.PORTFOLIO, ListType.EVALUATION, ListType.WATCHLIST})
MAX_NEEDS = 12
MAX_CLAIMS = 60
MAX_TEXT = 4000
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,31}$")
# Citation marks such as [3] or [3, 4] are references, not financial figures.
_CITE_MARK = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")
_ANY_QUOTE = re.compile(r"[“\"]([^”\"]+)[”\"]")
_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")

ItemStatus = Literal["admitted", "unadmitted_fact", "source_passage", "owner_record"]


class ToolInputError(ValueError):
    """The agent sent arguments the tool cannot accept."""


def item_status(item: EvidenceItem) -> ItemStatus:
    """Label what backs an item; only an admitted canonical cell is ``admitted``."""
    if item.kind == "fact":
        return "admitted" if item.canonical_reference is not None else "unadmitted_fact"
    if item.kind in NEED_DOC_KINDS:
        return "source_passage"
    return "owner_record"


def _item_json(item: EvidenceItem) -> dict[str, object]:
    return {**item.chip_payload(), "status": item_status(item), "text": item.text}


def _ticker(value: object) -> str:
    if not isinstance(value, str) or not _TICKER.match(value.strip().upper()):
        raise ToolInputError("ticker must be an exchange symbol such as NU or BRK.B")
    return value.strip().upper()


def _text(value: object, name: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > MAX_TEXT:
        raise ToolInputError(f"{name} must be a non-empty string of at most {MAX_TEXT} characters")
    return value


def _list(value: object, name: str, limit: int) -> list[object]:
    if not isinstance(value, list) or not value or len(cast("list[object]", value)) > limit:
        raise ToolInputError(f"{name} must be a list of 1 to {limit} entries")
    return cast("list[object]", value)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ToolInputError(f"{name} entries must be objects")
    return cast("Mapping[str, object]", value)


@dataclass(slots=True)
class ResearchToolSession:
    """State for one agent session: the database, the repo and the evidence ledger."""

    db_path: Path
    repo_root: Path
    today: date | None = None
    ledger: list[EvidenceItem] = field(default_factory=list[EvidenceItem])

    def _append(self, items: list[EvidenceItem]) -> list[EvidenceItem]:
        seen = {(i.kind, i.label, i.text) for i in self.ledger}
        added: list[EvidenceItem] = []
        for item in items:
            key = (item.kind, item.label, item.text)
            if key in seen:
                continue
            seen.add(key)
            numbered = replace(item, n=len(self.ledger) + 1)
            self.ledger.append(numbered)
            added.append(numbered)
        return added

    def coverage(self) -> dict[str, object]:
        """Portfolio, evaluation and watchlist names with their earnings dates."""
        today = self.today or datetime.now(UTC).date()
        conn = connect_sqlite(self.db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            companies = tracked_companies_for_user(
                conn, only_classified=False, list_types=ROSTER_LIST_TYPES
            )
            upcoming = upcoming_by_ticker(conn, today)
            reported = last_reported_by_ticker(conn, today)
        finally:
            conn.close()
        names: list[dict[str, object]] = []
        for company in companies:
            ticker = company.ticker.upper()
            next_date, last_date = upcoming.get(ticker), reported.get(ticker)
            names.append(
                {
                    "ticker": ticker,
                    "name": company.name,
                    "list_type": company.list_type.value,
                    "fiscal_year_end": company.fiscal_year_end,
                    # The calendar store holds provider estimates, never confirmations.
                    "next_earnings": next_date.isoformat() if next_date else None,
                    "next_earnings_status": "estimated" if next_date else "unannounced",
                    "last_report_estimate": last_date.isoformat() if last_date else None,
                    "has_thesis": (
                        self.repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
                    ).is_file(),
                }
            )
        return {"as_of": today.isoformat(), "names": names}

    def thesis(self, ticker: str) -> dict[str, object]:
        """The owner's recorded thesis file, added to the ledger as an owner record."""
        payload, error = read_holdings_payload(self.repo_root, ticker)
        if error != "none" or payload is None:
            return {"ticker": ticker, "available": False, "read_error": error}
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        item = EvidenceItem(
            n=0,
            kind="thesis",
            label=f"{ticker} · owner thesis",
            text=text,
            doc_id=None,
            href=None,
            ticker=ticker,
        )
        added = self._append([item])
        number = added[0].n if added else next(i.n for i in self.ledger if i.text == text)
        return {"ticker": ticker, "available": True, "item": number, "thesis": payload}

    def evidence(self, question: str, needs: list[EvidenceNeed]) -> dict[str, object]:
        """Fetch facts, filing sections, transcript passages or portfolio packs."""
        scope = list(dict.fromkeys(n.ticker for n in needs if n.ticker))
        found = gather_requested_evidence(
            needs,
            question=question,
            repo_root=self.repo_root,
            db_path=self.db_path,
            scope_tickers=scope,
            existing=list(self.ledger),
        )
        added = self._append(found)
        return {"added": [_item_json(i) for i in added], "ledger_size": len(self.ledger)}

    def verify_draft(
        self, claims: list[tuple[str, tuple[int, ...], tuple[MemoDerivedFigure, ...]]]
    ) -> dict[str, object]:
        """Bind every number and quote in each claim to the items it cites."""
        by_number = {i.n: i for i in self.ledger}
        results: list[dict[str, object]] = []
        for index, (text, cites, derived) in enumerate(claims):
            unknown = [n for n in cites if n not in by_number]
            if unknown:
                results.append(
                    {
                        "claim": index,
                        "status": "fail",
                        "reason": "unknown_citation",
                        "cites": unknown,
                    }
                )
                continue
            excerpts = tuple((by_number[n].text, (by_number[n].text,)) for n in cites)
            try:
                _check_claim_shape(text, cites, [by_number[n].text for n in cites])
                # Derivations are replayed here: operands bind numerically to cited
                # figures or earlier results, with no calendar exemption, and only the
                # displayed results join the supported figures. Constants never do.
                bound = _magnitudes(" ".join(by_number[n].text for n in cites))
                for figure in derived:
                    if any(_magnitude(token) not in bound for token in figure.operands.values()):
                        raise MemoClaimSupportError("memo_derived_operand_unbound")
                    if derived_figure_display(figure) != figure.displayed_value:
                        raise MemoClaimSupportError("memo_derived_value_mismatch")
                    bound.add(_magnitude(figure.displayed_value))
                verify_memo_analysis_text(
                    _CITE_MARK.sub(" ", text),
                    excerpts,
                    declared_values=tuple(figure.displayed_value for figure in derived),
                )
            except MemoClaimSupportError as exc:
                results.append({"claim": index, "status": "fail", "reason": str(exc)})
                continue
            statuses = sorted({item_status(by_number[n]) for n in cites})
            results.append({"claim": index, "status": "pass", "cited_status": statuses})
        failed = sum(1 for r in results if r["status"] == "fail")
        return {
            "claims": results,
            "passed": len(results) - failed,
            "failed": failed,
            "scope": "deterministic number and quote binding only; semantic review remains required",
        }

    def manifest(self) -> dict[str, object]:
        """The ledger to save with an artifact; texts are omitted, identities kept."""
        counts: dict[str, int] = {}
        for item in self.ledger:
            counts[item_status(item)] = counts.get(item_status(item), 0) + 1
        return {
            "items": [item.chip_payload() | {"status": item_status(item)} for item in self.ledger],
            "status_counts": counts,
        }


def _magnitude(token: str) -> Decimal:
    try:
        return abs(Decimal(re.sub(r"[,%$\s]", "", token).strip("()"))).normalize()
    except InvalidOperation as exc:
        raise MemoClaimSupportError("memo_derived_expression_invalid") from exc


def _magnitudes(text: str) -> set[Decimal]:
    return {_magnitude(token) for token in _NUMBER_TOKEN.findall(text)}


def _check_claim_shape(text: str, cites: tuple[int, ...], cited_texts: list[str]) -> None:
    """Inline marks must name cited items; every quote, however short, must be cited text."""
    marks = {int(n) for mark in _CITE_MARK.findall(text) for n in re.findall(r"\d+", mark)}
    if not marks <= set(cites):
        raise MemoClaimSupportError("inline_citation_not_in_cites")
    sources = [readable_source_text(t) for t in cited_texts]
    for quoted in _ANY_QUOTE.findall(readable_source_text(text)):
        if not any(quoted.strip() in source for source in sources):
            raise MemoClaimSupportError("memo_quoted_span_not_in_excerpt")


def _needs(arguments: Mapping[str, object]) -> list[EvidenceNeed]:
    needs: list[EvidenceNeed] = []
    for raw in _list(arguments.get("needs"), "needs", MAX_NEEDS):
        entry = _mapping(raw, "needs")
        kind = entry.get("kind")
        if not isinstance(kind, str) or kind not in NEED_KINDS:
            raise ToolInputError(f"need kind must be one of {sorted(NEED_KINDS)}")
        ticker = entry.get("ticker")
        needs.append(
            EvidenceNeed(
                kind=kind,
                ticker=None if ticker is None else _ticker(ticker),
                period=_text(entry.get("period"), "period", required=False) or None,
                query=_text(entry.get("query"), "query", required=False),
            )
        )
    return needs


def _claims(
    arguments: Mapping[str, object],
) -> list[tuple[str, tuple[int, ...], tuple[MemoDerivedFigure, ...]]]:
    claims: list[tuple[str, tuple[int, ...], tuple[MemoDerivedFigure, ...]]] = []
    for raw in _list(arguments.get("claims"), "claims", MAX_CLAIMS):
        entry = _mapping(raw, "claims")
        cites_raw = entry.get("cites", [])
        if not isinstance(cites_raw, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) for n in cast("list[object]", cites_raw)
        ):
            raise ToolInputError("cites must be a list of ledger item numbers")
        derived_raw = entry.get("derived", [])
        if not isinstance(derived_raw, list):
            raise ToolInputError("derived must be a list")
        try:
            derived = tuple(
                MemoDerivedFigure.model_validate(d) for d in cast("list[object]", derived_raw)
            )
        except ValidationError as exc:
            raise ToolInputError(
                "derived entries need expression, operands, displayed_value and decimals"
            ) from exc
        claims.append(
            (_text(entry.get("text"), "claim text"), tuple(cast("list[int]", cites_raw)), derived)
        )
    return claims


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]
    handler: Callable[[ResearchToolSession, Mapping[str, object]], dict[str, object]]


_TICKER_SCHEMA: dict[str, object] = {"type": "string", "description": "Exchange symbol, e.g. NU"}
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "coverage",
        "List portfolio, evaluation and watchlist names with next and last earnings dates "
        "and whether an owner thesis exists. Start here to scope a request.",
        {"type": "object", "properties": {}},
        lambda s, _a: s.coverage(),
    ),
    ToolSpec(
        "thesis",
        "Read the owner's recorded thesis for one ticker. The thesis joins the evidence "
        "ledger as an owner record, so claims may cite its thresholds.",
        {"type": "object", "properties": {"ticker": _TICKER_SCHEMA}, "required": ["ticker"]},
        lambda s, a: s.thesis(_ticker(a.get("ticker"))),
    ),
    ToolSpec(
        "evidence",
        "Fetch numbered evidence. Each need names a channel: fact (KPI and financial "
        "series), filing (10-K/10-Q/6-K sections), transcript (call passages; set period "
        "such as 'Q2 2026' for older calls) or a portfolio pack ("
        + ", ".join(sorted(NEED_KINDS - NEED_DOC_KINDS))
        + "). "
        "Each item has a status: admitted, unadmitted_fact, source_passage or owner_record.",
        {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The research question being answered",
                },
                "needs": {
                    "type": "array",
                    "maxItems": MAX_NEEDS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": sorted(NEED_KINDS)},
                            "ticker": _TICKER_SCHEMA,
                            "period": {"type": "string"},
                            "query": {"type": "string"},
                        },
                        "required": ["kind"],
                    },
                },
            },
            "required": ["question", "needs"],
        },
        lambda s, a: s.evidence(_text(a.get("question"), "question"), _needs(a)),
    ),
    ToolSpec(
        "verify_draft",
        "Check drafted claims before delivery. Every number and quoted span in a claim must "
        "occur in the ledger items it cites, or in a declared derivation over those figures. "
        "Returns pass or fail per claim. This does not judge meaning.",
        {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "maxItems": MAX_CLAIMS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "cites": {"type": "array", "items": {"type": "integer"}},
                            "derived": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": "Arithmetic shown in the claim. Each entry has an "
                                "expression such as (b - a) / a * 100, operands that map each name "
                                "to a figure from a cited item, the displayed_value, decimals and an "
                                "optional percent flag.",
                            },
                        },
                        "required": ["text", "cites"],
                    },
                }
            },
            "required": ["claims"],
        },
        lambda s, a: s.verify_draft(_claims(a)),
    ),
    ToolSpec(
        "manifest",
        "Return the identities and statuses of every ledger item, to save with the artifact.",
        {"type": "object", "properties": {}},
        lambda s, _a: s.manifest(),
    ),
)
_BY_NAME = {tool.name: tool for tool in TOOLS}


def call_tool(
    session: ResearchToolSession, name: str, arguments: Mapping[str, object]
) -> dict[str, object]:
    """Run one tool and wrap the result as MCP tool content."""
    tool = _BY_NAME.get(name)
    if tool is None:
        return _content({"error": f"unknown tool {name!r}"}, is_error=True)
    try:
        return _content(tool.handler(session, arguments))
    except ToolInputError as exc:
        return _content({"error": str(exc)}, is_error=True)
    except Exception as exc:  # one bad call must not end the session or lose its ledger
        _log("tool_failed", tool=name, error=type(exc).__name__, detail=redact(str(exc)))
        return _content({"error": f"{type(exc).__name__} while running {name}"}, is_error=True)


def _content(payload: dict[str, object], *, is_error: bool = False) -> dict[str, object]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), file=sys.stderr, flush=True)


def handle_message(session: ResearchToolSession, message: object) -> dict[str, object] | None:
    """Answer one JSON-RPC message; notifications return ``None``."""
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "invalid request")
    request = cast("dict[str, object]", message)
    method, request_id = request.get("method"), request.get("id")
    params = request.get("params")
    params_map = cast("dict[str, object]", params) if isinstance(params, dict) else {}
    if not isinstance(method, str):
        return _rpc_error(request_id, -32600, "invalid request")
    if request_id is None:
        return None
    if method == "initialize":
        asked = params_map.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        result: dict[str, object] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in TOOLS
            ]
        }
    elif method == "tools/call":
        name = params_map.get("name")
        arguments = params_map.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "tools/call needs a name and an arguments object")
        result = call_tool(session, name, cast("dict[str, object]", arguments))
    else:
        return _rpc_error(request_id, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(session: ResearchToolSession, stdin: IO[str], stdout: IO[str]) -> None:
    """Serve newline-delimited JSON-RPC until the client closes stdin."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            message: object = json.loads(line)
        except json.JSONDecodeError:
            reply: object = _rpc_error(None, -32700, "parse error")
        else:
            if not isinstance(message, list):
                reply = handle_message(session, message)
            elif not message:
                reply = _rpc_error(None, -32600, "empty batch")
            else:  # JSON-RPC batches, used by the 2025-03-26 protocol revision
                batch = cast("list[object]", message)
                replies = [r for m in batch if (r := handle_message(session, m)) is not None]
                reply = replies or None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
            stdout.flush()
