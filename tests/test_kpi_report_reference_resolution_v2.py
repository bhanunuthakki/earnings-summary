from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest
from alembic.config import Config

from alembic import command
from compute.thesis_evaluator import evaluate_ticker_thesis
from identity import DEFAULT_USER_ID
from models.kpis import BreachStatus
from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReference,
    ReportKpiReferenceDisposition,
    ReportKpiReferenceResolutionMethod,
    ReportKpiReferenceStatus,
    persist_report_kpi_reference_disposition,
    report_kpi_references,
)
from pipeline.kpi_report_reference_resolver import (
    ReportKpiReferenceProposalOutcome,
    propose_report_kpi_reference_resolution,
    resolve_report_kpi_reference_binding,
    verified_report_kpi_reference_definition,
)
from pipeline.kpi_semantic_dispositions import (
    KpiSemanticDispositionManifest,
    ReportKpiReferenceDispositionEntry,
    apply_kpi_semantic_disposition_manifest,
)
from pipeline.kpi_semantic_scope import scoped_kpi_definitions
from report.models import (
    AnnualKpiSeries,
    BreakRuleEvaluation,
    KpiLedgerRow,
    KpiSeries,
    QuarterlyLineItem,
)
from report.sections import financials as financials_module
from report.sections import thesis as thesis_module
from timeseries import signal_writer


class ResolvePriorities(Protocol):
    def __call__(
        self,
        requested: list[str],
        line_items: list[QuarterlyLineItem],
        ticker: str,
        repo_root: Path,
        quarter_labels: list[str],
        quarter_labels_full: list[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[list[str], list[KpiSeries], list[AnnualKpiSeries], list[int]]: ...


class BuildLedger(Protocol):
    def __call__(
        self,
        ticker: str,
        repo_root: Path,
        holdings: dict[str, object],
        evaluations: list[BreakRuleEvaluation],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[KpiLedgerRow]: ...


class MetricSpec(Protocol):
    @property
    def metric_name(self) -> str: ...

    @property
    def metric_kind(self) -> str: ...


class CollectMetricSpecs(Protocol):
    def __call__(
        self, ticker: str, repo_root: Path, *, conn: sqlite3.Connection | None = None
    ) -> list[MetricSpec]: ...


# Keep the same private test seams with explicit callable types.
_priorities_seam: object = getattr(financials_module, "_resolve_priorities")
assert callable(_priorities_seam)
_resolve_priorities = cast(ResolvePriorities, _priorities_seam)
_ledger_seam: object = getattr(thesis_module, "_build_ledger")
assert callable(_ledger_seam)
_build_ledger = cast(BuildLedger, _ledger_seam)
_metric_specs_seam: object = getattr(signal_writer, "_collect_metric_specs")
assert callable(_metric_specs_seam)
_collect_metric_specs = cast(CollectMetricSpecs, _metric_specs_seam)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _repo(tmp_path: Path, label: str = "Total customers") -> Path:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "NU.json").write_text(
        '{"ticker":"NU","tier_1_kpis":[{"name":"' + label + '"}]}',
        encoding="utf-8",
    )
    return tmp_path


def _repo_payload(tmp_path: Path, payload: str) -> Path:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "NU.json").write_text(payload, encoding="utf-8")
    return tmp_path


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_definitions(
          id INTEGER PRIMARY KEY,ticker TEXT,name TEXT,unit TEXT,
          primary_source TEXT,fallback_source TEXT,reporting_cadence TEXT,
          definition_origin TEXT
        );
        CREATE TABLE documents(
          id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,doc_type TEXT,
          period_end TEXT,sha256 TEXT
        );
        CREATE TABLE kpi_facts(
          id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
          kpi_definition_id INTEGER,value TEXT,unit TEXT,currency TEXT,source_doc_id INTEGER,
          locator TEXT,source_excerpt TEXT,extracted_by TEXT,confidence REAL,
          supersedes_id INTEGER
        );
        CREATE VIEW v_kpi_facts_resolved_current AS
          SELECT fact.* FROM kpi_facts fact WHERE NOT EXISTS (
            SELECT 1 FROM kpi_facts successor WHERE successor.supersedes_id=fact.id
          );
        CREATE TABLE kpi_fact_semantic_contexts(
          id INTEGER PRIMARY KEY,kpi_fact_id INTEGER,revision INTEGER,
          supersedes_context_id INTEGER,metric_name_as_reported TEXT,
          reported_period_end TEXT,period_role TEXT,publication_lane TEXT,
          accounting_basis TEXT,consolidation_scope TEXT,dimensions_json TEXT,
          unit_scale TEXT,source_row_label TEXT,source_column_header TEXT,
          source_value_text TEXT,status TEXT,reviewed_by TEXT,knowledge_at TEXT
        );
        CREATE TABLE fact_overrides(
          ticker TEXT,fact_kind TEXT,fact_key TEXT,action TEXT,status TEXT
        );
        CREATE TABLE report_kpi_reference_resolution_revisions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,ticker TEXT,source_path TEXT,
          json_pointer TEXT,reference_kind TEXT,requested_label TEXT,
          reference_content_sha256 TEXT,status TEXT,kpi_definition_id INTEGER,
          definition_identity_sha256 TEXT,evidence_fact_id INTEGER,evidence_context_id INTEGER,
          evidence_sha256 TEXT,resolution_method TEXT,policy_name TEXT,policy_version TEXT,
          policy_config_sha256 TEXT,reason_code TEXT,revision INTEGER,
          supersedes_resolution_id INTEGER UNIQUE,reviewed_by TEXT,knowledge_at TEXT,
          UNIQUE(user_id,ticker,source_path,json_pointer,revision)
        );
        """
    )
    _seed_definition(conn, 1)
    return conn


def _seed_definition(conn: sqlite3.Connection, definition_id: int) -> None:
    conn.execute(
        "INSERT INTO kpi_definitions VALUES (?,?,?,?,?,?,?,?)",
        (
            definition_id,
            "NU",
            "Total customers (millions)",
            "count",
            "ir_doc",
            None,
            "quarterly",
            "capture",
        ),
    )
    document_id = 10 + definition_id
    fact_id = 20 + definition_id
    context_id = 30 + definition_id
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?)",
        (document_id, "NU", "ir_doc", "earnings_release", "2024-12-31", "a" * 64),
    )
    conn.execute(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id,
            "NU",
            "2024-12-31",
            "Q4",
            definition_id,
            "114200000",
            "count",
            None,
            document_id,
            '{"page":7}',
            "Total customers reached 114.2 million",
            "source_review:owner",
            1.0,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            context_id,
            fact_id,
            1,
            None,
            "Total customers",
            "2024-12-31",
            "current",
            "current_actual",
            "management",
            "consolidated",
            "{}",
            "millions",
            "Total customers",
            "Q4 2024",
            "114.2",
            "admitted",
            "owner",
            "2026-08-30T02:07:33Z",
        ),
    )
    conn.commit()


def _accepted_binding(
    conn: sqlite3.Connection, repo: Path, *, user_id: str = "owner"
) -> tuple[ReportKpiReference, ReportKpiReferenceDisposition]:
    reference = report_kpi_references(repo, ("NU",))[0]
    return _accepted_binding_for_reference(conn, repo, reference=reference, user_id=user_id)


def _accepted_binding_for_reference(
    conn: sqlite3.Connection,
    repo: Path,
    *,
    reference: ReportKpiReference,
    user_id: str,
) -> tuple[ReportKpiReference, ReportKpiReferenceDisposition]:
    proposal = propose_report_kpi_reference_resolution(conn, repo_root=repo, reference=reference)
    assert proposal.outcome is ReportKpiReferenceProposalOutcome.CANDIDATE
    assert proposal.proposed_disposition is not None
    persist_report_kpi_reference_disposition(
        conn,
        user_id=user_id,
        reference=reference,
        disposition=proposal.proposed_disposition,
        reviewed_by="source-review:owner",
        knowledge_at=NOW,
    )
    conn.commit()
    return reference, proposal.proposed_disposition


def _apply_ready(conn: sqlite3.Connection, *, user_id: str = "owner") -> None:
    conn.executescript(
        """
        CREATE TABLE alembic_version(version_num TEXT);
        INSERT INTO alembic_version VALUES ('0036_add_data_coverage_dispositions');
        CREATE TABLE database_runtime_identity(singleton INTEGER,database_instance_id TEXT);
        INSERT INTO database_runtime_identity VALUES (1,'test-db');
        CREATE TABLE tracked_companies(
          ticker TEXT,list_type TEXT,user_id TEXT,archived_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('NU','portfolio',?,NULL)",
        (user_id,),
    )
    conn.commit()


