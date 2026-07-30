"""Adversarial cutover tests for audit binding, verifier derivation, and issuer parity."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

import provenance.population_cutover as cutover_module
from provenance.integrity_audit import (
    CutoverAuditOptions,
    CutoverGateCandidateCommitment,
    CutoverGateCoverage,
    CutoverReadinessSummary,
)
from provenance.legacy_canonical_parity import (
    ParityReport,
    ParityRequest,
    ProjectionCoordinate,
)
from provenance.population_completeness import REQUIRED_POPULATION_PLANES, PopulationTemporalScope
from provenance.population_cutover import (
    REQUIRED_CUTOVER_AUDIT_GATES,
    CutoverBlocker,
    CutoverBlockerCode,
    IssuerProjectionScope,
    PlaneVerifierEvidence,
    PopulationCutoverRequest,
    discover_issuer_projection_scopes,
    evaluate_population_cutover,
)

STAMP = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"population-cutover-test").hexdigest()
SCOPE = PopulationTemporalScope(knowledge_cutoff=STAMP, observed_through=OBSERVED)


def _ledger_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE population_run_headers (
          population_run_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
          policy_name TEXT, policy_version TEXT, policy_config_sha256 TEXT,
          source_snapshot_sha256 TEXT, knowledge_cutoff TEXT, observed_through TEXT,
          verified_at TEXT, canonical_identity_json TEXT, identity_sha256 TEXT,
          UNIQUE(policy_config_sha256,source_snapshot_sha256,knowledge_cutoff,observed_through)
        );
        CREATE TABLE population_plane_receipts (
          population_run_id TEXT, plane_name TEXT, expected_count INTEGER,
          materialized_count INTEGER, excluded_count INTEGER, failed_count INTEGER,
          input_commitment_sha256 TEXT, output_commitment_sha256 TEXT, status TEXT,
          canonical_details_json TEXT, details_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, verified_at TEXT,
          PRIMARY KEY(population_run_id,plane_name)
        );
        CREATE TABLE population_parity_receipts (
          population_run_id TEXT PRIMARY KEY, eligible_legacy_count INTEGER,
          canonical_count INTEGER, matched_count INTEGER, mismatched_count INTEGER,
          absent_count INTEGER, extra_count INTEGER, status TEXT,
          canonical_report_json TEXT, report_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, verified_at TEXT
        );
        CREATE TABLE population_cutover_audit_receipts (
          population_run_id TEXT PRIMARY KEY, verifier_name TEXT, verifier_version TEXT,
          verifier_code_sha256 TEXT, verifier_config_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, verified_at TEXT, required_gate_count INTEGER, eligible_count INTEGER,
          verified_count INTEGER, failed_count INTEGER, canonical_evidence_json TEXT,
          evidence_sha256 TEXT, canonical_receipt_json TEXT, receipt_sha256 TEXT
        );
        CREATE TABLE population_cutover_receipts (
          population_run_id TEXT PRIMARY KEY, required_plane_count INTEGER,
          complete_plane_count INTEGER, audit_receipt_sha256 TEXT,
          canonical_receipt_set_json TEXT, receipt_set_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, sealed_at TEXT
        );
        """
    )
    return conn


def _audit(*, empty_gate: str | None = None) -> CutoverReadinessSummary:
    coverage = tuple(
        CutoverGateCoverage(
            gate=name,
            eligible_count=0 if name == empty_gate else 1,
            verified_count=0 if name == empty_gate else 1,
            failed_count=0,
        )
        for name in REQUIRED_CUTOVER_AUDIT_GATES
    )
    return CutoverReadinessSummary(
        knowledge_cutoff=STAMP,
        observed_through=STAMP,
        generated_at=STAMP,
        coverage=coverage,
        candidate_commitments=tuple(
            CutoverGateCandidateCommitment(
                gate=name,
                selection_policy_id=f"{name}.v1",
                row_count=0 if name == empty_gate else 1,
                rows_sha256=SHA,
            )
            for name in REQUIRED_CUTOVER_AUDIT_GATES
        ),
        findings=(),
        has_blockers=False,
        tables_present=("population_test",),
    )


def _evidence() -> tuple[PlaneVerifierEvidence, ...]:
    return tuple(
        PlaneVerifierEvidence(
            plane_name=name,
            expected_count=1,
            materialized_count=1,
            exclusion_counts={},
            failed_count=0,
            input_commitment_sha256=SHA,
            output_commitment_sha256=SHA,
            verifier_name=f"{name}-verifier",
            verifier_version="1",
            verifier_code_sha256=SHA,
            artifact_sets=(
                {
                    "row_count": 1,
                    "rows_sha256": SHA,
                    "selection_policy_id": f"{name}.v1",
                    "table": f"{name}_artifacts",
                },
            ),
            result={
                "plane": name,
                **(
                    {
                        "governance": {
                            "evaluation_receipt_id": "evaluation-1",
                            "evaluation_evaluated_at": STAMP.isoformat(),
                            "promotion_id": "promotion-1",
                            "promotion_recorded_at": STAMP.isoformat(),
                            "projection_seal_ids": ["projection-1"],
                            "projection_sealed_at": {"projection-1": STAMP.isoformat()},
                            "runtime_registered_at": STAMP.isoformat(),
                            "runtime_registration_id": "runtime-1",
                        }
                    }
                    if name == "retrieval_runtime"
                    else {}
                ),
            },
        )
        for name in REQUIRED_POPULATION_PLANES
    )


