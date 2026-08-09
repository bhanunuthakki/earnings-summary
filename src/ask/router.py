"""Retrieval router — which portfolio evidence packs does this question need?

The shared rulebook bans keyword/substring intent classification, so pack
selection is a structured-output call to the fast classifier model (purpose
``ask_pack_router``: Haiku pin in ``llm.cli.LLM_MODELS``, budget row seeded
by alembic 0089, golden set ``evals/golden/ask_pack_router.json``): the
question plus the pack catalog go in, ``{"packs": [...]}`` — a closed enum
over ``ask.packs.PACK_KEYS`` — comes out. Deterministic *structural* gates
run first and spend nothing: an empty question, or a DB with no tracked
companies at all, can't need packs.

Failure policy — fail CLOSED, never guess. A wrong-context answer that
LOOKS right is this feature's worst failure mode, so on every non-ok path
(budget skip, CLI failure, unusable JSON after ``llm.structured``'s
retry-with-feedback) the route is ``()`` and the narrative turn proceeds on
document evidence alone — exactly the pre-S4 behavior. The cite-or-don't-
claim contract then keeps the gap visible: portfolio claims without a
marker stand out instead of reading as grounded.

Latency: one short Haiku call per narrative turn (the same order of cost as
``viewspec_compile`` on the data path). Selection quality is scored by the
golden set, not asserted inline.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, TypeAdapter, field_validator

from ask import turn_cache
from ask.packs import PACK_KEYS, PACKS
from llm.structured import call_llm_structured
from llm_budget import should_skip_for_budget
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

PURPOSE = "ask_pack_router"


class _PackRouteWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packs: list[str]

    @field_validator("packs", mode="before")
    @classmethod
    def _closed_pack_keys(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("packs must be an array")
        return [
            item
            for item in cast("list[object]", value)
            if isinstance(item, str) and item in PACK_KEYS
        ]


_PACK_ROUTE_ADAPTER = TypeAdapter(_PackRouteWire)


@dataclass(frozen=True, slots=True)
class PackRoute:
    """Which packs one narrative turn loads, and why.

    ``status``: "ok" (the classifier answered — ``packs`` may legitimately
    be empty), "gated" (a structural pre-gate said packs can't apply; no
    call spent), "skipped" (over the purpose's monthly budget; no call
    spent), or "error" (call/parse failed; fail-closed to no packs).
    """

    packs: tuple[str, ...]
    status: str
    note: str | None = None


def route_packs(question: str, *, db_path: Path) -> PackRoute:
    """Select evidence packs for one narrative question. Never raises."""
    q = question.strip()
    if not q:
        return PackRoute((), "gated", "empty question")
    if not _has_tracked_companies(db_path):
        return PackRoute((), "gated", "no tracked companies on file")
    skip = should_skip_for_budget(PURPOSE, db_path=db_path)
    if skip is not None:
        return PackRoute((), "skipped", f"over its ${float(skip.cap):g}/mo budget")
    # In-thread route reuse (L14): the router maps a question to packs
    # deterministically enough that reusing a recent decision is correct and
    # FREE — it skips the interactive fast-model round-trip on a repeated
    # question. Checked AFTER the structural + budget gates so their
    # state-dependent contract is unchanged; only the spent-call path is cached.
    norm = turn_cache.normalize_question(q)
    cached = turn_cache.get_route(norm)
    if cached is not None:
        return PackRoute(cached, "ok", "cached")
    try:
        payload = call_llm_structured(
            _build_prompt(q),
            purpose=PURPOSE,
            scope="ask",
            expect="object",
            required_keys=("packs",),
            schema=_PACK_ROUTE_ADAPTER,
        )
        chosen = set(_PackRouteWire.model_validate(payload).packs)
    except Exception as exc:
        # Interactive surface: a transport failure, hard stop, or doubly-bad
        # JSON must degrade to document-only evidence, never break the turn.
        log.warning({"event": "ask_pack_router_failed", "error": f"{type(exc).__name__}: {exc}"})
        return PackRoute((), "error", type(exc).__name__)
    # Canonical PACKS order; unknown keys are dropped (an advisory selector
    # inventing a key shouldn't void its valid picks).
    packs = tuple(k for k in PACK_KEYS if k in chosen)
    turn_cache.put_route(norm, packs)
    return PackRoute(packs, "ok")


def _has_tracked_companies(db_path: Path) -> bool:
    """Structural pre-gate: with nothing tracked, no pack can have data."""
    if not db_path.exists():
        return False
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return False
    try:
        return conn.execute("SELECT 1 FROM tracked_companies LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _build_prompt(question: str) -> str:
    catalog = "\n".join(f'- "{p.key}": {p.router_hint}' for p in PACKS)
    return f"""You select which PORTFOLIO EVIDENCE PACKS are needed to answer one
question an analyst asked about their own portfolio and research. Output ONLY a
JSON object — no prose, no markdown fences:

{{"packs": [zero or more pack keys from the catalog]}}

Catalog:
{catalog}

Rules:
- Select ONLY packs whose data the answer must cite. An empty list is the
  correct answer for questions about company fundamentals, filings, earnings
  calls, news, or metric definitions — document evidence handles those.
- Sizing questions (trim/add/initiate/exit, "too big", swap A for B) need
  "holdings" + "dcf", plus "conviction" when the analyst's stated stance or
  targets matter.
- Results questions ("how am I doing", "what's working", alpha, vs the index)
  need "performance", usually with "holdings".
- Memory questions ("what did I decide/say/worry about", open items) need
  "journal" and/or "decisions".
- Self-assessment / track-record questions ("am I getting better", "how
  accurate are my calls", "what's my hit rate", "can I trust my own
  high-conviction read") need "calibration". Add it to a conviction-driven
  sizing question ("should I trim this high-conviction name") so the answer
  can weigh the move against how that conviction cohort has actually graded.
- Tax questions (tax-loss harvesting, "which lot/account do I sell to
  minimize tax", taxable vs sheltered exposure) need "holdings" — it carries
  each name's per-account tax treatment and unrealized P&L. Add "dcf" only
  when the sale is also a valuation call, not for a pure tax/harvest ask.

Question: {question}
JSON:"""


__all__ = ["PURPOSE", "PackRoute", "route_packs"]
