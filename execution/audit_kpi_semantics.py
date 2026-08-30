"""Audit source-bound KPI semantics only for report and Facts & Metrics usage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.kpi_report_reference_dispositions import (  # noqa: E402
    ReportKpiReferenceSourceStatus,
    ReportKpiReferenceStatus,
)
from pipeline.kpi_semantic_scope import ScopedKpiDefinition, scoped_kpi_definitions  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class KpiSemanticAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    user_id: str
    empty_scope: bool
    gate_blocked: bool
    disposition_gate_blocked: bool
    decision_grade_admission_blocked: bool
    definitions: int
    facts: int
    admitted_contexts: int
    current_actual_contexts: int
    comparator_contexts: int
    guidance_target_contexts: int
    management_explanation_contexts: int
    analyst_question_contexts: int
    missing_contexts: int
    quarantined_contexts: int
    legacy_unknown_contexts: int
    unresolved_report_metrics: int
    undisposed_report_references: int
    disposed_unresolved_report_references: int
    invalid_or_missing_report_configurations: int
    rows: list[ScopedKpiDefinition]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--user-id",
        required=True,
        help="Explicit owner identity; the audit never guesses or defaults portfolio scope",
    )
    parser.add_argument("--gate", action="store_true", help="Exit 2 on missing/quarantined context")
    parser.add_argument(
        "--disposition-gate",
        action="store_true",
        help="Exit 3 while any scoped fact/reference lacks an explicit current disposition",
    )
    return parser


def summarize_kpi_semantic_audit(
    rows: tuple[ScopedKpiDefinition, ...], *, user_id: str
) -> KpiSemanticAuditSummary:
    """Compute distinct rollout-disposition and decision-grade admission truths."""
    missing = sum(row.missing_context_count for row in rows)
    quarantined = sum(row.quarantined_context_count for row in rows)
    legacy_unknown = sum(row.legacy_unknown_context_count for row in rows)
    undisposed = sum(
        row.kpi_definition_id is None
        and row.report_reference_source_status is ReportKpiReferenceSourceStatus.VALID
        and row.report_reference_status is None
        for row in rows
    )
    disposed_unresolved = sum(
        row.report_reference_status is ReportKpiReferenceStatus.UNRESOLVED for row in rows
    )
    invalid_sources = sum(
        row.report_reference_source_status is not None
        and row.report_reference_source_status.value != "valid"
        for row in rows
    )
    unresolved = undisposed + disposed_unresolved
    empty_scope = not rows
    disposition_gate_blocked = bool(
        empty_scope or missing or legacy_unknown or undisposed or invalid_sources
    )
    decision_grade_admission_blocked = bool(
        empty_scope or missing or quarantined or legacy_unknown or unresolved or invalid_sources
    )
    gate_blocked = decision_grade_admission_blocked
    return KpiSemanticAuditSummary(
        scope="portfolio report union facts_metrics",
        user_id=user_id,
        empty_scope=empty_scope,
        gate_blocked=gate_blocked,
        disposition_gate_blocked=disposition_gate_blocked,
        decision_grade_admission_blocked=decision_grade_admission_blocked,
        definitions=len(rows),
        facts=sum(row.fact_count for row in rows),
        admitted_contexts=sum(row.admitted_context_count for row in rows),
        current_actual_contexts=sum(row.current_actual_count for row in rows),
        comparator_contexts=sum(row.comparator_count for row in rows),
        guidance_target_contexts=sum(row.guidance_target_count for row in rows),
        management_explanation_contexts=sum(row.management_explanation_count for row in rows),
        analyst_question_contexts=sum(row.analyst_question_count for row in rows),
        missing_contexts=missing,
        quarantined_contexts=quarantined,
        legacy_unknown_contexts=legacy_unknown,
        unresolved_report_metrics=unresolved,
        undisposed_report_references=undisposed,
        disposed_unresolved_report_references=disposed_unresolved,
        invalid_or_missing_report_configurations=invalid_sources,
        rows=list(rows),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = scoped_kpi_definitions(conn, repo_root=PROJECT_ROOT, user_id=args.user_id)
    finally:
        conn.close()
    summary = summarize_kpi_semantic_audit(rows, user_id=args.user_id)
    sys.stderr.write(
        json.dumps(
            {
                "event": "kpi_semantic_audit_completed",
                "user_id": summary.user_id,
                "definitions": summary.definitions,
                "facts": summary.facts,
                "empty_scope": summary.empty_scope,
                "gate_blocked": summary.gate_blocked,
                "disposition_gate_blocked": summary.disposition_gate_blocked,
                "decision_grade_admission_blocked": summary.decision_grade_admission_blocked,
            },
            sort_keys=True,
        )
        + "\n"
    )
    print(summary.model_dump_json(indent=2))
    if args.gate and summary.gate_blocked:
        return 2
    if args.disposition_gate and summary.disposition_gate_blocked:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
