"""Typed, durable lineage for a DCF calculation input set."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from dcf.grade_evidence import MAX_VERIFICATION_EVIDENCE_BYTES

EquityDirectValuationArchetype = Literal[
    "bank_excess_return",
    "holdco_sotp",
    "fintech_sotp",
    "platform_sotp",
    "platform_fcfe",
    "operating_cashflow_equity",
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


def selected_statement_provenance(
    overlay: Mapping[str, object],
    *,
    selected_observation_ids: frozenset[str],
    historical_overlay_locator: str,
) -> dict[str, object]:
    """Keep exact selected lineage and commit the separately retained history."""
    if not selected_observation_ids or not historical_overlay_locator.strip():
        raise ValueError("selected_provenance_population_missing")
    raw_statements = overlay.get("statements")
    if not isinstance(raw_statements, Mapping):
        raise ValueError("selected_provenance_statements_missing")
    found: set[str] = set()
    statements: dict[str, object] = {}
    for name, raw in cast("Mapping[str, object]", raw_statements).items():
        if not isinstance(raw, Mapping):
            raise ValueError("selected_provenance_statement_invalid")
        statement = cast("Mapping[str, object]", raw)
        summary = {
            key: value
            for key, value in statement.items()
            if key not in {"applied", "conflicts", "rejected"}
        }
        for key in ("applied", "conflicts", "rejected"):
            entries = statement.get(key, [])
            if not isinstance(entries, list):
                raise ValueError("selected_provenance_entries_invalid")
            population = cast("list[object]", entries)
            summary[f"historical_{key}_count"] = len(population)
            if key == "applied":
                selected: list[dict[str, object]] = []
                for entry in population:
                    if not isinstance(entry, dict):
                        raise ValueError("selected_provenance_entry_invalid")
                    item = cast("dict[str, object]", entry)
                    observation_id = item.get("reported_observation_id")
                    if (
                        isinstance(observation_id, str)
                        and observation_id in selected_observation_ids
                    ):
                        selected.append(dict(item))
                        found.add(observation_id)
                summary["applied"] = selected
        statements[name] = summary
    if found != set(selected_observation_ids):
        raise ValueError("selected_observation_missing")
    if overlay.get("scope") == "selected_model_inputs":
        digest = overlay.get("historical_overlay_sha256")
        if (
            overlay.get("schema_version") != "dcf_selected_statement_provenance.v1"
            or overlay.get("selected_observation_ids") != sorted(selected_observation_ids)
            or overlay.get("historical_overlay_locator") != historical_overlay_locator
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("selected_provenance_population_mismatch")
        return dict(overlay)
    encoded = json.dumps(
        dict(overlay), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return {
        **{key: value for key, value in overlay.items() if key != "statements"},
        "schema_version": "dcf_selected_statement_provenance.v1",
        "scope": "selected_model_inputs",
        "historical_overlay_locator": historical_overlay_locator,
        "historical_overlay_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "selected_observation_ids": sorted(selected_observation_ids),
        "statements": statements,
    }


def validate_dcf_evidence_population(
    *,
    assumption_snapshot_json: str | None,
    provenance: DcfInputProvenance,
    metadata: Sequence[str] = (),
) -> None:
    """Reject an unverifiable new population without changing historical records."""
    repeated_checks: list[str] = []
    for block, field in (
        ("primary_fact_overlay", "status"),
        ("equity_bridge_receipt", "status"),
        ("country_risk_context", "authority"),
    ):
        raw = (provenance.detail or {}).get(block)
        if isinstance(raw, dict):
            value = cast("dict[str, object]", raw).get(field)
            if isinstance(value, str):
                repeated_checks.append(value)
    serialized = (
        assumption_snapshot_json or "",
        provenance.as_json(),
        json.dumps((*metadata, *repeated_checks), ensure_ascii=False, allow_nan=False),
    )
    # Reserve the typed read model's keys, checks and numeric fields. The source
    # population must fit without display truncation or a larger verifier limit.
    population_budget = MAX_VERIFICATION_EVIDENCE_BYTES - 16_384
    if sum(len(value.encode("utf-8")) for value in serialized) > population_budget:
        raise ValueError("verification_evidence_byte_limit")


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
            "clock_kind": "file_modified",
            "influences_calculation": influences_calculation,
        },
        observed_at,
    )


def build_file_source_record(path: Path, *, role: str, repo_root: Path) -> dict[str, object] | None:
    """Capture an immutable receipt before a mutable file input is overwritten."""
    captured = _source_record(path, role=role, repo_root=repo_root)
    return None if captured is None else captured[0]


def input_clock_summary(
    sources: Sequence[Mapping[str, object]], *, market_observed_at: datetime | None
) -> dict[str, object]:
    """Describe clock roles without promoting file metadata to issuer evidence."""
    observed: list[datetime] = []
    for source in sources:
        if (
            source.get("influences_calculation") is False
            or source.get("role") == "calculation_workbook"
        ):
            continue
        raw = source.get("observed_at")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            observed.append(parsed.astimezone(UTC))
    return {
        "schema_version": "dcf_input_clocks.v1",
        "legacy_inputs_as_of_semantics": "latest_input_observation_cutoff_not_financial_freshness",
        "market_observed_at": market_observed_at.isoformat() if market_observed_at else None,
        "non_market_latest_observed_at": max(observed).isoformat() if observed else None,
        "non_market_clock_semantics": "source_metadata_not_issuer_reporting_period",
        "financial_period_end": None,
        "financial_input_completeness": "unverified",
        "semantic_admission": "unverified",
        "assumption_reviewed_at": None,
    }


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
    model_input_receipt: Mapping[str, object] | None = None,
    equity_bridge_receipt: Mapping[str, object] | None = None,
    scenario_acceptance: Mapping[str, object] | None = None,
) -> DcfInputProvenance:
    """Build durable file-based lineage without treating the mutable DB as input.

    Specialized models have different source sets than redesigned FCFF.  They
    still need the same reproducibility contract: hash every actual file, carry
    the effective typed assumptions in the input hash, and use the latest file
    or market observation as a timezone-aware input cutoff.
    """
    if equity_bridge_receipt is not None and equity_direct_archetype is not None:
        raise ValueError("equity_bridge_authority_conflict")
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
    equity_bridge_receipt = (
        dict(equity_bridge_receipt) if equity_bridge_receipt is not None else None
    )
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
    if model_input_receipt is not None:
        canonical["model_input_receipt"] = dict(model_input_receipt)
    if scenario_acceptance is not None:
        canonical["scenario_acceptance"] = dict(scenario_acceptance)
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
            "input_clocks": input_clock_summary(sources, market_observed_at=normalized_live_at),
            "sources": sources,
            **(
                {"model_input_receipt": dict(model_input_receipt)}
                if model_input_receipt is not None
                else {}
            ),
            "market_price": market_price,
            "inputs_as_of_status": "observed" if observed_times else "unavailable",
            **(
                {"equity_bridge_receipt": equity_bridge_receipt}
                if equity_bridge_receipt is not None
                else {}
            ),
            **(
                {"scenario_acceptance": dict(scenario_acceptance)}
                if scenario_acceptance is not None
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
