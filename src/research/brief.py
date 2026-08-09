"""artifact_brief — takeaways + bull/bear (+ stress) over a saved article/deck (Phase 3).

The payoff of the engage pipeline: when the intent tap flags a musing
``brief_artifact``/``stress_artifact`` (Phase 1) and Phase 2 has extracted the
artifact's text, this produces the analyst-facing brief and stamps it onto the note
(``context['engage_brief']``) for the feed card + the Telegram push.

SECURITY: the artifact text is UNTRUSTED (owner-pasted URL / deck — an indirect
prompt-injection surface). It is wrapped with ``llm.untrusted.spotlight`` so the model
treats it as DATA, never instructions — the same defense the tap's trust-zone gate
gives the classifier. The text arrives already capped by Phase 2.

Two modes (from the recorded intent):
  * ``brief``  → {takeaways, bull, bear}
  * ``stress`` → the above + {changes_mind (falsifiers), second_order, portfolio_map}

Portfolio linkage: the owner's tracked tickers (+ the resolved ticker) are passed so a
stress brief maps the artifact's implications onto the book. Operational-purpose recipe
(directives/llm_calls.md): ``LLM_MODELS`` + ``prompt_versions`` only; ``call_llm_structured``
RAISES → the caller degrades (no brief beats a fabricated one), hard stops propagate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from llm.untrusted import spotlight
from research.artifact import ArtifactText, resolve_and_extract
from user_state.notes import get_note, patch_note_context

PURPOSE = "artifact_brief"

# (prompt) -> the LLM's schema-validated dict. Injected in tests.
BriefCall = Callable[[str], "dict[str, object]"]


def _str_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(s.strip() for s in cast("list[object]", value) if isinstance(s, str) and s.strip())


def _build_prompt(text: str, *, mode: str, ticker: str | None, holdings: Sequence[str]) -> str:
    held = ", ".join(holdings) if holdings else "(none tracked)"
    focus = f"The analyst has flagged {ticker}. " if ticker else ""
    wrapped = spotlight(text, source="saved article/deck (owner-pasted, untrusted)")
    if mode == "stress":
        ask = (
            "STRESS-TEST the artifact's argument for this analyst — do not merely "
            "summarize. Steelman it, then attack it. Return JSON ONLY:\n"
            '{"takeaways": ["<3-5 load-bearing claims, one line each>"], '
            '"bull": "<the strongest version of the artifact\'s case>", '
            '"bear": "<the strongest rebuttal / what it gets wrong or omits>", '
            '"changes_mind": "<the specific evidence that would falsify the takeaways>", '
            '"second_order": "<non-obvious second-order implications>", '
            '"portfolio_map": "<how this bears on the tracked names, by ticker; '
            "'none' if unrelated>\"}"
        )
    else:
        ask = (
            "Write a tight BRIEF of the artifact for this analyst — concrete, non-obvious, "
            "no filler. Return JSON ONLY:\n"
            '{"takeaways": ["<3-5 key takeaways, one line each>"], '
            '"bull": "<the bull reading>", "bear": "<the bear reading>"}'
        )
    return (
        "You are briefing a sharp buy-side analyst on something they saved to read. "
        f"{focus}Their tracked names: {held}.\n\n"
        f"{wrapped}\n\n"
        f"{ask}"
    )


def _default_call(prompt: str, *, ticker: str | None) -> dict[str, object]:
    from llm.contracts import ARTIFACT_BRIEF_SCHEMA
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        ticker=ticker,
        scope="ledger",
        expect="object",
        required_keys=("takeaways",),
        schema=ARTIFACT_BRIEF_SCHEMA,
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def build_brief(
    artifact: ArtifactText,
    *,
    mode: str,
    ticker: str | None = None,
    holdings: Sequence[str] = (),
    call: BriefCall | None = None,
) -> dict[str, object] | None:
    """Generate the brief payload (the dict stored in ``context['engage_brief']``) or
    None. Degrades to None on empty text, an unusable LLM response, or a transient
    failure; hard stops (budget/setup) propagate."""
    if not artifact.text.strip():
        return None
    from llm.cli import is_hard_stop
    from llm.structured import StructuredParseError

    prompt = _build_prompt(artifact.text, mode=mode, ticker=ticker, holdings=holdings)
    runner: BriefCall = call if call is not None else (lambda p: _default_call(p, ticker=ticker))
    try:
        raw = runner(prompt)
    except StructuredParseError:
        return None  # both attempts unusable — no brief beats a fabricated one
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        return None

    takeaways = _str_list(raw.get("takeaways"))
    if not takeaways:
        return None
    brief: dict[str, object] = {
        "mode": "stress" if mode == "stress" else "brief",
        "takeaways": list(takeaways),
        "bull": str(raw.get("bull") or "").strip(),
        "bear": str(raw.get("bear") or "").strip(),
        "source": artifact.source,
    }
    if mode == "stress":
        brief["changes_mind"] = str(raw.get("changes_mind") or "").strip()
        brief["second_order"] = str(raw.get("second_order") or "").strip()
        brief["portfolio_map"] = str(raw.get("portfolio_map") or "").strip()
    return brief


def generate_brief_for_note(
    note_id: int,
    *,
    db_path: Path | str | None = None,
    holdings: Sequence[str] = (),
    cache_dir: Path | str | None = None,
    call: BriefCall | None = None,
    fetch: Callable[[str], str] | None = None,
) -> dict[str, object] | None:
    """Resolve the artifact for a note flagged with an ``engage_intent``, brief it, and
    stamp ``context['engage_brief']``. Returns the brief dict, or None when there is no
    engage intent, no bindable artifact, or the brief degraded. Never raises on a
    transient failure (the caller is a fire-and-forget poller step)."""
    note = get_note(note_id, db_path=db_path)
    if note is None:
        return None
    engage = (note.context or {}).get("engage_intent")
    if not isinstance(engage, dict):
        return None
    engage_d = cast("dict[str, object]", engage)
    mode = "stress" if str(engage_d.get("mode") or "") == "stress" else "brief"
    ticker_val = engage_d.get("ticker")
    ticker = str(ticker_val) if isinstance(ticker_val, str) and ticker_val.strip() else note.ticker

    artifact = resolve_and_extract(note_id, db_path=db_path, cache_dir=cache_dir, fetch=fetch)
    if artifact is None:
        return None
    brief = build_brief(artifact, mode=mode, ticker=ticker, holdings=holdings, call=call)
    if brief is None:
        return None
    patch_note_context(note_id, {"engage_brief": brief}, db_path=db_path)
    return brief
