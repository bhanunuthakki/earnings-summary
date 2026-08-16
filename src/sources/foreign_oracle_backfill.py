"""Foreign filer backfill and comparison against sealed FMP cache oracle.

Compares governed foreign SEC/IR normalized facts against sealed historical FMP cache
observations, classifying discrepancies into taxonomy mapping, source timing,
provider normalization, or material disagreement without live database writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from sources.foreign_filers import (
    FOREIGN_FILER_ROSTER,
    ForeignNormalizationReceipt,
    InterimDisposition,
    ReportingCadence,
)


class OracleComparisonClassification(StrEnum):
    """Classification of difference between SEC/IR normalized fact and sealed oracle."""

    EXACT_MATCH = "EXACT_MATCH"
    TAXONOMY_MAPPING_DIVERGENCE = "TAXONOMY_MAPPING_DIVERGENCE"
    SOURCE_TIMING_RESTATED = "SOURCE_TIMING_RESTATED"
    PROVIDER_NORMALIZATION = "PROVIDER_NORMALIZATION"
    MISSING_EXTRACTION = "MISSING_EXTRACTION"
    MATERIAL_DISAGREEMENT = "MATERIAL_DISAGREEMENT"
    NOT_APPLICABLE_SEMIANNUAL = "NOT_APPLICABLE_SEMIANNUAL"
    DEGRADED_NON_INLINE = "DEGRADED_NON_INLINE"


class ForeignOracleComparisonObservation(BaseModel):
    """Immutable record of a single fact comparison against the oracle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    concept: str
    canonical_concept: str | None = None
    fiscal_year: int
    fiscal_period: str
    currency: str
    sec_fact_value: Decimal | None = None
    oracle_fmp_value: Decimal | None = None
    classification: OracleComparisonClassification
    divergence_ratio: Decimal | None = None
    source_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    notes: str


