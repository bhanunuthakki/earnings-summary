"""Canonical reported denominators and separate captured provider valuation context.

File formats and capture lookup live here. Dated consensus requires retained
capture evidence or the existing daily archive contract, never filesystem mtime.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from estimates_archive import ANNUAL_SUFFIX, ticker_archive_dates
from pipeline.cadence_policy import cadence_hours
from provenance.immutable_artifact import read_stable_artifact
from sources.adapters import DatedEstimateObservation, FmpProviderAdapter
from sources.discovery_financials import CanonicalFinancialHistory, read_financial_history
from sources.discovery_market import DiscoveryMarketContext, read_market_context


@dataclass(frozen=True)
class ValuationInputs:
    key_metrics: list[dict[str, object]]
    income: list[dict[str, object]]
    balance: list[dict[str, object]]
    estimates: tuple[DatedEstimateObservation, ...]
    estimate_unavailable_reason: str | None
    thesis: dict[str, JsonValue]
    profile: dict[str, object]
    market: DiscoveryMarketContext
    canonical_financials: CanonicalFinancialHistory
    manifest: dict[str, object]
    fingerprint: str


def _capture(
    conn: sqlite3.Connection | None, ticker: str, digest: str, cutoff: datetime, document_type: str
) -> dict[str, object] | None:
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT v.document_version_id,v.document_type,o.retrieved_at,o.observed_at,v.recorded_at "
            "FROM evidence_document_versions v JOIN evidence_source_observations o ON o.observation_id=v.observation_id "
            "WHERE v.ticker=? AND v.blob_sha256=? AND o.blob_sha256=v.blob_sha256 AND o.source_kind='fmp' AND v.document_type=?",
            (ticker, digest, document_type),
        ).fetchall()
        candidates: list[tuple[datetime, str, str]] = []
        for row in rows:
            stamps = [
                datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
                if datetime.fromisoformat(str(value)).tzinfo is None
                else datetime.fromisoformat(str(value)).astimezone(UTC)
                for value in row[2:]
            ]
            if all(stamp <= cutoff for stamp in stamps):
                candidates.append((stamps[0], str(row[0]), str(row[1])))
        if candidates:
            captured, version, kind = max(candidates)
            return {
                "captured_at": captured.isoformat(),
                "document_version_id": version,
                "document_type": kind,
                "time_authority": "evidence_source_observations",
            }
    except (sqlite3.Error, ValueError):
        pass
    return None


def _rows(body: bytes | None) -> list[dict[str, object]]:
    if body is None:
        return []
    parsed = TypeAdapter(list[dict[str, JsonValue]]).validate_json(body)
    return sorted(
        (dict(row) for row in parsed), key=lambda row: str(row.get("date") or ""), reverse=True
    )


def _distinct_estimates(
    observations: tuple[DatedEstimateObservation, ...],
) -> tuple[tuple[DatedEstimateObservation, ...], bool]:
    """Equal coordinates need identical complete observations, never row-order precedence."""
    selected: dict[tuple[str, str, str, datetime, str], DatedEstimateObservation] = {}
    conflict = False
    for observation in observations:
        coordinate = (
            observation.ticker,
            observation.provider,
            observation.metric.value,
            observation.target_period_end,
            observation.fiscal_period.value,
        )
        prior = selected.get(coordinate)
        if prior is not None and prior != observation:
            conflict = True
        selected[coordinate] = observation
    if conflict:
        # Retain the whole conflicting packet in the manifest for diagnosis.
        return observations, True
    return tuple(selected.values()), False


def read_valuation_inputs(
    root: Path, ticker: str, *, as_of: date, conn: sqlite3.Connection | None
) -> ValuationInputs:
    root = root.resolve()
    ticker = ticker.upper()
    cutoff = datetime.combine(as_of, time.max, tzinfo=UTC)
    sources: dict[str, object] = {}
    captures: dict[str, dict[str, object] | None] = {}
    bodies: dict[str, bytes | None] = {}
    paths = {
        name: root / "data/historical/fmp" / f"{ticker}_{suffix}.json"
        for name, suffix in {
            "key_metrics": "key_metrics_quarterly",
            "income": "income_statement_quarterly",
            "balance": "balance_sheet_quarterly",
            "estimates": "analyst_estimates_annual",
            "profile": "profile",
        }.items()
    }
    paths["thesis"] = root / "micro_thesis/holdings" / f"{ticker}.json"
    for name, path in paths.items():
        relative = path.relative_to(root).as_posix()
        try:
            snapshot, body = read_stable_artifact(path)
            capture = (
                None
                if name == "thesis"
                else _capture(
                    conn,
                    ticker,
                    snapshot.file_sha256,
                    cutoff,
                    {
                        "key_metrics": "fmp_key_metrics",
                        "income": "fmp_income_statement",
                        "balance": "fmp_balance_sheet",
                        "estimates": "fmp_analyst_estimates",
                        "profile": "fmp_profile",
                    }[name],
                )
            )
            bodies[name], captures[name] = body, capture
            sources[name] = {
                "path": relative,
                "sha256": snapshot.file_sha256,
                "bytes": len(body),
                "capture": capture,
                "temporal_scope": "current_owner_context"
                if name == "thesis"
                else "provider_snapshot_capture_unknown"
                if capture is None
                else "captured_provider_snapshot",
            }
        except FileNotFoundError:
            bodies[name], captures[name] = None, None
            sources[name] = {"path": relative, "status": "missing"}
    estimate_body = bodies["estimates"]
    currency_body = bodies["income"]
    currency_capture = captures["income"]
    estimate_capture = captures["estimates"]
    archive_root = root / "data/historical/fmp_snapshots"
    eligible = [
        day for day in ticker_archive_dates(ticker, archive_root) if day <= as_of.isoformat()
    ]
    sources["archive_selection"] = {
        "eligible_dates": eligible,
        "method": "latest_snapshot_date_at_or_before_cutoff",
    }
    # The current captured curve wins; the archive is only a dated alternative
    # when the current file has no admitted capture. No interpolation/backfill.
    if estimate_capture is None and eligible:
        day = eligible[-1]
        archive = archive_root / day
        archive_sources: dict[str, object] = {}
        try:
            est_snapshot, estimate_body = read_stable_artifact(archive / f"{ticker}{ANNUAL_SUFFIX}")
            currency_snapshot, currency_body = read_stable_artifact(
                archive / f"{ticker}_income_statement_quarterly.json"
            )
            estimate_capture = {
                "captured_at": f"{day}T00:00:00+00:00",
                "time_authority": "estimates_archive_daily_snapshot_date",
                "precision": "day",
            }
            currency_capture = {
                **estimate_capture,
                "source_payload_sha256": currency_snapshot.file_sha256,
            }
            for name, snapshot in (("estimates", est_snapshot), ("currency", currency_snapshot)):
                archive_sources[name] = {
                    "path": snapshot.path.relative_to(root).as_posix(),
                    "sha256": snapshot.file_sha256,
                }
        except FileNotFoundError:
            estimate_body = None
            archive_sources["status"] = "dated_estimate_or_contemporaneous_currency_packet_missing"
        sources["selected_archive"] = archive_sources
    estimates: tuple[DatedEstimateObservation, ...] = ()
    reason = "estimate_capture_unavailable"
    fresh_limit: float | None = None
    currency_fresh_limit: float | None = None
    if conn is not None:
        try:
            tiers = conn.execute(
                "SELECT DISTINCT list_type FROM tracked_companies WHERE ticker=? AND archived_at IS NULL",
                (ticker,),
            ).fetchall()
            limits = [cadence_hours(str(row[0]), "time_sensitive") for row in tiers]
            fresh_limit = min(limits) if limits else None
            currency_limits = [cadence_hours(str(row[0]), "statement") for row in tiers]
            currency_fresh_limit = min(currency_limits) if currency_limits else None
        except sqlite3.Error:
            pass
    if estimate_capture is not None and currency_capture is None:
        reason = "estimate_currency_source_capture_unavailable"
    if estimate_capture is not None and estimate_body is not None and currency_capture is not None:
        captured = datetime.fromisoformat(str(estimate_capture["captured_at"]))
        try:
            estimates = tuple(
                FmpProviderAdapter().parse_estimates(
                    estimate_body, ticker, observed_at=captured, currency_packet=currency_body
                )
            )
            estimates, conflicting = _distinct_estimates(estimates)
            currency_captured = datetime.fromisoformat(str(currency_capture["captured_at"]))
            if conflicting:
                reason = "conflicting_estimate_target_observations"
            elif fresh_limit is None:
                reason = "estimate_freshness_policy_unavailable"
            elif (cutoff - captured).total_seconds() / 3600 >= fresh_limit:
                reason = "estimate_snapshot_stale"
            elif currency_fresh_limit is None:
                reason = "estimate_currency_freshness_policy_unavailable"
            elif (cutoff - currency_captured).total_seconds() / 3600 >= currency_fresh_limit:
                reason = "estimate_currency_source_snapshot_stale"
            else:
                reason = ""
        except ValueError:
            reason = "estimate_currency_or_payload_invalid"
    market = (
        read_market_context(conn, paths["profile"].parent, ticker, as_of=as_of)
        if conn is not None
        else DiscoveryMarketContext(
            ticker=ticker, status="unavailable", reason_codes=("capture_database_unavailable",)
        )
    )
    profile_receipt = sources["profile"]
    if (
        market.source_payload_sha256 is not None
        and isinstance(profile_receipt, dict)
        and market.source_payload_sha256 != profile_receipt.get("sha256")
    ):
        raise ValueError("valuation profile changed between snapshot and capture admission")
    thesis = (
        TypeAdapter(dict[str, JsonValue]).validate_json(bodies["thesis"])
        if bodies["thesis"] is not None
        else {}
    )
    profiles = _rows(bodies["profile"])
    canonical_financials = (
        read_financial_history(conn, ticker, as_of=as_of, concepts=("net_income", "free_cash_flow"))
        if conn is not None
        else CanonicalFinancialHistory(
            ticker=ticker, as_of=as_of, reason_codes=("canonical_financial_database_unavailable",)
        )
    )
    manifest: dict[str, object] = {
        "schema_version": "valuation-inputs/v5",
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "sources": sources,
        "estimate_capture": estimate_capture,
        "estimate_currency_capture": currency_capture,
        "estimate_unavailable_reason": reason or None,
        "estimate_freshness_limit_hours": fresh_limit,
        "estimate_freshness_policy": "pipeline.cadence_policy/time_sensitive",
        "estimate_currency_freshness_limit_hours": currency_fresh_limit,
        "estimate_currency_freshness_policy": "pipeline.cadence_policy/statement",
        "dated_estimates": [item.model_dump(mode="json") for item in estimates],
        "market": market.model_dump(mode="json"),
        "canonical_financials": canonical_financials.model_dump(mode="json"),
        "financial_authority": "canonical_reported_supported_denominators_with_unmigrated_provider_context",
        "acquisition_completeness": "unverified",
        "decision_grade": False,
    }
    fingerprint = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ValuationInputs(
        _rows(bodies["key_metrics"]),
        _rows(bodies["income"]),
        _rows(bodies["balance"]),
        estimates,
        reason or None,
        thesis,
        profiles[0] if profiles else {},
        market,
        canonical_financials,
        manifest,
        fingerprint,
    )
