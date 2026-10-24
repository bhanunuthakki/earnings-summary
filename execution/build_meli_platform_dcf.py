"""Sum-of-the-parts platform valuation for MercadoLibre (MELI).

A single FCFF DCF mis-frames MELI: ~44% of revenue is Fintech, and inside Fintech
the Mercado Pago credit book is a spread-lending business that consumes regulatory
capital and throws credit losses — an operating-FCFF model treats its receivable
growth as a working-capital drain and prices its earnings on an EBITDA multiple,
both wrong. So value MELI the way it is actually built — three parts, two methods:

    Commerce (3P marketplace + ads + 1P + logistics)   operating FCFF  @ WACC
    Fintech-payments (acquiring, float, fees)           operating FCFF  @ WACC
    Fintech-credit (Mercado Pago credit book)           excess-return / FCFE @ Ke

    Equity value = Operating EV (Commerce + Fintech-payments)
                 + Credit-book equity value (FCFE on the lending franchise)
                 + net non-operating cash
    per share    = Equity value / diluted shares

The credit book is valued on its own cost of equity (lending is riskier than the
capital-light operating franchises) and charged the growth in required capital it
consumes — the capital intensity an FCFF model can't see. The operating side fades
growth on the same CONVEX curve as the redesigned FCFF engine
(``dcf.redesign.GROWTH_FADE_CURVATURE``), so the two models speak the same language.

Historical defaults are retained solely for isolated draft calculations. Verified
refreshes use canonical reported fiscal operands and an explicitly selected,
dated reviewed forecast artifact; no historical seed is promoted as current.
Non-credit Fintech includes installment and investment income where admitted
source definitions place them outside the separate credit portfolio. Segment
operating margins remain explicit forecasts because MELI reports profit by
geography, not Commerce/Fintech.

Verified refresh requires DCF_MELI_ASSUMPTIONS_PATH and the configured database,
plus staged DCF_DEST/DCF_PROMOTE_DEST. The artifact owns the reviewed effective
discount rates; implicit cached CAPM recalculation is disabled in this route.
Reviewed scenario vectors use that same assumptions artifact. Their existing
ScenarioAcceptance is supplied separately with DCF_MELI_SCENARIO_ACCEPTANCE_PATH
(optional SHA256 pin). DCF_MELI_INPUT_RECEIPT_PATH supplies the original retained
ModelInputReceipt (optional SHA256 pin); fresh verification preserves its
commitment and records current physical checks separately. Workbook execution
requires explicit loader-supplied DCF_ARTIFACT_NODE and DCF_ARTIFACT_NODE_MODULES.
DCF_PERSIST=0 permits only a labeled, non-promoting .tmp draft. Values are $M;
shares are millions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

# Import co-located package code (this script's repo), NOT REPO/src — REPO points
# at the DATA repo (which may be a different checkout, e.g. a worktree's data lives
# in the main repo), so resolving code from it would load a stale/foreign copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from db_paths import configured_db_path, require_db_path
from dcf import reverse_valuation as reverse_valuation_mod
from dcf.artifact_promotion import (
    ArtifactPromotion,
    live_path_from_env,
    promotion_from_env,
    run_dcf_entrypoint,
)
from dcf.input_evidence import (
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
    SourceReadContext,
    canonical_digest,
)
from dcf.meli_inputs import (
    ASSUMPTION_KEYS as ASSUMPTION_KEYS,
)
from dcf.meli_inputs import (
    RECIPE,
    REPORTED_DRIVER_KEYS,
    assumption_keys_for_recipe,
    calculate_actuals,
    effective_numeric_inputs,
    is_meli_recipe,
    model_output,
    prepare_meli_inputs,
    verify_meli_inputs,
)
from dcf.meli_model import Assum as Assum
from dcf.meli_model import Mirror as Mirror
from dcf.meli_model import mirror as mirror
from dcf.meli_model import validate_credit_terminal
from dcf.meli_workbook import (
    BearProvenance,
    MeliWorkbookSpec,
    WorkbookRuntime,
    build_workbook,
    validate_cases,
)
from dcf.meli_workbook import R as R
from dcf.meli_workbook import workbook_spec as _workbook_spec
from dcf.provenance import build_file_provenance, schema_supports_provenance
from dcf.readiness import replay_model_scenarios
from dcf.scenario_acceptance import ScenarioAcceptance, verify_scenario_acceptance
from dcf.specialized_price import (
    SpecializedPriceObservation,
    price_seed_source_files,
    resolve_specialized_price,
)
from pipeline.db_writer_gate import hold_writer_gate
from provenance.immutable_artifact import read_stable_artifact
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

try:  # persistence is best-effort -- the workbook builds without a DB
    from dcf import persist as _persist_module
except ImportError:  # pragma: no cover
    persist_mod = None
else:
    persist_mod = _persist_module
try:  # global macro assumptions + country risk -- best-effort; in-code defaults else
    from dcf import country_risk as _country_risk_module
    from dcf import global_assumptions as _global_assumptions_module
except ImportError:  # pragma: no cover
    country_risk = None
    global_dcf = None
else:
    country_risk = _country_risk_module
    global_dcf = _global_assumptions_module

try:  # scenario emission (Monthly Red Team PR8) -- best-effort like persistence
    from dcf import redesign as _redesign_module
except ImportError:  # pragma: no cover
    redesign_mod = None
else:
    redesign_mod = _redesign_module

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
T = os.environ.get("DCF_TICKER", "MELI")
DEST = Path(os.environ.get("DCF_DEST") or (REPO / "dcf" / f"{T}.xlsx"))


def reverse_valuation(s: Assum, m: Mirror) -> dict[str, object] | None:
    """Market-implied MELI operating multiple and credit-equity bridge residual."""
    if s.price <= 0 or m.vps <= 0:
        return None
    operating_exit_multiple = reverse_valuation_mod.solve_lever(
        lever_id="implied_operating_exit_multiple",
        label="Implied operating exit EBITDA multiple",
        unit="turns",
        base_value=s.op_exit_ebitda_mult,
        method="monotonic_bisection",
        price_at=lambda multiple: mirror(replace(s, op_exit_ebitda_mult=multiple)).vps,
        target_price=s.price,
        lower_bound=1.0,
        upper_bound=75.0,
    )
    market_equity_value = s.price * s.shares
    implied_credit_equity = market_equity_value - m.operating_ev - s.net_cash
    credit_residual = reverse_valuation_mod.residual_lever(
        lever_id="implied_credit_equity_value",
        label="Market-implied credit equity value",
        unit="usd_m",
        base_value=m.credit_equity_value,
        implied_value=implied_credit_equity,
        note="Market equity less modeled operating EV and net non-operating cash.",
    )
    return reverse_valuation_mod.ReverseValuation(
        archetype="meli_platform_sotp",
        price=s.price,
        base_value_per_share_usd=m.vps,
        valuation_scope="equity",
        levers=(operating_exit_multiple, credit_residual),
    ).to_snapshot_dict()


def load_assumptions(ticker: str, *, db_path: Path | None = None) -> Assum:
    """Assum defaults overridden by data/bank_assumptions/<T>_sotp.json, then the
    editable global tax + (opt-in) CAPM-derived discount rates with the
    revenue-weighted Damodaran country risk premium."""
    s = Assum()
    try:
        db = require_db_path(db_path)
    except (RuntimeError, OSError):
        if db_path is not None:
            raise
        db = None  # explicitly isolated draft: no checkout database fallback
    global_loaded = (
        global_dcf.load_with_provenance(db_path=db)
        if global_dcf is not None and db is not None
        else None
    )
    if global_loaded is not None:
        s.global_assumption_source = global_loaded.source_record
        s.tax = global_loaded.assumptions.tax_rate
    else:
        s.global_assumption_source = {
            "role": "global_dcf_assumptions",
            "status": "module_unavailable",
            "observed_at": None,
        }
    p = REPO / "data" / "bank_assumptions" / f"{ticker}_sotp.json"
    country_risk_overridden = False
    if p.exists():
        try:
            ov: Any = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ov = {}
        if isinstance(ov, dict):
            for k, v in cast("dict[str, Any]", ov).items():
                if (
                    k not in {"global_assumption_source", "country_risk_source"}
                    and hasattr(s, k)
                    and isinstance(v, (int, float))
                ):
                    setattr(s, k, v)
                    if k == "price":
                        s.price_seed_source = "owner_assumptions"
                        s.price_seed_path = f"data/bank_assumptions/{ticker}_sotp.json"
                    if k == "country_risk_premium":
                        country_risk_overridden = True
    # Systematic country risk premium (revenue-weighted), filled only when the
    # owner did not explicitly set it. This makes an override a true authority
    # boundary: no geographic file is read or claimed as a model input.
    if country_risk is not None and not country_risk_overridden and not s.country_risk_premium:
        country_observation = country_risk.country_risk_observation(REPO, ticker)
        s.country_risk_premium = country_observation.premium
        if country_observation.source_record is not None:
            s.country_risk_source = country_observation.source_record
    # Opt-in: derive both discount rates from the editable global rf/ERP + CRP.
    if global_loaded is not None and s.derive_capm:
        s.wacc = (
            global_loaded.assumptions.risk_free_rate
            + s.beta_op * global_loaded.assumptions.equity_risk_premium
            + s.country_risk_premium
        )
        s.credit_ke = (
            global_loaded.assumptions.risk_free_rate
            + s.beta_credit * global_loaded.assumptions.equity_risk_premium
            + s.country_risk_premium
        )
    prof = REPO / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    if prof.exists():
        try:
            raw_profile: object = json.loads(prof.read_text(encoding="utf-8"))
            if isinstance(raw_profile, list):
                profiles = cast("list[object]", raw_profile)
                raw_profile = profiles[0] if profiles else {}
            if isinstance(raw_profile, dict):
                profile = cast("dict[str, object]", raw_profile)
                price = profile.get("price")
                if isinstance(price, (int, float, str)) and price:
                    s.price = float(price)
                    s.price_seed_source = "fmp_profile"
                    s.price_seed_path = f"data/historical/fmp/{ticker}_profile.json"
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            pass
    return s


# --------------------------------------------------------------------------- #
# Scenario emission (Monthly Red Team PR8).
#
# Mirrors the redesigned refresher's persisted ``scenarios`` block (bull/base/
# bear fair values + bear provenance) so MELI stops being invisible to every
# scenario consumer (``dcf.scenario_reward``, ``bear_lint``, tail stress). The
# shared 6-lever ``ScenarioDeltas`` vocabulary maps onto THIS SOTP's levers:
#
#   growth deltas   -> Commerce, Fintech-payments AND credit-book growth
#                      (near/terminal) — a demand shock hits all three engines
#   margin deltas   -> Commerce + Fintech-payments EBIT margins AND the credit
#                      book's NIMAL (near/terminal) — a price war compresses the
#                      operating segments while a credit cycle compresses NIMAL
#   exit multiple Δ -> the operating exit EV/EBITDA (turns, floored at 1x). The
#                      credit leg's Gordon terminal compresses through NIMAL/g,
#                      not through this multiple
#   terminal g Δ    -> credit terminal growth g (and the operating perpetuity
#                      cross-check g, cosmetic)
#
# Bear deltas: holdings ``bear_deltas`` when present (provenance "thesis"), else
# the generic BEAR_SEED (provenance "seed" — a labeled fallback). Legible over
# precise, per the red-team directive.
# --------------------------------------------------------------------------- #
def _load_holdings(ticker: str) -> dict[str, object] | None:
    path = REPO / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def scenario_assumptions(s: Assum, deltas: Any) -> Assum:
    """``s`` shifted by one scenario's ``dcf.redesign.ScenarioDeltas`` under the
    documented lever mapping. Reject an inconsistent credit terminal rather
    than changing growth or ROE to make a scenario appear well-posed."""
    import copy

    s2 = copy.copy(s)
    for attr in ("comm_g_near", "fpay_g_near", "cbg_near"):
        setattr(s2, attr, getattr(s2, attr) + deltas.growth_near)
    for attr in ("comm_g_term", "fpay_g_term", "cbg_term"):
        setattr(s2, attr, getattr(s2, attr) + deltas.growth_term)
    for attr in ("comm_margin_near", "fpay_margin_near", "nimal_near"):
        setattr(s2, attr, getattr(s2, attr) + deltas.margin_near)
    for attr in ("comm_margin_term", "fpay_margin_term", "nimal_term"):
        setattr(s2, attr, getattr(s2, attr) + deltas.margin_term)
    s2.op_exit_ebitda_mult = max(1.0, s2.op_exit_ebitda_mult + deltas.exit_multiple)
    s2.credit_g_term += deltas.terminal_g
    s2.g_term += deltas.terminal_g
    validate_credit_terminal(asdict(s2))
    return s2


def scenarios_block(
    s: Assum,
    m: Mirror,
    holdings: dict[str, object] | None,
    *,
    cases: dict[str, Assum] | None = None,
    recipe: str = RECIPE,
) -> dict[str, object]:
    """The ``scenarios`` payload for ``dcf_runs.assumption_snapshot_json`` —
    structurally identical to ``refresh_dcf._redesign_snapshot``'s block, so
    ``parse_scenario_fair_values`` / ``parse_scenario_bear_provenance`` read it
    unchanged. Requires ``redesign_mod`` (caller gates on it)."""
    import dataclasses as _dc

    if cases is not None:
        verified = _validated_workbook_cases(s, holdings, cases, recipe=recipe)
        return {
            name: {
                "fair_value_per_share_usd": mirror(case).vps,
                "provenance": "explicit_vector_acceptance_separate",
                "effective_inputs_sha256": canonical_digest(
                    effective_numeric_inputs(asdict(case), recipe=recipe)
                ),
            }
            for name, case in verified.items()
        }

    if redesign_mod is None:
        raise RuntimeError("MELI scenario calculation requires the redesign module")
    bull_d = redesign_mod.BULL_SEED
    bear_d = redesign_mod.thesis_bear_seed(holdings)
    provenance = "thesis" if redesign_mod.parse_thesis_bear_deltas(holdings) is not None else "seed"
    bull_vps = mirror(scenario_assumptions(s, bull_d)).vps
    bear_vps = mirror(scenario_assumptions(s, bear_d)).vps
    return {
        "base": {"fair_value_per_share_usd": m.vps},
        "bull": {"fair_value_per_share_usd": bull_vps, "deltas": _dc.asdict(bull_d)},
        "bear": {
            "fair_value_per_share_usd": bear_vps,
            "deltas": _dc.asdict(bear_d),
            "provenance": provenance,
        },
    }


# --------------------------------------------------------------------------- #
# workbook
# --------------------------------------------------------------------------- #
def _validated_workbook_cases(
    s: Assum,
    holdings: dict[str, object] | None,
    cases: dict[str, Assum] | None,
    *,
    recipe: str = RECIPE,
) -> dict[str, Assum]:
    if cases is None:
        if redesign_mod is None:
            raise InputEvidenceError("workbook_scenarios_unavailable")
        cases = {
            "base": s,
            "bear": scenario_assumptions(s, redesign_mod.thesis_bear_seed(holdings)),
            "bull": scenario_assumptions(s, redesign_mod.BULL_SEED),
        }
    return validate_cases(s, cases, recipe=recipe)


def _workbook_bear_provenance(
    holdings: dict[str, object] | None,
    cases: dict[str, Assum] | None,
) -> BearProvenance:
    if cases is not None:
        return "explicit_vector_acceptance_separate"
    if redesign_mod is not None and redesign_mod.parse_thesis_bear_deltas(holdings) is not None:
        return "thesis"
    return "seed"


def workbook_spec(
    s: Assum,
    m: Mirror,
    holdings: dict[str, object] | None = None,
    *,
    cases: dict[str, Assum] | None = None,
    input_receipt: ModelInputReceipt | None = None,
    bear_provenance: BearProvenance | None = None,
) -> MeliWorkbookSpec:
    return _workbook_spec(
        s,
        m,
        cases=_validated_workbook_cases(
            s, holdings, cases, recipe=input_receipt.recipe if input_receipt else RECIPE
        ),
        input_receipt=input_receipt,
        ticker=T,
        bear_provenance=bear_provenance or _workbook_bear_provenance(holdings, cases),
    )


def build(
    s: Assum,
    m: Mirror,
    dest: Path,
    holdings: dict[str, object] | None = None,
    *,
    cases: dict[str, Assum] | None = None,
    runtime: WorkbookRuntime | None = None,
    input_receipt: ModelInputReceipt | None = None,
    bear_provenance: BearProvenance | None = None,
) -> None:
    if input_receipt is not None and cases is None:
        raise InputEvidenceError("reviewed_scenario_vectors_required")
    build_workbook(
        s,
        m,
        dest,
        cases=_validated_workbook_cases(
            s, holdings, cases, recipe=input_receipt.recipe if input_receipt else RECIPE
        ),
        runtime=runtime,
        input_receipt=input_receipt,
        ticker=T,
        bear_provenance=bear_provenance or _workbook_bear_provenance(holdings, cases),
    )


def persist_dcf_run(
    s: Assum,
    m: Mirror,
    holdings: dict[str, object] | None = None,
    price_observation: SpecializedPriceObservation | None = None,
    *,
    artifact_promotion: ArtifactPromotion | None = None,
    db_path: Path | None = None,
    input_receipt: ModelInputReceipt | None = None,
    assumptions_path: Path | None = None,
    source_context: SourceReadContext | None = None,
    cases: dict[str, Assum] | None = None,
    scenario_acceptance: ScenarioAcceptance | None = None,
) -> bool:
    """``holdings=None`` (the pre-PR10 2-arg call shape every test/caller uses)
    loads ``micro_thesis/holdings/<T>.json`` itself, same as before. ``main()``
    now passes the SAME dict ``build()``'s Scenario sheet used, so a mid-run
    file edit can never make the sheet and the persisted snapshot disagree —
    a ticker with genuinely no holdings JSON still resolves to ``None`` either
    way, so this collapses "not passed" and "no holdings on file" safely."""
    if input_receipt is None:
        raise InputEvidenceError("model_input_receipt_required")
    if assumptions_path is None or input_receipt.assumptions_source_path != str(
        assumptions_path.resolve()
    ):
        raise InputEvidenceError("explicit_assumptions_authority_required")
    try:
        current_source_hash = hashlib.sha256(assumptions_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise InputEvidenceError("assumptions_authority_unavailable") from exc
    if current_source_hash != input_receipt.assumptions_source_sha256:
        raise InputEvidenceError("assumptions_authority_changed_after_review")
    db = require_db_path(db_path)
    if persist_mod is None or not m.vps:
        return False
    recomputed = mirror(s)
    if recomputed != m:
        raise InputEvidenceError("model_output_input_mismatch")
    calculated_at = datetime.now(UTC)
    effective = effective_numeric_inputs(asdict(s), recipe=input_receipt.recipe)
    scenario_review: dict[str, object] | None = None
    scenario_vectors: dict[str, object] | None = None
    if cases is not None:
        cases = _validated_workbook_cases(s, holdings, cases, recipe=input_receipt.recipe)
        if scenario_acceptance is None:
            raise InputEvidenceError("scenario_acceptance_required")
        scenario_vectors = {
            name: effective_numeric_inputs(asdict(case), recipe=input_receipt.recipe)
            for name, case in cases.items()
        }
        replay = replay_model_scenarios(
            "MELI",
            effective,
            model_output(effective),
            {"reviewed_scenario_inputs": scenario_vectors},
            recipe=input_receipt.recipe,
        )
        verify_scenario_acceptance(
            scenario_acceptance,
            input_receipt=input_receipt,
            model="meli_platform_sotp",
            engine_version="meli_platform_sotp_v1",
            effective_inputs=effective,
            output=replay,
            as_of=calculated_at,
            calculated_at=calculated_at,
        )
        scenario_review = scenario_acceptance.model_dump(mode="json")
    elif scenario_acceptance is not None:
        raise InputEvidenceError("reviewed_scenario_vectors_required")
    if holdings is None:
        holdings = _load_holdings(T)
    mos: object = holdings.get("mos_bar") if holdings else None
    observed_at = price_observation.observed_at if price_observation is not None else None
    price_source = (
        price_observation.source_name if price_observation is not None else "assumption_seed"
    )
    live_workbook = live_path_from_env(DEST)
    snap_payload: dict[str, object] = {
        "model": "meli_platform_sotp",
        "effective_model_inputs": effective,
        "model_output": model_output(effective),
        "input_recipe": input_receipt.recipe,
        "financial_period_end": input_receipt.request.financial_period_end.isoformat(),
        "value_per_share": m.vps,
        "operating_ev_m": m.operating_ev,
        "credit_equity_value_m": m.credit_equity_value,
        "equity_value_m": m.equity_value,
        "terminal_operating_revenue_m": m.op_terminal_revenue,
        "terminal_credit_roe": m.credit_terminal_roe,
        "terminal_blended_op_margin": m.terminal_blended_op_margin,
        "wacc": s.wacc,
        "credit_ke": s.credit_ke,
        "country_risk_premium": s.country_risk_premium,
        "workbook": str(live_workbook),
        "assumption_provenance": {
            "authority": str(assumptions_path.resolve()),
            "sha256": current_source_hash,
            "rates": "dated_reviewed_effective_vector",
            "workbook_capture": "unsupported",
            "sync_status": "not_applicable",
        },
    }
    if scenario_vectors is not None:
        snap_payload["reviewed_scenario_inputs"] = scenario_vectors
    if cases is not None or redesign_mod is not None:
        snap_payload["scenarios"] = scenarios_block(
            s, m, holdings, cases=cases, recipe=input_receipt.recipe
        )
    reverse = reverse_valuation(s, m)
    if reverse is not None:
        snap_payload["reverse_valuation"] = reverse
    snap = json.dumps(snap_payload, indent=2)
    provenance = build_file_provenance(
        ticker=T,
        repo_root=REPO,
        workbook_path=DEST,
        workbook_locator_path=live_workbook,
        engine_version="meli_platform_sotp_v1",
        effective_inputs=model_parameters(s, input_receipt.recipe),
        assumption_snapshot=snap_payload,
        live_price=s.price or None,
        live_price_at=observed_at,
        live_price_source=price_source,
        source_files=(
            (assumptions_path, "owner_assumptions"),
            (REPO / "micro_thesis" / "holdings" / f"{T}.json", "holding_policy"),
            *(
                price_seed_source_files(REPO, price_observation)
                if price_observation is not None
                else ()
            ),
        ),
        source_records=tuple(
            record for record in (s.global_assumption_source, s.country_risk_source) if record
        ),
        equity_direct_archetype="platform_sotp",
        model_input_receipt=input_receipt.model_dump(mode="json"),
        scenario_acceptance=scenario_review,
    )
    row = persist_mod.DcfRunRow(
        ticker=T,
        valuation_date=date.today(),
        horizon_years=s.years,
        wacc=s.wacc,
        npv=m.equity_value,
        npv_per_share=m.vps,
        shares_outstanding=s.shares * 1e6,
        currency="USD",
        live_price=s.price or None,
        live_price_at=observed_at,
        mos_bar_used=float(mos) if isinstance(mos, (int, float)) else None,
        assumption_snapshot_json=snap,
        notes=f"workbook={live_workbook.name} (MELI sum-of-the-parts platform DCF)",
        provenance=provenance,
        calculated_at=calculated_at,
    )
    with (
        hold_writer_gate(db, owner=f"build_meli_platform_dcf:{T}"),
        connect_sqlite(str(db), role=SQLiteConnectionRole.WRITER, schema_preflight=True) as conn,
    ):
        # The cross-process writer gate serializes this upsert against the
        # daily brief driver and its sibling chain steps; it is NOT the
        # whole-job run lock.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        verify_meli_inputs(
            conn,
            input_receipt,
            effective_inputs=effective,
            as_of=datetime.now(UTC),
            source_context=source_context if source_context is not None else model_source_context(),
        )
        if not schema_supports_provenance(conn):
            raise InputEvidenceError("model_input_receipt_schema_unavailable")
        if artifact_promotion is None:
            return persist_mod.upsert(conn, row)
        return persist_mod.upsert(conn, row, artifact_promotion=artifact_promotion)


def model_source_context() -> SourceReadContext | None:
    """Resolve only an explicitly injected product-state authority."""
    state_root = os.environ.get("DCF_SOURCE_STATE_ROOT")
    return SourceReadContext.for_product_state_root(Path(state_root)) if state_root else None


def model_parameters(s: Assum, recipe: str = RECIPE) -> dict[str, Any]:
    """Preserve legacy v4 parameter serialization after the explicit v5 field."""
    values = asdict(s)
    if recipe == RECIPE:
        values.pop("financing_other_expenses_reserve", None)
    return values


def load_verified_assumptions(
    ticker: str,
    *,
    db_path: Path,
    assumptions_path: Path,
    expected_sha256: str | None = None,
    source_context: SourceReadContext | None = None,
    retained_receipt: ModelInputReceipt | None = None,
    verification_output: Path | None = None,
) -> tuple[Assum, ModelInputReceipt]:
    """Read one explicit reviewed artifact; no repo/data or DB-directory fallback.

    This vector records effective forecast rates. Refreshing CAPM or other
    forecast choices requires a new dated review, not implicit cache loading.
    """
    if ticker != "MELI":
        raise InputEvidenceError("meli_input_recipe_ticker_mismatch")
    try:
        source_bytes = assumptions_path.read_bytes()
    except OSError as exc:
        raise InputEvidenceError("model_input_request_missing_or_invalid") from exc
    if expected_sha256 is not None and hashlib.sha256(source_bytes).hexdigest() != expected_sha256:
        raise InputEvidenceError("assumptions_authority_changed_after_dispatch")
    try:
        request_payload = json.loads(source_bytes)
        request = ModelInputRequest.model_validate(request_payload.get("input_evidence"))
    except (OSError, ValueError, AttributeError) as exc:
        raise InputEvidenceError("model_input_request_missing_or_invalid") from exc
    if request.ticker != ticker:
        raise InputEvidenceError("meli_input_recipe_ticker_mismatch")
    if not is_meli_recipe(request.recipe):
        raise InputEvidenceError("model_input_recipe_mismatch")
    s = Assum()
    for key, assumption in request.assumptions.items():
        if key not in assumption_keys_for_recipe(request.recipe):
            raise InputEvidenceError("assumption_population_mismatch")
        setattr(
            s, key, int(assumption.value) if key in {"years", "derive_capm"} else assumption.value
        )
    # In verified builds no historical seed is offered as a current quote.
    s.price = 0.0
    s.price_seed_source = "unavailable"
    with connect_sqlite(require_db_path(db_path), role=SQLiteConnectionRole.READ_ONLY) as conn:
        conn.execute("BEGIN")
        context = source_context if source_context is not None else model_source_context()
        if retained_receipt is None:
            values, receipt = prepare_meli_inputs(
                conn,
                request,
                effective_inputs=effective_numeric_inputs(asdict(s), recipe=request.recipe),
                as_of=datetime.now(UTC),
                source_context=context,
            )
            refreshed = receipt
        else:
            if (
                retained_receipt.request != request
                or retained_receipt.assumptions_source_path != str(assumptions_path.resolve())
                or retained_receipt.assumptions_source_sha256
                != hashlib.sha256(source_bytes).hexdigest()
            ):
                raise InputEvidenceError("retained_input_request_or_source_mismatch")
            actuals, _ = calculate_actuals(retained_receipt)
            values = {
                **effective_numeric_inputs(asdict(s), recipe=request.recipe),
                **{key: actuals[key] for key in REPORTED_DRIVER_KEYS},
            }
            refreshed = verify_meli_inputs(
                conn,
                retained_receipt,
                effective_inputs=values,
                as_of=datetime.now(UTC),
                source_context=context,
            )
            # The exact original receipt is the scenario commitment. New physical
            # reads prove current availability without changing the analyst's input.
            receipt = retained_receipt
    if verification_output is not None:
        verification_output.parent.mkdir(parents=True, exist_ok=True)
        verification_output.write_text(
            refreshed.model_copy(
                update={
                    "assumptions_source_path": str(assumptions_path.resolve()),
                    "assumptions_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                }
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )
    for key, value in values.items():
        setattr(s, key, int(value) if key in {"years", "derive_capm"} else value)
    return s, receipt.model_copy(
        update={
            "assumptions_source_path": str(assumptions_path.resolve()),
            "assumptions_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
    )


def load_reviewed_cases(
    s: Assum,
    assumptions_path: Path,
    input_receipt: ModelInputReceipt,
    *,
    scenario_acceptance_path: Path,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Assum], ScenarioAcceptance]:
    """Use the existing reviewed-vector artifact and exact scenario receipt."""
    raw = _read_input_artifact(assumptions_path)
    if hashlib.sha256(raw).hexdigest() != input_receipt.assumptions_source_sha256:
        raise InputEvidenceError("assumptions_authority_changed_after_review")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise InputEvidenceError("reviewed_scenario_vectors_invalid") from exc
    if not isinstance(payload, dict):
        raise InputEvidenceError("reviewed_scenario_vectors_required")
    parsed = cast(dict[str, object], payload)
    raw_vectors = parsed.get("reviewed_scenario_inputs")
    if not isinstance(raw_vectors, dict):
        raise InputEvidenceError("reviewed_scenario_vectors_required")
    vectors = cast(dict[str, object], raw_vectors)
    cases: dict[str, Assum] = {}
    for name, value in vectors.items():
        if not isinstance(value, dict):
            raise InputEvidenceError("reviewed_scenario_vector_invalid")
        vector = cast(dict[str, object], value)
        if (
            frozenset(vector)
            != assumption_keys_for_recipe(input_receipt.recipe) | REPORTED_DRIVER_KEYS
        ):
            raise InputEvidenceError("reviewed_scenario_vector_population_invalid")
        numeric = effective_numeric_inputs(vector, recipe=input_receipt.recipe)
        case = replace(s)
        for key, number in numeric.items():
            setattr(case, key, int(number) if key in {"years", "derive_capm"} else number)
        cases[name] = case
    cases = _validated_workbook_cases(s, None, cases, recipe=input_receipt.recipe)
    # Acceptance commits the full receipt, which commits this assumptions file.
    # Embedding acceptance in that file would create an impossible hash cycle.
    acceptance_bytes = _read_input_artifact(scenario_acceptance_path)
    if (
        expected_sha256 is not None
        and hashlib.sha256(acceptance_bytes).hexdigest() != expected_sha256
    ):
        raise InputEvidenceError("scenario_acceptance_changed_after_dispatch")
    try:
        acceptance = ScenarioAcceptance.model_validate_json(acceptance_bytes)
    except ValueError as exc:
        raise InputEvidenceError("scenario_acceptance_invalid") from exc
    effective = effective_numeric_inputs(asdict(s), recipe=input_receipt.recipe)
    output = replay_model_scenarios(
        "MELI", effective, model_output(effective), parsed, recipe=input_receipt.recipe
    )
    now = datetime.now(UTC)
    verify_scenario_acceptance(
        acceptance,
        input_receipt=input_receipt,
        model="meli_platform_sotp",
        engine_version="meli_platform_sotp_v1",
        effective_inputs=effective,
        output=output,
        as_of=now,
        calculated_at=now,
    )
    return cases, acceptance


def _read_input_artifact(path: Path) -> bytes:
    try:
        _snapshot, raw = read_stable_artifact(path, max_bytes=8 * 1024 * 1024)
    except (OSError, ValueError, RuntimeError) as exc:
        raise InputEvidenceError("model_review_artifact_unavailable") from exc
    return raw


def load_retained_input_receipt(
    path: Path, expected_sha256: str | None = None
) -> ModelInputReceipt:
    raw = _read_input_artifact(path)
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise InputEvidenceError("retained_input_receipt_changed_after_dispatch")
    try:
        return ModelInputReceipt.model_validate_json(raw)
    except ValueError as exc:
        raise InputEvidenceError("retained_input_receipt_invalid") from exc


def _main_owned() -> int:
    draft = os.environ.get("DCF_PERSIST", "1") != "1"
    artifact_promotion = promotion_from_env(DEST)
    if draft:
        if artifact_promotion is not None or not DEST.resolve().is_relative_to(
            (REPO / ".tmp").resolve()
        ):
            raise InputEvidenceError("draft_destination_must_be_isolated_tmp")
    elif artifact_promotion is None or DEST.resolve() == live_path_from_env(DEST).resolve():
        raise InputEvidenceError("atomic_artifact_promotion_required_use_refresh_dcf")
    db_path = None if draft else require_db_path(configured_db_path(REPO))
    input_receipt: ModelInputReceipt | None = None
    assumptions_path: Path | None = None
    if draft:
        s = load_assumptions(T)
        sys.stderr.write(
            json.dumps(
                {
                    "event": "meli_isolated_draft",
                    "input_status": "unverified_defaults_or_assumptions",
                    "promotion_allowed": False,
                    "draft_input_source": str(
                        REPO / "data" / "bank_assumptions" / f"{T}_sotp.json"
                    ),
                }
            )
            + "\n"
        )
    else:
        assert db_path is not None
        raw_assumptions_path = os.environ.get("DCF_MELI_ASSUMPTIONS_PATH", "").strip()
        if not raw_assumptions_path:
            raise InputEvidenceError("explicit_assumptions_authority_required")
        assumptions_path = Path(raw_assumptions_path)
        retained_path = os.environ.get("DCF_MELI_INPUT_RECEIPT_PATH", "").strip()
        if not retained_path:
            raise InputEvidenceError("retained_input_receipt_required")
        retained_receipt = load_retained_input_receipt(
            Path(retained_path),
            os.environ.get("DCF_MELI_INPUT_RECEIPT_SHA256"),
        )
        s, input_receipt = load_verified_assumptions(
            T,
            db_path=db_path,
            assumptions_path=assumptions_path,
            expected_sha256=os.environ.get("DCF_MELI_ASSUMPTIONS_SHA256"),
            retained_receipt=retained_receipt,
            verification_output=DEST.with_suffix(DEST.suffix + ".input-verification.json"),
        )
    price_observation = resolve_specialized_price(
        REPO,
        T,
        fallback_price=s.price,
        fallback_source_name=s.price_seed_source,
        fallback_source_path=s.price_seed_path,
    )
    s.price = price_observation.price
    m = mirror(s)
    # Loaded once and threaded through both the Scenario sheet (build) and the
    # persisted snapshot (persist_dcf_run) — PR10: one holdings read, one bear,
    # never two that could drift on a mid-run file edit.
    holdings = _load_holdings(T)
    cases: dict[str, Assum] | None = None
    scenario_acceptance: ScenarioAcceptance | None = None
    if input_receipt is not None and assumptions_path is not None:
        acceptance_path = os.environ.get("DCF_MELI_SCENARIO_ACCEPTANCE_PATH", "").strip()
        if not acceptance_path:
            raise InputEvidenceError("scenario_acceptance_required")
        cases, scenario_acceptance = load_reviewed_cases(
            s,
            assumptions_path,
            input_receipt,
            scenario_acceptance_path=Path(acceptance_path),
            expected_sha256=os.environ.get("DCF_MELI_SCENARIO_ACCEPTANCE_SHA256"),
        )
    build(s, m, DEST, holdings, input_receipt=input_receipt, cases=cases)
    if os.environ.get("DCF_PERSIST", "1") != "1":
        persisted = False
    elif artifact_promotion is not None:
        persisted = persist_dcf_run(
            s,
            m,
            holdings,
            price_observation,
            artifact_promotion=artifact_promotion,
            db_path=db_path,
            input_receipt=input_receipt,
            assumptions_path=assumptions_path,
            cases=cases,
            scenario_acceptance=scenario_acceptance,
        )
    else:
        persisted = persist_dcf_run(
            s,
            m,
            holdings,
            price_observation,
            db_path=db_path,
            input_receipt=input_receipt,
            assumptions_path=assumptions_path,
            cases=cases,
            scenario_acceptance=scenario_acceptance,
        )
    up = (m.vps / s.price - 1) if s.price else 0.0
    print(
        f"RESULT\t{T}\tvalue/sh=${m.vps:,.2f}\tprice=${s.price:,.2f}\tupside={up:+.0%}"
        f"\topEV=${m.operating_ev:,.0f}M\tcreditEq=${m.credit_equity_value:,.0f}M"
        f"\tWACC={s.wacc:.1%}\tcreditKe={s.credit_ke:.1%}\tdcf_runs={'ok' if persisted else 'skip'}\t-> {DEST}"
    )
    print(
        f"{'Yr':>2} {'CommRev':>8} {'PayRev':>7} {'OpEBIT':>7} {'OpFCFF':>7} {'Book':>7} {'CrNI':>6} {'CrFCFE':>7}"
    )
    for r in m.rows:
        print(
            f"{r.t:>2} {r.comm_rev:>8,.0f} {r.fpay_rev:>7,.0f} {r.op_ebit:>7,.0f} {r.op_fcff:>7,.0f} "
            f"{r.cb:>7,.0f} {r.credit_ni:>6,.0f} {r.credit_fcfe:>7,.0f}"
        )
    print(
        f"\nSOTP: Operating EV ${m.operating_ev / 1000:,.1f}B + Credit equity ${m.credit_equity_value / 1000:,.1f}B "
        f"+ net cash ${s.net_cash / 1000:,.1f}B = equity ${m.equity_value / 1000:,.1f}B"
    )
    print(
        f"Value/share ${m.vps:,.2f} vs ${s.price:,.2f} ({up:+.0%}) | terminal op rev "
        f"${m.op_terminal_revenue / 1000:,.0f}B, credit ROE {m.credit_terminal_roe:.0%}"
    )
    return 0


def main() -> int:
    return run_dcf_entrypoint(
        REPO,
        T,
        _main_owned,
        owner="build-meli-platform-dcf",
        require_database=os.environ.get("DCF_PERSIST", "1") == "1",
    )


if __name__ == "__main__":
    raise SystemExit(main())
