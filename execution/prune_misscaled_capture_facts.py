"""Record exclusions for mis-scaled count-unit capture facts.

The capture extractor formerly applied a section-level monetary scale to rows
whose own label declares a share or unit count.  The affected legacy facts are
retained.  This command writes an immutable exclusion decision for each match;
downstream readers will opt into that projection in a later migration slice.

Usage:
    python execution/prune_misscaled_capture_facts.py            # dry-run
    python execution/prune_misscaled_capture_facts.py --apply    # append decisions
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.fact_selection import (  # noqa: E402
    FactSelectionDecision,
    FactSelectionLedger,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_COUNT_UNIT_PARENS: tuple[str, ...] = ("(in shares)", "(shares)", "(in units)", "(units)")
_POLICY_NAME = "capture_count_unit_scale_guard"
_POLICY_VERSION = "1"
_REASON_CODE = "mis_scaled_count_unit"
_POLICY_CONFIG = {
    "definition_origin": "capture",
    "required_stored_unit": "actual",
    "count_unit_parentheticals": _COUNT_UNIT_PARENS,
}
_POLICY_CONFIG_SHA256 = sha256(
    json.dumps(_POLICY_CONFIG, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

_SELECT = """
    SELECT kf.id AS fact_id, kd.id AS definition_id, kd.ticker AS ticker,
           kd.name AS definition_name, kf.value AS value, kf.unit AS unit
    FROM kpi_facts AS kf
    JOIN kpi_definitions AS kd ON kd.id = kf.kpi_definition_id
    WHERE kd.definition_origin = 'capture'
      AND kf.unit = 'actual'
