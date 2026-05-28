"""Tests for the trigger framework's Protocol + draft shapes.

Locks down the contract the downstream PRs (real trigger impls + the
CRUD layer that persists drafts) build against:

  * Protocol importable without circular import
  * KpiInflectionTrigger (fully implemented as of PR-N11) satisfies the
    runtime_checkable Protocol and exposes the right kind/cadence
  * Its lifecycle methods degrade gracefully on degenerate input — an
    unbacked connection / empty evidence — and fail loud where evidence is
    required (build_alert)
  * Dataclass field shapes match the documented contract
  * Drafts are immutable (frozen) so a sensor can't accidentally
    mutate one between build_alert and the CRUD INSERT
  * Cadence enum covers exactly the four driver phases
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime

import pytest

from triggers import (
    AlertDraft,
    Cadence,
    KpiInflectionTrigger,
    QueuedActionDraft,
    ThesisAnchor,
    Trigger,
    TriggerCandidate,
    UserStateContext,
)


def test_protocol_importable_without_circular_imports() -> None:
    # The asserts here are typed to force the imported symbols to land
    # at runtime even if the import block above gets reorganized.
    assert Trigger is not None
    assert Cadence is not None
    assert TriggerCandidate is not None
    assert AlertDraft is not None
    assert QueuedActionDraft is not None
    assert UserStateContext is not None
    assert ThesisAnchor is not None


def test_kpi_inflection_satisfies_runtime_checkable_protocol() -> None:
    instance = KpiInflectionTrigger()
    assert isinstance(instance, Trigger)


def test_kpi_inflection_class_attributes_match_contract() -> None:
    assert KpiInflectionTrigger.kind == "kpi_inflection"
    assert KpiInflectionTrigger.cadence is Cadence.DAILY


def test_scan_returns_empty_for_unbacked_connection() -> None:
    # An in-memory connection has no resolvable file path, so scan can't reach
    # the registry / loaders and degrades to [] rather than raising.
    conn = sqlite3.connect(":memory:")
    try:
        out = KpiInflectionTrigger().scan("AAPL", conn)
    finally:
        conn.close()
    assert out == []


def test_should_fire_false_for_empty_evidence() -> None:
    candidate = TriggerCandidate(
        ticker="AAPL",
        kind="kpi_inflection",
        key="placeholder",
        evidence={},
        computed_at=datetime(2026, 5, 27),
    )
    user_state = UserStateContext(
        registered_kpis=[],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert KpiInflectionTrigger().should_fire(candidate, user_state) is False


def test_build_alert_requires_populated_evidence() -> None:
    # build_alert is no longer a stub; it is only ever called on scan() output,
    # so it trusts the evidence is populated and fails loud (KeyError) on the
    # degenerate empty-evidence candidate rather than silently emitting junk.
    candidate = TriggerCandidate(
        ticker="AAPL",
        kind="kpi_inflection",
        key="placeholder",
        evidence={},
        computed_at=datetime(2026, 5, 27),
    )
    with pytest.raises(KeyError):
        _ = KpiInflectionTrigger().build_alert(candidate, None)


def test_draft_actions_empty_for_empty_evidence() -> None:
    candidate = TriggerCandidate(
        ticker="AAPL",
        kind="kpi_inflection",
        key="placeholder",
        evidence={},
        computed_at=datetime(2026, 5, 27),
    )
    draft = AlertDraft(
        trigger_kind="kpi_inflection",
        ticker="AAPL",
        fired_at=datetime(2026, 5, 27),
        evidence_json="{}",
        signature_sha="x",
        memo_text=None,
    )
    assert KpiInflectionTrigger().draft_actions(draft, candidate) == []


def _field_types(cls: type) -> dict[str, str]:
    """Map field name -> string type annotation (resolved via dataclass)."""
    return {f.name: f.type if isinstance(f.type, str) else str(f.type) for f in dataclasses.fields(cls)}


def test_trigger_candidate_fields() -> None:
    types = _field_types(TriggerCandidate)
    assert types == {
        "ticker": "str",
        "kind": "str",
        "key": "str",
        "evidence": "dict[str, Any]",
        "computed_at": "datetime",
    }


def test_alert_draft_fields() -> None:
    types = _field_types(AlertDraft)
    assert types == {
        "trigger_kind": "str",
        "ticker": "str",
        "fired_at": "datetime",
        "evidence_json": "str",
        "signature_sha": "str",
        "memo_text": "str | None",
    }


def test_queued_action_draft_fields() -> None:
    types = _field_types(QueuedActionDraft)
    assert types == {
        "action_kind": "str",
        "payload": "dict[str, Any]",
    }


def test_user_state_context_fields() -> None:
    types = _field_types(UserStateContext)
    assert types == {
        "registered_kpis": "list[dict[str, Any]]",
        "sizing_intents": "list[dict[str, Any]]",
        "recent_dismissed_signatures": "set[str]",
    }


def test_thesis_anchor_fields() -> None:
    types = _field_types(ThesisAnchor)
    assert types == {
        "ticker": "str",
        "thesis_statement": "str | None",
        "key_driver": "str | None",
        "tier_1_kpis": "list[dict[str, Any]]",
        "business_model_rules": "list[dict[str, Any]]",
    }


def test_alert_draft_is_frozen() -> None:
    draft = AlertDraft(
        trigger_kind="kpi_inflection",
        ticker="AAPL",
        fired_at=datetime(2026, 5, 27),
        evidence_json="{}",
        signature_sha="x",
        memo_text=None,
    )
    # setattr() routes through __setattr__ which the frozen dataclass
    # overrides to raise; using setattr lets the assertion compile under
    # strict typecheckers without a per-line ignore.
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(draft, "ticker", "MSFT")


def test_trigger_candidate_is_frozen() -> None:
    candidate = TriggerCandidate(
        ticker="AAPL",
        kind="kpi_inflection",
        key="k",
        evidence={},
        computed_at=datetime(2026, 5, 27),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(candidate, "ticker", "MSFT")


def test_queued_action_draft_is_frozen() -> None:
    qa = QueuedActionDraft(action_kind="thesis_update", payload={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(qa, "action_kind", "bear_append")


def test_cadence_has_four_expected_values() -> None:
    members = {m.name for m in Cadence}
    assert members == {"DAILY", "ON_EARNINGS", "ON_NEWS", "CALENDAR_DRIVEN"}


def test_cadence_string_values_stable() -> None:
    # String values are persistence-stable identifiers (see Cadence
    # docstring); guard against an accidental rename.
    assert Cadence.DAILY.value == "daily"
    assert Cadence.ON_EARNINGS.value == "on_earnings"
    assert Cadence.ON_NEWS.value == "on_news"
    assert Cadence.CALENDAR_DRIVEN.value == "calendar_driven"
