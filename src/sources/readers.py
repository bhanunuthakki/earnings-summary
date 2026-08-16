"""Provider-neutral financial data readers and dual-read shadowing harness.

Migrates downstream consumers (report, DCF, valuation, discovery, pricing) off
raw FMP filesystem caches and behind strongly-typed, provider-neutral adapters.
Supports dual-read verification to guarantee zero data divergence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from sources.adapters import (
    AdjustedPriceSeries,
    CorporateActionAdjustment,
    DatedEstimateObservation,
    FilingSectionPayload,
    FmpProviderAdapter,
    ProviderAdapter,
    SegmentStructureObservation,
)


class ParityStatus(StrEnum):
    """Classification of dual-read shadowing evaluation."""

    VERIFIED_MATCH = "VERIFIED_MATCH"
    VERIFIED_DIVERGENCE = "VERIFIED_DIVERGENCE"
    INDETERMINATE_UNAVAILABLE = "INDETERMINATE_UNAVAILABLE"


class DualReadParityReceipt(BaseModel):
    """Immutable receipt recording dual-read verification parity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    ticker: str
    consumer: str
    legacy_record_count: int
    adapter_record_count: int
    legacy_unique_count: int | None = None
    adapter_unique_count: int | None = None
    status: ParityStatus
    parity_passed: bool | None = None  # True if match, False if divergence, None if unavailable
    discrepancy_details: tuple[str, ...] = ()
    verified_at: datetime


