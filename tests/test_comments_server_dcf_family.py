"""Tests for comments_server DCF family routes (NU and MELI platform DCFs).

Covers the complete positive and negative test matrix for:
- GET /api/dcf/inputs/<ticker> (family shape, editable, read_only, active_pointer)
- POST /api/dcf/recompute (parity with builder mirror/scenarios_block without side effects)
- POST /api/dcf/save (staged promotion, active pointer, DB row, complete DcfRunRevision)
- MELI pending review proposal and byte-immutable retry
- Owner review / acceptance guards (stale review rejection)
- Field validation (read-only, bool-as-number, non-finite, unsupported keys, degenerate terminal)
- Committed input roundtrip & tampered active pointer fail-closed
- Identical-save reuse & writer contention (dcf_writer_busy)
- Pre-commit rollback & post-commit cleanup failure propagation
- Routing, family mismatch, and unsupported families
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import comments_server
import pytest
import refresh_dcf
from flask.testing import FlaskClient

from dcf import family_editor, meli_inputs
from dcf import input_evidence as evidence
from dcf import persist as persist_mod
from dcf.artifact_promotion import hold_dcf_artifacts
from dcf.redesign import ScenarioDeltas
from dcf.scenario_acceptance import ScenarioAcceptance, ScenarioReview
from execution import build_meli_platform_dcf as meli
from execution import build_nu_platform_dcf as nu
from tests.test_meli_input_evidence import (
    END,
    NOW,
    seal_model_projection_context,
    seed_real_input_facts,
    synthetic_input_values,
    synthetic_model_snapshot,
)

_NODE_BIN = (
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
    / "node"
    / "bin"
    / "node"
)
_NODE_MODULES = (
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
    / "node"
    / "node_modules"
)
if _NODE_BIN.is_file() and _NODE_MODULES.is_dir():
    os.environ.setdefault("DCF_ARTIFACT_NODE", str(_NODE_BIN))
    os.environ.setdefault("DCF_ARTIFACT_NODE_MODULES", str(_NODE_MODULES))


def _build_reviewed_request(
    conn: sqlite3.Connection,
    refs: dict[str, evidence.FactBinding],
    source_context: evidence.SourceReadContext,
    *,
    overrides: dict[str, float] | None = None,
) -> evidence.ModelInputRequest:
    inputs = synthetic_input_values()
    if overrides:
        inputs.update(overrides)
    request = evidence.ModelInputRequest(
        recipe=meli_inputs.RECIPE,
        ticker="MELI",
        research_snapshot_id="snapshot",
        financial_period_end=END,
        facts=refs,
        assumptions={
            key: evidence.AssumptionBasis(
                value=inputs[key],
                attribution="analyst",
                rationale="Explicit synthetic forecast choice",
            )
            for key in meli_inputs.ASSUMPTION_KEYS
        },
    )
    proof = evidence.verify_model_inputs(
        conn,
        request,
        recipe=meli_inputs.RECIPE,
        requirements=meli_inputs.requirements_for(END),
        effective_inputs={key: inputs[key] for key in meli_inputs.ASSUMPTION_KEYS},
        assumption_keys=meli_inputs.ASSUMPTION_KEYS,
        as_of=NOW,
        source_context=source_context,
    )
    actuals, _calculations = meli_inputs.calculate_actuals(proof)
    complete = {**inputs, **{key: actuals[key] for key in meli_inputs.REPORTED_DRIVER_KEYS}}
    review = evidence.AssumptionReview(
        reviewed_at=NOW,
        reviewer="synthetic analyst",
        effective_inputs_sha256=evidence.canonical_digest(complete),
        actuals_sha256=evidence.canonical_digest(actuals),
        drivers={
            key: evidence.DriverReview(
                observed=actual,
                forecast=forecast,
                variance=forecast - actual,
                rationale="Synthetic fixture explicitly accepts this forecast variance",
            )
            for key, (actual, forecast) in meli_inputs.review_drivers(actuals, complete).items()
        },
    )
    return request.model_copy(update={"assumption_review": review})


def _build_reviewed_scenarios(
    s: meli.Assum, receipt: evidence.ModelInputReceipt
) -> tuple[dict[str, meli.Assum], ScenarioAcceptance]:
    zero = ScenarioDeltas()
    cases: dict[str, meli.Assum] = {
        "base": s,
        "bear": meli.scenario_assumptions(s, zero),
        "bull": meli.scenario_assumptions(s, zero),
    }
    effective = meli.effective_numeric_inputs(dataclasses.asdict(s), recipe=receipt.recipe)
    vectors = {
        name: meli.effective_numeric_inputs(dataclasses.asdict(case), recipe=receipt.recipe)
        for name, case in cases.items()
    }
    replay = meli.replay_model_scenarios(
        "MELI",
        effective,
        meli.model_output(effective),
        {"reviewed_scenario_inputs": vectors},
        recipe=receipt.recipe,
    )
    scenarios = cast(dict[str, object], replay["scenarios"])
    acceptance = ScenarioAcceptance(
        ticker="MELI",
        recipe=receipt.recipe,
        model="meli_platform_sotp",
        engine_version="meli_platform_sotp_v1",
        reviewer="synthetic analyst",
        attribution="analyst",
        reviewed_at=receipt.verified_at,
        model_input_receipt_sha256=evidence.canonical_digest(receipt.model_dump(mode="json")),
        effective_inputs_sha256=evidence.canonical_digest(effective),
        actuals_sha256=cast(str, receipt.actuals_sha256),
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        inventory_snapshot_ids=receipt.inventory_snapshot_ids,
        model_output_sha256=evidence.canonical_digest(dict(replay)),
        scenarios={
            name: ScenarioReview(
                output_sha256=evidence.canonical_digest(value),
                accepted=True,
                rationale="Synthetic zero-delta coherent vector; no admission claim.",
            )
            for name, value in scenarios.items()
        },
    )
    return cases, acceptance


def _setup_nu_fixture(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> tuple[FlaskClient, Path, Path, Path]:
    repo_root = tmp_path / "nu-repo"
    state_root = tmp_path / "nu-state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")

    for root in (repo_root, state_root):
        holdings = root / "micro_thesis" / "holdings" / "NU.json"
        holdings.parent.mkdir(parents=True, exist_ok=True)
        holdings.write_text(
            json.dumps({"valuation_model": "platform_dcf", "mos_bar": 0.2}),
            encoding="utf-8",
        )

        assumptions = root / "data" / "bank_assumptions" / "NU_platform.json"
        assumptions.parent.mkdir(parents=True, exist_ok=True)
        assumptions.write_text(
            json.dumps({"price": 14.25, "tax": 0.31}),
            encoding="utf-8",
        )

        profile = root / "data" / "historical" / "fmp" / "NU_profile.json"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(
            json.dumps({"price": 14.25}),
            encoding="utf-8",
        )

    workbook = repo_root / "dcf" / "NU.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    s = nu.load_assumptions("NU", repo_root=repo_root, db_path=db_path)
    m = nu.mirror(s)
    nu.build(s, m, workbook, ticker="NU")
    nu.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="NU",
        repo_root=repo_root,
        db_path=db_path,
        workbook_path=workbook,
    )

    app = comments_server.create_app(
        repo_root,
        db_path=db_path,
        source_state_root=state_root,
    )
    return app.test_client(), repo_root, state_root, db_path


def _setup_meli_fixture(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FlaskClient, Path, Path, Path, dict[str, Path], meli.Assum]:
    repo_root = tmp_path / "meli-repo"
    state_root = tmp_path / "meli-state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")

    for root in (repo_root, state_root):
        holdings = root / "micro_thesis" / "holdings" / "MELI.json"
        holdings.parent.mkdir(parents=True, exist_ok=True)
        holdings.write_text(
            json.dumps({"valuation_model": "meli_platform_sotp"}),
            encoding="utf-8",
        )

    source_context = evidence.SourceReadContext.for_product_state_root(state_root)
    with sqlite3.connect(db_path) as conn:
        refs = seed_real_input_facts(
            conn,
            source_path=source_context.content_roots[0] / "fixture.json",
        )
        snapshot = synthetic_model_snapshot()
        seal_model_projection_context(conn, snapshot)

    def coverage(
        _conn: sqlite3.Connection,
        _request: evidence.ModelInputRequest,
        _cutoff: object,
        *,
        source_context: evidence.SourceReadContext | None = None,
    ) -> tuple[Any, str, tuple[str, ...]]:
        del source_context
        return snapshot, "b" * 64, ("inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)

    with sqlite3.connect(db_path) as conn:
        request = _build_reviewed_request(conn, refs, source_context)

    assumptions_path = state_root / "approved" / "MELI_sotp.json"
    assumptions_path.parent.mkdir(parents=True, exist_ok=True)
    assumptions_path.write_text(
        json.dumps({"input_evidence": request.model_dump(mode="json")}),
        encoding="utf-8",
    )
    s, receipt = meli.load_verified_assumptions(
        "MELI",
        db_path=db_path,
        assumptions_path=assumptions_path,
        source_context=source_context,
    )

    cases, _acceptance_pre = _build_reviewed_scenarios(s, receipt)
    vectors = {
        name: meli.effective_numeric_inputs(dataclasses.asdict(case), recipe=receipt.recipe)
        for name, case in cases.items()
    }
    assumptions_path.write_text(
        json.dumps(
            {
                "input_evidence": request.model_dump(mode="json"),
                "reviewed_scenario_inputs": vectors,
            }
        ),
        encoding="utf-8",
    )
    (repo_root / "approved").mkdir(parents=True, exist_ok=True)
    (repo_root / "approved" / "MELI_sotp.json").write_bytes(assumptions_path.read_bytes())

    receipt = receipt.model_copy(
        update={
            "assumptions_source_path": str(assumptions_path.resolve()),
            "assumptions_source_sha256": hashlib.sha256(assumptions_path.read_bytes()).hexdigest(),
        }
    )
    receipt_path = state_root / "approved" / "MELI_receipt.json"
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")

    cases, acceptance = _build_reviewed_scenarios(s, receipt)
    acceptance_path = state_root / "approved" / "MELI_acceptance.json"
    acceptance_path.write_text(acceptance.model_dump_json(), encoding="utf-8")

    workbook = repo_root / "dcf" / "MELI.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    m = meli.mirror(s)
    meli.build(s, m, dest=workbook, ticker="MELI", cases=cases, input_receipt=receipt)
    meli.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="MELI",
        repo_root=repo_root,
        db_path=db_path,
        workbook_path=workbook,
        input_receipt=receipt,
        assumptions_path=assumptions_path,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
    )

    app = comments_server.create_app(
        repo_root,
        db_path=db_path,
        source_state_root=state_root,
    )
    paths = {
        "assumptions": assumptions_path,
        "receipt": receipt_path,
        "acceptance": acceptance_path,
    }
    return app.test_client(), repo_root, state_root, db_path, paths, s


def test_family_inputs_get_nu_and_meli_shapes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_nu, _repo_nu, _state_nu, _db_nu = _setup_nu_fixture(tmp_path / "nu", migrated_db)
    resp_nu = client_nu.get("/api/dcf/inputs/NU")
    assert resp_nu.status_code == 200
    body_nu = cast("dict[str, Any]", resp_nu.get_json())
    assert body_nu["ticker"] == "NU"
    assert body_nu["family"] == family_editor.NU_FAMILY
    assert body_nu["editable"] == sorted(
        k for k, spec in family_editor.NU_FIELDS.items() if spec.editable
    )
    assert body_nu["read_only"] == {
        k: spec.read_only_reason
        for k, spec in family_editor.NU_FIELDS.items()
        if not spec.editable and spec.read_only_reason
    }
    assert isinstance(body_nu["inputs"], dict)
    assert body_nu["inputs"]["tax"] == 0.31
    assert body_nu["inputs"]["ke"] == 0.125
    assert body_nu["active_pointer"] is None

    (
        client_meli,
        _repo_meli,
        _state_meli,
        _db_meli,
        _paths_meli,
        _s_meli,
    ) = _setup_meli_fixture(tmp_path / "meli", migrated_db, monkeypatch)
    resp_meli = client_meli.get("/api/dcf/inputs/MELI")
    assert resp_meli.status_code == 200
    body_meli = cast("dict[str, Any]", resp_meli.get_json())
    assert body_meli["ticker"] == "MELI"
    assert body_meli["family"] == "meli_platform_sotp"
    assert body_meli["editable"] == sorted(
        k for k, spec in family_editor.MELI_FIELDS.items() if spec.editable
    )
    assert body_meli["read_only"] == {
        k: spec.read_only_reason
        for k, spec in family_editor.MELI_FIELDS.items()
        if not spec.editable and spec.read_only_reason
    }
    assert isinstance(body_meli["inputs"], dict)
    assert "comm_g_near" in body_meli["inputs"]
    assert body_meli["active_pointer"] is None


def test_family_preview_parity_nu_and_meli(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_nu, repo_nu, state_nu, db_nu = _setup_nu_fixture(tmp_path / "nu", migrated_db)
    wb_nu = repo_nu / "dcf" / "NU.xlsx"
    wb_nu_bytes = wb_nu.read_bytes()
    with sqlite3.connect(db_nu) as conn:
        row_count_nu_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    nu_edits = {"ke": 0.16, "terminal_roe": 0.28}
    resp_nu = client_nu.post(
        "/api/dcf/recompute",
        json={"ticker": "NU", "family": family_editor.NU_FAMILY, "inputs": nu_edits},
    )
    assert resp_nu.status_code == 200
    body_nu = cast("dict[str, Any]", resp_nu.get_json())
    assert body_nu["ticker"] == "NU"
    assert body_nu["family"] == family_editor.NU_FAMILY
    assert body_nu["edits"] == nu_edits

    curr_nu = refresh_dcf.family_current_vector(
        "NU", family_editor.NU_FAMILY, repo_nu, db_nu, state_nu
    )
    resolved_nu = {**curr_nu, **nu_edits}
    assum_nu = refresh_dcf.build_nu_assum(resolved_nu)
    m_nu = nu.mirror(assum_nu)
    assert body_nu["value_per_share"] == pytest.approx(m_nu.vps)
    assert body_nu["terminal_roe"] == pytest.approx(m_nu.roe_term)
    assert body_nu["value_per_share_residual_income"] == pytest.approx(m_nu.vps_ri)
    assert body_nu["value_per_share_exit_pe"] == pytest.approx(m_nu.vps_pe)

    assert wb_nu.read_bytes() == wb_nu_bytes
    with sqlite3.connect(db_nu) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == row_count_nu_before
    assert not family_editor.active_family_pointer_path(state_nu, "NU").exists()

    (
        client_meli,
        repo_meli,
        state_meli,
        db_meli,
        _paths_meli,
        _s_meli,
    ) = _setup_meli_fixture(tmp_path / "meli", migrated_db, monkeypatch)
    wb_meli = repo_meli / "dcf" / "MELI.xlsx"
    wb_meli_bytes = wb_meli.read_bytes()
    with sqlite3.connect(db_meli) as conn:
        row_count_meli_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    meli_edits = {"comm_g_near": 0.25, "comm_g_term": 0.06}
    resp_meli = client_meli.post(
        "/api/dcf/recompute",
        json={"ticker": "MELI", "family": "meli_platform_sotp", "inputs": meli_edits},
    )
    assert resp_meli.status_code == 200
    body_meli = cast("dict[str, Any]", resp_meli.get_json())
    assert body_meli["ticker"] == "MELI"
    assert body_meli["family"] == "meli_platform_sotp"

    curr_meli = refresh_dcf.family_current_vector(
        "MELI", "meli_platform_sotp", repo_meli, db_meli, state_meli
    )
    resolved_meli = {**curr_meli, **meli_edits}
    assum_meli = refresh_dcf.build_meli_assum(resolved_meli)
    m_meli = meli.mirror(assum_meli)
    assert body_meli["value_per_share"] == pytest.approx(m_meli.vps)
    assert body_meli["operating_ev_m"] == pytest.approx(m_meli.operating_ev)
    assert body_meli["credit_equity_value_m"] == pytest.approx(m_meli.credit_equity_value)
    assert body_meli["equity_value_m"] == pytest.approx(m_meli.equity_value)

    assert wb_meli.read_bytes() == wb_meli_bytes
    with sqlite3.connect(db_meli) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == row_count_meli_before
    assert not family_editor.active_family_pointer_path(state_meli, "MELI").exists()


def test_family_save_nu_commits_and_promotes_pointer(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    client, repo_root, state_root, db_path = _setup_nu_fixture(tmp_path, migrated_db)
    wb_path = repo_root / "dcf" / "NU.xlsx"
    prior_wb_bytes = wb_path.read_bytes()

    payload = {
        "ticker": "NU",
        "family": family_editor.NU_FAMILY,
        "inputs": {"ke": 0.165},
        "author": "Analyst Alice",
        "rationale": "Updating cost of equity following macro review (min 20 chars)",
    }
    resp = client.post("/api/dcf/save", json=payload)
    assert resp.status_code == 200
    body = cast("dict[str, Any]", resp.get_json())
    assert body["saved"] is True
    assert body["status"] == "ok"
    assert body["family"] == family_editor.NU_FAMILY
    assert body["inputs"]["ke"] == 0.165

    rev = body["model_revision"]
    assert isinstance(rev["row_id"], int) and rev["row_id"] > 0
    assert rev["ticker"] == "NU"
    assert rev["engine_version"] == "nu_platform_fcfe_v1"
    assert len(rev["input_sha256"]) == 64
    assert len(rev["workbook_sha256"]) == 64
    assert len(rev["assumption_snapshot_sha256"]) == 64
    assert isinstance(rev["provenance_sha256"], str)
    assert len(rev["provenance_sha256"]) == 64

    active_ptr = family_editor.read_active_family_pointer(state_root, "NU")
    assert active_ptr is not None
    assert active_ptr.proposal_id == body["proposal_id"]
    assert active_ptr.run_row_id == rev["row_id"]
    assert active_ptr.workbook_sha256 == rev["workbook_sha256"]

    new_wb_bytes = wb_path.read_bytes()
    assert new_wb_bytes != prior_wb_bytes
    assert hashlib.sha256(new_wb_bytes).hexdigest() == rev["workbook_sha256"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM dcf_runs WHERE id = ?", (rev["row_id"],)).fetchone()
        assert row is not None
        assert row["is_latest"] == 1
        assert row["workbook_sha256"] == rev["workbook_sha256"]

    get_resp = client.get("/api/dcf/inputs/NU")
    assert get_resp.status_code == 200
    get_body = cast("dict[str, Any]", get_resp.get_json())
    assert get_body["inputs"]["ke"] == 0.165
    assert get_body["active_pointer"]["proposal_id"] == body["proposal_id"]


def test_family_save_meli_with_reviewed_packet_commits(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _repo_root, state_root, _db_path, paths, s = _setup_meli_fixture(
        tmp_path, migrated_db, monkeypatch
    )
    monkeypatch.setenv("DCF_MELI_ASSUMPTIONS_PATH", str(paths["assumptions"]))
    monkeypatch.setenv("DCF_MELI_INPUT_RECEIPT_PATH", str(paths["receipt"]))
    monkeypatch.setenv("DCF_MELI_SCENARIO_ACCEPTANCE_PATH", str(paths["acceptance"]))
    monkeypatch.setenv(
        "DCF_MELI_ASSUMPTIONS_SHA256",
        hashlib.sha256(paths["assumptions"].read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(
        "DCF_MELI_SCENARIO_ACCEPTANCE_SHA256",
        hashlib.sha256(paths["acceptance"].read_bytes()).hexdigest(),
    )

    edits: dict[str, float | int] = {"comm_g_near": s.comm_g_near}
    payload: dict[str, Any] = {
        "ticker": "MELI",
        "family": "meli_platform_sotp",
        "inputs": edits,
        "author": "Analyst Bob",
        "rationale": "Applying analyst reviewed forecast package (min 20 chars)",
    }
    resp = client.post("/api/dcf/save", json=payload)
    assert resp.status_code == 200
    body = cast("dict[str, Any]", resp.get_json())
    assert body["saved"] is True
    assert body["status"] == "ok"
    assert body["family"] == "meli_platform_sotp"

    rev = body["model_revision"]
    assert isinstance(rev["row_id"], int) and rev["row_id"] > 0
    assert rev["ticker"] == "MELI"
    assert rev["engine_version"] == "meli_platform_sotp_v1"

    ptr = family_editor.read_active_family_pointer(state_root, "MELI")
    assert ptr is not None
    assert ptr.proposal_id == body["proposal_id"]
    assert ptr.run_row_id == rev["row_id"]

    get_resp = client.get("/api/dcf/inputs/MELI")
    assert get_resp.status_code == 200
    assert get_resp.get_json()["active_pointer"]["proposal_id"] == body["proposal_id"]


def test_family_save_meli_without_reviewed_packet_pending_handoff_and_retry_immutable(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repo_root, state_root, db_path, _paths, _s = _setup_meli_fixture(
        tmp_path, migrated_db, monkeypatch
    )
    monkeypatch.delenv("DCF_MELI_ASSUMPTIONS_PATH", raising=False)
    monkeypatch.delenv("DCF_MELI_INPUT_RECEIPT_PATH", raising=False)
    monkeypatch.delenv("DCF_MELI_SCENARIO_ACCEPTANCE_PATH", raising=False)
    monkeypatch.delenv("DCF_MELI_ASSUMPTIONS_SHA256", raising=False)
    monkeypatch.delenv("DCF_MELI_SCENARIO_ACCEPTANCE_SHA256", raising=False)

    wb_path = repo_root / "dcf" / "MELI.xlsx"
    wb_bytes_before = wb_path.read_bytes()
    with sqlite3.connect(db_path) as conn:
        row_count_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    payload = {
        "ticker": "MELI",
        "family": "meli_platform_sotp",
        "inputs": {"comm_g_near": 0.28},
        "author": "Analyst Charlie",
        "rationale": "Proposing unreviewed commerce rate change (min 20 chars)",
    }
    resp = client.post("/api/dcf/save", json=payload)
    assert resp.status_code == 200
    body = cast("dict[str, Any]", resp.get_json())
    assert body["saved"] is False
    assert body["status"] == "proposal_pending_review"
    assert "model_revision" not in body

    proposal_id = body["proposal_id"]
    proposal_path = Path(body["proposal_path"])
    assert proposal_path.is_file()
    assert proposal_id in str(proposal_path)
    proposal_bytes_first = proposal_path.read_bytes()

    handoff = body["pending_handoff"]
    assert handoff["proposal_id"] == proposal_id
    assert "DCF_MELI_ASSUMPTIONS_PATH" in handoff["missing_bindings"]

    assert wb_path.read_bytes() == wb_bytes_before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == row_count_before
    assert not family_editor.active_family_pointer_path(state_root, "MELI").exists()

    resp_retry = client.post("/api/dcf/save", json=payload)
    assert resp_retry.status_code == 200
    body_retry = cast("dict[str, Any]", resp_retry.get_json())
    assert body_retry["proposal_id"] == proposal_id
    assert proposal_path.read_bytes() == proposal_bytes_first


def test_family_save_meli_stale_review_guards_rejected(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _repo_root, _state_root, db_path, paths, s = _setup_meli_fixture(
        tmp_path, migrated_db, monkeypatch
    )
    with sqlite3.connect(db_path) as conn:
        row_count_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    monkeypatch.setenv("DCF_MELI_ASSUMPTIONS_PATH", str(paths["assumptions"]))
    monkeypatch.setenv("DCF_MELI_INPUT_RECEIPT_PATH", str(paths["receipt"]))
    monkeypatch.setenv("DCF_MELI_SCENARIO_ACCEPTANCE_PATH", str(paths["acceptance"]))

    # Guard 1: assumptions authority changed after dispatch
    monkeypatch.setenv("DCF_MELI_ASSUMPTIONS_SHA256", "0" * 64)
    monkeypatch.setenv(
        "DCF_MELI_SCENARIO_ACCEPTANCE_SHA256",
        hashlib.sha256(paths["acceptance"].read_bytes()).hexdigest(),
    )
    payload = {
        "ticker": "MELI",
        "family": "meli_platform_sotp",
        "inputs": {"comm_g_near": s.comm_g_near},
        "author": "Analyst David",
        "rationale": "Applying guarded assumptions package (min 20 chars)",
    }
    resp1 = client.post("/api/dcf/save", json=payload)
    assert resp1.status_code == 409
    assert resp1.get_json()["reason"] == "assumptions_authority_changed_after_dispatch"

    # Guard 2: scenario acceptance changed after dispatch
    monkeypatch.setenv(
        "DCF_MELI_ASSUMPTIONS_SHA256",
        hashlib.sha256(paths["assumptions"].read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("DCF_MELI_SCENARIO_ACCEPTANCE_SHA256", "0" * 64)
    resp2 = client.post("/api/dcf/save", json=payload)
    assert resp2.status_code == 409
    assert resp2.get_json()["reason"] == "scenario_acceptance_changed_after_dispatch"

    # Guard 3: assumptions authority changed after review
    monkeypatch.setenv(
        "DCF_MELI_SCENARIO_ACCEPTANCE_SHA256",
        hashlib.sha256(paths["acceptance"].read_bytes()).hexdigest(),
    )
    paths["assumptions"].write_text(
        json.dumps({"input_evidence": {"recipe": "meli_sotp_v1"}}), encoding="utf-8"
    )
    monkeypatch.setenv(
        "DCF_MELI_ASSUMPTIONS_SHA256",
        hashlib.sha256(paths["assumptions"].read_bytes()).hexdigest(),
    )
    resp3 = client.post("/api/dcf/save", json=payload)
    assert resp3.status_code == 409
    assert resp3.get_json()["reason"] == "assumptions_authority_changed_after_review"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == row_count_before


def test_family_field_validation_and_domain_errors(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_nu, _repo_nu, _state_nu, _db_nu = _setup_nu_fixture(tmp_path / "nu", migrated_db)

    # Read-only fields rejected -> 400
    r_ro = client_nu.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"price": 20.0},
            "author": "Analyst",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert r_ro.status_code == 400
    assert r_ro.get_json()["reason"] == "read_only_field"
    assert r_ro.get_json()["field"] == "price"

    # Bool-as-number rejected -> 400
    r_bool = client_nu.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"ke": True},
            "author": "Analyst",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert r_bool.status_code == 400
    assert r_bool.get_json()["reason"] == "bool_as_number_rejected"

    # Non-finite editable rejected -> 400
    r_inf = client_nu.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"ke": float("inf")},
            "author": "Analyst",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert r_inf.status_code == 400
    assert r_inf.get_json()["reason"] == "non_finite"

    # Unsupported keys rejected -> 400
    r_unsupp = client_nu.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"unknown_field_xyz": 1.0},
            "author": "Analyst",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert r_unsupp.status_code == 400
    assert r_unsupp.get_json()["reason"] == "unsupported_field"

    # Zero/negative-valid accepted -> 200
    r_zero = client_nu.post(
        "/api/dcf/recompute",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"g_term": 0.0},
        },
    )
    assert r_zero.status_code == 200

    # MELI degenerate terminal -> 422
    (
        client_meli,
        _repo_meli,
        _state_meli,
        _db_meli,
        _paths,
        _s_meli,
    ) = _setup_meli_fixture(tmp_path / "meli", migrated_db, monkeypatch)
    r_degen = client_meli.post(
        "/api/dcf/recompute",
        json={
            "ticker": "MELI",
            "family": "meli_platform_sotp",
            "inputs": {"cbg_term": 0.05, "credit_g_term": 0.02},
        },
    )
    assert r_degen.status_code == 422
    assert r_degen.get_json()["reason"] == "degenerate_terminal_inputs"


def test_family_committed_roundtrip_and_tampered_pointer_fails_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    client, _repo_root, state_root, db_path = _setup_nu_fixture(tmp_path, migrated_db)

    save_resp = client.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"ke": 0.165},
            "author": "Analyst Eva",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert save_resp.status_code == 200

    # Committed input roundtrip from database snapshot
    committed = refresh_dcf.family_committed_inputs(db_path, "NU", family_editor.NU_FAMILY)
    assert committed is not None
    assert committed["ke"] == 0.165
    assert committed["tax"] == 0.31

    # Tampered pointer: corrupt JSON -> 409 fail closed, no silent baseline fallback
    ptr_path = family_editor.active_family_pointer_path(state_root, "NU")
    ptr_path.write_bytes(b"{corrupted json")
    get_corrupt = client.get("/api/dcf/inputs/NU")
    assert get_corrupt.status_code == 409
    assert "family_pointer_invalid:NU" in get_corrupt.get_json()["error"]

    # Tampered pointer: mismatched workbook_sha256 -> 409
    save_body = cast("dict[str, Any]", save_resp.get_json())
    # Even if read failed due to corrupt json, write a well-formed pointer with bad sha:
    tampered_dict: dict[str, Any] = {
        "schema_version": "dcf_family_active.v1",
        "ticker": "NU",
        "family": family_editor.NU_FAMILY,
        "proposal_id": save_body["proposal_id"],
        "proposal_sha256": "a" * 64,
        "run_row_id": save_body["model_revision"]["row_id"],
        "input_sha256": save_body["model_revision"]["input_sha256"],
        "workbook_sha256": "0" * 64,
        "engine_version": save_body["model_revision"]["engine_version"],
        "assumption_snapshot_sha256": save_body["model_revision"]["assumption_snapshot_sha256"],
        "updated_at": family_editor.now_utc().isoformat(),
    }
    ptr_path.write_text(json.dumps(tampered_dict), encoding="utf-8")
    get_tampered = client.get("/api/dcf/inputs/NU")
    assert get_tampered.status_code == 409
    assert "family_pointer_invalid:NU" in get_tampered.get_json()["error"]


def test_family_identical_save_reuse_and_cache_break(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    client, repo_root, _state_root, db_path = _setup_nu_fixture(tmp_path, migrated_db)

    payload = {
        "ticker": "NU",
        "family": family_editor.NU_FAMILY,
        "inputs": {"ke": 0.165},
        "author": "Analyst Frank",
        "rationale": "Valid length rationale string here for test",
    }
    resp1 = client.post("/api/dcf/save", json=payload)
    assert resp1.status_code == 200
    body1 = cast("dict[str, Any]", resp1.get_json())
    assert body1["saved"] is True
    assert body1["reused_existing_revision"] is False

    with sqlite3.connect(db_path) as conn:
        count_after_save1 = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    # Identical save reuse
    resp2 = client.post("/api/dcf/save", json=payload)
    assert resp2.status_code == 200
    body2 = cast("dict[str, Any]", resp2.get_json())
    assert body2["saved"] is True
    assert body2["reused_existing_revision"] is True
    assert body2["model_revision"]["row_id"] == body1["model_revision"]["row_id"]
    assert body2["model_revision"]["workbook_sha256"] == body1["model_revision"]["workbook_sha256"]

    with sqlite3.connect(db_path) as conn:
        count_after_save2 = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    assert count_after_save2 == count_after_save1

    # Cache break with changed input
    resp3 = client.post(
        "/api/dcf/save",
        json={**payload, "inputs": {"ke": 0.170}},
    )
    assert resp3.status_code == 200
    body3 = cast("dict[str, Any]", resp3.get_json())
    assert body3["saved"] is True
    assert body3["reused_existing_revision"] is False

    with sqlite3.connect(db_path) as conn:
        count_after_save3 = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    assert count_after_save3 == count_after_save2 + 1

    # Concurrent save returns 409 dcf_writer_busy
    with (
        hold_dcf_artifacts(repo_root, "NU", owner="concurrent-test", wait_s=0),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        resp_busy = pool.submit(client.post, "/api/dcf/save", json=payload).result(timeout=5)
    assert resp_busy.status_code == 409
    assert resp_busy.get_json()["error"] == "dcf_writer_busy"


def test_family_precommit_rollback_and_postcommit_cleanup_failure(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repo_root, state_root, db_path = _setup_nu_fixture(tmp_path, migrated_db)
    wb_path = repo_root / "dcf" / "NU.xlsx"
    wb_bytes_initial = wb_path.read_bytes()

    with sqlite3.connect(db_path) as conn:
        count_initial = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    # Pre-commit failure: builder throws -> rollback, wb untouched, no DB row
    def fail_build(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated builder crash before commit")

    monkeypatch.setattr(nu, "build", fail_build)
    resp_fail = client.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"ke": 0.165},
            "author": "Analyst Grace",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert resp_fail.status_code == 500
    assert wb_path.read_bytes() == wb_bytes_initial
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == count_initial
    assert not family_editor.active_family_pointer_path(state_root, "NU").exists()

    monkeypatch.undo()

    # Post-commit cleanup failure: commits row+pointer, returns 200 with warning
    orig_persist = nu.persist_dcf_run_with_receipt

    def cleanup_fail_persist(*args: Any, **kwargs: Any) -> persist_mod.DcfPersistenceResult:
        real_result = orig_persist(*args, **kwargs)
        assert real_result is not None
        raise persist_mod.DcfCommittedPointerCleanupError(
            real_result,
            "retained staging files in cleanup failure simulation",
        )

    monkeypatch.setattr(nu, "persist_dcf_run_with_receipt", cleanup_fail_persist)
    resp = client.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"ke": 0.165},
            "author": "Analyst Grace",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert resp.status_code == 200
    body = cast("dict[str, Any]", resp.get_json())
    assert body["saved"] is True
    assert body["status"] == "committed_cleanup_failed"
    assert body["recovery_required"] is True
    assert "retained staging files" in body["cleanup_warning"]
    assert isinstance(body["model_revision"]["row_id"], int)

    active_ptr = family_editor.read_active_family_pointer(state_root, "NU")
    assert active_ptr is not None
    assert active_ptr.run_row_id == body["model_revision"]["row_id"]


def test_family_routing_and_unsupported_families(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    client, repo_root, _state_root, db_path = _setup_nu_fixture(tmp_path, migrated_db)

    # Unsupported family ticker -> 409 family_unsupported
    bank_holdings = repo_root / "micro_thesis" / "holdings" / "ITUB.json"
    bank_holdings.parent.mkdir(parents=True, exist_ok=True)
    bank_holdings.write_text(
        json.dumps({"valuation_model": "bank_excess_return"}), encoding="utf-8"
    )

    resp_bank = client.get("/api/dcf/inputs/ITUB")
    assert resp_bank.status_code == 409
    assert resp_bank.get_json()["reason"] == "family_unsupported"

    # Family mismatch -> 409 family_mismatch
    resp_mismatch = client.post(
        "/api/dcf/recompute",
        json={"ticker": "NU", "family": "meli_platform_sotp", "inputs": {}},
    )
    assert resp_mismatch.status_code == 409
    assert resp_mismatch.get_json()["reason"] == "family_mismatch"

    # Missing state_root -> 400 family_state_root_required
    app_no_state = comments_server.create_app(repo_root, db_path=db_path)
    client_no_state = app_no_state.test_client()
    resp_no_state = client_no_state.post(
        "/api/dcf/save",
        json={
            "ticker": "NU",
            "family": family_editor.NU_FAMILY,
            "inputs": {"ke": 0.165},
            "author": "Analyst",
            "rationale": "Valid length rationale string here for test",
        },
    )
    assert resp_no_state.status_code == 400
    assert resp_no_state.get_json()["reason"] == "family_state_root_required"

    # FCFF ticker without workbook -> 404
    resp_fcff = client.get("/api/dcf/inputs/AAPL")
    assert resp_fcff.status_code == 404