def _manifest(
    reference: ReportKpiReference,
    disposition: ReportKpiReferenceDisposition,
) -> KpiSemanticDispositionManifest:
    return KpiSemanticDispositionManifest(
        user_id="owner",
        logical_idempotency_key="test:report-reference-resolution",
        reviewer="source-review:owner",
        knowledge_at=NOW,
        expected_schema_revision="0036_add_data_coverage_dispositions",
        expected_database_instance_sha256=hashlib.sha256(b"test-db").hexdigest(),
        review_bundle_sha256="b" * 64,
        backup_restore_evidence_id="c" * 64,
        fact_dispositions=(),
        report_reference_dispositions=(
            ReportKpiReferenceDispositionEntry(
                reference=reference,
                expected_resolution_head_id=None,
                expected_resolution_revision=0,
                disposition=disposition,
            ),
        ),
    )


def test_v2_disposition_shape_requires_complete_binding() -> None:
    with pytest.raises(ValueError, match="complete evidence"):
        ReportKpiReferenceDisposition(
            status=ReportKpiReferenceStatus.RESOLVED,
            kpi_definition_id=1,
            reason_code="incomplete",
        )
    with pytest.raises(ValueError, match="cannot carry"):
        ReportKpiReferenceDisposition(
            status=ReportKpiReferenceStatus.RETIRED,
            kpi_definition_id=1,
            reason_code="not_recurring_reported_kpi",
        )
    retired = ReportKpiReferenceDisposition(
        status=ReportKpiReferenceStatus.RETIRED,
        reason_code="not_recurring_reported_kpi",
    )
    assert retired.kpi_definition_id is None