def _parity() -> ParityReport:
    return ParityReport(
        knowledge_cutoff=STAMP,
        observed_through=OBSERVED,
        projection_generation_id="generation-1",
        issuer_id="issuer-1",
        complete=True,
        truncated=False,
        cutover_ready=True,
        pages_scanned=1,
        projection_pages_scanned=1,
        legacy_rows_scanned=1,
        canonical_coordinates_scanned=1,
        comparable_rows=1,
        equal_rows=1,
        mismatch_rows=0,
        blocking_legacy_rows=0,
        disposition_counts={"equal": 1},
        legacy_fact_universe_sha256=SHA,
        parity_rows_sha256=SHA,
        next_cursor=None,
        projection_next_cursor=None,
        rows=(),
    )


class _UnusedReader:
    def read_coordinates(
        self,
        *,
        generation_id: str,
        canonical_metric_cell_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> Mapping[str, ProjectionCoordinate]:
        del generation_id, canonical_metric_cell_ids, cutoff_at
        return {}

    def read_coordinate_page(
        self,
        *,
        generation_id: str,
        after_coordinate: str | None,
        limit: int,
        cutoff_at: datetime,
    ) -> Sequence[ProjectionCoordinate]:
        del generation_id, after_coordinate, limit, cutoff_at
        return ()


_READER = _UnusedReader()


def _derive_stub(
    conn: sqlite3.Connection,
    temporal_scope: PopulationTemporalScope,
    *,
    runtime: object,
) -> tuple[PlaneVerifierEvidence, ...]:
    del conn, temporal_scope, runtime
    return _evidence()


def _scope_stub(
    conn: sqlite3.Connection,
    temporal_scope: PopulationTemporalScope,
) -> tuple[tuple[IssuerProjectionScope, ...], list[CutoverBlocker]]:
    del conn, temporal_scope
    return (
        (
            IssuerProjectionScope(
                issuer_id="issuer-1",
                projection_generation_id="generation-1",
                legacy_fact_count=1,
            ),
        ),
        [],
    )


def _parity_stub(
    conn: sqlite3.Connection,
    request: ParityRequest,
    reader: _UnusedReader,
) -> ParityReport:
    del conn, request, reader
    return _parity()


def _audit_stub(
    conn: sqlite3.Connection,
    options: CutoverAuditOptions,
) -> CutoverReadinessSummary:
    del conn, options
    return _audit()


def _patch_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cutover_module,
        "audit_cutover_readiness",
        _audit_stub,
    )
    monkeypatch.setattr(cutover_module, "_derive_plane_evidence", _derive_stub)
    monkeypatch.setattr(
        cutover_module,
        "discover_issuer_projection_scopes",
        _scope_stub,
    )
    monkeypatch.setattr(
        cutover_module,
        "scan_legacy_canonical_parity",
        _parity_stub,
    )


def test_dry_run_is_eligible_but_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ledger_database()
    _patch_clean(monkeypatch)
    result = evaluate_population_cutover(
        conn,
        PopulationCutoverRequest(knowledge_cutoff=STAMP, observed_through=STAMP),
        projection_reader=_READER,
    )
    assert result.outcome == "eligible"
    assert result.cutover_ready is False
    assert result.audit_receipt is not None
    assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone()[0] == 0


def test_apply_revalidates_and_binds_audit_into_final_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ledger_database()
    _patch_clean(monkeypatch)
    result = evaluate_population_cutover(
        conn,
        PopulationCutoverRequest(knowledge_cutoff=STAMP, observed_through=STAMP, apply=True),
        projection_reader=_READER,
    )
    assert result.outcome == "sealed"
    assert result.cutover_ready is True
    assert result.cutover_receipt is not None
    assert result.audit_receipt is not None
    assert result.cutover_receipt.audit_receipt_sha256 == result.audit_receipt.receipt_sha256
    payload = conn.execute(
        "SELECT canonical_receipt_set_json FROM population_cutover_receipts"
    ).fetchone()[0]
    assert result.audit_receipt.receipt_sha256 in str(payload)


def test_transactional_revalidation_blocks_state_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ledger_database()
    _patch_clean(monkeypatch)
    calls = 0

    def changing_audit(
        conn: sqlite3.Connection,
        options: object,
    ) -> CutoverReadinessSummary:
        nonlocal calls
        del conn, options
        calls += 1
        return _audit(empty_gate="research_snapshots") if calls == 2 else _audit()

    monkeypatch.setattr(cutover_module, "audit_cutover_readiness", changing_audit)
    result = evaluate_population_cutover(
        conn,
        PopulationCutoverRequest(knowledge_cutoff=STAMP, observed_through=STAMP, apply=True),
        projection_reader=_READER,
    )
    assert result.outcome == "blocked"
    assert result.cutover_ready is False
    assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone()[0] == 0


