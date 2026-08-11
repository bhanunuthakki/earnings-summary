"""Phase-1 Wave-3 tests: the DCF dry-run artifact (draft + gated upsert apply).

The live upsert is exercised via an injected ``persist_fn`` spy; the risky
dict->DcfRunRow reconstruction (what the real live write builds) is validated
directly by a round-trip, so no test writes to a real dcf_runs.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import research.apply as apply_mod
from research.dcf_artifact import (
    _reconstruct_row,
    apply_dcf_proposal,
    draft_dcf_proposal,
)

_PROVENANCE: dict[str, object] = {
    "input_sha256": "a" * 64,
    "workbook_sha256": "b" * 64,
    "engine_version": "test_dcf_v1",
    "inputs_as_of": "2026-06-30T08:00:00+00:00",
    "detail": {
        "sources": [
            {
                "role": "effective_assumptions",
                "locator": "inline://dcf/NU/test",
                "sha256": "c" * 64,
            }
        ]
    },
}


_PROPOSED: dict[str, object] = {
    "ticker": "NU",
    "valuation_date": "2026-06-30",
    "horizon_years": 10,
    "wacc": 0.11,
    "npv": 50000.0,
    "npv_per_share": 22.10,
    "shares_outstanding": 4800.0,
    "currency": "USD",
    "live_price": 13.17,
    "live_price_at": "2026-06-30T08:00:00",
    "mos_bar_used": 0.25,
    "assumption_snapshot_json": '{"g": 0.4}',
    "notes": None,
    "run_id": None,
    "provenance": _PROVENANCE,
}


def test_dcf_registers_itself_behind_the_gate() -> None:
    assert apply_mod._MUTATING_APPLIERS.get("dcf") is apply_dcf_proposal


def test_draft_persists_a_dcf_proposal_with_oracle_ok() -> None:
    captured: dict[str, object] = {}
    pid = draft_dcf_proposal(
        ticker="nu",
        proposed_row=dict(_PROPOSED),
        old_npv_per_share=18.0,
        create_fn=lambda **kw: captured.update(kw) or 21,
    )
    assert pid == 21
    assert captured["kind"] == "dcf"
    assert captured["ticker"] == "NU"
    art = json.loads(str(captured["artifact_json"]))
    assert art["oracle_ok"] is True
    assert art["proposed_row"]["npv_per_share"] == 22.10
    assert "22.10" in str(captured["body_md"]) and "+22.8%" in str(captured["body_md"])


def test_draft_rejects_a_non_positive_or_missing_fair_value() -> None:
    assert (
        draft_dcf_proposal(
            ticker="NU", proposed_row={"npv_per_share": 0.0}, create_fn=lambda **_k: 1
        )
        is None
    )
    assert draft_dcf_proposal(ticker="NU", proposed_row={}, create_fn=lambda **_k: 1) is None
    assert (
        draft_dcf_proposal(ticker="", proposed_row=dict(_PROPOSED), create_fn=lambda **_k: 1)
        is None
    )


def _create_one(**_kwargs: object) -> int:
    return 1


def test_draft_rejects_missing_provenance() -> None:
    row = dict(_PROPOSED)
    row.pop("provenance")
    with pytest.raises(ValueError, match="provenance"):
        draft_dcf_proposal(ticker="NU", proposed_row=row, create_fn=_create_one)


def test_draft_coerces_date_objects_so_json_survives() -> None:
    row = dict(_PROPOSED)
    row["valuation_date"] = date(2026, 6, 30)
    row["live_price_at"] = datetime(2026, 6, 30, 8, 0, 0)
    captured: dict[str, object] = {}
    draft_dcf_proposal(
        ticker="NU", proposed_row=row, create_fn=lambda **kw: captured.update(kw) or 1
    )
    art = json.loads(str(captured["artifact_json"]))  # must not raise
    assert art["proposed_row"]["valuation_date"] == "2026-06-30"


def test_apply_reconstructs_and_upserts_via_persist(monkeypatch) -> None:
    prop = SimpleNamespace(
        kind="dcf",
        ticker="NU",
        artifact_json=json.dumps({"proposed_row": _PROPOSED, "oracle_ok": True}),
    )
    persisted: list[dict[str, object]] = []
    note = apply_dcf_proposal(
        4, get_fn=lambda _pid, **_k: prop, persist_fn=lambda row, **_k: persisted.append(row)
    )
    assert persisted and persisted[0]["ticker"] == "NU"
    assert "22.10" in note and "live" in note


def test_apply_rejects_non_dcf_and_missing_row() -> None:
    with pytest.raises(ValueError, match="not a dcf"):
        apply_dcf_proposal(
            1, get_fn=lambda _pid, **_k: SimpleNamespace(kind="memo", artifact_json="{}")
        )
    bad = SimpleNamespace(kind="dcf", ticker="NU", artifact_json=json.dumps({"oracle_ok": True}))
    with pytest.raises(ValueError, match="no proposed_row"):
        apply_dcf_proposal(1, get_fn=lambda _pid, **_k: bad, persist_fn=lambda *_a, **_k: None)


def test_apply_oracle_recheck_blocks_a_non_positive_fair_value(monkeypatch) -> None:
    row = dict(_PROPOSED)
    row["npv_per_share"] = -5.0
    prop = SimpleNamespace(kind="dcf", ticker="NU", artifact_json=json.dumps({"proposed_row": row}))
    persisted: list[object] = []
    with pytest.raises(ValueError, match="fair value"):
        apply_dcf_proposal(
            1, get_fn=lambda _pid, **_k: prop, persist_fn=lambda *_a, **_k: persisted.append(1)
        )
    assert not persisted  # never wrote


def test_reconstruct_row_roundtrips_to_a_valid_dcf_run_row() -> None:
    row = _reconstruct_row(_PROPOSED)
    assert row.ticker == "NU"
    assert row.valuation_date == date(2026, 6, 30)
    assert row.live_price_at == datetime(2026, 6, 30, 8, 0, 0)
    assert row.npv_per_share == 22.10
    assert row.horizon_years == 10
    assert row.wacc == 0.11
    assert row.currency == "USD"
    assert row.provenance is not None
    assert row.provenance.input_sha256 == "a" * 64