def test_inventory_excludes_financial_chart_items_and_tracks_rule_kpi_leaves(
    tmp_path: Path,
) -> None:
    repo = _repo_payload(
        tmp_path,
        """{
          "ticker":"NU",
          "chart_priorities":["Revenue","Total customers"],
          "business_model_rules":[{"kpi_name":"Total customers"}],
          "break_rules_soft":[{
            "name":"customer_warning",
            "predicate":{"type":"compound","params":{"op":"and","predicates":[
              {"type":"series_below","params":{"metric":"Total customers","source":"kpi"}},
              {"type":"series_below","params":{"metric":"revenue","source":"financial"}}
            ]}}
          }]
        }""",
    )

    references = report_kpi_references(repo, ("NU",))

    assert [
        (reference.reference_kind.value, reference.json_pointer) for reference in references
    ] == [
        ("chart_priority", "/chart_priorities/1"),
        ("business_model_rule", "/business_model_rules/0/kpi_name"),
        (
            "soft_rule_kpi",
            "/break_rules_soft/0/predicate/params/predicates/0/params/metric",
        ),
    ]
    assert all(reference.requested_label == "Total customers" for reference in references)


def test_financial_chart_priority_is_consumed_without_kpi_backlog(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo_payload(tmp_path, '{"ticker":"NU","chart_priorities":["Revenue"]}')
    line_item = financials_module.QuarterlyLineItem(
        line_item="Revenue",
        unit="USD millions",
        quarters=[],
        values=[],
        levels_full=[],
    )

    resolved, quarterly, annual, years = _resolve_priorities(
        ["Revenue"], [line_item], "NU", repo, [], [], conn=conn
    )

    assert report_kpi_references(repo, ("NU",)) == ()
    assert resolved == ["Revenue"]
    assert quarterly == []
    assert annual == []
    assert years == []


def test_chart_alias_classification_matches_inventory_report_and_signal_writer(
    tmp_path: Path,
) -> None:
    conn = _db()
    repo = _repo_payload(
        tmp_path,
        '{"ticker":"NU","chart_priorities":["Capital expenditure","Operating margin"]}',
    )
    capex = financials_module.QuarterlyLineItem(
        line_item="Capex",
        unit="USD millions",
        quarters=[],
        values=[],
        levels_full=[],
    )

    resolved, _, _, _ = _resolve_priorities(
        ["Capital expenditure", "Operating margin"],
        [capex],
        "NU",
        repo,
        [],
        [],
        conn=conn,
    )
    references = report_kpi_references(repo, ("NU",))
    specs = _collect_metric_specs("NU", repo, conn=conn)

    assert resolved == ["Capex"]
    assert [(reference.json_pointer, reference.requested_label) for reference in references] == [
        ("/chart_priorities/1", "Operating margin")
    ]
    assert any(
        spec.metric_kind == "financial" and spec.metric_name == "capital_expenditure"
        for spec in specs
    )
    assert not any(spec.metric_kind == "kpi" for spec in specs)


def test_candidate_proposal_is_deterministic_and_never_persists(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    reference = report_kpi_references(repo, ("NU",))[0]

    proposal = propose_report_kpi_reference_resolution(conn, repo_root=repo, reference=reference)

    assert proposal.outcome is ReportKpiReferenceProposalOutcome.CANDIDATE
    assert proposal.candidate_definition_ids == (1,)
    assert proposal.proposed_disposition is not None
    assert (
        proposal.proposed_disposition.resolution_method
        is ReportKpiReferenceResolutionMethod.UNIT_SURFACE_ALIAS
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM report_kpi_reference_resolution_revisions").fetchone()[0]
        == 0
    )


def test_candidate_proposal_rejects_ambiguous_definition_family(tmp_path: Path) -> None:
    conn = _db()
    _seed_definition(conn, 2)
    repo = _repo(tmp_path)
    reference = report_kpi_references(repo, ("NU",))[0]

    proposal = propose_report_kpi_reference_resolution(conn, repo_root=repo, reference=reference)

    assert proposal.outcome is ReportKpiReferenceProposalOutcome.AMBIGUOUS
    assert proposal.candidate_definition_ids == (1, 2)
    assert proposal.proposed_disposition is None


def test_reader_reconstructs_exact_current_evidence(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    reference, _ = _accepted_binding(conn, repo)

    binding = resolve_report_kpi_reference_binding(
        conn, repo_root=repo, user_id="owner", reference=reference
    )

    assert binding is not None
    assert binding.kpi_definition_id == 1
    assert binding.definition_name == "Total customers (millions)"
    assert binding.evidence_fact_id == 21
    assert binding.evidence_context_id == 31


def test_scope_reconstructs_resolved_and_blocks_stale_binding(tmp_path: Path) -> None:
    conn = _db()
    _apply_ready(conn)
    repo = _repo(tmp_path)
    _accepted_binding(conn, repo)

    resolved = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")

    definition = next(row for row in resolved if row.kpi_definition_id == 1)
    assert "report" in definition.reasons
    conn.execute("UPDATE kpi_definitions SET primary_source='changed' WHERE id=1")
    conn.commit()

    stale = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")

    blocker = next(row for row in stale if row.kpi_definition_id is None)
    assert blocker.report_reference_status is ReportKpiReferenceStatus.UNRESOLVED
    assert blocker.report_reference_reason_code == "stale_report_reference_binding"
    facts_only = next(row for row in stale if row.kpi_definition_id == 1)
    assert facts_only.reasons == ("facts_metrics",)


def test_scope_defaults_to_canonical_owner_identity(tmp_path: Path) -> None:
    conn = _db()
    _apply_ready(conn, user_id=DEFAULT_USER_ID)
    repo = _repo(tmp_path)
    _accepted_binding(conn, repo, user_id=DEFAULT_USER_ID)

    rows = scoped_kpi_definitions(conn, repo_root=repo)

    definition = next(row for row in rows if row.kpi_definition_id == 1)
    assert definition.reasons == ("report", "facts_metrics")


def test_scope_honors_retired_before_exact_direct_matching(tmp_path: Path) -> None:
    conn = _db()
    _apply_ready(conn)
    repo = _repo(tmp_path, "Total customers (millions)")
    reference = report_kpi_references(repo, ("NU",))[0]
    persist_report_kpi_reference_disposition(
        conn,
        user_id="owner",
        reference=reference,
        disposition=ReportKpiReferenceDisposition(
            status=ReportKpiReferenceStatus.RETIRED,
            reason_code="not_a_current_report_metric",
        ),
        reviewed_by="source-review:owner",
        knowledge_at=NOW,
    )
    conn.commit()

    rows = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")

    definition = next(row for row in rows if row.kpi_definition_id == 1)
    assert definition.reasons == ("facts_metrics",)
    assert all(row.kpi_definition_id is not None for row in rows)


@pytest.mark.parametrize("mutation", ["swapped_id", "fake_hash", "override", "fact_drift"])
def test_locked_apply_recomputes_exact_resolved_proposal(
    tmp_path: Path,
    mutation: str,
) -> None:
    conn = _db()
    _apply_ready(conn)
    repo = _repo(tmp_path)
    reference = report_kpi_references(repo, ("NU",))[0]
    proposal = propose_report_kpi_reference_resolution(conn, repo_root=repo, reference=reference)
    assert proposal.proposed_disposition is not None
    disposition = proposal.proposed_disposition
    if mutation == "swapped_id":
        disposition = disposition.model_copy(update={"kpi_definition_id": 99})
    elif mutation == "fake_hash":
        disposition = disposition.model_copy(update={"definition_identity_sha256": "f" * 64})
    elif mutation == "override":
        conn.execute(
            "INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','replace','active')"
        )
    else:
        conn.execute("UPDATE kpi_facts SET source_excerpt='changed' WHERE id=21")
    conn.commit()

    with pytest.raises(ValueError, match="no longer matches its current proposal"):
        apply_kpi_semantic_disposition_manifest(
            conn,
            repo_root=repo,
            manifest=_manifest(reference, disposition),
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM report_kpi_reference_resolution_revisions").fetchone()[0]
        == 0
    )


def test_locked_apply_accepts_only_exact_current_proposal(tmp_path: Path) -> None:
    conn = _db()
    _apply_ready(conn)
    repo = _repo(tmp_path)
    reference = report_kpi_references(repo, ("NU",))[0]
    proposal = propose_report_kpi_reference_resolution(conn, repo_root=repo, reference=reference)
    assert proposal.proposed_disposition is not None

    result = apply_kpi_semantic_disposition_manifest(
        conn,
        repo_root=repo,
        manifest=_manifest(reference, proposal.proposed_disposition),
    )

    assert result.inserted_reference_rows == 1
    verified = verified_report_kpi_reference_definition(
        conn,
        repo_root=repo,
        user_id="owner",
        reference=reference,
    )
    assert verified is not None
    assert verified.definition_name == "Total customers (millions)"


def test_chart_priority_uses_verified_binding_and_blocks_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _db()
    repo = _repo_payload(
        tmp_path,
        '{"ticker":"NU","chart_priorities":["Total customers"]}',
    )
    _accepted_binding(conn, repo, user_id=DEFAULT_USER_ID)
    calls: list[str] = []

    def fake_series(
        _conn: sqlite3.Connection,
        _ticker: str,
        name: str,
        _quarter_labels: list[str],
        _quarter_labels_full: list[str],
    ) -> None:
        calls.append(name)

    def quarterly_cadence(
        _conn: sqlite3.Connection,
        _ticker: str,
        _name: str,
    ) -> str:
        return "quarterly"

    monkeypatch.setattr(financials_module, "_kpi_series_for", fake_series)
    monkeypatch.setattr(financials_module, "reporting_cadence_for", quarterly_cadence)

    _resolve_priorities(["Total customers"], [], "NU", repo, [], [], conn=conn)
    assert calls == ["Total customers (millions)"]

    conn.execute(
        "INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','replace','active')"
    )
    conn.commit()
    calls.clear()
    _resolve_priorities(["Total customers"], [], "NU", repo, [], [], conn=conn)
    assert calls == []


def test_tier_ledger_and_signal_writer_share_verified_binding(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    reference, _ = _accepted_binding(conn, repo, user_id=DEFAULT_USER_ID)
    holdings: dict[str, object] = {"tier_1_kpis": [{"name": "Total customers"}]}

    ledger = _build_ledger("NU", repo, holdings, evaluations=[], conn=conn)
    specs = _collect_metric_specs("NU", repo, conn=conn)

    assert ledger[0].kpi_definition_id == 1
    assert any(
        spec.metric_kind == "kpi" and spec.metric_name == "Total customers (millions)"
        for spec in specs
    )

    conn.execute("UPDATE kpi_definitions SET primary_source='changed' WHERE id=1")
    conn.commit()
    stale_ledger = _build_ledger("NU", repo, holdings, evaluations=[], conn=conn)
    stale_specs = _collect_metric_specs("NU", repo, conn=conn)
    assert stale_ledger[0].kpi_definition_id is None
    assert not any(spec.metric_kind == "kpi" for spec in stale_specs)
    conn.execute("UPDATE kpi_definitions SET primary_source='ir_doc' WHERE id=1")
    conn.execute("INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','drop','active')")
    conn.commit()
    blocked_ledger = _build_ledger("NU", repo, holdings, evaluations=[], conn=conn)
    blocked_specs = _collect_metric_specs("NU", repo, conn=conn)
    assert blocked_ledger[0].kpi_definition_id is None
    assert not any(spec.metric_kind == "kpi" for spec in blocked_specs)

    conn.execute("DELETE FROM fact_overrides")
    persist_report_kpi_reference_disposition(
        conn,
        user_id=DEFAULT_USER_ID,
        reference=reference,
        disposition=ReportKpiReferenceDisposition(
            status=ReportKpiReferenceStatus.RETIRED,
            reason_code="no_longer_used_in_report",
        ),
        reviewed_by="source-review:owner",
        knowledge_at=NOW,
    )
    conn.commit()
    retired_ledger = _build_ledger("NU", repo, holdings, evaluations=[], conn=conn)
    retired_specs = _collect_metric_specs("NU", repo, conn=conn)
    assert retired_ledger[0].kpi_definition_id is None
    assert not any(spec.metric_kind == "kpi" for spec in retired_specs)


def test_hard_break_rule_uses_binding_and_fails_closed_on_override(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo_payload(
        tmp_path,
        """{
          "ticker":"NU",
          "thesis":"customer compounder",
          "break_rules":[{
            "rule_id":"customers_below_floor",
            "kpi_name":"Total customers",
            "comparator":"lt",
            "threshold":100000000,
            "unit":"count",
            "consecutive_periods":1,
            "narrative":"Customers below floor"
          }]
        }""",
    )
    _accepted_binding(conn, repo, user_id=DEFAULT_USER_ID)

    verdict = evaluate_ticker_thesis(
        conn,
        ticker="NU",
        holdings_dir=repo / "micro_thesis" / "holdings",
    )
    assert verdict.rule_evaluations[0].status is BreachStatus.OK

    conn.execute(
        "INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','replace','active')"
    )
    conn.commit()
    blocked = evaluate_ticker_thesis(
        conn,
        ticker="NU",
        holdings_dir=repo / "micro_thesis" / "holdings",
    )
    assert blocked.rule_evaluations[0].status is BreachStatus.UNRESOLVED


def test_nu_business_and_soft_rules_share_verified_bindings_and_fail_closed(
    tmp_path: Path,
) -> None:
    conn = _db()
    repo = _repo_payload(
        tmp_path,
        """{
          "ticker":"NU",
          "thesis":"customer compounder",
          "business_model_rules":[{
            "rule_id":"customers_above_floor",
            "kpi_name":"Total customers",
            "comparator":"lt",
            "threshold":100000000,
            "unit":"count",
            "consecutive_periods":1,
            "narrative":"Customers remain above floor"
          }],
          "break_rules_soft":[{
            "name":"customers_below_watch",
            "predicate":{"type":"series_below","params":{
              "metric":"Total customers",
              "source":"kpi",
              "threshold":120000000,
              "periods":1
            }}
          }]
        }""",
    )
    references = {
        reference.json_pointer: reference for reference in report_kpi_references(repo, ("NU",))
    }
    for reference in references.values():
        _accepted_binding_for_reference(
            conn,
            repo,
            reference=reference,
            user_id=DEFAULT_USER_ID,
        )

    verdict = evaluate_ticker_thesis(
        conn,
        ticker="NU",
        holdings_dir=repo / "micro_thesis" / "holdings",
    )

    assert verdict.rule_evaluations[0].status is BreachStatus.OK
    assert verdict.soft_rule_results[0].status.value == "yellow"
    conn.execute(
        "INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','replace','active')"
    )
    conn.commit()

    blocked = evaluate_ticker_thesis(
        conn,
        ticker="NU",
        holdings_dir=repo / "micro_thesis" / "holdings",
    )

    assert blocked.rule_evaluations[0].status is BreachStatus.UNRESOLVED
    assert blocked.soft_rule_results[0].status.value == "unresolved"
    assert blocked.soft_rule_results[0].details == {"reason": "unverified_report_kpi_reference"}
    conn.execute("DELETE FROM fact_overrides")
    for reference in references.values():
        persist_report_kpi_reference_disposition(
            conn,
            user_id=DEFAULT_USER_ID,
            reference=reference,
            disposition=ReportKpiReferenceDisposition(
                status=ReportKpiReferenceStatus.RETIRED,
                reason_code="rule_no_longer_active",
            ),
            reviewed_by="source-review:owner",
            knowledge_at=NOW,
        )
    conn.commit()

    retired = evaluate_ticker_thesis(
        conn,
        ticker="NU",
        holdings_dir=repo / "micro_thesis" / "holdings",
    )

    assert retired.rule_evaluations[0].status is BreachStatus.UNRESOLVED
    assert retired.soft_rule_results[0].status.value == "unresolved"


def test_soft_rule_raw_index_survives_invalid_predecessor_compaction(tmp_path: Path) -> None:
    conn = _db()
    repo = _repo_payload(
        tmp_path,
        """{
          "ticker":"NU",
          "thesis":"customer compounder",
          "break_rules_soft":[
            {"name":"invalid_missing_predicate"},
            {
              "name":"customers_below_watch",
              "predicate":{"type":"series_below","params":{
                "metric":"Total customers",
                "source":"kpi",
                "threshold":120000000,
                "periods":1
              }}
            }
          ]
        }""",
    )

    verdict = evaluate_ticker_thesis(
        conn,
        ticker="NU",
        holdings_dir=repo / "micro_thesis" / "holdings",
    )

    assert len(verdict.soft_rule_results) == 1
    result = verdict.soft_rule_results[0]
    assert result.rule_name == "customers_below_watch"
    assert result.status.value == "unresolved"
    assert result.details == {"reason": "unverified_report_kpi_reference"}


@pytest.mark.parametrize(
    "mutation",
    [
        "definition",
        "fact",
        "context_successor",
        "override",
        "config",
    ],
)
def test_reader_fails_closed_on_identity_or_evidence_drift(tmp_path: Path, mutation: str) -> None:
    conn = _db()
    repo = _repo(tmp_path)
    reference, _ = _accepted_binding(conn, repo)
    if mutation == "definition":
        conn.execute("UPDATE kpi_definitions SET primary_source='changed' WHERE id=1")
    elif mutation == "fact":
        conn.execute("UPDATE kpi_facts SET source_excerpt='changed' WHERE id=21")
    elif mutation == "context_successor":
        conn.execute(
            "INSERT INTO kpi_fact_semantic_contexts VALUES "
            "(99,21,2,31,'Total customers','2024-12-31','unknown','unclassified',"
            "'unknown','unknown','{}','unknown',NULL,NULL,NULL,'quarantined','owner',"
            "'2026-08-31T00:00:00Z')"
        )
    elif mutation == "override":
        conn.execute(
            "INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','replace','active')"
        )
    else:
        _repo(tmp_path, "Active customers")
    conn.commit()

    assert (
        resolve_report_kpi_reference_binding(
            conn, repo_root=repo, user_id="owner", reference=reference
        )
        is None
    )


def _config(path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_migration_preserves_v1_ids_and_installs_immutable_v2_contract(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = tmp_path / "reference-v2.db"
    label_hash = hashlib.sha256(b"Unmapped").hexdigest()

    def seed_v1(db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO report_kpi_reference_resolution_revisions "
                "(id,user_id,ticker,source_path,json_pointer,reference_kind,requested_label,"
                "reference_content_sha256,status,kpi_definition_id,reason_code,revision,"
                "supersedes_resolution_id,reviewed_by,knowledge_at) "
                "VALUES (42,'owner','NU','micro_thesis/holdings/NU.json',"
                "'/tier_1_kpis/0/name','tier_1_kpi','Unmapped',?,'unresolved',NULL,"
                "'no_matching_reported_definition',1,NULL,'owner','2026-08-30T00:00:00Z')",
                (label_hash,),
            )
            conn.commit()

    migrated_db(
        path,
        upgrade_from="0034_add_investment_profile_label_reviews",
        before_upgrade=seed_v1,
        target="0036_add_data_coverage_dispositions",
    )
    config = _config(path)

    with sqlite3.connect(path) as conn:
        preserved = conn.execute(
            "SELECT id,status,kpi_definition_id,definition_identity_sha256 "
            "FROM report_kpi_reference_resolution_revisions"
        ).fetchone()
        assert preserved == (42, "unresolved", None, None)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO report_kpi_reference_resolution_revisions "
                "(user_id,ticker,source_path,json_pointer,reference_kind,requested_label,"
                "reference_content_sha256,status,reason_code,revision,reviewed_by,knowledge_at) "
                "VALUES ('owner','NU','x','/x','tier_1_kpi','x',?,'resolved','bad',1,'owner','x')",
                (hashlib.sha256(b"x").hexdigest(),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE report_kpi_reference_resolution_revisions SET reason_code='changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM report_kpi_reference_resolution_revisions")

    command.downgrade(config, "0034_add_investment_profile_label_reviews")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT id,status FROM report_kpi_reference_resolution_revisions"
        ).fetchone() == (42, "unresolved")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(report_kpi_reference_resolution_revisions)")
        }
        assert "definition_identity_sha256" not in columns


@pytest.mark.parametrize("status", ["resolved", "retired"])
def test_downgrade_refuses_any_v2_history(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    status: str,
) -> None:
    path = tmp_path / f"reference-v2-{status}.db"
    config = _config(path)
    migrated_db(path, target="0036_add_data_coverage_dispositions")
    label_hash = hashlib.sha256(b"Metric").hexdigest()
    with sqlite3.connect(path) as conn:
        binding_columns = ""
        binding_values: tuple[object, ...] = ()
        if status == "resolved":
            binding_columns = (
                ",kpi_definition_id,definition_identity_sha256,evidence_fact_id,"
                "evidence_context_id,evidence_sha256,resolution_method,policy_name,"
                "policy_version,policy_config_sha256"
            )
            binding_values = (
                999_001,
                "d" * 64,
                999_002,
                999_003,
                "e" * 64,
                "exact_definition_identity",
                "test",
                "v2",
                "f" * 64,
            )
        placeholders = ",".join("?" for _ in binding_values)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO report_kpi_reference_resolution_revisions "
            "(user_id,ticker,source_path,json_pointer,reference_kind,requested_label,"
            "reference_content_sha256,status,reason_code,revision,reviewed_by,knowledge_at"
            + binding_columns
            + ") VALUES ('owner','NU','x','/x','tier_1_kpi','Metric',?,?,?,1,'owner','now'"
            + ("," + placeholders if placeholders else "")
            + ")",
            (label_hash, status, status, *binding_values),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade"):
        command.downgrade(config, "0034_add_investment_profile_label_reviews")


def test_downgrade_refuses_new_reference_kind_even_when_unresolved(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(
        tmp_path / "reference-v2-soft-kind.db",
        target="0036_add_data_coverage_dispositions",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO report_kpi_reference_resolution_revisions "
            "(user_id,ticker,source_path,json_pointer,reference_kind,requested_label,"
            "reference_content_sha256,status,reason_code,revision,reviewed_by,knowledge_at) "
            "VALUES ('owner','NU','x','/x','soft_rule_kpi','Metric',?,'unresolved',"
            "'unresolved',1,'owner','now')",
            (hashlib.sha256(b"Metric").hexdigest(),),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade"):
        command.downgrade(_config(path), "0034_add_investment_profile_label_reviews")


def test_scope_reuses_request_inventory_and_each_disposition_without_changing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.kpi_report_reference_resolver as resolver
    import pipeline.kpi_semantic_scope as scope
    from pipeline.kpi_report_reference_dispositions import (
        ReportKpiReferenceDispositionRevision,
        ReportKpiReferenceInventory,
        current_report_kpi_reference_disposition,
        load_report_kpi_reference_inventory,
    )

    conn = _db()
    _apply_ready(conn)
    repo = _repo_payload(
        tmp_path,
        '{"ticker":"NU","tier_1_kpis":[{"name":"Total customers"},'
        '{"name":"Total customers (millions)"},{"name":"Not reported"}]}',
    )
    _accepted_binding(conn, repo)
    # Full outputs captured from immutable f6ea4d32 source on this fixture.
    expected = [
        {
            "admitted_context_count": 0,
            "analyst_question_count": 0,
            "comparator_count": 0,
            "current_actual_count": 0,
            "fact_count": 0,
            "guidance_target_count": 0,
            "kpi_definition_id": None,
            "legacy_unknown_context_count": 0,
            "management_explanation_count": 0,
            "missing_context_count": 0,
            "name": "Not reported",
            "quarantined_context_count": 0,
            "reasons": ["report"],
            "report_reference_pointer": "/tier_1_kpis/2/name",
            "report_reference_reason_code": None,
            "report_reference_source_reason_code": None,
            "report_reference_source_status": "valid",
            "report_reference_status": None,
            "ticker": "NU",
        },
        {
            "admitted_context_count": 1,
            "analyst_question_count": 0,
            "comparator_count": 0,
            "current_actual_count": 1,
            "fact_count": 1,
            "guidance_target_count": 0,
            "kpi_definition_id": 1,
            "legacy_unknown_context_count": 0,
            "management_explanation_count": 0,
            "missing_context_count": 0,
            "name": "Total customers (millions)",
            "quarantined_context_count": 0,
            "reasons": ["report", "facts_metrics"],
            "report_reference_pointer": None,
            "report_reference_reason_code": None,
            "report_reference_source_reason_code": None,
            "report_reference_source_status": None,
            "report_reference_status": None,
            "ticker": "NU",
        },
    ]
    inventory_reads: list[tuple[str, ...]] = []
    disposition_reads: list[str] = []

    def inventory_reader(root: Path, tickers: tuple[str, ...]) -> ReportKpiReferenceInventory:
        inventory_reads.append(tickers)
        return load_report_kpi_reference_inventory(root, tickers)

    def disposition_reader(
        connection: sqlite3.Connection, *, user_id: str, reference: ReportKpiReference
    ) -> ReportKpiReferenceDispositionRevision | None:
        disposition_reads.append(reference.json_pointer)
        return current_report_kpi_reference_disposition(
            connection, user_id=user_id, reference=reference
        )

    for module in (resolver, scope):
        monkeypatch.setattr(
            module, "load_report_kpi_reference_inventory", inventory_reader, raising=False
        )
        monkeypatch.setattr(
            module, "current_report_kpi_reference_disposition", disposition_reader, raising=False
        )
    actual = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")
    assert [row.model_dump(mode="json") for row in actual] == expected
    definition = next(row for row in actual if row.kpi_definition_id == 1)
    assert definition.reasons == ("report", "facts_metrics")
    assert (
        definition.fact_count
        == definition.admitted_context_count
        == definition.current_actual_count
        == 1
    )
    assert next(row for row in actual if row.name == "Not reported").kpi_definition_id is None
    assert inventory_reads == [("NU",), ("NU",)], (
        "one initial and one final source read per request"
    )
    assert len(disposition_reads) == 3
    assert len(set(disposition_reads)) == 3
    conn.close()


@pytest.mark.parametrize("change", ["label", "missing", "invalid"])
def test_scope_fails_closed_if_report_inventory_changes_during_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import pipeline.kpi_report_reference_resolver as resolver
    from pipeline.kpi_report_reference_dispositions import (
        ReportKpiReferenceDispositionRevision,
        ReportKpiReferenceSourceStatus,
        current_report_kpi_reference_disposition,
    )

    conn = _db()
    _apply_ready(conn)
    repo = _repo(tmp_path)
    _accepted_binding(conn, repo)
    changed = False

    def disposition_reader(
        connection: sqlite3.Connection, *, user_id: str, reference: ReportKpiReference
    ) -> ReportKpiReferenceDispositionRevision | None:
        nonlocal changed
        result = current_report_kpi_reference_disposition(
            connection, user_id=user_id, reference=reference
        )
        if not changed:
            changed = True
            path = repo / "micro_thesis/holdings/NU.json"
            if change == "label":
                _repo(tmp_path, "Active customers")
            elif change == "missing":
                path.unlink()
            else:
                path.write_text("{", encoding="utf-8")
        return result

    monkeypatch.setattr(resolver, "current_report_kpi_reference_disposition", disposition_reader)
    rows = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")
    invalid = next(row for row in rows if row.name == "<report_configuration>")
    assert invalid.report_reference_source_status is ReportKpiReferenceSourceStatus.INVALID
    assert (
        invalid.report_reference_source_reason_code == "report_configuration_changed_during_scope"
    )
    assert all("report" not in row.reasons for row in rows)
    conn.close()


@pytest.mark.parametrize(
    "mutation", ["definition", "fact", "context_successor", "override", "disposition"]
)
def test_scope_rechecks_evidence_on_each_request(tmp_path: Path, mutation: str) -> None:
    conn = _db()
    _apply_ready(conn)
    repo = _repo(tmp_path)
    reference, _ = _accepted_binding(conn, repo)
    first = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")
    assert "report" in next(row for row in first if row.kpi_definition_id == 1).reasons
    if mutation == "definition":
        conn.execute("UPDATE kpi_definitions SET primary_source='changed' WHERE id=1")
    elif mutation == "fact":
        conn.execute("UPDATE kpi_facts SET source_excerpt='changed' WHERE id=21")
    elif mutation == "context_successor":
        conn.execute(
            "INSERT INTO kpi_fact_semantic_contexts VALUES "
            "(99,21,2,31,'Total customers','2024-12-31','unknown','unclassified',"
            "'unknown','unknown','{}','unknown',NULL,NULL,NULL,'quarantined','owner',"
            "'2026-08-31T00:00:00Z')"
        )
    elif mutation == "override":
        conn.execute(
            "INSERT INTO fact_overrides VALUES ('NU','kpi','Total customers','replace','active')"
        )
    else:
        persist_report_kpi_reference_disposition(
            conn,
            user_id="owner",
            reference=reference,
            disposition=ReportKpiReferenceDisposition(
                status=ReportKpiReferenceStatus.UNRESOLVED,
                reason_code="requires_new_source_review",
            ),
            reviewed_by="source-review:owner",
            knowledge_at=NOW,
        )
    conn.commit()
    second = scoped_kpi_definitions(conn, repo_root=repo, user_id="owner")
    stale = next(row for row in second if row.kpi_definition_id is None)
    assert stale.report_reference_status is ReportKpiReferenceStatus.UNRESOLVED
    assert stale.report_reference_reason_code == (
        "requires_new_source_review"
        if mutation == "disposition"
        else "stale_report_reference_binding"
    )
    assert "report" not in next(row for row in second if row.kpi_definition_id == 1).reasons
    conn.close()
