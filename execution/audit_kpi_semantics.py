"""Audit source-bound KPI semantics only for report and Facts & Metrics usage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.kpi_semantic_scope import ScopedKpiDefinition, scoped_kpi_definitions  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


class KpiSemanticAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
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
    rows: list[ScopedKpiDefinition]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--gate", action="store_true", help="Exit 2 on missing/quarantined context")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = open_db(args.db)
    try:
        rows = scoped_kpi_definitions(conn, repo_root=PROJECT_ROOT, user_id=args.user_id)
    finally:
        conn.close()
    missing = sum(row.missing_context_count for row in rows)
    quarantined = sum(row.quarantined_context_count for row in rows)
    legacy_unknown = sum(row.legacy_unknown_context_count for row in rows)
    unresolved = sum(row.kpi_definition_id is None for row in rows)
    summary = KpiSemanticAuditSummary(
        scope="portfolio report union facts_metrics",
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
        rows=list(rows),
    )
    sys.stderr.write(
        json.dumps(
            {
                "event": "kpi_semantic_audit_completed",
                "definitions": summary.definitions,
                "facts": summary.facts,
                "gate_blocked": bool(missing or quarantined or legacy_unknown or unresolved),
            },
            sort_keys=True,
        )
        + "\n"
    )
    print(summary.model_dump_json(indent=2))
    return 2 if args.gate and (missing or quarantined or legacy_unknown or unresolved) else 0


if __name__ == "__main__":
    raise SystemExit(main())
