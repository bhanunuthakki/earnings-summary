"""Context packs for the single durable Work OS Ask surface."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Caps keep the portfolio system prompt interactive-sized even when the
# tracked universe is large (the watchlist sweep added ~60 eval names).
_MAX_THESIS_LINES = 30
_THESIS_LINE_CHARS = 200
_MAX_LIST_NAMES = 80


@dataclass(slots=True)
class ContextPack:
    """Everything scope-dependent about one conversational turn."""

    scope: Literal["portfolio"]
    default_tickers: list[str] = field(default_factory=list[str])
    system_context: str | None = None
    # The LLM purpose the portfolio-narrative path bills/routes under. Packs
    # that are conversational surfaces of their own (the positioning coach)
    # override this so their spend and model routing are governed separately
    # from the Ask tab (LLM_MODELS / llm_budgets are purpose-keyed).
    narrative_purpose: str = "ask_answer"


def build_portfolio_pack(repo_root: Path, db_path: Path) -> ContextPack:
    """The Ask tab's pack: the tracked universe, portfolio-scoped prompt."""
    by_list = _tracked_by_list(db_path)
    portfolio = by_list.get("portfolio", [])
    return ContextPack(
        scope="portfolio",
        default_tickers=portfolio,
        system_context=_portfolio_system_context(repo_root, by_list),
    )


def tracked_tickers(db_path: Path) -> set[str]:
    """Every ACTIVE tracked symbol (any list type, any user, archived names
    excluded per the repo-wide ``archived_at IS NULL`` convention) — the
    routing layer's membership test for ticker-ish tokens. Best-effort: empty
    on missing DB."""
    if not db_path.exists():
        return set()
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM tracked_companies WHERE archived_at IS NULL"
        ).fetchall()
        return {str(r[0]).upper() for r in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _tracked_by_list(db_path: Path) -> dict[str, list[str]]:
    """Tracked symbols grouped by list_type for the prompt's universe block."""
    if not db_path.exists():
        return {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, list_type FROM tracked_companies "
            "WHERE user_id = ? AND archived_at IS NULL ORDER BY list_type, ticker",
            (DEFAULT_USER_ID,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for ticker, list_type in rows:
        out.setdefault(str(list_type), []).append(str(ticker).upper())
    return out


def _thesis_one_liners(repo_root: Path, tickers: list[str]) -> list[str]:
    """Compact per-holding thesis lines from micro_thesis/holdings/<T>.json."""
    lines: list[str] = []
    for sym in tickers[:_MAX_THESIS_LINES]:
        path = repo_root / "micro_thesis" / "holdings" / f"{sym}.json"
        if not path.exists():
            continue
        try:
            payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        thesis = payload.get("thesis") or payload.get("thesis_full")
        if not isinstance(thesis, str) or not thesis.strip():
            continue
        flat = " ".join(thesis.split())
        if len(flat) > _THESIS_LINE_CHARS:
            flat = flat[: _THESIS_LINE_CHARS - 1].rstrip() + "…"
        lines.append(f"- {sym}: {flat}")
    return lines


def _owner_memory_block(repo_root: Path) -> str:
    """The owner's standing memory — Worldview tenets + themes + affirmed
    profile facts — for the OPEN-ENDED portfolio scope.

    2026-07-19 review, gap G3: these anchors rode only the retired report chat, so
    exactly the surface meant for macro/allocation/method questions ran with
    zero knowledge of the owner's beliefs. Same hand-composed spotlight
    treatment as the former report-chat prompt (this site doesn't route through
    compose_anchor_block); every loader degrades to "" and never raises, so a
    bare install renders the prompt unchanged."""
    bits: list[str] = []
    for loader_name, source in (
        ("load_worldview_anchor", "the investor's Worldview tenets"),
        ("load_themes_anchor", "the investor's standing themes"),
        ("load_owner_profile_anchor", "the owner's affirmed profile facts"),
    ):
        try:
            from llm import anchors as anchors_mod
            from llm.untrusted import spotlight

            raw = getattr(anchors_mod, loader_name)(repo_root)
            if raw:
                bits.append(spotlight(raw, source=source))
        except Exception:  # anchor stack must stay unkillable
            continue
    return ("\n\n" + "\n\n".join(bits)) if bits else ""


def _portfolio_system_context(repo_root: Path, by_list: dict[str, list[str]]) -> str:
    """The Ask tab's narrative system prompt — the portfolio sibling of
    the durable Ask system prompt. The grounding is the tracked universe."""
    portfolio = by_list.get("portfolio", [])
    universe_bits: list[str] = []
    for list_type, symbols in sorted(by_list.items()):
        shown = ", ".join(symbols[:_MAX_LIST_NAMES])
        more = (
            f" (+{len(symbols) - _MAX_LIST_NAMES} more)" if len(symbols) > _MAX_LIST_NAMES else ""
        )
        universe_bits.append(f"- {list_type}: {shown}{more}")
    universe = "\n".join(universe_bits) if universe_bits else "(no tracked companies on file)"

    thesis_lines = _thesis_one_liners(repo_root, portfolio)
    thesis_block = (
        "\n\nPER-HOLDING THESES (one-liners):\n" + "\n".join(thesis_lines) if thesis_lines else ""
    )
    owner_block = _owner_memory_block(repo_root)

    return f"""You are the analyst's portfolio research assistant, embedded in
the command center's Ask tab. Your scope is the whole tracked universe, not
a single report:

{universe}{thesis_block}{owner_block}

Portfolio-state evidence: when the question concerns the book itself
(sizing, weights, cost basis, valuation gaps, past decisions, the journal),
the numbered EVIDENCE block may carry portfolio packs — live holdings,
stated conviction and targets, DCF fair values, the decision ledger, open
notes, performance vs benchmarks. Cite them like any other numbered source;
when a pack reports "offline" or "none on file", say the data is
unavailable instead of estimating it.

Answer from this context first. Deterministic retrieval supplies the numbered
evidence for each turn; use only that evidence for factual claims and flag
anything it does not cover as unverified.

When the analyst asks you to **edit** something, respond with both a
natural-language explanation and a JSON code-fenced block at the end of
your message with the proposed diff in this exact shape:

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

Don't propose a diff unless the user explicitly asks for an edit.

When the analyst asks for a STANCE on a holding (buy/add/hold/trim/sell,
"what would you do", "should I size up") — do NOT give one here. Stances
exist only through the per-holding Socratic think-through
(http://localhost:7421/socratic/<TICKER>, also under Portfolio -> Memos),
which asks THEIR read first and records the result for outcome scoring.
You may still discuss evidence freely — the restriction is on stances.

Metric questions ("NU vs MELI revenue growth, last 8 quarters") render as
live data views before reaching you; "/view <question>" forces that path.
The discovery queue has deterministic commands too: "/discovery list",
"/discovery queue <T>", "/discovery dismiss <T>", "/discovery build <T>".
"""


__all__ = [
    "ContextPack",
    "build_portfolio_pack",
    "tracked_tickers",
]
