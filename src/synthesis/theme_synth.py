"""Per-holding stance synthesis — consolidate a name's musings into a cited
standing stance (Ledger Wave B; a morning-pipeline stage).

For each ticker that has captured musings advanced past its last-synthesized
watermark, one governed ``theme_synthesis`` LLM call distils the musings into the
investor's CURRENT STANCE, grounded in (and citing) those musings. The citation
set is then validated DETERMINISTICALLY against the input musing ids before any
insight is recorded — an LLM cannot fabricate a "you said X" that points at a
musing it wasn't given. Incremental + degrade-safe: a scope with no new musings
is skipped with zero LLM cost, and any per-scope LLM failure leaves the prior
cached stance standing.

The LLM call is injected (``call=``) so the engine is unit-testable without the
CLI; the default routes through ``call_llm_structured`` (purpose-pinned to Sonnet,
budget 0119, schema-validated).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from synthesis.insights import current_watermark, record_insight
from user_state.notes import AnalystNoteRow, list_notes

# A synthesis call: (ticker, musings) -> (stance_markdown, cited_note_ids) | None.
SynthCall = Callable[[str, Sequence[AnalystNoteRow]], "tuple[str, list[int]] | None"]


def _build_prompt(ticker: str, musings: Sequence[AnalystNoteRow]) -> str:
    lines = [f"- [{m.id}] ({m.created_at:%Y-%m-%d}) {m.body}" for m in musings]
    return (
        "You are consolidating an investor's OWN captured musings about one holding "
        "into their CURRENT STANDING STANCE. Ground every point ONLY in the musings "
        "below and cite the musing id(s) it rests on. Do NOT invent any view they did "
        "not express; if the musings are thin, say so briefly.\n\n"
        f"Holding: {ticker}\n"
        "Musings (id, date, text):\n" + "\n".join(lines) + "\n\n"
        'Return JSON ONLY: {"stance": "<markdown: their current stance in 2-5 sentences, '
        'each point grounded in the musings>", "citations": [<the musing ids you used>]}'
    )


def _default_call(ticker: str, musings: Sequence[AnalystNoteRow]) -> tuple[str, list[int]] | None:
    from llm.structured import call_llm_structured  # lazy: tests/CI need no CLI

    obj = call_llm_structured(
        _build_prompt(ticker, musings),
        purpose="theme_synthesis",
        ticker=ticker,
        expect="object",
        required_keys=("stance", "citations"),
    )
    if not isinstance(obj, dict):
        return None
    data = cast("dict[str, object]", obj)
    stance = str(data.get("stance") or "")
    raw = data.get("citations")
    cites: list[int] = []
    if isinstance(raw, list):
        for c in cast("list[object]", raw):
            if isinstance(c, int):
                cites.append(c)
            elif isinstance(c, str) and c.strip().isdigit():
                cites.append(int(c))
    return stance, cites


def run_synthesis(
    db_path: Path | str | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    call: SynthCall | None = None,
) -> dict[str, int]:
    """Synthesize per-ticker stances over captured musings. Returns counts.
    Incremental (watermark-gated) and degrade-safe (per-scope failures skip)."""
    synth = call or _default_call
    musings = list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=10_000)

    by_ticker: dict[str, list[AnalystNoteRow]] = {}
    for note in musings:
        if note.ticker:
            by_ticker.setdefault(note.ticker, []).append(note)

    counts = {
        "scopes": 0,
        "synthesized": 0,
        "skipped_unchanged": 0,
        "skipped_groundless": 0,
        "failed": 0,
    }
    for ticker, group in by_ticker.items():
        counts["scopes"] += 1
        max_id = max(n.id for n in group)
        watermark = current_watermark(ticker, "stance", db_path=db_path)
        if watermark is not None and max_id <= watermark:
            counts["skipped_unchanged"] += 1
            continue
        try:
            result = synth(ticker, group)
        except Exception as exc:
            # Hard stops (budget exhausted, auth failure) must propagate — they
            # affect every scope, not just this one. Soft failures (parse, net)
            # are per-scope and safely degrade.
            from llm.cli import LLMBudgetExceeded, LLMSetupError

            if isinstance(exc, (LLMBudgetExceeded, LLMSetupError)):
                raise
            counts["failed"] += 1
            continue
        if result is None:
            counts["failed"] += 1
            continue
        stance, citations = result
        valid_ids = {n.id for n in group}
        cited = [c for c in citations if c in valid_ids]
        # Deterministic grounding gate: refuse a stance with no valid citation —
        # a "you said X" must point at a real musing the model was given.
        if not stance.strip() or not cited:
            counts["skipped_groundless"] += 1
            continue
        record_insight(
            scope_key=ticker,
            kind="stance",
            body_md=stance,
            source_note_ids=sorted(cited),
            watermark_id=max_id,
            meta={"ticker": ticker, "musing_count": len(group)},
            db_path=db_path,
        )
        counts["synthesized"] += 1
    return counts
