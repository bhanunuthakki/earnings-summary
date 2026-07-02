"""The capture_intent classifier — the live intent gate on captured musings.

The TRUST-ZONE pre-gate stays (no LLM on a non-musing / fetched body); the regex
pre-gate is GONE — the classifier runs on EVERY owner musing (the maximalist fix
for the 'curious about the takeaways here' the old allowlist dropped). This
unit-tests the gating + parse seam with an injected call; the real-LLM quality bar
is the golden set (evals/golden/capture_intent.json)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from research.intent import INTENTS, classify_intent, classify_intent_for_eval

Call = Callable[[str], "dict[str, object]"]


def _mk(intent: str, *, ticker: object = None, claim: str = "x") -> Call:
    def call(_text: str) -> dict[str, object]:
        return {"intent": intent, "claim": claim, "ticker": ticker}

    return call


def test_intents_enum_is_the_closed_set() -> None:
    assert set(INTENTS) == {"observation", "wondering", "brief_artifact", "stress_artifact"}


def test_trust_zone_gate_short_circuits_non_musing() -> None:
    calls = {"n": 0}

    def spy(_t: str) -> dict[str, object]:
        calls["n"] += 1
        return {"intent": "wondering"}

    v = classify_intent("research this: do margins hold?", kind="decision", call=spy)
    assert v.intent == "observation"
    assert v.gate == "trust_zone"
    assert calls["n"] == 0  # a non-musing never reaches the model (token + injection boundary)


def test_trust_zone_gate_blocks_fetched_text() -> None:
    calls = {"n": 0}

    def spy(_t: str) -> dict[str, object]:
        calls["n"] += 1
        return {"intent": "brief_artifact"}

    v = classify_intent("ingest this and summarize", provenance="contains_fetched", call=spy)
    assert v.intent == "observation" and v.gate == "trust_zone"
    assert calls["n"] == 0


def test_no_regex_gate_every_owner_musing_reaches_the_model() -> None:
    # A flat observation with none of the old trigger words STILL calls the model —
    # that is the whole point: semantics decide, not a keyword allowlist.
    calls = {"n": 0}

    def spy(_t: str) -> dict[str, object]:
        calls["n"] += 1
        return {"intent": "observation"}

    v = classify_intent("NU's NPL formation ticked up to 4.5% this quarter", call=spy)
    assert calls["n"] == 1
    assert v.intent == "observation" and v.gate == "llm"


def test_each_intent_flows_through() -> None:
    assert classify_intent("x", call=_mk("wondering")).is_wondering
    brief = classify_intent("x", call=_mk("brief_artifact"))
    assert brief.intent == "brief_artifact" and brief.is_artifact
    stress = classify_intent("x", call=_mk("stress_artifact"))
    assert stress.intent == "stress_artifact" and stress.is_artifact
    obs = classify_intent("x", call=_mk("observation"))
    assert not obs.is_artifact and not obs.is_wondering


def test_ticker_normalized_uppercase_and_nullish_dropped() -> None:
    assert classify_intent("x", call=_mk("wondering", ticker="nu")).ticker == "NU"
    assert classify_intent("x", call=_mk("wondering", ticker="null")).ticker is None
    assert classify_intent("x", call=_mk("wondering", ticker=None)).ticker is None


def test_unknown_or_missing_intent_fails_closed_to_observation() -> None:
    # An unexpected enum value must never fabricate an artifact action.
    v = classify_intent("x", call=lambda _t: {"intent": "banana"})
    assert v.intent == "observation" and v.gate == "llm"
    assert classify_intent("x", call=lambda _t: {}).intent == "observation"


def test_claim_defaults_to_the_musing_text() -> None:
    v = classify_intent("some musing body", call=lambda _t: {"intent": "wondering"})
    assert v.claim == "some musing body"


def test_eval_entry_returns_only_pinned_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import research.intent as intent_mod

    def fake(_t: str) -> dict[str, object]:
        return {"intent": "wondering", "ticker": "nu"}

    monkeypatch.setattr(intent_mod, "_default_call", fake)
    out = classify_intent_for_eval("do NU's margins hold?")
    assert out == {"intent": "wondering", "ticker": "NU"}
