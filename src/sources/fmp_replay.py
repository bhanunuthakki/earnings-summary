"""Deterministic, offline replay of the provider-adapter FMP corpus slice.

The adapter contract consumes only immutable cache files.  This module maps
the four supported FMP file families to adapter calls, seals every primary and
companion packet in a manifest digest, and records failures without issuing
network calls or mutating a database.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sources.adapters import (
    CorporateActionAdjustment,
    FmpProviderAdapter,
    SegmentDimension,
)


class ReplayFailure(BaseModel):
    """One cache input the strict adapter could not admit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relative_path: str = Field(min_length=1)
    family: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)


class FmpAdapterReplayReport(BaseModel):
    """Sealed summary of one offline replay, suitable for a receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    selected_files: int = Field(ge=0)
    manifest_files: int = Field(ge=0)
    succeeded_files: int = Field(ge=0)
    failed_files: int = Field(ge=0)
    emitted_records: dict[str, int]
    failures: tuple[ReplayFailure, ...]

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True)
class _ReplayInput:
    primary_path: Path
    ticker: str
    family: str
    companion_path: Path | None = None
    fiscal_year: int | None = None


_FAMILY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("form_10k_", "filing"),
    ("analyst_estimates_annual", "estimate"),
    ("analyst_estimates_quarterly", "estimate"),
    ("geo_segments_annual", "geo_segment"),
    ("geo_segments_quarterly", "geo_segment"),
    ("product_segments_annual", "product_segment"),
    ("product_segments_quarterly", "product_segment"),
    ("price_chart_10y_div_adj", "price"),
)


def _safe_relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"cache path escapes corpus root: {path}") from exc


def _ticker_and_family(path: Path) -> tuple[str, str, int | None] | None:
    name = path.name
    if not name.endswith(".json"):
        return None
    stem = name.removesuffix(".json")
    for suffix, family in _FAMILY_SUFFIXES:
        marker = f"_{suffix}"
        if marker not in stem:
            continue
        ticker, tail = stem.split(marker, maxsplit=1)
        if not ticker:
            return None
        if family != "filing":
            return ticker.upper(), family, None
        if not tail.isdigit() or len(tail) != 4:
            return None
        return ticker.upper(), family, int(tail)
    return None


def _find_companion(corpus_root: Path, primary_path: Path, ticker: str, family: str) -> Path | None:
    if family == "estimate":
        candidates = (
            corpus_root / f"{ticker}_income_statement_annual.json",
            corpus_root / f"{ticker}_income_statement_quarterly.json",
        )
    elif family == "price":
        candidates = (corpus_root / f"{ticker}_profile.json",)
    elif family in {"geo_segment", "product_segment"}:
        period = "annual" if primary_path.stem.endswith("_annual") else "quarterly"
        candidates = (corpus_root / f"{ticker}_income_statement_{period}.json",)
    else:
        return None
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def select_replay_inputs(corpus_root: Path) -> tuple[_ReplayInput, ...]:
    """Return the deterministic supported cache slice and its companions."""
    root = corpus_root.resolve()
    if not root.is_dir():
        raise ValueError(f"corpus root is not a directory: {corpus_root}")
    selected: list[_ReplayInput] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        classified = _ticker_and_family(path)
        if classified is None:
            continue
        ticker, family, fiscal_year = classified
        selected.append(
            _ReplayInput(
                primary_path=path,
                ticker=ticker,
                family=family,
                companion_path=_find_companion(root, path, ticker, family),
                fiscal_year=fiscal_year,
            )
        )
    return tuple(selected)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def corpus_manifest_sha256(corpus_root: Path, inputs: tuple[_ReplayInput, ...]) -> tuple[str, int]:
    """Hash the complete replay input set, including required companion packets."""
    paths = {item.primary_path for item in inputs}
    paths.update(item.companion_path for item in inputs if item.companion_path is not None)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: _safe_relative(corpus_root, item)):
        digest.update(_safe_relative(corpus_root, path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), len(paths)


def validate_report_output_path(
    corpus_root: Path,
    output_path: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Reject report destinations that could mutate the immutable cache."""
    resolved_root = corpus_root.resolve()
    resolved_output = output_path.resolve()
    try:
        resolved_output.relative_to(resolved_root)
    except ValueError:
        pass
    else:
        raise ValueError("replay report output must be outside the corpus root")
    if resolved_output.exists() and not overwrite:
        raise ValueError("replay report output already exists; pass --overwrite to replace it")
    if resolved_output.is_dir():
        raise ValueError("replay report output must be a file")
    return resolved_output


