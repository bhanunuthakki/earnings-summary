"""Selected publisher calls retain their exact native duty outside SEC inventory."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from provenance.analysis_scope import (
    AnalysisEvidenceScope,
    AnalysisScopeRequest,
    build_analysis_scope,
    require_analysis_documents,
)
from provenance.population_research_snapshots import ResearchSnapshotPlanError
from provenance.research_snapshot import resolve_corpus_obligation_bindings
from tests import test_analysis_issuer_pdf_scope as issuer_fixture
from tests.test_historical_analysis_scope import ISSUER, K
from tests.test_historical_analysis_scope import historical as historical

_historical_call: Callable[..., AnalysisScopeRequest] = getattr(issuer_fixture, "_historical_call")


def _memberships(conn: sqlite3.Connection, scope: AnalysisEvidenceScope) -> None:
    require_analysis_documents(conn, scope, K, K)
    conn.executescript(
        "CREATE TEMP TABLE search_corpus_document_memberships (membership_id TEXT,manifest_id TEXT,"
        "expected_document_key TEXT,document_version_id TEXT,membership_status TEXT);"
        "CREATE TEMP TABLE search_manifest_source_inventories (manifest_id TEXT,snapshot_id TEXT);"
    )
    for entry in scope.entries:
        if entry.role != "research_document":
            continue
        expected = entry.expected_document
        selected = next(
            (
                s
                for s in scope.request.issuer_documents
                if s.expected_document_id == expected.expected_document_id
            ),
            None,
        )
        version = selected.document_version_id if selected else "original-financial"
        conn.execute(
            "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
            (
                expected.expected_document_id,
                "manifest",
                expected.expected_document_key,
                version,
                "included",
            ),
        )
        if selected is None:
            conn.execute(
                "INSERT INTO search_manifest_source_inventories VALUES (?,?)",
                ("manifest", expected.snapshot_id),
            )


def test_native_historical_issuer_call_joins_exact_obligation_without_sec_inventory(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(conn, _historical_call(conn, tmp_path, payload))
    _memberships(conn, scope)
    before = conn.total_changes
    rows = resolve_corpus_obligation_bindings(conn, ("manifest",), analysis_scope=scope)
    assert len(rows) == 2
    assert {str(r[5]) for r in rows} == {"original-financial", "nu-call-version"}
    assert all(str(r[2]) == ISSUER for r in rows)
    assert conn.total_changes == before
    from provenance import population_research_snapshots as population

    helper: Callable[..., tuple[str, ...]] = getattr(population, "_obligation_coordinates")
    assert helper(conn, "manifest", ISSUER, analysis_scope=scope) == tuple(
        sorted({str(r[1]) for r in rows})
    )
    with pytest.raises(ResearchSnapshotPlanError, match="corpus_obligation_binding_incomplete"):
        helper(conn, "manifest", ISSUER)
    # Listing the exact same publisher inventory must not count one duty twice.
    issuer_expected = next(
        e.expected_document
        for e in scope.entries
        if e.expected_document_id == scope.request.issuer_documents[0].expected_document_id
    )
    conn.execute(
        "INSERT INTO search_manifest_source_inventories VALUES (?,?)",
        ("manifest", issuer_expected.snapshot_id),
    )
    assert resolve_corpus_obligation_bindings(conn, ("manifest",), analysis_scope=scope) == rows


@pytest.mark.parametrize(
    "mutation", ["version", "key", "missing", "binding_issuer", "binding_revision"]
)
def test_selected_call_does_not_bind_wrong_membership_or_duty(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
    mutation: str,
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(conn, _historical_call(conn, tmp_path, payload))
    _memberships(conn, scope)
    key = scope.request.issuer_documents[0].expected_document_id
    if mutation in {"version", "key", "missing"}:
        column, value = {
            "version": ("document_version_id", "other-version"),
            "key": ("expected_document_key", "other-key"),
            "missing": ("membership_status", "missing"),
        }[mutation]
        conn.execute(
            f"UPDATE search_corpus_document_memberships SET {column}=? WHERE membership_id=?",
            (value, key),
        )
    else:
        conn.execute(
            "CREATE TEMP TABLE expected_document_obligation_bindings AS SELECT * FROM main.expected_document_obligation_bindings"
        )
        column, value = (
            ("issuer_id", "other-issuer")
            if mutation == "binding_issuer"
            else ("source_obligation_revision_id", "other-duty")
        )
        conn.execute(
            f"UPDATE expected_document_obligation_bindings SET {column}=? WHERE expected_document_id=?",
            (value, key),
        )
    with pytest.raises(ValueError, match="exactly one"):
        resolve_corpus_obligation_bindings(conn, ("manifest",), analysis_scope=scope)
