"""Phase-2 multi-channel decision capture (plan §4.2).

Logs a decision ONLY on a position change above a size threshold. Assembles the
structured shape ``{ticker, direction, size_pct, conviction, falsifier}`` from
explicit fields + an optional extractor (the governed ``musing_decision_extract``,
injected -- ``extract_decision`` below), persists via the decisions writer
(injected; ``conviction`` /
``falsifier`` are WRITE-ONCE at the writer), and links the decision back to the
originating musing.

Dependency-injected (``extract_fn`` / ``persist_fn`` / ``link_fn``) so the core is
testable without an LLM or a DB, and the SAME core serves all three channels:
Telegram, the in-app form, or an ambient voice musing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def capture_decision(
    *,
    text: str = "",
    ticker: str | None = None,
    direction: str | None = None,
    size_pct: float | None = None,
    conviction: str | None = None,
    falsifier: str | None = None,
    size_threshold: float = 1.0,
    note_id: int | None = None,
    db_path: Path | str | None = None,
    extract_fn: Callable[..., dict[str, object]] | None = None,
    persist_fn: Callable[..., int] | None = None,
    link_fn: Callable[..., Any] | None = None,
) -> int | None:
    """Capture a decision iff the position change clears the size threshold.

    Returns the new decision id, or None (below threshold / missing a required
    field / no persist wired -- the real flow asks a one-line follow-up when a
    required field is missing).
    """
    fields: dict[str, object | None] = {
        "ticker": ticker.strip().upper() if ticker else None,
        "direction": direction.strip().lower() if direction else None,
        "size_pct": size_pct,
        "conviction": conviction,
        "falsifier": falsifier,
    }

    # Fill gaps from the extractor (the governed musing_decision_extract) when needed.
    if extract_fn is not None and (fields["ticker"] is None or fields["direction"] is None):
        extracted = extract_fn(text) or {}
        for key in ("size_pct", "conviction", "falsifier"):
            if fields[key] is None and extracted.get(key) is not None:
                fields[key] = extracted.get(key)
        if fields["ticker"] is None and extracted.get("ticker"):
            fields["ticker"] = str(extracted["ticker"]).strip().upper()
        if fields["direction"] is None and extracted.get("direction"):
            fields["direction"] = str(extracted["direction"]).strip().lower()

    # Size threshold: a trim/add below the threshold is NOT a logged decision.
    size = _as_float(fields.get("size_pct"))
    if size is not None and size < size_threshold:
        return None
    if not fields["ticker"] or not fields["direction"]:
        return None
    if persist_fn is None:
        return None

    decision_id = persist_fn(note_id=note_id, db_path=db_path, **fields)
    if link_fn is not None and note_id is not None and decision_id:
        link_fn(note_id=note_id, decision_id=int(decision_id), db_path=db_path)
    return int(decision_id) if decision_id else None


# --- the governed musing_decision_extract gap-filler (capture_decision's extract_fn) ---

DecisionExtractCall = Callable[[str], "dict[str, object]"]

_ROSTER: frozenset[str] = frozenset(
    {
        "NU",
        "MELI",
        "NOW",
        "VEEV",
        "RBRK",
        "WIX",
        "NVO",
        "GOOGL",
        "META",
        "BN",
        "BKNG",
        "UBER",
        "HDB",
        "SOFI",
    }
)
_DIRECTIONS: frozenset[str] = frozenset({"add", "trim", "buy", "sell", "hold"})
_CONVICTIONS: frozenset[str] = frozenset({"high", "medium", "low"})

# NB: the PURPOSE is ``musing_decision_extract``, deliberately distinct from the
# adjacent, similarly-named ``decision_extraction`` (memo-paragraph extractor,
# src/decision_extractor.py) and ``decision_conditions_extract`` (falsifier prose).
# This one parses a free-text OWNER MUSING -- do not conflate them in the registries.
_EXTRACT_PROMPT = (
    "You extract the structured fields of an investing DECISION from a short "
    "first-person note the portfolio owner wrote about a position change. Fill ONLY "
    "what the note states or clearly implies; leave anything unstated as null. Do NOT "
    "invent numbers or reasons.\n\n"
    "Known tickers (map company names/obvious aliases to these EXACT symbols; use null "
    "if none is clearly referenced): NU MELI NOW VEEV RBRK WIX NVO GOOGL META BN BKNG "
    "UBER HDB SOFI.\n\n"
    "Fields:\n"
    "- ticker: one symbol from the list above, or null.\n"
    '- direction: exactly one of "add" | "trim" | "buy" | "sell" | "hold" '
    "(bought/opened=buy, sold-out/closed=sell, increased=add, reduced/shaved=trim, no "
    "change/keeping=hold), or null.\n"
    "- size_pct: the size of the change as a POSITIVE number in percent, if stated "
    '("trimmed 3%", "added half a percent"->0.5, "sold a third of it"->33). "a little"/'
    '"a bit"/no number -> null. Never negative.\n'
    '- conviction: one of "high" | "medium" | "low" if the note conveys it, else null.\n'
    '- falsifier: a ONE-LINE paraphrase of any "what would change my mind" / exit '
    "condition the note states, else null.\n\n"
    "Return JSON ONLY, exactly:\n"
    '{"ticker": "<SYMBOL or null>", "direction": "<add|trim|buy|sell|hold or null>", '
    '"size_pct": <number or null>, "conviction": "<high|medium|low or null>", '
    '"falsifier": "<one line or null>"}\n\n'
    "Note: "
)


def extract_decision(text: str, *, call: DecisionExtractCall | None = None) -> dict[str, object]:
    """The governed ``musing_decision_extract`` gap-filler for ``capture_decision``'s
    ``extract_fn`` seam: parse a free-text musing into the confidently-set subset of
    ``{ticker, direction, size_pct, conviction, falsifier}``. Fills only what the note
    states; DROPS anything unknown/out-of-enum so a hallucinated field can never reach
    the write-once ``conviction`` column. Returns ``{}`` when nothing is confidently
    extractable -- the seam then fills no gap and capture asks its one-line follow-up.

    ``call`` is the injected structured caller (tests); the default runs the governed
    purpose via ``call_llm_structured`` -- WEB-LESS, no writer in context (off the
    lethal-trifecta path; the sole input is the owner's own musing text).
    """
    text = (text or "").strip()
    if not text:
        return {}
    caller = call or _default_extract_call
    obj = caller(text)  # contract: a dict (the default coerces non-dict/parse-fail to {})

    out: dict[str, object] = {}
    ticker = obj.get("ticker")
    if isinstance(ticker, str) and ticker.strip().upper() in _ROSTER:
        out["ticker"] = ticker.strip().upper()
    direction = obj.get("direction")
    if isinstance(direction, str) and direction.strip().lower() in _DIRECTIONS:
        out["direction"] = direction.strip().lower()
    size = _as_float(obj.get("size_pct"))
    if size is not None and size >= 0:
        out["size_pct"] = size
    conviction = obj.get("conviction")
    if isinstance(conviction, str) and conviction.strip().lower() in _CONVICTIONS:
        out["conviction"] = conviction.strip().lower()
    falsifier = obj.get("falsifier")
    if isinstance(falsifier, str) and falsifier.strip():
        out["falsifier"] = falsifier.strip()
    return out


def _default_extract_call(text: str) -> dict[str, object]:
    """Default structured caller: the governed ``musing_decision_extract`` purpose.

    Lazily imports ``llm.structured`` so importing this module stays LLM-free. A
    double-parse failure (``StructuredParseError``) degrades to ``{}``; a budget/setup
    HARD stop is NOT swallowed here -- it propagates as configuration, per the repo's
    LLM exception policy."""
    from llm.structured import StructuredParseError, call_llm_structured

    try:
        obj = call_llm_structured(
            _EXTRACT_PROMPT + text,
            purpose="musing_decision_extract",
            expect="object",
            required_keys=("ticker", "direction"),
        )
    except StructuredParseError:
        return {}
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}