class ReaderUnavailableStatus(BaseModel):
    """Typed fallback representation when data is unavailable from the configured provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    provider: str
    data_type: str
    reason: str
    as_of: datetime


class ProviderNeutralDataReader:
    """Standardized reader interface backed by pluggable ProviderAdapter implementations."""

    def __init__(
        self,
        repo_root: Path,
        *,
        adapter: ProviderAdapter | None = None,
        cache_subdir: str = "data/historical/fmp",
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fmp_dir = self.repo_root / cache_subdir
        self.adapter = adapter or FmpProviderAdapter()

    def get_filing_sections(
        self,
        ticker: str,
        *,
        form: str = "10-K",
        year: int | None = None,
    ) -> list[FilingSectionPayload] | ReaderUnavailableStatus:
        """Read and parse filing sections using the provider-neutral adapter."""
        ticker_clean = ticker.upper().strip()
        matching_files: list[Path] = []

        if self.fmp_dir.exists():
            if year is not None:
                target = (
                    self.fmp_dir
                    / f"{ticker_clean}_form_{form.lower().replace('-', '')}_{year}.json"
                )
                if target.exists():
                    matching_files.append(target)
            else:
                pattern = f"{ticker_clean}_form_{form.lower().replace('-', '')}_*.json"
                matching_files = sorted(self.fmp_dir.glob(pattern), reverse=True)

        if not matching_files:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="filing_sections",
                reason=f"No filing cache found for {ticker_clean} (form={form}, year={year})",
                as_of=datetime.now(UTC),
            )

        target_file = matching_files[0]
        try:
            content = target_file.read_bytes()
            return self.adapter.parse_filing_sections(
                content,
                ticker=ticker_clean,
                form=form,
                fiscal_year=year,
            )
        except Exception as e:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="filing_sections",
                reason=f"Failed parsing filing sections from {target_file.name}: {type(e).__name__}: {e}",
                as_of=datetime.now(UTC),
            )

    def get_analyst_estimates(
        self,
        ticker: str,
        *,
        metric: str | None = None,
    ) -> list[DatedEstimateObservation] | ReaderUnavailableStatus:
        """Read and parse analyst consensus estimates."""
        ticker_clean = ticker.upper().strip()
        candidates = [
            self.fmp_dir / f"{ticker_clean}_analyst_estimates.json",
            self.fmp_dir / f"{ticker_clean}_analyst_estimates_annual.json",
        ]

        found_file: Path | None = next((c for c in candidates if c.exists()), None)

        if not found_file:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="analyst_estimates",
                reason=f"No estimates cache found for {ticker_clean}",
                as_of=datetime.now(UTC),
            )

        try:
            content = found_file.read_bytes()
            all_estimates = self.adapter.parse_estimates(content, ticker=ticker_clean)
            if metric:
                return [e for e in all_estimates if e.metric.lower() == metric.lower()]
            return all_estimates
        except Exception as e:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="analyst_estimates",
                reason=f"Failed parsing analyst estimates from {found_file.name}: {type(e).__name__}: {e}",
                as_of=datetime.now(UTC),
            )

    def get_segment_structure(
        self,
        ticker: str,
        *,
        dim_type: str = "geography",
    ) -> list[SegmentStructureObservation] | ReaderUnavailableStatus:
        """Read and parse product or geographic revenue segments."""
        ticker_clean = ticker.upper().strip()
        suffix = "product" if "prod" in dim_type.lower() else "geographic"
        candidates = [
            self.fmp_dir / f"{ticker_clean}_revenue_{suffix}_segmentation.json",
            self.fmp_dir / f"{ticker_clean}_revenue_segmentation_{suffix}.json",
            self.fmp_dir / f"{ticker_clean}_segment_{suffix}.json",
        ]

        found_file: Path | None = next((c for c in candidates if c.exists()), None)

        if not found_file:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type=f"segments_{dim_type}",
                reason=f"No {dim_type} segmentation cache found for {ticker_clean}",
                as_of=datetime.now(UTC),
            )

        try:
            content = found_file.read_bytes()
            return self.adapter.parse_segments(content, ticker=ticker_clean, dim_type=dim_type)
        except Exception as e:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type=f"segments_{dim_type}",
                reason=f"Failed parsing segmentation from {found_file.name}: {type(e).__name__}: {e}",
                as_of=datetime.now(UTC),
            )

    def get_adjusted_price_series(
        self,
        ticker: str,
        *,
        adjustment: CorporateActionAdjustment = CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
        currency: str = "USD",
    ) -> AdjustedPriceSeries | ReaderUnavailableStatus:
        """Read and parse historical adjusted prices."""
        ticker_clean = ticker.upper().strip()
        candidates = [
            self.fmp_dir / f"{ticker_clean}_price_chart_10y_div_adj.json",
            self.fmp_dir / f"{ticker_clean}_historical_price_full.json",
            self.fmp_dir / f"{ticker_clean}_prices.json",
        ]

        found_file: Path | None = next((c for c in candidates if c.exists()), None)

        if not found_file:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="adjusted_prices",
                reason=f"No price history cache found for {ticker_clean}",
                as_of=datetime.now(UTC),
            )

        try:
            content = found_file.read_bytes()
            return self.adapter.parse_prices(
                content,
                ticker=ticker_clean,
                adjustment_method=adjustment,
                currency=currency,
            )
        except Exception as e:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="adjusted_prices",
                reason=f"Failed parsing price series from {found_file.name}: {type(e).__name__}: {e}",
                as_of=datetime.now(UTC),
            )

    def get_latest_price(
        self,
        ticker: str,
    ) -> tuple[Decimal, datetime, str] | ReaderUnavailableStatus:
        """Extract latest closing price, observation timestamp, and currency."""
        ticker_clean = ticker.upper().strip()
        res = self.get_adjusted_price_series(ticker_clean)
        if isinstance(res, ReaderUnavailableStatus):
            return res
        if not res.points:
            return ReaderUnavailableStatus(
                ticker=ticker_clean,
                provider=self.adapter.provider_name,
                data_type="latest_price",
                reason=f"No price points found for {ticker_clean}",
                as_of=datetime.now(UTC),
            )
        latest_point = res.points[-1]
        return latest_point.close, latest_point.as_of_date, res.currency


class DualReadShadowingVerifier:
    """Harness to evaluate field-level dual-read parity between legacy raw readers and adapter readers."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.reader = ProviderNeutralDataReader(repo_root=self.repo_root)

    def _make_run_id(self) -> str:
        return f"parity_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"

    def verify_price_parity(self, ticker: str) -> DualReadParityReceipt:
        """Verify price series equality across legacy direct JSON and adapter reader with field-level checks."""
        ticker_clean = ticker.upper().strip()
        fmp_dir = self.repo_root / "data" / "historical" / "fmp"
        candidates = [
            fmp_dir / f"{ticker_clean}_price_chart_10y_div_adj.json",
            fmp_dir / f"{ticker_clean}_historical_price_full.json",
            fmp_dir / f"{ticker_clean}_prices.json",
        ]

        found_file: Path | None = next((c for c in candidates if c.exists()), None)
        discrepancies: list[str] = []
        legacy_points: dict[str, Decimal] = {}
        raw_legacy_count = 0

        if found_file:
            try:
                raw_obj: object = json.loads(found_file.read_text(encoding="utf-8"))
                historical: list[object] = []
                if isinstance(raw_obj, dict):
                    raw_dict = cast("dict[str, object]", raw_obj)
                    h_val = raw_dict.get("historical")
                    if isinstance(h_val, list):
                        historical = cast("list[object]", h_val)
                elif isinstance(raw_obj, list):
                    historical = cast("list[object]", raw_obj)

                raw_legacy_count = len(historical)
                seen_dates: set[str] = set()

                for item in historical:
                    if isinstance(item, dict):
                        item_dict = cast("dict[str, object]", item)
                        d_str = str(item_dict.get("date", "")).strip()
                        if d_str in seen_dates:
                            discrepancies.append(f"Duplicate date {d_str} in legacy price JSON")
                        seen_dates.add(d_str)

                        c_val = (
                            item_dict.get("adjClose")
                            if item_dict.get("adjClose") is not None
                            else item_dict.get("close")
                        )
                        if d_str and c_val is not None:
                            legacy_points[d_str] = Decimal(str(c_val))
            except Exception as e:
                discrepancies.append(f"Legacy read error: {type(e).__name__}: {e}")
        else:
            discrepancies.append(f"No legacy price cache found for {ticker_clean}")

        adapter_res = self.reader.get_adjusted_price_series(ticker_clean)
        adapter_points: dict[str, Decimal] = {}
        raw_adapter_count = 0

        if isinstance(adapter_res, AdjustedPriceSeries):
            raw_adapter_count = len(adapter_res.points)
            for pt in adapter_res.points:
                d_str = pt.as_of_date.strftime("%Y-%m-%d")
                adapter_points[d_str] = pt.close
        else:
            discrepancies.append(f"Adapter read unavailable: {adapter_res.reason}")

        if not found_file and isinstance(adapter_res, ReaderUnavailableStatus):
            return DualReadParityReceipt(
                run_id=self._make_run_id(),
                ticker=ticker_clean,
                consumer="price_history",
                legacy_record_count=0,
                adapter_record_count=0,
                legacy_unique_count=0,
                adapter_unique_count=0,
                status=ParityStatus.INDETERMINATE_UNAVAILABLE,
                parity_passed=None,
                discrepancy_details=("Data unavailable in both legacy and adapter regimes",),
                verified_at=datetime.now(UTC),
            )

        if not discrepancies:
            if raw_legacy_count != raw_adapter_count:
                discrepancies.append(
                    f"Price count mismatch: legacy={raw_legacy_count} vs adapter={raw_adapter_count}"
                )
            else:
                legacy_set = set(legacy_points.items())
                adapter_set = set(adapter_points.items())
                diff = legacy_set.symmetric_difference(adapter_set)
                if diff:
                    discrepancies.append(
                        f"Price observations divergence on {len(diff)} items: {list(diff)[:5]}"
                    )

        passed = len(discrepancies) == 0

        return DualReadParityReceipt(
            run_id=self._make_run_id(),
            ticker=ticker_clean,
            consumer="price_history",
            legacy_record_count=raw_legacy_count,
            adapter_record_count=raw_adapter_count,
            legacy_unique_count=len(legacy_points),
            adapter_unique_count=len(adapter_points),
            status=ParityStatus.VERIFIED_MATCH if passed else ParityStatus.VERIFIED_DIVERGENCE,
            parity_passed=passed,
            discrepancy_details=tuple(discrepancies),
            verified_at=datetime.now(UTC),
        )

    def verify_estimates_parity(self, ticker: str) -> DualReadParityReceipt:
        """Verify estimate observations equality across legacy direct JSON and adapter reader with field-level checks."""
        ticker_clean = ticker.upper().strip()
        fmp_dir = self.repo_root / "data" / "historical" / "fmp"
        candidates = [
            fmp_dir / f"{ticker_clean}_analyst_estimates.json",
            fmp_dir / f"{ticker_clean}_analyst_estimates_annual.json",
        ]

        found_file: Path | None = next((c for c in candidates if c.exists()), None)
        discrepancies: list[str] = []
        legacy_metrics: set[tuple[str, str, Decimal]] = set()
        raw_legacy_count = 0

        if found_file:
            try:
                raw_obj: object = json.loads(found_file.read_text(encoding="utf-8"))
                if isinstance(raw_obj, list):
                    raw_legacy_count = len(cast("list[object]", raw_obj))
                    for item in cast("list[object]", raw_obj):
                        if isinstance(item, dict):
                            item_dict = cast("dict[str, object]", item)
                            d_str = str(item_dict.get("date", "")).strip()
                            if not d_str:
                                continue
                            metric_map: list[tuple[str, object | None]] = [
                                ("revenue", item_dict.get("revenueAvg")),
                                ("eps", item_dict.get("epsAvg")),
                                ("ebitda", item_dict.get("ebitdaAvg")),
                                ("ebit", item_dict.get("ebitAvg")),
                                ("net_income", item_dict.get("netIncomeAvg")),
                            ]
                            for m_name, m_val in metric_map:
                                if m_val is not None:
                                    try:
                                        legacy_metrics.add((d_str, m_name, Decimal(str(m_val))))
                                    except Exception:
                                        continue
            except Exception as e:
                discrepancies.append(f"Legacy read error: {type(e).__name__}: {e}")
        else:
            discrepancies.append(f"No legacy estimates cache found for {ticker_clean}")

        adapter_res = self.reader.get_analyst_estimates(ticker_clean)
        adapter_metrics: set[tuple[str, str, Decimal]] = set()
        raw_adapter_count = 0

        if isinstance(adapter_res, list):
            raw_adapter_count = len(adapter_res)
            for est in adapter_res:
                d_str = est.observation_date.strftime("%Y-%m-%d")
                adapter_metrics.add((d_str, est.metric, est.estimated_avg))
        else:
            discrepancies.append(f"Adapter read unavailable: {adapter_res.reason}")

        if not found_file and isinstance(adapter_res, ReaderUnavailableStatus):
            return DualReadParityReceipt(
                run_id=self._make_run_id(),
                ticker=ticker_clean,
                consumer="analyst_estimates",
                legacy_record_count=0,
                adapter_record_count=0,
                legacy_unique_count=0,
                adapter_unique_count=0,
                status=ParityStatus.INDETERMINATE_UNAVAILABLE,
                parity_passed=None,
                discrepancy_details=("Data unavailable in both legacy and adapter regimes",),
                verified_at=datetime.now(UTC),
            )

        if not discrepancies:
            if len(legacy_metrics) != len(adapter_metrics):
                discrepancies.append(
                    f"Estimate metric count mismatch: legacy={len(legacy_metrics)} vs adapter={len(adapter_metrics)}"
                )
            else:
                diff = legacy_metrics.symmetric_difference(adapter_metrics)
                if diff:
                    discrepancies.append(
                        f"Estimate observations divergence on {len(diff)} items: {list(diff)[:5]}"
                    )

        passed = len(discrepancies) == 0

        return DualReadParityReceipt(
            run_id=self._make_run_id(),
            ticker=ticker_clean,
            consumer="analyst_estimates",
            legacy_record_count=raw_legacy_count,
            adapter_record_count=raw_adapter_count,
            legacy_unique_count=len(legacy_metrics),
            adapter_unique_count=len(adapter_metrics),
            status=ParityStatus.VERIFIED_MATCH if passed else ParityStatus.VERIFIED_DIVERGENCE,
            parity_passed=passed,
            discrepancy_details=tuple(discrepancies),
            verified_at=datetime.now(UTC),
        )

    def verify_segments_parity(
        self, ticker: str, dim_type: str = "geography"
    ) -> DualReadParityReceipt:
        """Verify segment observation equality across legacy direct JSON and adapter reader."""
        ticker_clean = ticker.upper().strip()
        fmp_dir = self.repo_root / "data" / "historical" / "fmp"
        suffix = "product" if "prod" in dim_type.lower() else "geographic"
        candidates = [
            fmp_dir / f"{ticker_clean}_revenue_{suffix}_segmentation.json",
            fmp_dir / f"{ticker_clean}_revenue_segmentation_{suffix}.json",
            fmp_dir / f"{ticker_clean}_segment_{suffix}.json",
        ]

        found_file: Path | None = next((c for c in candidates if c.exists()), None)
        discrepancies: list[str] = []
        legacy_segments: set[tuple[str, str, Decimal]] = set()
        raw_legacy_count = 0

        if found_file:
            try:
                raw_obj: object = json.loads(found_file.read_text(encoding="utf-8"))
                if isinstance(raw_obj, list):
                    raw_legacy_count = len(cast("list[object]", raw_obj))
                    for item in cast("list[object]", raw_obj):
                        if isinstance(item, dict):
                            item_dict = cast("dict[str, object]", item)
                            d_str = str(item_dict.get("date", "")).strip()
                            raw_data = item_dict.get("data")
                            if d_str and isinstance(raw_data, dict):
                                data_dict = cast("dict[str, object]", raw_data)
                                for s_name, val in data_dict.items():
                                    if val is not None:
                                        try:
                                            legacy_segments.add(
                                                (d_str, str(s_name), Decimal(str(val)))
                                            )
                                        except Exception:
                                            continue
            except Exception as e:
                discrepancies.append(f"Legacy read error: {type(e).__name__}: {e}")
        else:
            discrepancies.append(f"No legacy {dim_type} segment cache found for {ticker_clean}")

        adapter_res = self.reader.get_segment_structure(ticker_clean, dim_type=dim_type)
        adapter_segments: set[tuple[str, str, Decimal]] = set()
        raw_adapter_count = 0

        if isinstance(adapter_res, list):
            raw_adapter_count = len(adapter_res)
            for seg in adapter_res:
                d_str = seg.period_end.strftime("%Y-%m-%d")
                adapter_segments.add((d_str, seg.segment_name, seg.value))
        else:
            discrepancies.append(f"Adapter read unavailable: {adapter_res.reason}")

        if not found_file and isinstance(adapter_res, ReaderUnavailableStatus):
            return DualReadParityReceipt(
                run_id=self._make_run_id(),
                ticker=ticker_clean,
                consumer=f"segments_{dim_type}",
                legacy_record_count=0,
                adapter_record_count=0,
                legacy_unique_count=0,
                adapter_unique_count=0,
                status=ParityStatus.INDETERMINATE_UNAVAILABLE,
                parity_passed=None,
                discrepancy_details=("Data unavailable in both legacy and adapter regimes",),
                verified_at=datetime.now(UTC),
            )

        if not discrepancies:
            if len(legacy_segments) != len(adapter_segments):
                discrepancies.append(
                    f"Segment count mismatch: legacy={len(legacy_segments)} vs adapter={len(adapter_segments)}"
                )
            else:
                diff = legacy_segments.symmetric_difference(adapter_segments)
                if diff:
                    discrepancies.append(
                        f"Segment divergence on {len(diff)} items: {list(diff)[:5]}"
                    )

        passed = len(discrepancies) == 0

        return DualReadParityReceipt(
            run_id=self._make_run_id(),
            ticker=ticker_clean,
            consumer=f"segments_{dim_type}",
            legacy_record_count=raw_legacy_count,
            adapter_record_count=raw_adapter_count,
            legacy_unique_count=len(legacy_segments),
            adapter_unique_count=len(adapter_segments),
            status=ParityStatus.VERIFIED_MATCH if passed else ParityStatus.VERIFIED_DIVERGENCE,
            parity_passed=passed,
            discrepancy_details=tuple(discrepancies),
            verified_at=datetime.now(UTC),
        )

    def verify_filing_sections_parity(
        self, ticker: str, form: str = "10-K"
    ) -> DualReadParityReceipt:
        """Verify filing section extraction equality across legacy direct JSON and adapter reader."""
        ticker_clean = ticker.upper().strip()
        fmp_dir = self.repo_root / "data" / "historical" / "fmp"
        pattern = f"{ticker_clean}_form_{form.lower().replace('-', '')}_*.json"
        matching_files = sorted(fmp_dir.glob(pattern), reverse=True) if fmp_dir.exists() else []

        discrepancies: list[str] = []
        legacy_sections: dict[str, str] = {}

        if matching_files:
            try:
                raw_obj: object = json.loads(matching_files[0].read_text(encoding="utf-8"))
                if isinstance(raw_obj, dict):
                    raw_dict = cast("dict[str, object]", raw_obj)
                    # Retrieve metadata keys declared by the provider adapter
                    metadata_keys = self.reader.adapter.filing_metadata_keys
                    for k, v in raw_dict.items():
                        if k not in metadata_keys and v is not None:
                            if isinstance(v, (list, dict)):
                                cleaned = json.dumps(v, sort_keys=True)
                            elif isinstance(v, str):
                                cleaned = v.strip()
                            else:
                                cleaned = str(v).strip()
                            if cleaned:
                                legacy_sections[str(k)] = cleaned
            except Exception as e:
                discrepancies.append(f"Legacy read error: {type(e).__name__}: {e}")
        else:
            discrepancies.append(f"No legacy filing cache found for {ticker_clean} ({form})")

        adapter_res = self.reader.get_filing_sections(ticker_clean, form=form)
        adapter_sections: dict[str, str] = {}
        if isinstance(adapter_res, list):
            for sec in adapter_res:
                adapter_sections[sec.section_name] = sec.raw_text
        else:
            discrepancies.append(f"Adapter read unavailable: {adapter_res.reason}")

        if not matching_files and isinstance(adapter_res, ReaderUnavailableStatus):
            return DualReadParityReceipt(
                run_id=self._make_run_id(),
                ticker=ticker_clean,
                consumer=f"filing_sections_{form}",
                legacy_record_count=0,
                adapter_record_count=0,
                legacy_unique_count=0,
                adapter_unique_count=0,
                status=ParityStatus.INDETERMINATE_UNAVAILABLE,
                parity_passed=None,
                discrepancy_details=("Data unavailable in both legacy and adapter regimes",),
                verified_at=datetime.now(UTC),
            )

        if not discrepancies:
            if len(legacy_sections) != len(adapter_sections):
                discrepancies.append(
                    f"Filing section count mismatch: legacy={len(legacy_sections)} vs adapter={len(adapter_sections)}"
                )
            else:
                for sec_name, leg_text in legacy_sections.items():
                    if sec_name not in adapter_sections:
                        discrepancies.append(f"Section {sec_name} missing in adapter sections")
                    elif adapter_sections[sec_name] != leg_text:
                        discrepancies.append(f"Section text mismatch on {sec_name}")

        passed = len(discrepancies) == 0

        return DualReadParityReceipt(
            run_id=self._make_run_id(),
            ticker=ticker_clean,
            consumer=f"filing_sections_{form}",
            legacy_record_count=len(legacy_sections),
            adapter_record_count=len(adapter_sections),
            legacy_unique_count=len(legacy_sections),
            adapter_unique_count=len(adapter_sections),
            status=ParityStatus.VERIFIED_MATCH if passed else ParityStatus.VERIFIED_DIVERGENCE,
            parity_passed=passed,
            discrepancy_details=tuple(discrepancies),
            verified_at=datetime.now(UTC),
        )
