"""Explicit MELI owner-authority persistence regressions.

Reuses the actual reviewed ModelInputRequest/Receipt fixture machinery from
``test_meli_input_evidence`` (real published/ontologized/resolved facts in a
migrated database); only source-inventory coverage stays isolated, exactly as
in that suite. No migration is fabricated per test and no FK constraints are
avoided. Financial admission is real: ``load_verified_assumptions`` and the
in-gate ``verify_meli_inputs`` both run against the migrated database.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import openpyxl
import pytest

from dcf import input_evidence as evidence
from dcf import meli_inputs
from dcf import persist as persist_mod
from dcf.artifact_promotion import ArtifactPromotion, StagedFilePromotion
from dcf.redesign import ScenarioDeltas
from dcf.scenario_acceptance import ScenarioAcceptance, ScenarioReview
from execution import build_meli_platform_dcf as meli
from tests.test_meli_input_evidence import (
    END,
    NOW,
    seal_model_projection_context,
    seed_real_input_facts,
    synthetic_input_values,
    synthetic_model_snapshot,
)


def _reviewed_request(
    conn: sqlite3.Connection,
    refs: dict[str, evidence.FactBinding],
    source_context: evidence.SourceReadContext,
) -> evidence.ModelInputRequest:
    """Same real reviewed-request assembly as the MELI evidence suite."""
    inputs = synthetic_input_values()
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


@pytest.fixture
def source_context(tmp_path: Path) -> evidence.SourceReadContext:
    """Explicit isolated raw-byte authority; no database-path inference."""
    return evidence.SourceReadContext.for_sec_state_root(tmp_path / "source-state")


@pytest.fixture
def real_inputs(
    source_context: evidence.SourceReadContext,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[sqlite3.Connection, evidence.ModelInputRequest], None, None]:
    # Same fixture shape as test_meli_input_evidence.real_inputs: only
    # source-coverage verification is isolated; publication, metric
    # revisions, dimensions, resolver and provenance reads remain real.
    conn = sqlite3.connect(migrated_db(tmp_path / "meli-owner.db"))
    refs = seed_real_input_facts(conn, source_path=source_context.content_roots[0] / "fixture.json")
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
    try:
        yield conn, _reviewed_request(conn, refs, source_context)
    finally:
        conn.close()


def _reviewed_artifact(root: Path, request: evidence.ModelInputRequest) -> Path:
    artifact = root / "approved" / "MELI_sotp.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps({"input_evidence": request.model_dump(mode="json")}), encoding="utf-8"
    )
    return artifact


def _explicit_owner(
    conn: sqlite3.Connection,
    db: Path,
    source_context: evidence.SourceReadContext,
    tmp_path: Path,
    request: evidence.ModelInputRequest,
) -> tuple[Path, meli.Assum, evidence.ModelInputReceipt, Path]:
    root = tmp_path / "owner-root"
    root.mkdir(parents=True, exist_ok=True)
    artifact = _reviewed_artifact(root, request)
    s, receipt = meli.load_verified_assumptions(
        "MELI", db_path=db, assumptions_path=artifact, source_context=source_context
    )
    return root, s, receipt, artifact


def _reviewed_scenarios(
    s: meli.Assum, receipt: evidence.ModelInputReceipt
) -> tuple[dict[str, meli.Assum], ScenarioAcceptance]:
    """Explicitly reviewed coherent scenario vectors for a fixed base.

    Zero ``ScenarioDeltas`` keep the derived cases inside the
    ``validate_credit_terminal`` identity (nonzero seeds shift ``cbg_term``
    and ``credit_g_term`` differently), so the bear/bull vectors stay exactly
    the analyst-reviewed base and every replay hash matches the gate's.
    """
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


def test_explicit_owner_authority_overrides_ambient_globals_and_env(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    cases, acceptance = _reviewed_scenarios(s, receipt)
    ambient = tmp_path / "ambient-root"
    ambient_db = migrated_db(tmp_path / "ambient-state" / "portfolio.db")
    monkeypatch.setattr(meli, "REPO", ambient)
    monkeypatch.setattr(meli, "T", "ZZZ")
    monkeypatch.setattr(meli, "DEST", ambient / "dcf" / "ZZZ.xlsx")
    monkeypatch.setenv("DCF_REPO_ROOT", str(ambient))
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("DCF_DEST", str(ambient / "dcf" / "ZZZ.xlsx"))
    monkeypatch.setenv("DCF_PROMOTE_DEST", str(ambient / "dcf" / "promoted.xlsx"))
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))

    m = meli.mirror(s)
    staged = root / "dcf" / "MELI.staged.xlsx"
    live = root / "dcf" / "MELI.live.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    candidate = b"explicit staged workbook bytes"
    staged.write_bytes(candidate)
    live.write_bytes(b"prior live bytes the promotion must replace")

    result = meli.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="MELI",
        repo_root=root,
        db_path=db,
        workbook_path=staged,
        workbook_locator_path=live,
        input_receipt=receipt,
        assumptions_path=artifact,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
        artifact_promotion=StagedFilePromotion(staged, live),
    )
    assert result is not None
    assert result.written is True
    assert result.revision.ticker == "MELI"
    assert result.revision.workbook_sha256 == hashlib.sha256(candidate).hexdigest()
    assert live.read_bytes() == candidate
    assert not staged.exists()

    with sqlite3.connect(db) as check:
        ticker, provenance_json, snapshot_json = check.execute(
            "SELECT ticker, provenance_json, assumption_snapshot_json FROM dcf_runs WHERE id=?",
            (result.revision.row_id,),
        ).fetchone()
    assert ticker == "MELI"
    provenance = json.loads(provenance_json)
    assert provenance["ticker"] == "MELI"
    calculation_workbook = next(
        source for source in provenance["sources"] if source["role"] == "calculation_workbook"
    )
    assert calculation_workbook["path"] == "dcf/MELI.live.xlsx"
    assert calculation_workbook["sha256"] == hashlib.sha256(candidate).hexdigest()
    assert json.loads(snapshot_json)["workbook"] == str(live)

    with sqlite3.connect(ambient_db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
    assert not (ambient / "dcf" / "promoted.xlsx").exists()


def test_wrong_issuer_and_root_without_db_fail_closed(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    ambient_db = migrated_db(tmp_path / "decoy-state" / "portfolio.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    monkeypatch.setenv("DCF_REPO_ROOT", str(root))
    staged = root / "dcf" / "MELI.staged.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged bytes")

    with pytest.raises(ValueError, match="MELI platform owner requires ticker"):
        meli.persist_dcf_run_with_receipt(
            s,
            meli.mirror(s),
            ticker="NU",
            repo_root=root,
            db_path=db,
            workbook_path=staged,
            input_receipt=receipt,
            assumptions_path=artifact,
            source_context=source_context,
        )

    with pytest.raises(
        ValueError, match="explicit repo_root authority requires an explicit db_path"
    ):
        meli.load_assumptions("MELI", repo_root=root)

    # A missing explicit db_path must fail closed before any effect.
    incomplete: dict[str, Any] = {
        "ticker": "MELI",
        "repo_root": root,
        "workbook_path": staged,
        "input_receipt": receipt,
        "assumptions_path": artifact,
    }
    with pytest.raises(TypeError):
        meli.persist_dcf_run_with_receipt(s, meli.mirror(s), **incomplete)

    with sqlite3.connect(db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
    with sqlite3.connect(ambient_db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)


def test_missing_or_stale_receipt_evidence_remains_blocked(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    staged = root / "dcf" / "MELI.staged.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged bytes")
    m = meli.mirror(s)
    base: dict[str, Any] = {
        "ticker": "MELI",
        "repo_root": root,
        "db_path": db,
        "workbook_path": staged,
        "input_receipt": receipt,
        "assumptions_path": artifact,
        "source_context": source_context,
    }

    with pytest.raises(evidence.InputEvidenceError, match="model_input_receipt_required"):
        missing_receipt: dict[str, Any] = {**base, "input_receipt": None}
        meli.persist_dcf_run_with_receipt(s, m, **missing_receipt)

    with pytest.raises(
        evidence.InputEvidenceError, match="explicit_assumptions_authority_required"
    ):
        missing_authority: dict[str, Any] = {**base, "assumptions_path": None}
        meli.persist_dcf_run_with_receipt(s, m, **missing_authority)

    artifact.write_text(json.dumps({"input_evidence": {"recipe": "meli_sotp_v1"}}))
    with pytest.raises(
        evidence.InputEvidenceError, match="assumptions_authority_changed_after_review"
    ):
        meli.persist_dcf_run_with_receipt(s, m, **base)

    with sqlite3.connect(db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)


def test_unverified_and_mismatched_workbook_authority_is_rejected(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    workbook = root / "dcf" / "MELI.actual.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"actual workbook bytes")
    m = meli.mirror(s)
    claimed = root / "dcf" / "MELI.claimed.xlsx"

    with pytest.raises(ValueError, match="dcf_workbook_locator_unverified"):
        meli.persist_dcf_run_with_receipt(
            s,
            m,
            ticker="MELI",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
            workbook_locator_path=claimed,
            input_receipt=receipt,
            assumptions_path=artifact,
            source_context=source_context,
        )

    other_staged = root / "dcf" / "MELI.foreign.staged.xlsx"
    other_staged.write_bytes(workbook.read_bytes())
    with pytest.raises(ValueError, match="dcf_artifact_promotion_mismatch"):
        meli.persist_dcf_run_with_receipt(
            s,
            m,
            ticker="MELI",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
            workbook_locator_path=claimed,
            input_receipt=receipt,
            assumptions_path=artifact,
            source_context=source_context,
            artifact_promotion=StagedFilePromotion(other_staged, claimed),
        )

    with sqlite3.connect(db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker='MELI'").fetchone() == (0,)
    assert not claimed.exists()
    assert other_staged.exists()


def test_receipt_identity_and_legacy_bool_semantics(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    cases, acceptance = _reviewed_scenarios(s, receipt)
    workbook = root / "dcf" / "MELI.actual.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"identical workbook bytes")
    m = meli.mirror(s)
    kwargs: dict[str, Any] = {
        "ticker": "MELI",
        "repo_root": root,
        "db_path": db,
        "workbook_path": workbook,
        "input_receipt": receipt,
        "assumptions_path": artifact,
        "source_context": source_context,
        "cases": cases,
        "scenario_acceptance": acceptance,
    }

    first = meli.persist_dcf_run_with_receipt(s, m, **kwargs)
    repeated = meli.persist_dcf_run_with_receipt(s, m, **kwargs)
    assert first is not None and first.written is True
    assert repeated is not None and repeated.written is False
    assert repeated.revision == first.revision

    assert meli.persist_dcf_run(s, m, **kwargs) is False


def test_returned_receipt_is_stable_after_interleaved_commit(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    cases, acceptance = _reviewed_scenarios(s, receipt)
    workbook = root / "dcf" / "MELI.actual.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"workbook bytes")
    m = meli.mirror(s)
    original = persist_mod.persist_run

    def interleaved(
        conn: sqlite3.Connection,
        row: persist_mod.DcfRunRow,
        *,
        artifact_promotion: ArtifactPromotion | None = None,
    ) -> persist_mod.DcfPersistenceResult:
        result = original(conn, row, artifact_promotion=artifact_promotion)
        if row.provenance is not None:
            second = dataclasses.replace(
                row,
                npv_per_share=row.npv_per_share + 1.0,
                provenance=dataclasses.replace(row.provenance, input_sha256="f" * 64),
            )
            original(conn, second)
        return result

    monkeypatch.setattr(meli.persist_mod, "persist_run", interleaved)
    result = meli.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="MELI",
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
        input_receipt=receipt,
        assumptions_path=artifact,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
    )
    assert result is not None and result.written is True

    with sqlite3.connect(db) as check:
        latest_id = check.execute(
            "SELECT id FROM dcf_runs WHERE ticker='MELI' AND is_latest=1"
        ).fetchone()[0]
        persisted = check.execute(
            "SELECT npv_per_share FROM dcf_runs WHERE id=?", (result.revision.row_id,)
        ).fetchone()
    assert latest_id != result.revision.row_id
    assert persisted == (m.vps,)


class _CommitFailer:
    """Real connection proxy whose commit fails after a real promotion applies."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> _CommitFailer:
        self._conn.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._conn.__exit__(exc_type, exc_value, traceback)

    def commit(self) -> None:
        raise sqlite3.OperationalError("synthetic_commit_failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_failed_commit_restores_prior_live_workbook_and_current_row(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    cases, acceptance = _reviewed_scenarios(s, receipt)
    workbook = root / "dcf" / "MELI.actual.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"prior staged workbook")
    m = meli.mirror(s)
    first = meli.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="MELI",
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
        input_receipt=receipt,
        assumptions_path=artifact,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
    )
    assert first is not None and first.written is True

    live = root / "dcf" / "MELI.live.xlsx"
    prior_live = b"prior live workbook bytes"
    live.write_bytes(prior_live)
    staged = root / "dcf" / "MELI.staged.xlsx"
    staged.write_bytes(b"candidate staged workbook with different bytes")

    original_connect = meli.connect_sqlite

    def failing_connect(*args: Any, **kwargs: Any) -> _CommitFailer:
        return _CommitFailer(original_connect(*args, **kwargs))

    monkeypatch.setattr(meli, "connect_sqlite", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic_commit_failure"):
        meli.persist_dcf_run_with_receipt(
            s,
            m,
            ticker="MELI",
            repo_root=root,
            db_path=db,
            workbook_path=staged,
            workbook_locator_path=live,
            input_receipt=receipt,
            assumptions_path=artifact,
            source_context=source_context,
            cases=cases,
            scenario_acceptance=acceptance,
            artifact_promotion=StagedFilePromotion(staged, live),
        )

    assert live.read_bytes() == prior_live
    assert staged.read_bytes() == b"candidate staged workbook with different bytes"
    with sqlite3.connect(db) as check:
        rows = check.execute(
            "SELECT id, is_latest FROM dcf_runs WHERE ticker='MELI' ORDER BY id"
        ).fetchall()
    assert rows == [(first.revision.row_id, 1)]


def test_partial_injection_fails_closed_before_any_db_read(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """Any new-mode injection without an explicit ``db_path`` fails closed.

    Pre-fix, ``workbook_path``-only injection (locator defaults to the
    workbook, so the authority check passes) resolved the ambient
    ``EARNINGS_SUMMARY_DB_PATH`` database and could persist into it; the
    guard must fire before any database path is selected or read.
    """
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    ambient_db = migrated_db(tmp_path / "decoy-state" / "portfolio.db")
    monkeypatch.setattr(meli, "REPO", tmp_path / "ambient-root")
    monkeypatch.setattr(meli, "T", "ZZZ")
    monkeypatch.setattr(meli, "DEST", tmp_path / "ambient-root" / "dcf" / "ZZZ.xlsx")
    monkeypatch.setenv("DCF_REPO_ROOT", str(tmp_path / "ambient-root"))
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("DCF_DEST", str(tmp_path / "ambient-root" / "dcf" / "ZZZ.xlsx"))
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    staged = root / "dcf" / "MELI.staged.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged bytes must survive")
    live = root / "dcf" / "MELI.live.xlsx"
    live.write_bytes(b"prior live bytes must survive")
    m = meli.mirror(s)
    full_authority: dict[str, Any] = {
        "input_receipt": receipt,
        "assumptions_path": artifact,
        "source_context": source_context,
    }
    partial_calls: tuple[dict[str, Any], ...] = (
        {"ticker": "MELI"},
        {"repo_root": root},
        {"workbook_path": staged},
        {"workbook_locator_path": live},
        {"ticker": "MELI", **full_authority},
        {"repo_root": root, **full_authority},
        {"workbook_path": staged, **full_authority},
    )
    for kwargs in partial_calls:
        with pytest.raises(
            ValueError, match="explicit MELI owner authority requires an explicit db_path"
        ):
            meli.persist_dcf_run(s, m, **kwargs)
    assert staged.read_bytes() == b"staged bytes must survive"
    assert live.read_bytes() == b"prior live bytes must survive"
    with sqlite3.connect(db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
    with sqlite3.connect(ambient_db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
    assert not (tmp_path / "ambient-root" / "dcf" / "promoted.xlsx").exists()


def test_omitted_ticker_under_explicit_authority_resolves_family_identity(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """Explicit owner authority with an omitted ticker pins ``MELI``.

    Pre-fix the omitted ticker borrowed the ambient ``DCF_TICKER``/module
    ``T`` ("ZZZ" here), so the row and holdings load keyed on the decoy
    issuer; the fixed family identity must never track the environment.
    """
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    cases, acceptance = _reviewed_scenarios(s, receipt)
    ambient_db = migrated_db(tmp_path / "decoy-state" / "portfolio.db")
    monkeypatch.setattr(meli, "T", "ZZZ")
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    holdings = root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "MELI.json").write_text(json.dumps({"mos_bar": 0.33}), encoding="utf-8")
    (holdings / "ZZZ.json").write_text(json.dumps({"mos_bar": 0.99}), encoding="utf-8")
    workbook = root / "dcf" / "MELI.actual.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"omitted ticker workbook bytes")
    m = meli.mirror(s)

    written = meli.persist_dcf_run(
        s,
        m,
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
        input_receipt=receipt,
        assumptions_path=artifact,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
    )
    assert written is True
    with sqlite3.connect(db) as check:
        row = check.execute(
            "SELECT ticker, mos_bar_used, provenance_json FROM dcf_runs WHERE ticker='ZZZ'"
        ).fetchone()
        assert row is None
        ticker, mos_bar_used, provenance_json = check.execute(
            "SELECT ticker, mos_bar_used, provenance_json FROM dcf_runs WHERE ticker='MELI'"
        ).fetchone()
    assert ticker == "MELI"
    assert mos_bar_used == pytest.approx(0.33)
    provenance = json.loads(provenance_json)
    assert provenance["ticker"] == "MELI"

    with sqlite3.connect(ambient_db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)


def _workbook_runtime_or_skip() -> meli.WorkbookRuntime:
    if not os.environ.get("DCF_ARTIFACT_NODE") and not os.environ.get("DCF_ARTIFACT_NODE_MODULES"):
        pytest.skip("Real artifact engine requires explicit loader-supplied runtime paths")
    return meli.WorkbookRuntime.from_environment()


def test_real_workbook_bytes_are_hashed_and_promoted(
    source_context: evidence.SourceReadContext,
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """The persisted hash and locator commit the real built workbook bytes."""
    _workbook_runtime_or_skip()
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    root, s, receipt, artifact = _explicit_owner(conn, db, source_context, tmp_path, request)
    cases, acceptance = _reviewed_scenarios(s, receipt)
    ambient_db = migrated_db(tmp_path / "ambient-state" / "portfolio.db")
    monkeypatch.setattr(meli, "T", "ZZZ")
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    holdings = root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "MELI.json").write_text(json.dumps({"mos_bar": 0.33}), encoding="utf-8")
    (holdings / "ZZZ.json").write_text(json.dumps({"mos_bar": 0.99}), encoding="utf-8")
    m = meli.mirror(s)
    staged = root / "dcf" / "MELI.staged.xlsx"
    live = root / "dcf" / "MELI.live.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"prior live bytes the promotion must replace")
    meli.build(
        s,
        m,
        staged,
        json.loads((holdings / "MELI.json").read_text(encoding="utf-8")),
        ticker="MELI",
        cases=cases,
    )
    staged_bytes = staged.read_bytes()

    result = meli.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="MELI",
        repo_root=root,
        db_path=db,
        workbook_path=staged,
        workbook_locator_path=live,
        input_receipt=receipt,
        assumptions_path=artifact,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
        artifact_promotion=StagedFilePromotion(staged, live),
    )
    assert result is not None
    assert result.written is True
    assert result.revision.ticker == "MELI"
    assert result.revision.workbook_sha256 == hashlib.sha256(staged_bytes).hexdigest()
    assert live.read_bytes() == staged_bytes
    assert not staged.exists()

    with sqlite3.connect(db) as check:
        provenance_json = check.execute(
            "SELECT provenance_json FROM dcf_runs WHERE id=?", (result.revision.row_id,)
        ).fetchone()[0]
    provenance = json.loads(provenance_json)
    assert provenance["ticker"] == "MELI"
    calculation_workbook = next(
        source for source in provenance["sources"] if source["role"] == "calculation_workbook"
    )
    assert calculation_workbook["path"] == "dcf/MELI.live.xlsx"
    assert calculation_workbook["sha256"] == hashlib.sha256(staged_bytes).hexdigest()

    wb = openpyxl.load_workbook(live, data_only=False)
    try:
        assert "MELI" in str(wb["Dashboard"]["A1"].value)
        selected = wb["Dashboard"]["E2"].value
        assert isinstance(selected, str) and selected.startswith("=CHOOSE(")
        valuation_case = wb["Valuation"]["B1"].value
        assert isinstance(valuation_case, str) and valuation_case.startswith("='Dashboard'!")
    finally:
        wb.close()

    with sqlite3.connect(ambient_db) as check:
        assert check.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
