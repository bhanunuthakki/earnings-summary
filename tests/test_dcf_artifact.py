"""Governed DCF dry-run artifact draft and apply tests."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import research.apply as apply_mod
from dcf.provenance import build_effective_provenance
from research.dcf_artifact import _reconstruct_row, apply_dcf_proposal, draft_dcf_proposal


def _proposed(repo_root: Path) -> dict[str, object]:
    workbook = repo_root / "dcf" / "NU.xlsx"
    workbook.parent.mkdir(exist_ok=True)
    workbook.write_bytes(b"workbook-v1")
    snapshot = '{"g":0.4}'
    provenance = build_effective_provenance(
        ticker="NU",
        repo_root=repo_root,
        workbook_path=workbook,
        assumption_snapshot_json=snapshot,
        engine_version="test_dcf_v1",
    )
    return {
        "ticker": "NU",
        "valuation_date": "2026-06-30",
        "horizon_years": 10,
        "wacc": 0.11,
        "npv": 50000.0,
        "npv_per_share": 22.10,
        "shares_outstanding": 4.8e9,
        "currency": "USD",
        "live_price": 13.17,
        "live_price_at": "2026-06-30T08:00:00",
        "mos_bar_used": 0.25,
        "assumption_snapshot_json": snapshot,
        "notes": None,
        "run_id": None,
        "provenance": {
            **dataclasses.asdict(provenance),
            "inputs_as_of": provenance.inputs_as_of_iso(),
        },
    }


def test_dcf_registers_itself_behind_the_gate() -> None:
    assert apply_mod._MUTATING_APPLIERS.get("dcf") is apply_dcf_proposal


def test_draft_persists_a_dcf_proposal_with_oracle_ok(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    pid = draft_dcf_proposal(
        ticker="nu",
        proposed_row=_proposed(tmp_path),
        old_npv_per_share=18.0,
        repo_root=tmp_path,
        create_fn=lambda **kw: captured.update(kw) or 21,
    )
    assert pid == 21
    assert captured["kind"] == "dcf"
    assert captured["ticker"] == "NU"
    art = json.loads(str(captured["artifact_json"]))
    assert art["oracle_ok"] is True
    assert art["proposed_row"]["npv_per_share"] == 22.10
    assert "22.10" in str(captured["body_md"]) and "+22.8%" in str(captured["body_md"])


def test_draft_rejects_a_non_positive_or_missing_fair_value(tmp_path: Path) -> None:
    assert (
        draft_dcf_proposal(
            ticker="NU",
            proposed_row={"npv_per_share": 0.0},
            repo_root=tmp_path,
            create_fn=lambda **_k: 1,
        )
        is None
    )
    assert (
        draft_dcf_proposal(
            ticker="NU", proposed_row={}, repo_root=tmp_path, create_fn=lambda **_k: 1
        )
        is None
    )
    assert (
        draft_dcf_proposal(
            ticker="",
            proposed_row=_proposed(tmp_path),
            repo_root=tmp_path,
            create_fn=lambda **_k: 1,
        )
        is None
    )


def _create_one(**_kwargs: object) -> int:
    return 1


def test_draft_rejects_missing_or_tampered_provenance(tmp_path: Path) -> None:
    row = _proposed(tmp_path)
    row.pop("provenance")
    with pytest.raises(ValueError, match="provenance"):
        draft_dcf_proposal(ticker="NU", proposed_row=row, repo_root=tmp_path, create_fn=_create_one)

    tampered = _proposed(tmp_path)
    provenance = dict(cast("dict[str, object]", tampered["provenance"]))
    provenance["input_sha256"] = "f" * 64
    tampered["provenance"] = provenance
    with pytest.raises(ValueError, match="canonical commitments"):
        draft_dcf_proposal(
            ticker="NU", proposed_row=tampered, repo_root=tmp_path, create_fn=_create_one
        )


def test_draft_coerces_date_objects_so_json_survives(tmp_path: Path) -> None:
    row = _proposed(tmp_path)
    row["valuation_date"] = date(2026, 6, 30)
    row["live_price_at"] = datetime(2026, 6, 30, 8, 0, 0)
    captured: dict[str, object] = {}
    draft_dcf_proposal(
        ticker="NU",
        proposed_row=row,
        repo_root=tmp_path,
        create_fn=lambda **kw: captured.update(kw) or 1,
    )
    art = json.loads(str(captured["artifact_json"]))
    assert art["proposed_row"]["valuation_date"] == "2026-06-30"


def test_apply_reconstructs_and_upserts_via_persist(tmp_path: Path) -> None:
    proposed = _proposed(tmp_path)
    prop = SimpleNamespace(
        kind="dcf",
        ticker="NU",
        artifact_json=json.dumps({"proposed_row": proposed, "oracle_ok": True}),
    )
    persisted: list[dict[str, object]] = []
    note = apply_dcf_proposal(
        4,
        repo_root=tmp_path,
        get_fn=lambda _pid, **_k: prop,
        persist_fn=lambda row, **_k: persisted.append(row),
    )
    assert persisted and persisted[0]["ticker"] == "NU"
    assert "22.10" in note and "live" in note


def test_apply_rejects_non_dcf_and_missing_row(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a dcf"):
        apply_dcf_proposal(
            1,
            repo_root=tmp_path,
            get_fn=lambda _pid, **_k: SimpleNamespace(kind="memo", artifact_json="{}"),
        )
    bad = SimpleNamespace(kind="dcf", ticker="NU", artifact_json=json.dumps({"oracle_ok": True}))
    with pytest.raises(ValueError, match="no proposed_row"):
        apply_dcf_proposal(
            1,
            repo_root=tmp_path,
            get_fn=lambda _pid, **_k: bad,
            persist_fn=lambda *_a, **_k: None,
        )


def test_apply_oracle_recheck_blocks_a_non_positive_fair_value(tmp_path: Path) -> None:
    row = _proposed(tmp_path)
    row["npv_per_share"] = -5.0
    prop = SimpleNamespace(kind="dcf", ticker="NU", artifact_json=json.dumps({"proposed_row": row}))
    persisted: list[object] = []
    with pytest.raises(ValueError, match="fair value"):
        apply_dcf_proposal(
            1,
            repo_root=tmp_path,
            get_fn=lambda _pid, **_k: prop,
            persist_fn=lambda *_a, **_k: persisted.append(1),
        )
    assert not persisted


def test_apply_rejects_stale_proposal_after_workbook_changes(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> int:
        captured.update(kwargs)
        return 1

    draft_dcf_proposal(
        ticker="NU",
        proposed_row=_proposed(tmp_path),
        repo_root=tmp_path,
        create_fn=capture,
    )
    (tmp_path / "dcf" / "NU.xlsx").write_bytes(b"workbook-v2")
    prop = SimpleNamespace(kind="dcf", ticker="NU", artifact_json=captured["artifact_json"])
    persisted: list[object] = []

    def get_proposal(_proposal_id: int, **_kwargs: object) -> SimpleNamespace:
        return prop

    def persist(*_args: object, **_kwargs: object) -> None:
        persisted.append(1)

    with pytest.raises(ValueError, match="current file hash"):
        apply_dcf_proposal(
            1,
            repo_root=tmp_path,
            get_fn=get_proposal,
            persist_fn=persist,
        )
    assert not persisted


def test_reconstruct_row_roundtrips_to_a_valid_dcf_run_row(tmp_path: Path) -> None:
    row = _reconstruct_row(_proposed(tmp_path))
    assert row.ticker == "NU"
    assert row.valuation_date == date(2026, 6, 30)
    assert row.live_price_at == datetime(2026, 6, 30, 8, 0, 0)
    assert row.npv_per_share == 22.10
    assert row.provenance is not None
    assert len(row.provenance.input_sha256) == 64
