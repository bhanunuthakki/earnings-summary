"""Risk and supporting valuation factors must retain real model readiness gates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

import pytest

from allocation.book_risk import BookRisk
from dcf.input_evidence import (
    AssumptionBasis,
    AssumptionReview,
    DriverReview,
    InputEvidenceError,
    ModelInputRequest,
    canonical_digest,
    verify_model_inputs,
)
from dcf.latest import latest_dcf_row
from dcf.meli_input_preview import preview_meli_inputs
from dcf.meli_inputs import (
    ASSUMPTION_KEYS,
    RECIPE,
    REPORTED_DRIVER_KEYS,
    calculate_actuals,
    effective_numeric_inputs,
    model_output,
    prepare_meli_inputs,
    requirements_for,
    review_drivers,
)
from dcf.meli_model import Assum, mirror
from dcf.persist import DcfRunRow, upsert
from dcf.provenance import build_file_provenance
from dcf.readiness import ValuationReadiness, load_valuation_readiness
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.meli_role_admission import (
    ReviewedRoleAdmission,
    apply_reviewed_meli_role_admission,
    plan_meli_role_admission,
)
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.population_research_snapshots import assemble_research_snapshot_request
from provenance.research_snapshot import build_research_snapshot, verify_research_snapshot
from risk_reward import Reward, build_gap_rows
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_analysis_expected_document_inventory,
)
from tests.test_meli_input_evidence import real_inputs
from tests.test_meli_role_admission import PERIOD, synthetic_current_population

# This retained fixture replaces source coverage. Its replay test is unit-only;
# the full28 test below uses unpatched public snapshot and admission owners.
__all__ = ["real_inputs"]


class RewardLegs(Protocol):
    def __call__(
        self, db_path: Path, tickers: Sequence[str], today: date, *, as_of: datetime | None = None
    ) -> dict[str, Reward]: ...


reward_legs = cast(RewardLegs, getattr(import_module("risk_reward"), "_dcf_reward_legs"))


class DcfUpside(Protocol):
    def __call__(
        self,
        db_path: Path,
        tickers: Sequence[str],
        *,
        as_of: datetime | None = None,
        unavailable: dict[str, str] | None = None,
    ) -> dict[str, tuple[float, str]]: ...


valuation_factor = cast(DcfUpside, getattr(import_module("allocation.model"), "_dcf_upside"))
NOW = datetime(2026, 10, 1, 23, 59, 59, 999999, tzinfo=UTC)


def _persist(
    conn: sqlite3.Connection,
    *,
    ticker: str = "META",
    provenance: object = None,
    snapshot: object = None,
    npv: float = 150,
    total: float = 1000,
    at: datetime = NOW,
) -> Path:
    conn.execute(
        """INSERT INTO dcf_runs (ticker,valuation_date,horizon_years,revenue_growths_json,
        fcf_margin,wacc,terminal_growth,npv,npv_per_share,created_at,live_price,live_price_at,
        input_sha256,workbook_sha256,engine_version,inputs_as_of,assumption_snapshot_json,provenance_json)
        VALUES (?,?,10,'[]',0,.135,.045,?,?,?,100,?,?,?,'meli_platform_sotp_v1',?,?,?)""",
        (
            ticker,
            at.date().isoformat(),
            total,
            npv,
            at.isoformat(),
            at.isoformat(),
            "a" * 64,
            "b" * 64,
            at.isoformat(),
            json.dumps(snapshot or {}),
            json.dumps(provenance or {}),
        ),
    )
    conn.commit()
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def test_current_quote_and_scalar_value_do_not_establish_readiness(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db = migrated_db(tmp_path / "risk-readiness.db")
    with sqlite3.connect(db) as conn:
        _persist(conn)
    before = db.read_bytes()
    leg = reward_legs(db, ["META"], NOW.date())["META"]
    assert leg.expected_return is None
    assert leg.low_confidence
    assert "financial_input_lineage_missing" in (leg.confidence_reason or "")
    assert "financial_input_completeness_unverified" in (leg.confidence_reason or "")
    assert valuation_factor(db, ["META"], as_of=NOW) == {}
    assert db.read_bytes() == before
    book = BookRisk(
        tickers=["META"],
        weights={"META": 1},
        marginal_vol_ann={"META": 0.3},
        risk_contribution_ann={"META": 0.3},
        risk_share={"META": 1},
        corr_to_book={"META": 1},
        portfolio_vol_ann=0.3,
        prices_through=NOW.date(),
        cov_obs=252,
        shrinkage=0.1,
    )
    rows, valued = build_gap_rows(book, {"META": leg}, {"META": 2})
    assert valued == 0
    assert rows[0].mismatch_score == 2
    assert rows[0].reward_share_pct is None
    assert any("conviction 2/5" in reason for reason in rows[0].mismatch_reasons)


@pytest.mark.parametrize(
    ("price_at", "reason"),
    [
        ((NOW + timedelta(days=1)).isoformat(), "market_timestamp_after_cutoff"),
        ("invalid", "row_decode_failed"),
        ((NOW - timedelta(days=30)).isoformat(), "market_price_stale"),
    ],
)
def test_quote_rejections_remain_explicit(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    price_at: str,
    reason: str,
) -> None:
    db = migrated_db(tmp_path / "risk-quote.db")
    with sqlite3.connect(db) as conn:
        _persist(conn)
        conn.execute("UPDATE dcf_runs SET live_price_at=?", (price_at,))
    leg = reward_legs(db, ["META"], NOW.date())["META"]
    assert leg.expected_return is None
    assert reason in (leg.confidence_reason or "")


def test_replayed_reported_base_remains_unavailable_without_scenario_acceptance(
    real_inputs: tuple[sqlite3.Connection, ModelInputRequest],
) -> None:
    # Source coverage is a test double in this retained partial unit fixture.
    conn, request = real_inputs
    assert request.assumption_review is not None
    cutoff = request.assumption_review.reviewed_at
    inputs, receipt = prepare_meli_inputs(
        conn,
        request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=cutoff,
    )
    output = model_output(inputs)
    vps = output["vps"]
    equity_value = output["equity_value"]
    assert isinstance(vps, (float, int)) and isinstance(equity_value, (float, int))
    db = _persist(
        conn,
        ticker="MELI",
        npv=vps,
        total=equity_value,
        snapshot={
            "model": "meli_platform_sotp",
            "effective_model_inputs": inputs,
            "value_per_share": output["vps"],
            "equity_value_m": output["equity_value"],
            "operating_ev_m": output["operating_ev"],
            "credit_equity_value_m": output["credit_equity_value"],
        },
        provenance={"model_input_receipt": receipt.model_dump(mode="json")},
    )
    readiness = load_valuation_readiness(conn, "MELI", as_of=NOW)
    assert readiness.financial_input_completeness == "verified", readiness.reason_codes
    assert readiness.reason_codes == ("scenario_acceptance_unverified",)
    leg = reward_legs(db, ["MELI"], NOW.date())["MELI"]
    assert leg.expected_return is None
    assert leg.confidence_reason == "scenario_acceptance_unverified"
    unavailable: dict[str, str] = {}
    assert valuation_factor(db, ["MELI"], as_of=NOW, unavailable=unavailable) == {}
    assert unavailable == {"MELI": "scenario_acceptance_unverified"}
    conn.execute("UPDATE dcf_runs SET npv=npv+1000")
    conn.commit()
    assert "persisted_model_output_replay_mismatch" in (
        reward_legs(db, ["MELI"], NOW.date())["MELI"].confidence_reason or ""
    )


def test_exact_run_and_cutoff_share_a_read_only_snapshot(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = migrated_db(tmp_path / "risk-snapshot.db")
    with sqlite3.connect(db) as writer:
        _persist(writer)
        writer.execute("PRAGMA journal_mode=WAL")
    evaluated: list[datetime] = []

    def assess(conn: sqlite3.Connection, ticker: str, *, as_of: datetime) -> ValuationReadiness:
        assert conn.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE dcf_runs SET npv_per_share=888")
        row = latest_dcf_row(conn, ticker)
        assert row is not None and row.npv_per_share == 150
        with sqlite3.connect(db) as writer:
            writer.execute("UPDATE dcf_runs SET npv_per_share=999")
        receipt = load_valuation_readiness(conn, ticker, as_of=as_of)
        same_row = latest_dcf_row(conn, ticker)
        assert same_row is not None and same_row.npv_per_share == 150
        assert receipt.run_id == same_row.id
        assert not receipt.ready  # Actual readiness is retained; this is no acceptance seam.
        evaluated.append(as_of)
        return receipt

    monkeypatch.setattr("risk_reward.load_valuation_readiness", assess)
    local_cutoff = NOW.astimezone(timezone(timedelta(hours=-7)))
    leg = reward_legs(db, ["META"], NOW.date(), as_of=local_cutoff)["META"]
    assert leg.expected_return is None
    assert evaluated == [NOW]
    assert evaluated[0].tzinfo is UTC
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT npv_per_share FROM dcf_runs").fetchone()[0] == 999


def test_missing_database_has_a_precise_reason(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    leg = reward_legs(absent, ["META"], NOW.date())["META"]
    assert leg.expected_return is None
    assert leg.confidence_reason == "dcf_database_missing"
    assert not absent.exists()


def test_naive_cutoff_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        reward_legs(tmp_path / "absent.db", ["META"], NOW.date(), as_of=NOW.replace(tzinfo=None))


def test_full_28_public_source_pipeline_does_not_qualify_a_draft_model(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """Real seals and all28 inputs; no coverage/readiness/verifier replacement.

    This role fixture intentionally has synthetic, unreconciled debt/lease pools.
    It proves reported-input closure and an explicit economic-model refusal.
    It cannot prove accepted scenarios or ready valuation.
    """
    conn, role_request = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        plan = plan_meli_role_admission(conn, role_request)
        assert plan.state == "planned", plan.blockers
        review = ReviewedRoleAdmission(
            plan=plan,
            plan_sha256=plan.commitment_sha256,
            reviewer="synthetic-readiness-reviewer",
            reviewed_at=datetime.now(UTC),
            decision="approved",
        )
        roles = apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert roles.inserted_revisions == 14 and roles.model_ready is False
        conn.commit()
        cutoff = datetime.now(UTC)
        MetricOntology(conn).seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="ontology:readiness-roles",
                idempotency_key="ontology:readiness-roles",
                cutoff_at=cutoff,
                recorded_at=cutoff,
            )
        )
        resolution = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True
            ),
        )
        assert resolution.state == "complete" and resolution.resolved_cell_count == 28
        scope = build_analysis_scope(
            conn,
            AnalysisScopeRequest(
                purpose="post_earnings_readout",
                issuer_id="issuer-1",
                inventory_key="issuer-1:sec",
                required_period_ends=(PERIOD,),
                cutoff_at=cutoff,
                observed_through=cutoff,
            ),
        )
        processing = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True, analysis_scope=scope
            ),
        )
        assert processing.processing_snapshot_count == 1
        inventory, inventory_ids = load_analysis_expected_document_inventory(
            conn, scope, cutoff_at=cutoff, observed_through=cutoff
        )
        conn.commit()
        build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key=scope.scope_id,
                revision=1,
                selector_code_version="synthetic-readiness-roles@1",
                recorded_at=cutoff,
                knowledge_cutoff=cutoff,
                expected_documents=inventory.expected_documents,
                source_inventory_snapshot_ids=inventory_ids,
                analysis_scope=scope,
                apply=True,
            ),
        )
        snapshot = assemble_research_snapshot_request(
            conn, "issuer-1", cutoff, analysis_scope=scope, projection_mode="lexical_only"
        )
        admission = build_research_snapshot(conn, snapshot)
        assert admission.admitted
        assert verify_research_snapshot(conn, snapshot.research_snapshot_id) == admission
        preview = preview_meli_inputs(
            conn,
            research_snapshot_id=snapshot.research_snapshot_id,
            financial_period_end=PERIOD,
            as_of=cutoff,
        )
        assert len(preview.facts) == 28 and preview.model_ready is False
        assert all(slot.state == "matched" for slot in preview.slots)
        draft = Assum(derive_capm=0)
        draft.credit_terminal_roe = mirror(draft).credit_terminal_roe * (1 + draft.credit_g_term)
        numeric = effective_numeric_inputs(asdict(draft))
        proposed = {key: numeric[key] for key in ASSUMPTION_KEYS}
        request = ModelInputRequest(
            recipe=RECIPE,
            ticker="MELI",
            research_snapshot_id=snapshot.research_snapshot_id,
            financial_period_end=PERIOD,
            facts=preview.facts,
            assumptions={
                key: AssumptionBasis(
                    value=value, attribution="analyst", rationale="Synthetic unaccepted draft"
                )
                for key, value in proposed.items()
            },
        )
        changes = conn.total_changes
        proof = verify_model_inputs(
            conn,
            request,
            recipe=RECIPE,
            requirements=requirements_for(PERIOD),
            effective_inputs=proposed,
            assumption_keys=ASSUMPTION_KEYS,
            as_of=cutoff,
        )
        assert len(proof.inputs) == 28 and conn.total_changes == changes
        reason = "cash_funding_allocation_outside_reported_pools"
        with pytest.raises(InputEvidenceError, match=reason):
            prepare_meli_inputs(conn, request, effective_inputs=proposed, as_of=cutoff)
        db = _persist(
            conn,
            ticker="MELI",
            snapshot={"model": "meli_platform_sotp", "effective_model_inputs": numeric},
            provenance={"model_input_receipt": proof.model_dump(mode="json")},
            at=cutoff,
        )
        readiness = load_valuation_readiness(conn, "MELI", as_of=cutoff)
        assert not readiness.ready and reason in readiness.reason_codes
        assert readiness.financial_input_completeness == "unverified"
        leg = reward_legs(db, ["MELI"], cutoff.date(), as_of=cutoff)["MELI"]
        assert leg.expected_return is None and reason in (leg.confidence_reason or "")
        unavailable: dict[str, str] = {}
        assert valuation_factor(db, ["MELI"], as_of=cutoff, unavailable=unavailable) == {}
        assert reason in unavailable["MELI"]
    finally:
        conn.close()


def test_reconciled_full_28_base_still_requires_scenario_acceptance(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """Synthetic economic review proves base replay, not owner scenario acceptance.

    Values enter before public XBRL/source/observation seals. All 28 reported
    inputs, role revisions and downstream seals use real owners without doubles.
    The calculation artifact is a synthetic JSON readout, not a production workbook.
    """
    million = Decimal(1_000_000)
    flow_values = {
        "comm_rev0": (1000, 600, 500),
        "revenue_total": (1500, 900, 750),
        "revenue_fintech": (500, 300, 250),
        "revenue_credit": (200, 120, 100),
        "operating_income": (180, 100, 80),
        "depreciation": (40, 24, 20),
        "capex": (60, 36, 30),
    }
    values = {
        f"{key}_{period}": Decimal(value) * million
        for key, amounts in flow_values.items()
        for period, value in zip(("fy", "ytd", "prior_ytd"), amounts, strict=True)
    }
    values.update(
        {
            "cb0": Decimal(1200) * million,
            "shares": Decimal(100) * million,
            "reported_available_cash_and_investments": Decimal(500) * million,
            "reported_total_financial_debt_and_leases": Decimal(800) * million,
            "reported_current_operating_lease_liabilities": Decimal(50) * million,
            "reported_noncurrent_operating_lease_liabilities": Decimal(150) * million,
            "nimal_actual": Decimal("0.19"),
        }
    )
    assert len(values) == 28
    conn, role_request = synthetic_current_population(tmp_path, migrated_db, numeric_values=values)
    try:
        conn.execute("BEGIN")
        plan = plan_meli_role_admission(conn, role_request)
        assert plan.state == "planned", plan.blockers
        review = ReviewedRoleAdmission(
            plan=plan,
            plan_sha256=plan.commitment_sha256,
            reviewer="synthetic-readiness-reviewer",
            reviewed_at=datetime.now(UTC),
            decision="approved",
        )
        roles = apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert roles.inserted_revisions == 14 and roles.model_ready is False
        conn.commit()
        cutoff = datetime.now(UTC)
        MetricOntology(conn).seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="ontology:readiness-roles",
                idempotency_key="ontology:readiness-roles",
                cutoff_at=cutoff,
                recorded_at=cutoff,
            )
        )
        resolution = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True
            ),
        )
        assert resolution.state == "complete" and resolution.resolved_cell_count == 28
        scope = build_analysis_scope(
            conn,
            AnalysisScopeRequest(
                purpose="post_earnings_readout",
                issuer_id="issuer-1",
                inventory_key="issuer-1:sec",
                required_period_ends=(PERIOD,),
                cutoff_at=cutoff,
                observed_through=cutoff,
            ),
        )
        processing = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True, analysis_scope=scope
            ),
        )
        assert processing.processing_snapshot_count == 1
        inventory, inventory_ids = load_analysis_expected_document_inventory(
            conn, scope, cutoff_at=cutoff, observed_through=cutoff
        )
        conn.commit()
        build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key=scope.scope_id,
                revision=1,
                selector_code_version="synthetic-readiness-roles@1",
                recorded_at=cutoff,
                knowledge_cutoff=cutoff,
                expected_documents=inventory.expected_documents,
                source_inventory_snapshot_ids=inventory_ids,
                analysis_scope=scope,
                apply=True,
            ),
        )
        snapshot = assemble_research_snapshot_request(
            conn, "issuer-1", cutoff, analysis_scope=scope, projection_mode="lexical_only"
        )
        admission = build_research_snapshot(conn, snapshot)
        assert admission.admitted
        assert verify_research_snapshot(conn, snapshot.research_snapshot_id) == admission
        preview = preview_meli_inputs(
            conn,
            research_snapshot_id=snapshot.research_snapshot_id,
            financial_period_end=PERIOD,
            as_of=cutoff,
        )
        assert len(preview.facts) == 28 and preview.model_ready is False
        assert all(slot.state == "matched" for slot in preview.slots)
        draft = Assum(
            derive_capm=0,
            credit_cash_allocation=100,
            operating_cash_reserve=50,
            credit_funding_debt_allocation=200,
        )
        draft.credit_terminal_roe = mirror(draft).credit_terminal_roe * (1 + draft.credit_g_term)
        numeric = effective_numeric_inputs(asdict(draft))
        proposed = {key: numeric[key] for key in ASSUMPTION_KEYS}
        request = ModelInputRequest(
            recipe=RECIPE,
            ticker="MELI",
            research_snapshot_id=snapshot.research_snapshot_id,
            financial_period_end=PERIOD,
            facts=preview.facts,
            assumptions={
                key: AssumptionBasis(
                    value=value,
                    attribution="analyst",
                    rationale="Synthetic test forecast; no owner financial acceptance",
                )
                for key, value in proposed.items()
            },
        )
        changes = conn.total_changes
        proof = verify_model_inputs(
            conn,
            request,
            recipe=RECIPE,
            requirements=requirements_for(PERIOD),
            effective_inputs=proposed,
            assumption_keys=ASSUMPTION_KEYS,
            as_of=cutoff,
        )
        assert len(proof.inputs) == 28 and conn.total_changes == changes
        actuals, calculations = calculate_actuals(proof)
        for key, expected in {
            "comm_rev0": 1100,
            "fpay_rev0": 330,
            "cb0": 1200,
            "shares": 100,
            "reported_available_cash_and_investments": 500,
            "reported_total_financial_debt_and_leases": 800,
            "operating_lease_liabilities": 200,
            "financial_debt_pool": 600,
            "net_cash": -50,
        }.items():
            assert actuals[key] == pytest.approx(expected)
        complete = {**proposed, **{key: actuals[key] for key in REPORTED_DRIVER_KEYS}}
        assumption_review = AssumptionReview(
            reviewed_at=cutoff,
            reviewer="synthetic-economic-reviewer",
            effective_inputs_sha256=canonical_digest(complete),
            actuals_sha256=canonical_digest(actuals),
            drivers={
                key: DriverReview(
                    observed=observed,
                    forecast=forecast,
                    variance=forecast - observed,
                    rationale=(
                        "Synthetic fixture explicitly reviews this variance; no owner acceptance"
                    ),
                )
                for key, (observed, forecast) in review_drivers(actuals, complete).items()
            },
        )
        request = request.model_copy(update={"assumption_review": assumption_review})
        inputs, receipt = prepare_meli_inputs(
            conn, request, effective_inputs=proposed, as_of=cutoff
        )
        assert inputs == complete and receipt.calculations == calculations
        assert receipt.actuals_sha256 == canonical_digest(actuals)
        output = model_output(inputs)
        assert receipt.model_output_sha256 == canonical_digest(output)
        model = Assum()
        for key, value in inputs.items():
            setattr(model, key, int(value) if key in {"years", "derive_capm"} else value)
        replay = mirror(model)
        assert asdict(replay) == output and replay.vps > 0
        assert replay.equity_value / inputs["shares"] == pytest.approx(replay.vps)

        # Public provenance computes real artifact commitments. No grade/hash grants.
        assumption_artifact = tmp_path / "synthetic-reviewed-inputs.json"
        assumption_artifact.write_text(request.model_dump_json())
        calculation_artifact = tmp_path / "synthetic-calculation.json"
        calculation_artifact.write_text(json.dumps(output, allow_nan=False))
        calculated_at = datetime.now(UTC)
        snapshot_payload = {
            "model": "meli_platform_sotp",
            "effective_model_inputs": inputs,
            "value_per_share": replay.vps,
            "equity_value_m": replay.equity_value,
            "operating_ev_m": replay.operating_ev,
            "credit_equity_value_m": replay.credit_equity_value,
        }
        provenance = build_file_provenance(
            ticker="MELI",
            repo_root=tmp_path,
            workbook_path=calculation_artifact,
            engine_version="meli_platform_sotp_v1",
            effective_inputs=inputs,
            assumption_snapshot=snapshot_payload,
            live_price=replay.vps,
            live_price_at=calculated_at,
            live_price_source="synthetic_quote",
            source_files=((assumption_artifact, "synthetic_reviewed_inputs"),),
            equity_direct_archetype="platform_sotp",
            model_input_receipt=receipt.model_dump(mode="json"),
        )
        assert upsert(
            conn,
            DcfRunRow(
                ticker="MELI",
                valuation_date=calculated_at.date(),
                horizon_years=int(inputs["years"]),
                wacc=inputs["wacc"],
                npv=replay.equity_value,
                npv_per_share=replay.vps,
                shares_outstanding=inputs["shares"] * 1_000_000,
                currency="USD",
                live_price=replay.vps,
                live_price_at=calculated_at,
                mos_bar_used=None,
                assumption_snapshot_json=json.dumps(snapshot_payload),
                provenance=provenance,
                calculated_at=calculated_at,
            ),
        )
        conn.commit()
        as_of = datetime.now(UTC)
        reason = "scenario_acceptance_unverified"
        changes = conn.total_changes
        readiness = load_valuation_readiness(conn, "MELI", as_of=as_of)
        assert not readiness.ready and readiness.status == "degraded"
        assert readiness.financial_input_completeness == "verified"
        assert readiness.latest_reporting_period_status == "verified"
        assert len(readiness.financial_inputs) == 28
        assert readiness.reason_codes == (reason,)
        assert readiness.input_sha256 == provenance.input_sha256
        assert conn.total_changes == changes
        db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        leg = reward_legs(db, ["MELI"], as_of.date(), as_of=as_of)["MELI"]
        assert leg.expected_return is None
        assert leg.confidence_reason == reason
        unavailable: dict[str, str] = {}
        assert valuation_factor(db, ["MELI"], as_of=as_of, unavailable=unavailable) == {}
        assert unavailable == {"MELI": reason}
    finally:
        conn.close()
