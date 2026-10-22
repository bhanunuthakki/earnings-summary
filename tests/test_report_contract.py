"""Binding fixtures isolate native admission; no live qualification is implied."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from earnings_brief import SECTION_TITLES as PRE_TITLES
from earnings_brief import BriefCandidate
from earnings_readout import SECTION_TITLES as POST_TITLES
from earnings_readout import ReportedQuarter
from research import earnings_report_contract as adapters
from research import report_contract as contract
from research.memo_claim_support import MemoDerivedFigure, MemoSourceExcerpt

STAMP = datetime(2026, 1, 1, tzinfo=UTC)
EVENT = date(2026, 2, 1)
TEXT = "Revenue was 100 USD and costs were 50 USD."


def draft(*, family: str = "pre", drafted_at: datetime = STAMP) -> contract.ReportDraft:
    titles = PRE_TITLES if family == "pre" else POST_TITLES
    return contract.ReportDraft.model_validate(
        {
            "family": family,
            "ticker": "SYNTH",
            "market_mic": "XNAS",
            "issuer_id": "issuer-1",
            "research_snapshot_id": "snapshot-1",
            "drafted_at": drafted_at,
            "event_date": EVENT,
            "sections": [
                {
                    "title": title,
                    "paragraphs": [
                        {
                            "paragraph_id": f"paragraph-{index}",
                            "voice": "reported",
                            "text": TEXT,
                            "excerpts": [{"evidence_node_ids": ["node-1"], "text": TEXT}],
                        }
                    ],
                }
                for index, title in enumerate(titles)
            ],
        }
    )


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return STAMP

    monkeypatch.setattr(contract, "datetime", Clock)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE research_snapshot_headers(research_snapshot_id TEXT,request_json TEXT)"
    )
    conn.execute("CREATE TABLE evidence_nodes(node_id TEXT,text TEXT)")
    conn.execute("INSERT INTO evidence_nodes VALUES (?,?)", ("node-1", TEXT))
    request = {
        "research_snapshot_id": "snapshot-1",
        "idempotency_key": "snapshot-1",
        "research_universe": {
            "issuer_id": "issuer-1",
            "reporting_entity_ids": ["reporting-1"],
            "document_version_ids": ["document-1"],
            "source_obligation_revision_ids": ["obligation-1"],
        },
        "processing_snapshot_ids": ["processing-1"],
        "corpus_bundles": [{"corpus_manifest_id": "corpus-1", "lexical_index_run_id": "lexical-1"}],
        "source_fact_publication_ids": [],
        "ontology_snapshot_id": "ontology-1",
        "canonical_fact_resolution_snapshot_id": "resolution-1",
        "canonical_fact_projection_run_id": "projection-1",
        "cutoff_at": STAMP.isoformat(),
        "recorded_at": STAMP.isoformat(),
    }
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?)", ("snapshot-1", json.dumps(request))
    )

    def admission(_conn: sqlite3.Connection, identity: str) -> SimpleNamespace:
        assert identity == "snapshot-1"
        return SimpleNamespace(member_set_sha256="a" * 64)

    def node(_conn: sqlite3.Connection, identity: str, *_args: object) -> None:
        if identity != "node-1":
            raise ValueError("node outside fixture snapshot")

    class Registry:
        def __init__(self, _conn: sqlite3.Connection) -> None:
            pass

        def resolve_listing(
            self, mic: str, ticker: str, *, knowledge_at: datetime
        ) -> SimpleNamespace:
            assert (mic, ticker, knowledge_at) == ("XNAS", "SYNTH", STAMP)
            return SimpleNamespace(issuer_id="issuer-1")

    monkeypatch.setattr(contract, "verify_research_snapshot", admission)
    monkeypatch.setattr(contract, "verify_memo_snapshot_node", node)
    monkeypatch.setattr(contract, "IssuerRegistry", Registry)
    conn.commit()
    yield conn
    conn.close()


def test_source_binding_receipt_never_claims_semantic_or_full_acceptance(
    connection: sqlite3.Connection,
) -> None:
    item = draft()
    before = connection.total_changes
    result = contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)
    assert result.binding_status == "verified"
    assert result.source_completeness == "not_assessed"
    assert result.semantic_review == "required" and not result.decision_grade
    assert result.temporal_status == "contemporaneous_draft"
    assert result.paragraph_ids == tuple(f"paragraph-{i}" for i in range(4))
    assert connection.total_changes == before


def test_late_pre_draft_is_historical_not_resealed(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LateClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return datetime(2026, 2, 2, tzinfo=UTC)

    monkeypatch.setattr(contract, "datetime", LateClock)
    item = draft()  # An old draft timestamp cannot backdate today's binding.

    result = adapters.bind_pre_earnings_draft(
        connection, item, BriefCandidate(ticker="SYNTH", er_date=EVENT, days_until=0)
    )
    assert result.temporal_status == "historical_reconstruction"
    assert not result.decision_grade


@pytest.mark.parametrize(
    "damage", ["number", "excerpt", "node", "missing", "duplicate", "reported_inference"]
)
def test_bad_paragraph_fails_without_partial_receipt(
    connection: sqlite3.Connection, damage: str
) -> None:
    item = draft()
    paragraph = item.sections[0].paragraphs[0]
    updates: dict[str, object] = {}
    if damage == "number":
        updates["text"] = "Revenue was 999 USD."
    elif damage == "excerpt":
        updates["excerpts"] = (
            MemoSourceExcerpt(evidence_node_ids=("node-1",), text="Invented source"),
        )
    elif damage == "node":
        updates["excerpts"] = (MemoSourceExcerpt(evidence_node_ids=("another-node",), text=TEXT),)
    elif damage == "missing":
        updates["excerpts"] = ()
    elif damage == "duplicate":
        updates["excerpts"] = (
            MemoSourceExcerpt(evidence_node_ids=("node-1", "node-1"), text=TEXT),
        )
    else:
        updates["text"] = "I expect revenue of 100 USD."
    changed = paragraph.model_copy(update=updates)
    item = item.model_copy(
        update={
            "sections": (
                item.sections[0].model_copy(update={"paragraphs": (changed,)}),
                *item.sections[1:],
            )
        }
    )
    with pytest.raises(ValueError):
        contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)


def test_analysis_replays_arithmetic_and_retains_semantic_review(
    connection: sqlite3.Connection,
) -> None:
    item = draft()
    paragraph = (
        item.sections[0]
        .paragraphs[0]
        .model_copy(
            update={
                "voice": "analyst_inference",
                "text": "The implied margin is 50%.",
                "derived": (
                    MemoDerivedFigure(
                        expression="(revenue - costs) / revenue",
                        operands={"revenue": "100", "costs": "50"},
                        displayed_value="50%",
                        decimals=0,
                        percent=True,
                    ),
                ),
            }
        )
    )
    item = item.model_copy(
        update={
            "sections": (
                item.sections[0].model_copy(update={"paragraphs": (paragraph,)}),
                *item.sections[1:],
            )
        }
    )
    receipt = contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)
    assert receipt.semantic_review == "required"


def test_exact_section_and_paragraph_population(connection: sqlite3.Connection) -> None:
    item = draft()
    with pytest.raises(ValueError, match="section_population"):
        contract.bind_report_draft(
            connection,
            item.model_copy(update={"sections": item.sections[:-1]}),
            required_titles=PRE_TITLES,
        )
    with pytest.raises(ValidationError, match="unique"):
        contract.bind_report_draft(
            connection,
            item.model_copy(update={"sections": (*item.sections, item.sections[0])}),
            required_titles=PRE_TITLES,
        )


def test_post_adapter_keeps_missing_pre_explicit(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    quarter = ReportedQuarter(
        ticker="SYNTH",
        list_type="portfolio",
        transcript_id=1,
        document_id=1,
        fiscal_period_type="Q4",
        period_end="2025-12-31",
        call_date=STAMP.date().isoformat(),
    )
    selected: list[ReportedQuarter] = []

    def baseline(_conn: sqlite3.Connection, supplied: ReportedQuarter) -> dict[str, object]:
        selected.append(supplied)
        return {
            "status": "unavailable",
            "artifact_id": None,
            "reason": "missing_exact_event_baseline",
        }

    monkeypatch.setattr(adapters, "pre_call_baseline", baseline)
    result = adapters.bind_post_earnings_draft(
        connection, draft(family="post").model_copy(update={"event_date": STAMP.date()}), quarter
    )
    assert selected == [quarter]
    assert result.baseline_status == "unavailable"
    assert result.baseline_binding == "not_verified"
    assert result.baseline_reason == "missing_exact_event_baseline"


def test_unsealed_native_snapshot_cannot_be_bound(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "unsealed-report.db")
    with sqlite3.connect(path) as conn:
        before = conn.total_changes
        with pytest.raises(ValueError):
            contract.bind_report_draft(conn, draft(), required_titles=PRE_TITLES)
        assert conn.total_changes == before


@pytest.mark.parametrize("damage", ["issuer", "future", "post_before_event"])
def test_identity_and_time_mismatches_fail_closed(
    connection: sqlite3.Connection, damage: str
) -> None:
    item = draft()
    titles = PRE_TITLES
    if damage == "issuer":
        item = item.model_copy(update={"issuer_id": "another-issuer"})
    elif damage == "future":
        item = item.model_copy(update={"drafted_at": datetime(2026, 3, 1, tzinfo=UTC)})
    else:
        item = draft(family="post")
        titles = POST_TITLES
    with pytest.raises(contract.ReportBindingError):
        contract.bind_report_draft(connection, item, required_titles=titles)


def test_selected_pre_baseline_is_not_promoted_to_bound_evidence(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    quarter = ReportedQuarter(
        ticker="SYNTH",
        list_type="portfolio",
        transcript_id=1,
        document_id=1,
        fiscal_period_type="Q4",
        period_end="2025-12-31",
        call_date=STAMP.date().isoformat(),
    )

    def baseline(_conn: sqlite3.Connection, _quarter: ReportedQuarter) -> dict[str, object]:
        return {"status": "selected", "artifact_id": 7, "reason": None, "output_sha256": "b" * 64}

    monkeypatch.setattr(adapters, "pre_call_baseline", baseline)
    item = draft(family="post").model_copy(update={"event_date": STAMP.date()})
    result = adapters.bind_post_earnings_draft(connection, item, quarter)
    assert result.baseline_status == "selected_legacy"
    assert result.baseline_artifact_id == 7
    assert result.baseline_binding == "not_verified"


@pytest.mark.parametrize("fail", [False, True])
def test_binding_closes_only_its_own_read_snapshot(
    connection: sqlite3.Connection, fail: bool
) -> None:
    item = draft()
    if fail:
        item = item.model_copy(update={"issuer_id": "wrong"})
    trace: list[str] = []
    connection.set_trace_callback(trace.append)
    assert not connection.in_transaction
    if fail:
        with pytest.raises(ValueError):
            contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)
    else:
        contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)
    assert not connection.in_transaction
    assert trace[0] == "BEGIN" and trace[-1] == "ROLLBACK"
    assert "COMMIT" not in trace


@pytest.mark.parametrize("fail", [False, True])
def test_binding_preserves_caller_transaction(connection: sqlite3.Connection, fail: bool) -> None:
    connection.execute("INSERT INTO evidence_nodes VALUES ('caller-work','uncommitted')")
    item = draft()
    if fail:
        item = item.model_copy(update={"issuer_id": "wrong"})
    trace: list[str] = []
    connection.set_trace_callback(trace.append)
    if fail:
        with pytest.raises(ValueError):
            contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)
    else:
        contract.bind_report_draft(connection, item, required_titles=PRE_TITLES)
    assert connection.in_transaction
    assert not any(sql in {"BEGIN", "COMMIT", "ROLLBACK"} for sql in trace)
    assert (
        connection.execute(
            "SELECT text FROM evidence_nodes WHERE node_id='caller-work'"
        ).fetchone()[0]
        == "uncommitted"
    )
    connection.rollback()
    assert (
        connection.execute("SELECT text FROM evidence_nodes WHERE node_id='caller-work'").fetchone()
        is None
    )


def test_post_baseline_and_binding_share_one_read_snapshot(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "snapshot.db"
    with sqlite3.connect(path) as target:
        connection.backup(target)
    reader = sqlite3.connect(path)
    writer = sqlite3.connect(path)
    reader.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA journal_mode=WAL")

    def baseline(conn: sqlite3.Connection, _quarter: ReportedQuarter) -> dict[str, object]:
        assert conn.in_transaction
        assert (
            conn.execute("SELECT text FROM evidence_nodes WHERE node_id='node-1'").fetchone()[0]
            == TEXT
        )
        writer.execute(
            "UPDATE evidence_nodes SET text='changed after baseline' WHERE node_id='node-1'"
        )
        writer.commit()
        return {
            "status": "unavailable",
            "reason": "missing_exact_event_baseline",
            "artifact_id": None,
        }

    monkeypatch.setattr(adapters, "pre_call_baseline", baseline)
    quarter = ReportedQuarter(
        ticker="SYNTH",
        list_type="portfolio",
        transcript_id=1,
        document_id=1,
        fiscal_period_type="Q4",
        period_end="2025-12-31",
        call_date=STAMP.date().isoformat(),
    )
    try:
        result = adapters.bind_post_earnings_draft(
            reader, draft(family="post").model_copy(update={"event_date": STAMP.date()}), quarter
        )
        assert result.report.binding_status == "verified"
        assert not reader.in_transaction
        assert (
            reader.execute("SELECT text FROM evidence_nodes WHERE node_id='node-1'").fetchone()[0]
            == "changed after baseline"
        )
    finally:
        reader.close()
        writer.close()