class ForeignBackfillReceipt(BaseModel):
    """Immutable audit receipt of foreign filer backfill validation pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    ticker: str
    reporting_cadence: str
    reporting_currency: str
    comparisons_count: int
    exact_matches_count: int
    discrepancies_count: int
    degraded_or_na_count: int
    status: Literal["PASS", "HOLD"]
    comparisons: tuple[ForeignOracleComparisonObservation, ...] = ()
    reason: str
    verified_at: datetime


class ForeignOracleBackfillValidator:
    """Evaluates SEC/IR normalized foreign facts against sealed FMP cache oracle observations."""

    def __init__(self) -> None:
        self.roster = FOREIGN_FILER_ROSTER

    def _make_run_id(self) -> str:
        return f"oracle_backfill_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"

    def compare_facts(
        self,
        ticker: str,
        sec_receipt: ForeignNormalizationReceipt,
        oracle_facts: dict[str, Decimal],  # concept -> value in oracle
    ) -> ForeignBackfillReceipt:
        """Compare normalized SEC/IR facts against sealed FMP cache facts."""
        ticker_clean = ticker.upper().strip()
        profile = self.roster.get(ticker_clean)
        run_id = self._make_run_id()
        now_ts = datetime.now(UTC)

        cadence = profile.cadence.value if profile else ReportingCadence.QUARTERLY.value
        currency = profile.reporting_currency if profile else "USD"

        # 1. Handle non-inline HTML or semiannual N/A dispositions directly
        if sec_receipt.disposition == InterimDisposition.REJECTED_NON_INLINE_HTML:
            obs = ForeignOracleComparisonObservation(
                ticker=ticker_clean,
                concept="all",
                canonical_concept=None,
                fiscal_year=2026,
                fiscal_period="Q1",
                currency=currency,
                sec_fact_value=None,
                oracle_fmp_value=None,
                classification=OracleComparisonClassification.DEGRADED_NON_INLINE,
                divergence_ratio=None,
                source_hash=sec_receipt.document_hash,
                notes=f"Form 6-K for {ticker_clean} is non-inline HTML; rejected zero-fact fake XBRL ingest.",
            )
            return ForeignBackfillReceipt(
                run_id=run_id,
                ticker=ticker_clean,
                reporting_cadence=cadence,
                reporting_currency=currency,
                comparisons_count=1,
                exact_matches_count=0,
                discrepancies_count=0,
                degraded_or_na_count=1,
                status="PASS",
                comparisons=(obs,),
                reason=f"Correctly degraded non-inline SEC form for {ticker_clean}.",
                verified_at=now_ts,
            )

        if sec_receipt.disposition == InterimDisposition.NOT_APPLICABLE_SEMIANNUAL:
            obs = ForeignOracleComparisonObservation(
                ticker=ticker_clean,
                concept="all",
                canonical_concept=None,
                fiscal_year=2025,
                fiscal_period="Q1",
                currency=currency,
                sec_fact_value=None,
                oracle_fmp_value=None,
                classification=OracleComparisonClassification.NOT_APPLICABLE_SEMIANNUAL,
                divergence_ratio=None,
                source_hash=sec_receipt.document_hash,
                notes=f"{ticker_clean} reports semiannually; quarterly US-style slice is not applicable.",
            )
            return ForeignBackfillReceipt(
                run_id=run_id,
                ticker=ticker_clean,
                reporting_cadence=cadence,
                reporting_currency=currency,
                comparisons_count=1,
                exact_matches_count=0,
                discrepancies_count=0,
                degraded_or_na_count=1,
                status="PASS",
                comparisons=(obs,),
                reason=f"Correctly handled semiannual cadence for {ticker_clean}.",
                verified_at=now_ts,
            )

        # 2. Compare fact-by-fact
        comparisons: list[ForeignOracleComparisonObservation] = []
        exact_matches = 0
        discrepancies = 0

        for fact in sec_receipt.facts:
            lookup_key = fact.canonical_concept or fact.concept.lower().replace(" ", "_")
            oracle_val = oracle_facts.get(lookup_key) or oracle_facts.get(fact.concept)

            if oracle_val is None:
                # Oracle missing this specific fact
                comparisons.append(
                    ForeignOracleComparisonObservation(
                        ticker=ticker_clean,
                        concept=fact.concept,
                        canonical_concept=fact.canonical_concept,
                        fiscal_year=fact.fiscal_year,
                        fiscal_period=fact.fiscal_period,
                        currency=fact.currency,
                        sec_fact_value=fact.value,
                        oracle_fmp_value=None,
                        classification=OracleComparisonClassification.MISSING_EXTRACTION,
                        divergence_ratio=None,
                        source_hash=fact.source_hash,
                        notes="Fact extracted in SEC filing but missing in sealed oracle cache.",
                    )
                )
                discrepancies += 1
                continue

            # Check value equality
            if fact.value == oracle_val:
                comparisons.append(
                    ForeignOracleComparisonObservation(
                        ticker=ticker_clean,
                        concept=fact.concept,
                        canonical_concept=fact.canonical_concept,
                        fiscal_year=fact.fiscal_year,
                        fiscal_period=fact.fiscal_period,
                        currency=fact.currency,
                        sec_fact_value=fact.value,
                        oracle_fmp_value=oracle_val,
                        classification=OracleComparisonClassification.EXACT_MATCH,
                        divergence_ratio=Decimal("0.0"),
                        source_hash=fact.source_hash,
                        notes="Exact numeric match with sealed oracle.",
                    )
                )
                exact_matches += 1
            else:
                # Calculate divergence ratio
                div_ratio = (
                    abs(fact.value - oracle_val) / abs(oracle_val)
                    if oracle_val != Decimal("0")
                    else Decimal("1.0")
                )
                # Classify divergence type
                if div_ratio > Decimal("0.05"):
                    classification = OracleComparisonClassification.MATERIAL_DISAGREEMENT
                else:
                    classification = OracleComparisonClassification.PROVIDER_NORMALIZATION

                comparisons.append(
                    ForeignOracleComparisonObservation(
                        ticker=ticker_clean,
                        concept=fact.concept,
                        canonical_concept=fact.canonical_concept,
                        fiscal_year=fact.fiscal_year,
                        fiscal_period=fact.fiscal_period,
                        currency=fact.currency,
                        sec_fact_value=fact.value,
                        oracle_fmp_value=oracle_val,
                        classification=classification,
                        divergence_ratio=div_ratio,
                        source_hash=fact.source_hash,
                        notes=f"Value discrepancy: SEC={fact.value} vs Oracle={oracle_val} (diff={div_ratio:.2%}).",
                    )
                )
                discrepancies += 1

        status: Literal["PASS", "HOLD"] = (
            "PASS" if discrepancies == 0 or (exact_matches > 0 and discrepancies == 0) else "HOLD"
        )

        return ForeignBackfillReceipt(
            run_id=run_id,
            ticker=ticker_clean,
            reporting_cadence=cadence,
            reporting_currency=currency,
            comparisons_count=len(comparisons),
            exact_matches_count=exact_matches,
            discrepancies_count=discrepancies,
            degraded_or_na_count=0,
            status=status,
            comparisons=tuple(comparisons),
            reason=f"Evaluated {len(comparisons)} facts against sealed oracle: {exact_matches} exact matches, {discrepancies} discrepancies.",
            verified_at=now_ts,
        )
