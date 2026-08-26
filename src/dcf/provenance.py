"""Typed, durable lineage for a DCF calculation input set."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

EquityDirectValuationArchetype = Literal[
    "bank_excess_return",
    "holdco_sotp",
    "fintech_sotp",
    "platform_sotp",
    "platform_fcfe",
]


@dataclass(frozen=True)
class DcfInputProvenance:
    """Hashes and clock needed to reproduce or audit a DCF result."""

    input_sha256: str
    workbook_sha256: str | None
    engine_version: str
    inputs_as_of: datetime
    detail: dict[str, object] | None = None

    def as_json(self) -> str:
        return json.dumps(self.detail or {}, sort_keys=True, separators=(",", ":"))

    def inputs_as_of_iso(self) -> str:
        return self.inputs_as_of.isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_record(
    path: Path,
    *,
    role: str,
    repo_root: Path,
    influences_calculation: bool = True,
) -> tuple[dict[str, object], datetime] | None:
    """A hashed source record for a file actually used by a specialized builder."""
    if not path.is_file():
        return None
    stat = path.stat()
    observed_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    try:
        locator = str(path.relative_to(repo_root))
    except ValueError:
        locator = str(path)
    return (
        {
            "role": role,
            "path": locator.replace("\\", "/"),
            "sha256": _sha256_file(path),
            "bytes": stat.st_size,
            "observed_at": observed_at.isoformat(),
            "influences_calculation": influences_calculation,
        },
        observed_at,
    )


def build_file_source_record(path: Path, *, role: str, repo_root: Path) -> dict[str, object] | None:
    """Capture an immutable receipt before a mutable file input is overwritten."""
    captured = _source_record(path, role=role, repo_root=repo_root)
    return None if captured is None else captured[0]


def build_file_provenance(
    *,
    ticker: str,
    repo_root: Path,
    workbook_path: Path,
    engine_version: str,
    effective_inputs: Mapping[str, object],
    assumption_snapshot: Mapping[str, object],
    live_price: float | None,
    live_price_at: datetime | None,
    live_price_source: str | None,
    source_files: Sequence[tuple[Path, str]],
    source_records: Sequence[Mapping[str, object]] = (),
    workbook_locator_path: Path | None = None,
    equity_direct_archetype: EquityDirectValuationArchetype | None = None,
) -> DcfInputProvenance:
    """Build durable file-based lineage without treating the mutable DB as input.

    Specialized models have different source sets than redesigned FCFF.  They
    still need the same reproducibility contract: hash every actual file, carry
    the effective typed assumptions in the input hash, and use the latest file
    or market observation as a timezone-aware input cutoff.
    """
    sources: list[dict[str, object]] = []
    observed_times: list[datetime] = []
    for path, role in source_files:
        record = _source_record(path, role=role, repo_root=repo_root)
        if record is not None:
            detail, observed_at = record
            sources.append(detail)
            observed_times.append(observed_at)
    for source_record in source_records:
        detail = dict(source_record)
        detail.setdefault("influences_calculation", True)
        sources.append(detail)
        if detail["influences_calculation"] is False:
            continue
        raw_observed_at = detail.get("observed_at")
        if not isinstance(raw_observed_at, str) or not raw_observed_at:
            continue
        try:
            parsed = datetime.fromisoformat(raw_observed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        observed_times.append(
            parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        )

    input_sources = [
        source for source in sources if source.get("influences_calculation") is not False
    ]

    workbook_record = _source_record(
        workbook_path,
        role="calculation_workbook",
        repo_root=repo_root,
        influences_calculation=False,
    )
    workbook_sha256: str | None = None
    if workbook_record is not None:
        detail, _generated_at = workbook_record
        if workbook_locator_path is not None:
            try:
                locator = str(workbook_locator_path.relative_to(repo_root))
            except ValueError:
                locator = str(workbook_locator_path)
            detail["path"] = locator.replace("\\", "/")
        sources.append(detail)
        workbook_sha256 = str(detail["sha256"])

    normalized_live_at: datetime | None = None
    if live_price_at is not None:
        normalized_live_at = (
            live_price_at.replace(tzinfo=UTC)
            if live_price_at.tzinfo is None
            else live_price_at.astimezone(UTC)
        )
        observed_times.append(normalized_live_at)
    market_price = {
        "price": live_price,
        "observed_at": normalized_live_at.isoformat() if normalized_live_at else None,
        "source": live_price_source,
    }
    equity_bridge_receipt: dict[str, object] | None = None
    if equity_direct_archetype is not None:
        equity_bridge_receipt = {
            "schema_version": "dcf_equity_bridge_receipt.v3",
            "status": "not_applicable",
            "reason_code": "equity_direct_valuation",
            "valuation_scope": "equity",
            "valuation_archetype": equity_direct_archetype,
        }
    canonical = {
        "engine_version": engine_version,
        "ticker": ticker.upper(),
        "effective_inputs": dict(effective_inputs),
        "assumption_snapshot": dict(assumption_snapshot),
        "market_price": market_price,
        "sources": input_sources,
        "workbook_sha256": workbook_sha256,
        "equity_bridge_receipt": equity_bridge_receipt,
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return DcfInputProvenance(
        input_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        workbook_sha256=workbook_sha256,
        engine_version=engine_version,
        inputs_as_of=max(observed_times, default=datetime(1970, 1, 1, tzinfo=UTC)),
        detail={
            "ticker": ticker.upper(),
            "sources": sources,
            "market_price": market_price,
            "inputs_as_of_status": "observed" if observed_times else "unavailable",
            **(
                {"equity_bridge_receipt": equity_bridge_receipt}
                if equity_bridge_receipt is not None
                else {}
            ),
        },
    )


def schema_supports_provenance(conn: sqlite3.Connection) -> bool:
    """Whether this database can durably store the provenance envelope."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(dcf_runs)")}
    return {
        "input_sha256",
        "workbook_sha256",
        "engine_version",
        "inputs_as_of",
        "provenance_json",
    }.issubset(columns)