def replay_fmp_adapter_corpus(
    corpus_root: Path,
    *,
    observed_at: datetime,
    expected_manifest_sha256: str | None = None,
) -> FmpAdapterReplayReport:
    """Replay every supported cache file with exact-byte companion bindings.

    ``observed_at`` is deliberately an explicit operator-supplied replay
    timestamp.  It is not represented as the historical fetch time of an FMP
    packet; cache/manifest loaders remain responsible for that source fact.
    """
    inputs = select_replay_inputs(corpus_root)
    if not inputs:
        raise ValueError("corpus contains no supported provider-adapter files")
    manifest_sha256, manifest_files = corpus_manifest_sha256(corpus_root, inputs)
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != manifest_sha256:
        raise ValueError("corpus manifest SHA-256 does not match expected value")

    adapter = FmpProviderAdapter()
    emitted = {"filing": 0, "estimate": 0, "segment": 0, "price": 0}
    failures: list[ReplayFailure] = []
    succeeded = 0
    for item in inputs:
        relative_path = _safe_relative(corpus_root, item.primary_path)
        try:
            primary = item.primary_path.read_bytes()
            if item.family == "filing":
                emitted["filing"] += len(
                    adapter.parse_filing_sections(
                        primary,
                        item.ticker,
                        form="10-K",
                        fiscal_year=item.fiscal_year,
                        fetched_at=observed_at,
                    )
                )
            elif item.family == "estimate":
                if item.companion_path is None:
                    raise ValueError("missing income-statement currency companion")
                emitted["estimate"] += len(
                    adapter.parse_estimates(
                        primary,
                        item.ticker,
                        observed_at=observed_at,
                        currency_packet=item.companion_path.read_bytes(),
                    )
                )
            elif item.family in {"geo_segment", "product_segment"}:
                emitted["segment"] += len(
                    adapter.parse_segments(
                        primary,
                        item.ticker,
                        dim_type=(
                            SegmentDimension.GEOGRAPHY
                            if item.family == "geo_segment"
                            else SegmentDimension.PRODUCT
                        ),
                        currency_packet=(
                            item.companion_path.read_bytes()
                            if item.companion_path is not None
                            else None
                        ),
                    )
                )
            elif item.family == "price":
                if item.companion_path is None:
                    raise ValueError("missing profile currency companion")
                emitted["price"] += len(
                    adapter.parse_prices(
                        primary,
                        item.ticker,
                        adjustment_method=CorporateActionAdjustment.SPLIT_AND_DIVIDEND,
                        currency_packet=item.companion_path.read_bytes(),
                    ).points
                )
            else:  # pragma: no cover - _ReplayInput only originates from the classifier.
                raise AssertionError(f"unsupported replay family {item.family}")
        except (OSError, ValueError) as exc:
            failures.append(
                ReplayFailure(
                    relative_path=relative_path,
                    family=item.family,
                    error_type=type(exc).__name__,
                    message=str(exc)[:500],
                )
            )
        else:
            succeeded += 1
    return FmpAdapterReplayReport(
        corpus_manifest_sha256=manifest_sha256,
        observed_at=observed_at,
        selected_files=len(inputs),
        manifest_files=manifest_files,
        succeeded_files=succeeded,
        failed_files=len(failures),
        emitted_records=emitted,
        failures=tuple(failures),
    )
