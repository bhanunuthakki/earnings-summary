"""Read-only, input-bound common-drawdown adapter for owner reviews and proposals.

Ratings are qualitative analyst scenarios, never return forecasts. ETF factors
retain actual covered basket weights: unknown constituents are never scaled up.
Fresh capture proves acquisition recency, not issuer publication currency.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from allocation.eligibility import WEIGHTS_CACHE_STALE_HOURS
from db_paths import resolve_db_path
from etf_sources.profile_evidence import (
    PROFILE_CAPTURE_MAX_AGE,
    PROFILE_CAPTURE_POLICY,
    admitted_profile_fields,
)
from instrument_store import get_etf_holdings, get_etf_profile, get_instrument_kind
from macro_regime_playbook import (
    INITIAL_REGIMES,
    REGIME_REGISTRY_VERSION,
    ActionRegimeImpact,
    PortfolioRegimeAssessment,
    evaluate_portfolio_regime,
    select_top_regimes_for_action,
)
from portfolio_weights import read_materialized_weight_snapshot
from risk_factors import TAXONOMY_VERSION, current_factor_input_sha, current_ticker_loadings
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

FRESHNESS_POLICY = "common_drawdown.current_inputs.v1"
State = Literal["full", "partial", "stale", "unavailable"]
CASH = frozenset({"USD", "CASH", "CURRENCY"})


class SecurityEvidence(BaseModel):
    ticker: str
    instrument_kind: str
    state: State
    factors: dict[str, float] = Field(default_factory=dict[str, float])
    covered_fraction: float = 0.0
    source_as_of: str | None = None
    context: dict[str, str] = Field(default_factory=dict[str, str])
    reasons: list[str] = Field(default_factory=list[str])
    input_sha: str = ""


class CommonDrawdownRead(BaseModel):
    state: State = "unavailable"
    registry_version: str = REGIME_REGISTRY_VERSION
    freshness_policy: str = FRESHNESS_POLICY
    input_sha: str = ""
    holdings_as_of: str | None = None
    source_as_of: str | None = None
    coverage_pct: float = 0.0
    after_coverage_pct: float | None = None
    current_weights: dict[str, float] = Field(default_factory=dict[str, float])
    after_weights: dict[str, float] | None = None
    securities: dict[str, SecurityEvidence] = Field(default_factory=dict[str, SecurityEvidence])
    regimes: list[PortfolioRegimeAssessment] = Field(
        default_factory=list[PortfolioRegimeAssessment]
    )
    top_two: list[ActionRegimeImpact] = Field(default_factory=list[ActionRegimeImpact])
    reasons: list[str] = Field(default_factory=list[str])
    scenario_label: str = "Current book; qualitative analyst inference"
    target_verified: bool = False


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _security(
    conn: sqlite3.Connection, db: Path, root: Path, ticker: str, now: datetime
) -> SecurityEvidence:
    kind = get_instrument_kind(conn, ticker)
    result = SecurityEvidence(
        ticker=ticker, instrument_kind=str(kind or "unknown"), state="unavailable"
    )
    raw_identity: list[object] = [
        ticker,
        TAXONOMY_VERSION,
        current_factor_input_sha(db, root, ticker),
    ]
    if kind != "etf":
        result.factors = current_ticker_loadings(db, root, ticker)
        rows = conn.execute(
            "SELECT factor,loading,input_sha,created_at FROM business_factor_exposures WHERE ticker=? AND is_latest=1",
            (ticker,),
        ).fetchall()
        raw_identity.extend([list(row) for row in rows])
        thesis = root / "micro_thesis" / "holdings" / f"{ticker}.json"
        if thesis.is_file():
            raw_identity.append(hashlib.sha256(thesis.read_bytes()).hexdigest())
        if result.factors:
            result.state = "full"
            result.covered_fraction = 1
            result.source_as_of = min(str(row[3]) for row in rows)
        else:
            result.state = "stale" if rows else "unavailable"
            result.reasons.append(
                "No current-input factor evidence; refresh required"
                if rows
                else "No factor evidence"
            )
    else:
        profile = get_etf_profile(conn, ticker)
        if profile:
            admitted = admitted_profile_fields(profile, now=now)
            result.context.update({key: str(receipt.value) for key, receipt in admitted.items()})
            raw_identity.append(profile.model_dump(mode="json"))
        holdings = get_etf_holdings(conn, ticker)
        raw_identity.extend(row.model_dump(mode="json") for row in holdings)
        total = sum(row.weight_pct or 0 for row in holdings)
        invalid = (
            any(
                row.weight_pct is not None
                and (not math.isfinite(row.weight_pct) or row.weight_pct < 0)
                for row in holdings
            )
            or total > 1.000001
        )
        if invalid:
            result.reasons.append("Invalid ETF basket weights; no look-through admitted")
        elif holdings:
            result.source_as_of = min(row.as_of_date.isoformat() for row in holdings)
            fresh = all(
                row.fetched_at.tzinfo is not None
                and datetime.min.replace(tzinfo=UTC) <= row.fetched_at <= now
                and now - row.fetched_at <= PROFILE_CAPTURE_MAX_AGE
                and row.as_of_date <= now.date()
                for row in holdings
            )
            if fresh:
                for row in holdings:
                    for key, value in (
                        ("country", row.country),
                        ("sector", row.sector),
                        ("asset_class", row.asset_class),
                    ):
                        if value:
                            label = f"{key}:{value}"
                            result.context[label] = (
                                f"{float(result.context.get(label, '0')) + (row.weight_pct or 0):.6f}"
                            )
                    if not row.constituent_ticker or not row.weight_pct:
                        continue
                    constituent = (
                        _security(conn, db, root, row.constituent_ticker.upper(), now)
                        if get_instrument_kind(conn, row.constituent_ticker) != "etf"
                        else SecurityEvidence(
                            ticker=row.constituent_ticker,
                            instrument_kind="etf",
                            state="unavailable",
                            reasons=["Nested ETF look-through unavailable"],
                        )
                    )
                    raw_identity.append(constituent.model_dump(mode="json"))
                    if constituent.factors:
                        result.covered_fraction += row.weight_pct
                        for factor, loading in constituent.factors.items():
                            result.factors[factor] = (
                                result.factors.get(factor, 0) + row.weight_pct * loading
                            )
                result.state = "partial" if result.factors else "unavailable"
                if result.covered_fraction < 1:
                    result.reasons.append(
                        f"ETF constituent factors cover {result.covered_fraction:.1%} of basket; uncovered weight remains unknown"
                    )
                result.reasons.append(
                    "ETF source publication currency unverified; fresh capture is not current holdings proof"
                )
            else:
                result.state = "stale"
                result.reasons.append(
                    f"ETF capture stale/invalid under {PROFILE_CAPTURE_POLICY}; no look-through admitted"
                )
        else:
            result.reasons.append(
                "ETF holdings unavailable; profile labels cannot substitute for constituent factors"
            )
    raw_identity.append(result.model_dump(mode="json"))
    result.input_sha = _sha(raw_identity)
    return result


def _coverage(weights: dict[str, float], securities: dict[str, SecurityEvidence]) -> float:
    total = sum(w for t, w in weights.items() if t not in CASH)
    covered = sum(w * securities[t].covered_fraction for t, w in weights.items() if t in securities)
    return 100 * covered / total if total else 0


def read_common_drawdown(
    db_path: Path | str | None,
    repo_root: Path,
    *,
    proposed_deltas: dict[str, float] | None = None,
    now: datetime | None = None,
) -> CommonDrawdownRead:
    """Rebuild every read; changes to source bytes/weights/ETF evidence invalidate identity.

    Deltas are fractions of total book NAV. Cash is the explicit residual funding
    leg. Reject oversells or unfunded adds; do not clamp or infer executed trades.
    """
    now = now or datetime.now(UTC)
    result = CommonDrawdownRead()
    snapshot = read_materialized_weight_snapshot(repo_root)
    db = resolve_db_path(db_path)
    if snapshot is None or db is None or not db.is_file():
        result.reasons.append("Materialized holdings or explicit database unavailable")
        return result
    result.holdings_as_of = snapshot.computed_at.isoformat()
    result.current_weights = dict(snapshot.weights)
    age = (now - snapshot.computed_at).total_seconds() / 3600
    if not 0 <= age <= WEIGHTS_CACHE_STALE_HOURS:
        result.state = "stale"
        result.reasons.append(
            f"Materialized holdings outside {WEIGHTS_CACHE_STALE_HOURS}h freshness window"
        )
        result.input_sha = _sha([result.current_weights, result.holdings_as_of, FRESHNESS_POLICY])
        return result
    tickers = {t for t, w in snapshot.weights.items() if w > 0 and t not in CASH}
    if proposed_deltas:
        after = dict(snapshot.weights)
        for ticker, delta in proposed_deltas.items():
            if ticker != ticker.upper() or ticker in CASH or not math.isfinite(delta):
                raise ValueError(
                    "Proposals require uppercase non-cash tickers and finite fractional deltas"
                )
            after[ticker] = after.get(ticker, 0) + delta
            if after[ticker] < -1e-9:
                raise ValueError(f"Proposed sale exceeds materialized holding: {ticker}")
            after[ticker] = max(0, after[ticker])
        cash = sum(w for t, w in snapshot.weights.items() if t in CASH) - sum(
            proposed_deltas.values()
        )
        if cash < -1e-9:
            raise ValueError("Proposed add exceeds sale proceeds and materialized cash")
        after = {t: w for t, w in after.items() if t not in CASH}
        after["CASH"] = max(0, cash)
        result.after_weights = after
        result.scenario_label = (
            "Unverified synthetic proposal; no executed fills or approved target inferred"
        )
        tickers.update(t for t, w in after.items() if w > 0 and t not in CASH)
    try:
        conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            result.securities = {t: _security(conn, db, repo_root, t, now) for t in sorted(tickers)}
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        result.reasons.append(f"Evidence unavailable: {type(exc).__name__}")
        return result
    factors = {t: security.factors for t, security in result.securities.items() if security.factors}
    result.coverage_pct = _coverage(result.current_weights, result.securities)
    result.after_coverage_pct = (
        _coverage(result.after_weights, result.securities)
        if result.after_weights is not None
        else None
    )
    relevant = [s for s in result.securities.values()]
    result.state = (
        "full"
        if relevant and all(s.state == "full" for s in relevant)
        else "partial"
        if factors
        else "stale"
        if any(s.state == "stale" for s in relevant)
        else "unavailable"
    )
    sources = [s.source_as_of for s in relevant if s.source_as_of]
    result.source_as_of = min(sources) if sources else None
    if factors:
        for regime in INITIAL_REGIMES:
            assessment = evaluate_portfolio_regime(result.current_weights, factors, regime)
            # Coverage is actual factor-backed basket weight, never all of a partially mapped ETF.
            result.regimes.append(
                assessment.model_copy(
                    update={
                        "coverage_pct": round(result.coverage_pct, 2),
                        "covered_weight_pct": assessment.total_non_cash_weight_pct
                        * result.coverage_pct
                        / 100,
                        "availability": "full"
                        if result.state == "full"
                        else "partial"
                        if result.coverage_pct > 0
                        else "unavailable",
                    }
                )
            )
        if result.coverage_pct > 0 and (
            result.after_coverage_pct is None or result.after_coverage_pct > 0
        ):
            result.top_two = select_top_regimes_for_action(
                result.current_weights, proposed_deltas or {}, factors
            )
        else:
            result.reasons.append(
                "Before/after factor coverage unavailable; no action ranking admitted"
            )
    result.input_sha = _sha(
        [
            result.model_dump(mode="json"),
            REGIME_REGISTRY_VERSION,
            FRESHNESS_POLICY,
            PROFILE_CAPTURE_POLICY,
        ]
    )
    return result


def paired_wix_avdv_scenario(
    db_path: Path | str | None, repo_root: Path, target_weight: float
) -> CommonDrawdownRead:
    """Explicit endpoint within the unverified 4.5-5.0% target band, not a recommendation."""
    if not 0.045 <= target_weight <= 0.05:
        raise ValueError("Synthetic AVDV target must lie in the unverified 4.5-5.0% band")
    snapshot = read_materialized_weight_snapshot(repo_root)
    if snapshot is None or snapshot.weights.get("WIX", 0) <= 0:
        return CommonDrawdownRead(
            reasons=[
                "Paired scenario requires a materialized before-state holding WIX; no historical fills invented"
            ]
        )
    result = read_common_drawdown(
        db_path,
        repo_root,
        proposed_deltas={
            "WIX": -snapshot.weights["WIX"],
            "AVDV": target_weight - snapshot.weights.get("AVDV", 0),
        },
    )
    result.scenario_label = f"Unverified synthetic WIX sell + AVDV {target_weight:.1%}; proposed band 4.5-5.0%; no executed fills"
    result.input_sha = _sha([result.input_sha, result.scenario_label])
    return result


def review_summary(read: CommonDrawdownRead) -> str:
    evidence = f"{read.state}; factor-backed coverage {read.coverage_pct:.1f}%; holdings {read.holdings_as_of or 'unavailable'}; source {read.source_as_of or 'unavailable'}; registry {read.registry_version}; policy {read.freshness_policy}; input {read.input_sha or 'unavailable'}"
    ratings = "; ".join(f"{r.regime_name}: {r.before_rating}" for r in read.top_two)
    reasons = sorted(
        {reason for security in read.securities.values() for reason in security.reasons}
        | set(read.reasons)
    )
    evidence += "; " + "; ".join(reasons) if reasons else ""
    return f"{evidence}; qualitative analyst inference" + (
        f"; {ratings}" if ratings else "; ratings unavailable"
    )
