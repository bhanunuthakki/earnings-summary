"""Phase-1 Wave-2 tests: the saved-view artifact draft + apply primitives.

Pure-unit via the codebase's dependency-injection idiom (fake compiler / store /
saver) — no LLM, no DB. The apply path exercises the real ``ViewSpec.from_dict``
oracle, since re-validation is the safety guarantee before the live write.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from research.view_artifact import apply_view_proposal, draft_view_proposal
from viewspec.spec import ViewSpec, ViewSpecError

_VALID_SPEC_DICT = {"tickers": ["NU"], "metrics": ["fin:revenue"], "periods": 8}


def _real_spec() -> ViewSpec:
    return ViewSpec.from_dict(_VALID_SPEC_DICT)


def _fake_ok_compile(query: str, **_kw: object) -> SimpleNamespace:
    return SimpleNamespace(status="ok", spec=_real_spec(), message=None)


def test_draft_persists_a_view_proposal_carrying_the_spec() -> None:
    captured: dict[str, object] = {}

    def fake_create(**kw: object) -> int:
        captured.update(kw)
        return 77

    res = draft_view_proposal(
        claim="NU revenue last 8 quarters",
        ticker="NU",
        note_id=5,
        compile_fn=_fake_ok_compile,
        create_fn=fake_create,
    )
    assert res.status == "drafted"
    assert res.proposal_id == 77
    assert captured["kind"] == "view"
    assert captured["ticker"] == "NU"
    spec_back = json.loads(str(captured["artifact_json"]))
    assert spec_back["tickers"] == ["NU"]
    # the stored spec round-trips through the oracle (approve-safe)
    assert ViewSpec.from_dict(spec_back).tickers == ("NU",)


def test_draft_degrades_and_persists_nothing_when_compile_not_ok() -> None:
    calls: list[object] = []

    def fake_compile(query: str, **_kw: object) -> SimpleNamespace:
        return SimpleNamespace(status="error", spec=None, message="couldn't parse")

    res = draft_view_proposal(
        claim="???",
        compile_fn=fake_compile,
        create_fn=lambda **kw: calls.append(kw) or 1,
    )
    assert res.status == "degraded"
    assert res.proposal_id is None
    assert "couldn't parse" in res.message
    assert not calls  # nothing persisted


def test_apply_writes_the_real_saved_view() -> None:
    prop = SimpleNamespace(
        kind="view",
        status="approved",
        title="NU revenue",
        artifact_json=json.dumps(_real_spec().to_dict()),
    )
    saved: dict[str, object] = {}

    def fake_save(**kw: object) -> SimpleNamespace:
        saved.update(kw)
        return SimpleNamespace(id=1, name=kw["name"])

    row = apply_view_proposal(9, get_fn=lambda _pid, **_k: prop, save_fn=fake_save)
    assert saved["name"] == "NU revenue"
    assert isinstance(saved["spec"], dict)
    assert saved["spec"]["tickers"] == ["NU"]
    assert row.id == 1


def test_apply_name_override_wins() -> None:
    prop = SimpleNamespace(
        kind="view",
        status="approved",
        title="auto",
        artifact_json=json.dumps(_real_spec().to_dict()),
    )
    saved: dict[str, object] = {}
    apply_view_proposal(
        1, name="My NU view", get_fn=lambda _pid, **_k: prop, save_fn=lambda **kw: saved.update(kw)
    )
    assert saved["name"] == "My NU view"


def test_apply_rejects_a_non_view_proposal() -> None:
    prop = SimpleNamespace(kind="memo", status="approved", artifact_json="{}")
    with pytest.raises(ValueError, match="not a view"):
        apply_view_proposal(1, get_fn=lambda _pid, **_k: prop, save_fn=lambda **_k: None)


def test_apply_rejects_a_rejected_proposal() -> None:
    prop = SimpleNamespace(kind="view", status="rejected", artifact_json="{}")
    with pytest.raises(ValueError, match="cannot apply"):
        apply_view_proposal(1, get_fn=lambda _pid, **_k: prop, save_fn=lambda **_k: None)


def test_apply_requires_a_spec_payload() -> None:
    prop = SimpleNamespace(kind="view", status="approved", title="x", artifact_json=None)
    with pytest.raises(ValueError, match="no ViewSpec"):
        apply_view_proposal(1, get_fn=lambda _pid, **_k: prop, save_fn=lambda **_k: None)


def test_apply_oracle_rejects_a_now_invalid_spec() -> None:
    # A stored spec that no longer validates must NOT reach save_view.
    prop = SimpleNamespace(
        kind="view",
        status="approved",
        title="x",
        artifact_json=json.dumps({"tickers": [], "metrics": []}),
    )
    saved: list[object] = []
    with pytest.raises(ViewSpecError):
        apply_view_proposal(
            1, get_fn=lambda _pid, **_k: prop, save_fn=lambda **kw: saved.append(kw)
        )
    assert not saved  # the oracle blocked the write


def test_describe_view_spec_speaks_owner_language() -> None:
    """No ref grammar, no function names on cards — the 2026-07-02 'so
    confusing to read' correction."""
    from research.view_artifact import describe_view_spec

    title, body = describe_view_spec(
        {
            "tickers": ["NU"],
            "metrics": [
                {"domain": "fin", "key": "interest_expense"},
                {"domain": "kpi", "key": "Net interest margin"},
            ],
            "periods": 12,
        }
    )
    assert title == "Tracking table for NU — 2 series, last 12 quarters"
    assert "Interest expense · Net interest margin" in body
    for jargon in ("fin:", "kpi:", "execute_view", "["):
        assert jargon not in title and jargon not in body