def _scope_database(
    *,
    reused_ticker: bool = False,
    unbound: bool = False,
    post_cutoff_fact: bool = False,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE documents (
          id INTEGER PRIMARY KEY,fetched_at TEXT
        );
        CREATE TABLE financial_facts (
          id INTEGER PRIMARY KEY,ticker TEXT,source_doc_id INTEGER
        );
        CREATE TABLE kpi_facts (
          id INTEGER PRIMARY KEY,ticker TEXT,computed_from TEXT,formula_id TEXT,
          formula_version TEXT,extracted_by TEXT,source_doc_id INTEGER
        );
        CREATE TABLE legacy_fact_evidence_match_revisions (
          match_revision_id TEXT,fact_table TEXT,fact_row_id INTEGER,issuer_id TEXT,
          revision INTEGER,knowledge_at TEXT,recorded_at TEXT
        );
        CREATE TABLE canonical_fact_resolution_snapshot_scope_headers (
          issuer_id TEXT,resolution_snapshot_id TEXT,cutoff_at TEXT
        );
        CREATE TABLE canonical_fact_resolution_snapshot_scope_seals (
          resolution_snapshot_id TEXT
        );
        CREATE TABLE canonical_fact_projection_scope_bindings (
          resolution_snapshot_id TEXT,generation_id TEXT
        );
        CREATE TABLE canonical_fact_projection_generations (
          generation_id TEXT,cutoff_at TEXT
        );
        CREATE TABLE canonical_fact_projection_seals (generation_id TEXT);
        CREATE TABLE canonical_fact_projection_audit_receipts (generation_id TEXT);
        """
    )
    issuers = ("issuer-1", "issuer-2") if reused_ticker else ("issuer-1",)
    for index, issuer in enumerate(issuers, start=1):
        ticker = "SAME" if reused_ticker else "OLD"
        conn.execute(
            "INSERT INTO documents VALUES (?,?)",
            (index, STAMP.isoformat()),
        )
        conn.execute("INSERT INTO financial_facts VALUES (?,?,?)", (index, ticker, index))
        if not (unbound and index == 1):
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions VALUES (?,?,?,?,?,?,?)",
                (
                    f"match-{index}",
                    "financial_facts",
                    index,
                    issuer,
                    1,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                ),
            )
        snapshot, generation = f"snapshot-{index}", f"generation-{index}"
        conn.execute(
            "INSERT INTO canonical_fact_resolution_snapshot_scope_headers VALUES (?,?,?)",
            (issuer, snapshot, STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO canonical_fact_resolution_snapshot_scope_seals VALUES (?)",
            (snapshot,),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_scope_bindings VALUES (?,?)",
            (snapshot, generation),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations VALUES (?,?)",
            (generation, STAMP.isoformat()),
        )
        conn.execute("INSERT INTO canonical_fact_projection_seals VALUES (?)", (generation,))
        conn.execute(
            "INSERT INTO canonical_fact_projection_audit_receipts VALUES (?)",
            (generation,),
        )
    if post_cutoff_fact:
        conn.execute(
            "INSERT INTO documents VALUES (?,?)",
            (99, "2026-07-30T12:00:00+00:00"),
        )
        conn.execute("INSERT INTO financial_facts VALUES (99,'OLD',99)")
    return conn


def test_historical_ticker_fact_is_scoped_by_durable_issuer_match() -> None:
    scopes, blockers = discover_issuer_projection_scopes(_scope_database(), SCOPE)
    assert blockers == []
    assert scopes[0].legacy_fact_count == 1


def test_post_cutoff_source_fact_does_not_contaminate_historical_scope() -> None:
    scopes, blockers = discover_issuer_projection_scopes(
        _scope_database(post_cutoff_fact=True),
        SCOPE,
    )

    assert blockers == []
    assert scopes[0].legacy_fact_count == 1


def test_late_recorded_issuer_match_is_visible_through_observed_clock() -> None:
    conn = _scope_database()
    conn.execute(
        "UPDATE legacy_fact_evidence_match_revisions SET recorded_at='2026-07-29T12:30:00+00:00'"
    )

    scopes, blockers = discover_issuer_projection_scopes(conn, SCOPE)

    assert blockers == []
    assert scopes[0].legacy_fact_count == 1


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"unbound": True}, CutoverBlockerCode.LEGACY_FACT_UNBOUND),
        ({"reused_ticker": True}, CutoverBlockerCode.LEGACY_TICKER_REUSED),
    ],
)
def test_unbound_or_reused_ticker_facts_are_blockers(
    kwargs: dict[str, bool],
    code: CutoverBlockerCode,
) -> None:
    _, blockers = discover_issuer_projection_scopes(_scope_database(**kwargs), SCOPE)
    assert any(item.code is code for item in blockers)