"""


class _CommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MisScaledCaptureFact(_CommandModel):
    fact_id: int = Field(gt=0)
    definition_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=16)
    definition_name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    unit: str = Field(min_length=1)


class PruneSummary(_CommandModel):
    mode: str
    matched_facts: int = Field(ge=0)
    decisions_appended: int = Field(ge=0)
    already_current: int = Field(ge=0)
    ticker_counts: tuple[tuple[str, int], ...]


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True, default=str), file=sys.stderr)


def _matches_count_unit_parenthetical(definition_name: str) -> bool:
    normalized = definition_name.lower()
    return any(marker in normalized for marker in _COUNT_UNIT_PARENS)


def select_misscaled_capture_facts(conn: sqlite3.Connection) -> list[MisScaledCaptureFact]:
    """Return only source-gated legacy rows that the corrected extractor defers."""
    rows = conn.execute(_SELECT).fetchall()
    matches: list[MisScaledCaptureFact] = []
    for row in rows:
        name = str(row["definition_name"])
        if _matches_count_unit_parenthetical(name):
            matches.append(
                MisScaledCaptureFact(
                    fact_id=int(row["fact_id"]),
                    definition_id=int(row["definition_id"]),
                    ticker=str(row["ticker"]),
                    definition_name=name,
                    value=str(row["value"]),
                    unit=str(row["unit"]),
                )
            )
    return matches


def _ticker_counts(matches: list[MisScaledCaptureFact]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for match in matches:
        counts[match.ticker] = counts.get(match.ticker, 0) + 1
    return tuple(sorted(counts.items()))


def summarize_matches(matches: list[MisScaledCaptureFact], *, applied: bool) -> PruneSummary:
    """Produce a schema-validated dry-run/apply summary without mutating facts."""
    return PruneSummary(
        mode="applied" if applied else "dry_run",
        matched_facts=len(matches),
        decisions_appended=0,
        already_current=0,
        ticker_counts=_ticker_counts(matches),
    )


def _reason_details(match: MisScaledCaptureFact) -> tuple[tuple[str, str], ...]:
    return (
        ("definition_id", str(match.definition_id)),
        ("definition_name", match.definition_name),
        ("stored_unit", match.unit),
        ("stored_value", match.value),
        ("ticker", match.ticker),
    )


def _decision_identity(match: MisScaledCaptureFact, *, revision: int) -> str:
    payload = {
        "decision_kind": "deterministic",
        "policy_config_sha256": _POLICY_CONFIG_SHA256,
        "policy_name": _POLICY_NAME,
        "policy_version": _POLICY_VERSION,
        "reason_code": _REASON_CODE,
        "reason_details": _reason_details(match),
        "selection_state": "excluded",
        "target_row_id": match.fact_id,
        "target_table": "kpi_facts",
        "revision": revision,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _current_is_exact(conn: sqlite3.Connection, match: MisScaledCaptureFact) -> bool:
    row = conn.execute(
        "SELECT selection_state, reason_code, reason_details_json, decision_kind, policy_name, "
        "policy_version, policy_config_sha256, evidence_node_id, validation_issue_id, material_dissent "
        "FROM v_fact_selection_current WHERE target_table = 'kpi_facts' AND target_row_id = ?",
        (match.fact_id,),
    ).fetchone()
    if row is None:
        return False
    return (
        str(row["selection_state"]) == "excluded"
        and str(row["reason_code"]) == _REASON_CODE
        and str(row["reason_details_json"])
        == json.dumps(dict(_reason_details(match)), sort_keys=True, separators=(",", ":"))
        and str(row["decision_kind"]) == "deterministic"
        and str(row["policy_name"]) == _POLICY_NAME
        and str(row["policy_version"]) == _POLICY_VERSION
        and str(row["policy_config_sha256"]) == _POLICY_CONFIG_SHA256
        and row["evidence_node_id"] is None
        and row["validation_issue_id"] is None
        and not bool(row["material_dissent"])
    )


def _next_revision(conn: sqlite3.Connection, match: MisScaledCaptureFact) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT decision_id, revision FROM v_fact_selection_current "
        "WHERE target_table = 'kpi_facts' AND target_row_id = ?",
        (match.fact_id,),
    ).fetchone()
    if row is None:
        return (1, None)
    return (int(row["revision"]) + 1, str(row["decision_id"]))


def append_exclusions(
    conn: sqlite3.Connection, matches: list[MisScaledCaptureFact], *, recorded_at: datetime
) -> PruneSummary:
    """Append one exclusion revision per changed matching fact; never alter facts."""
    ledger = FactSelectionLedger(conn)
    appended = 0
    already_current = 0
    for match in matches:
        if _current_is_exact(conn, match):
            already_current += 1
            continue
        revision, supersedes = _next_revision(conn, match)
        decision_identity = _decision_identity(match, revision=revision)
        result = ledger.persist(
            FactSelectionDecision(
                decision_id=f"fact-selection:kpi_facts:{match.fact_id}:r{revision}:{decision_identity[:16]}",
                idempotency_key=f"fact-selection:{decision_identity}",
                target_table="kpi_facts",
                target_row_id=match.fact_id,
                revision=revision,
                selection_state="excluded",
                reason_code=_REASON_CODE,
                reason_details=_reason_details(match),
                decision_kind="deterministic",
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=_POLICY_CONFIG_SHA256,
                evidence_node_id=None,
                validation_issue_id=None,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_decision_id=supersedes,
                material_dissent=False,
            )
        )
        appended += int(result.created)
        already_current += int(not result.created)
    return PruneSummary(
        mode="applied",
        matched_facts=len(matches),
        decisions_appended=appended,
        already_current=already_current,
        ticker_counts=_ticker_counts(matches),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Append exclusion decisions.")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args(argv)
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    _event("fact_selection_prune_started", mode="applied" if args.apply else "dry_run")
    conn = connect_sqlite(args.db, role=role, schema_preflight=args.apply)
    try:
        matches = select_misscaled_capture_facts(conn)
        if args.apply:
            summary = append_exclusions(conn, matches, recorded_at=datetime.now(UTC))
            conn.commit()
        else:
            summary = summarize_matches(matches, applied=False)
        _event("fact_selection_prune_completed", **summary.model_dump())
        print(summary.model_dump_json())
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
