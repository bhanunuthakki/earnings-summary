"""Governed Copilot Ask proposal creation and explicit approval authority."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from ask.exchange_store import (  # noqa: E402
    ExchangeArtifactsV1,
    SessionContextV1,
    begin_exchange,
    complete_exchange,
    fail_exchange,
    hash_request_payload,
    put_session_context,
)
from ask.store import delete_session, ensure_session  # noqa: E402
from research.apply import apply_governed_proposal  # noqa: E402
from research.proposal_approval import (  # noqa: E402
    AskProposalDecisionV1,
    ProposalConflictError,
    ProposalValidationError,
    TargetDriftError,
    bind_ask_proposal_events,
    create_ask_proposal,
    decide_ask_proposal,
    get_ask_proposal_detail,
)
from research.proposals import get_proposal  # noqa: E402


def _holdings_payload(*, thesis: str = "Old thesis") -> dict[str, object]:
    return {
        "ticker": "NU",
        "name": "Nu Holdings",
        "thesis": thesis,
        "tier_1_kpis": [{"name": "NIM", "source": "earnings release"}],
        "tier_2_kpis": [],
        "tier_3_kpis": [],
        "break_rules": [],
        "business_model_rules": [],
        "break_rules_soft": [],
    }


@pytest.fixture
def authority(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "repo"
    holdings = repo_root / "micro_thesis" / "holdings" / "NU.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text(json.dumps(_holdings_payload(), indent=2), encoding="utf-8")
    db_path = migrated_db(tmp_path / "authority.db", target="head")
    return repo_root, db_path, holdings


def _thesis_diff(*, old: str = "Old thesis", new: str = "New thesis") -> dict[str, object]:
    return {
        "target_file": "micro_thesis/holdings/NU.json",
        "target_path": "/thesis",
        "old_value": old,
        "new_value": new,
        "summary": "Refresh the NU thesis",
    }


def _decision(proposal_id: int, *, request_id: str = "decision-1", revision: int = 0):
    return AskProposalDecisionV1(
        proposal_id=proposal_id,
        decision="approve",
        expected_proposal_revision=revision,
        decision_request_id=request_id,
    )


def _create_active(diff: dict[str, object], *, repo_root: Path, db_path: Path):
    session = ensure_session("test-session", scope="portfolio", db_path=db_path)
    put_session_context(session.id, SessionContextV1(company_ticker="NU"), db_path=db_path)
    begun = begin_exchange(
        session_id=session.id,
        request_id="test-exchange",
        payload_sha256=hash_request_payload({"query": "test proposal"}),
        user_text="test proposal",
        expected_revision=0,
        db_path=db_path,
    )
    ref = create_ask_proposal(
        diff,
        repo_root=repo_root,
        db_path=db_path,
        exchange_request_id="test-exchange",
    )
    complete_exchange(
        request_id="test-exchange",
        assistant_text="proposal ready",
        artifacts=ExchangeArtifactsV1(proposal_ref=ref),
        expected_revision=begun.exchange.session_revision,
        db_path=db_path,
    )
    return get_ask_proposal_detail(ref.proposal_id, db_path=db_path) or ref


def test_approve_applies_thesis_atomically_and_synchronizes_mirrors(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)

    receipt = decide_ask_proposal(_decision(ref.proposal_id), repo_root=repo_root, db_path=db_path)

    assert receipt.status == "approved"
    assert receipt.proposal_revision == 1
    assert receipt.applied is True
    assert receipt.replayed is False
    assert json.loads(holdings.read_text(encoding="utf-8"))["thesis"] == "New thesis"
    with sqlite3.connect(db_path) as connection:
        mirrored = connection.execute(
            "SELECT thesis,raw_json FROM thesis_state WHERE ticker='NU'"
        ).fetchone()
        ledger = connection.execute(
            "SELECT entry_kind,body FROM thesis_ledger_entries WHERE ticker='NU'"
        ).fetchall()
    assert mirrored is not None and mirrored[0] == "New thesis"
    assert json.loads(str(mirrored[1]))["thesis"] == "New thesis"
    assert ledger == [("thesis_update", "New thesis")]


def test_decision_replay_is_idempotent_and_status_revision_are_cas(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, _holdings = authority
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)
    request = _decision(ref.proposal_id)
    first = decide_ask_proposal(request, repo_root=repo_root, db_path=db_path)
    replay = decide_ask_proposal(request, repo_root=repo_root, db_path=db_path)

    assert replay.model_copy(update={"replayed": False}) == first
    assert replay.replayed is True
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM thesis_ledger_entries WHERE ticker='NU'"
        ).fetchone() == (1,)
    with pytest.raises(ProposalConflictError):
        decide_ask_proposal(
            _decision(ref.proposal_id, request_id="decision-2", revision=0),
            repo_root=repo_root,
            db_path=db_path,
        )
    with pytest.raises(ProposalConflictError):
        decide_ask_proposal(
            _decision(ref.proposal_id, request_id="decision-3", revision=1),
            repo_root=repo_root,
            db_path=db_path,
        )


def test_target_drift_fails_closed_without_changing_proposal(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)
    holdings.write_text(json.dumps(_holdings_payload(thesis="External change")), encoding="utf-8")

    with pytest.raises(TargetDriftError) as raised:
        decide_ask_proposal(_decision(ref.proposal_id), repo_root=repo_root, db_path=db_path)

    assert raised.value.expected_target_sha256 != raised.value.actual_target_sha256
    detail = get_ask_proposal_detail(ref.proposal_id, db_path=db_path)
    assert detail is not None and detail.status == "pending" and detail.proposal_revision == 0


def test_retry_recovers_when_file_post_hash_is_already_present(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)

    def _crash() -> None:
        raise RuntimeError("simulated process loss after atomic replacement")

    with pytest.raises(RuntimeError, match="simulated process loss"):
        decide_ask_proposal(
            _decision(ref.proposal_id),
            repo_root=repo_root,
            db_path=db_path,
            after_replace=_crash,
        )
    assert json.loads(holdings.read_text(encoding="utf-8"))["thesis"] == "New thesis"

    recovered = decide_ask_proposal(
        _decision(ref.proposal_id), repo_root=repo_root, db_path=db_path
    )
    assert recovered.status == "approved" and recovered.applied is True


@pytest.mark.parametrize(
    "diff",
    [
        {**_thesis_diff(), "target_file": "directives/design_language.md"},
        {**_thesis_diff(), "target_path": "/break_rules"},
        {**_thesis_diff(), "old_value": "not the current thesis"},
    ],
)
def test_invalid_operation_path_or_precondition_is_rejected(
    authority: tuple[Path, Path, Path], diff: dict[str, object]
) -> None:
    repo_root, db_path, _holdings = authority
    with pytest.raises(ProposalValidationError):
        create_ask_proposal(
            diff,
            repo_root=repo_root,
            db_path=db_path,
            exchange_request_id="test-exchange",
        )


def test_kpi_proposal_is_typed_bounded_and_sanitized(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    old = _holdings_payload()["tier_1_kpis"]
    diff = {
        "target_file": "micro_thesis/holdings/NU.json",
        "target_path": "/tier_1_kpis",
        "old_value": old,
        "new_value": [
            {"name": "**NIM**", "source": "earnings release"},
            {"name": "Approval-only KPI", "source": "earnings release"},
        ],
        "summary": "Normalize the primary KPI",
    }
    ref = _create_active(diff, repo_root=repo_root, db_path=db_path)
    detail = get_ask_proposal_detail(ref.proposal_id, db_path=db_path)
    assert detail is not None and detail.kind == "kpi"
    assert detail.model_dump(mode="json")["new_value"] == [
        {"name": "NIM", "source": "earnings release"},
        {"name": "Approval-only KPI", "source": "earnings release"},
    ]

    request = _decision(ref.proposal_id)
    decide_ask_proposal(request, repo_root=repo_root, db_path=db_path)
    decide_ask_proposal(request, repo_root=repo_root, db_path=db_path)

    assert json.loads(holdings.read_text(encoding="utf-8"))["tier_1_kpis"] == [
        {"name": "NIM", "source": "earnings release"},
        {"name": "Approval-only KPI", "source": "earnings release"},
    ]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT unit,primary_source,threshold_tier FROM kpi_definitions "
            "WHERE ticker='NU' AND name='Approval-only KPI'"
        ).fetchone() == ("percent", "ir_doc", "tier_1_break")
        assert connection.execute(
            "SELECT COUNT(*) FROM user_kpi_registry "
            "WHERE ticker='NU' AND kpi_name='Approval-only KPI'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM thesis_ledger_entries "
            "WHERE ticker='NU' AND entry_kind='kpi_update'"
        ).fetchone() == (1,)


def test_kpi_approval_reconciles_removals_without_destroying_facts(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    payload = _holdings_payload()
    payload["tier_2_kpis"] = [{"name": "Removed KPI"}]
    holdings.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS trg_kpi_facts_observation_insert")
        connection.execute(
            "INSERT INTO documents (ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES ('NU','ir_doc','earnings_release','removed.txt','removed-sha',"
            "'2026-08-01','ok',0)"
        )
        document_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO kpi_definitions "
            "(ticker,name,unit,primary_source,threshold_tier,definition_origin) "
            "VALUES ('NU','Removed KPI','actual','ir_doc','tier_2_monitor','analyst')"
        )
        definition_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO kpi_facts (ticker,period_end,fiscal_period_type,kpi_definition_id,"
            "value,unit,source_doc_id) VALUES ('NU','2026-06-30','Q2',?,1,'count',?)",
            (definition_id, document_id),
        )
        connection.execute(
            "INSERT INTO user_kpi_registry "
            "(user_id,ticker,kpi_name,is_thesis_breaker,scaffold_source,created_at,updated_at) "
            "VALUES ('bhanu','NU','Removed KPI',1,'copilot_ask_approval',"
            "'2026-08-01','2026-08-01')"
        )
        connection.execute(
            "INSERT INTO kpi_definitions "
            "(ticker,name,unit,primary_source,threshold_tier,notes,definition_origin) "
            "VALUES ('NU','Unrelated KPI','count','manual_entry','tier_1_break',"
            "'external notes','capture')"
        )
        unrelated_definition_id = int(
            connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        )
        connection.execute(
            "INSERT INTO user_kpi_registry "
            "(user_id,ticker,kpi_name,is_thesis_breaker,scaffold_source,notes,created_at,updated_at) "
            "VALUES ('bhanu','NU','Unrelated KPI',1,'manual','external registry',"
            "'2026-01-01','2026-01-01')"
        )
        unrelated_registry_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        unrelated_definition_before = connection.execute(
            "SELECT * FROM kpi_definitions WHERE id=?", (unrelated_definition_id,)
        ).fetchone()
        unrelated_registry_before = connection.execute(
            "SELECT * FROM user_kpi_registry WHERE id=?", (unrelated_registry_id,)
        ).fetchone()
        connection.commit()
    diff = {
        "target_file": "micro_thesis/holdings/NU.json",
        "target_path": "/tier_2_kpis",
        "old_value": [{"name": "Removed KPI"}],
        "new_value": [],
        "summary": "Remove the stale monitor",
    }
    ref = _create_active(diff, repo_root=repo_root, db_path=db_path)
    decide_ask_proposal(_decision(ref.proposal_id), repo_root=repo_root, db_path=db_path)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT unit,primary_source,threshold_tier,definition_origin "
            "FROM kpi_definitions WHERE id=?",
            (definition_id,),
        ).fetchone() == ("actual", "ir_doc", None, "analyst")
        assert connection.execute(
            "SELECT COUNT(*) FROM kpi_facts WHERE kpi_definition_id=?", (definition_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM user_kpi_registry WHERE ticker='NU' AND kpi_name='Removed KPI'"
        ).fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT * FROM kpi_definitions WHERE id=?", (unrelated_definition_id,)
            ).fetchone()
            == unrelated_definition_before
        )
        assert (
            connection.execute(
                "SELECT * FROM user_kpi_registry WHERE id=?", (unrelated_registry_id,)
            ).fetchone()
            == unrelated_registry_before
        )


def test_kpi_approval_reuses_recorded_fact_unit_and_rejects_cross_tier_duplicate(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS trg_kpi_facts_observation_insert")
        connection.execute(
            "INSERT INTO documents (ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES ('NU','ir_doc','earnings_release','unit.txt','unit-sha','2026-08-01','ok',0)"
        )
        document_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO kpi_definitions "
            "(ticker,name,unit,primary_source,notes,definition_origin) "
            "VALUES ('NU','Customer metric','actual','manual_entry','external notes','capture')"
        )
        definition_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO kpi_facts (ticker,period_end,fiscal_period_type,kpi_definition_id,"
            "value,unit,source_doc_id) VALUES ('NU','2026-06-30','Q2',?,10,'count',?)",
            (definition_id, document_id),
        )
        connection.execute(
            "INSERT INTO user_kpi_registry "
            "(user_id,ticker,kpi_name,is_thesis_breaker,scaffold_source,notes,created_at,updated_at) "
            "VALUES ('bhanu','NU','Customer metric',1,'manual','external registry',"
            "'2026-01-01','2026-01-01')"
        )
        registry_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        registry_before = connection.execute(
            "SELECT * FROM user_kpi_registry WHERE id=?", (registry_id,)
        ).fetchone()
        connection.commit()
    add_diff = {
        "target_file": "micro_thesis/holdings/NU.json",
        "target_path": "/tier_2_kpis",
        "old_value": [],
        "new_value": [{"name": "Customer metric"}],
        "summary": "Add the customer monitor",
    }
    ref = _create_active(add_diff, repo_root=repo_root, db_path=db_path)
    receipt = decide_ask_proposal(_decision(ref.proposal_id), repo_root=repo_root, db_path=db_path)
    assert "follow-up required for 1 externally owned" in receipt.message
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT unit,primary_source,notes,definition_origin FROM kpi_definitions WHERE id=?",
            (definition_id,),
        ).fetchone() == ("count", "manual_entry", "external notes", "capture")
        assert (
            connection.execute(
                "SELECT * FROM user_kpi_registry WHERE id=?", (registry_id,)
            ).fetchone()
            == registry_before
        )

    # A fresh fixture/session is required because the first proposal is terminal.
    payload = json.loads(holdings.read_text(encoding="utf-8"))
    payload["tier_3_kpis"] = [{"name": "NIM"}]
    holdings.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProposalValidationError, match="appears in both"):
        from research.proposal_approval import apply_canonical_ask_change

        proposal = get_proposal(ref.proposal_id, db_path=db_path)
        assert proposal is not None
        with sqlite3.connect(db_path) as connection:
            apply_canonical_ask_change(proposal, repo_root=repo_root, connection=connection)


def test_explicit_decision_is_the_only_higher_bar_override(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)
    prop = get_proposal(ref.proposal_id, db_path=db_path)

    blocked = apply_governed_proposal(
        prop,
        proposal_id=ref.proposal_id,
        db_path=db_path,
        steer_authorized=False,
    )

    assert isinstance(blocked, str) and blocked.startswith("blocked (higher bar)")
    assert json.loads(holdings.read_text(encoding="utf-8"))["thesis"] == "Old thesis"
    receipt = decide_ask_proposal(_decision(ref.proposal_id), repo_root=repo_root, db_path=db_path)
    assert receipt.status == "approved"


def test_failed_exchange_invalidates_orphan_proposal(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, _holdings = authority
    session = ensure_session("orphan-session", scope="portfolio", db_path=db_path)
    put_session_context(session.id, SessionContextV1(company_ticker="NU"), db_path=db_path)
    begun = begin_exchange(
        session_id=session.id,
        request_id="orphan-exchange",
        payload_sha256=hash_request_payload({"query": "change thesis"}),
        user_text="change thesis",
        expected_revision=0,
        db_path=db_path,
    )
    events = list(
        bind_ask_proposal_events(
            iter([{"type": "diff_proposal", "diff": _thesis_diff()}]),
            repo_root=repo_root,
            db_path=db_path,
            exchange_request_id=begun.exchange.request_id,
        )
    )
    reference = events[0]["proposal_ref"]
    assert isinstance(reference, dict) and reference["allowed_actions"] == []

    fail_exchange(
        request_id=begun.exchange.request_id,
        error_code="engine_error",
        expected_revision=begun.exchange.session_revision,
        db_path=db_path,
    )

    detail = get_ask_proposal_detail(int(reference["proposal_id"]), db_path=db_path)
    assert detail is not None
    assert detail.status == "superseded"
    assert detail.allowed_actions == []


def test_deleted_exchange_makes_stale_decision_url_non_actionable(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, _holdings = authority
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)
    with sqlite3.connect(db_path) as connection:
        session_id = str(
            connection.execute(
                "SELECT session_id FROM ask_exchanges WHERE request_id='test-exchange'"
            ).fetchone()[0]
        )

    assert delete_session(session_id, db_path=db_path) is True

    detail = get_ask_proposal_detail(ref.proposal_id, db_path=db_path)
    assert detail is not None and detail.status == "superseded"
    assert detail.allowed_actions == []
    with pytest.raises(ProposalConflictError) as raised:
        decide_ask_proposal(_decision(ref.proposal_id), repo_root=repo_root, db_path=db_path)
    assert raised.value.code == "revision_conflict"


def test_legacy_chat_cutover_never_mutates_files_or_database(
    authority: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, db_path, holdings = authority
    legacy_thread = repo_root / "output" / "NU" / "2026-08-01" / "chat.json"
    legacy_thread.parent.mkdir(parents=True)
    legacy_thread.write_text('[{"role":"user","text":"old"}]', encoding="utf-8")
    before_files = {path: path.read_bytes() for path in (holdings, legacy_thread)}
    with sqlite3.connect(db_path) as connection:
        before_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ask_turns", "research_proposals")
        )
    monkeypatch.setattr(
        comments_server,
        "respond_turn",
        lambda *_args, **_kwargs: pytest.fail("retired chat invoked the Ask engine"),
    )
    client = comments_server.create_app(repo_root, db_path=db_path).test_client()

    responses = [
        client.get("/chat/NU?report_date=2026-08-01"),
        client.post("/chat/NU", json={"report_date": "2026-08-01", "message": "hello"}),
        client.post(
            "/chat/NU/apply",
            json={"report_date": "2026-08-01", "diff": {"target_path": "/thesis"}},
        ),
    ]

    assert [response.status_code for response in responses] == [410, 410, 410]
    assert all(
        response.get_json()["schema_version"] == "chat_migrated.v1" for response in responses
    )
    assert {path: path.read_bytes() for path in before_files} == before_files
    with sqlite3.connect(db_path) as connection:
        after_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ask_turns", "research_proposals")
        )
    assert after_counts == before_counts


def _sse_events(response_text: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def _durable_payload(*, request_id: str, revision: int = 0) -> dict[str, object]:
    return {
        "query": "Update the thesis",
        "request_id": request_id,
        "expected_revision": revision,
        "session_context": {
            "company_ticker": "NU",
            "coverage_role_at_creation": "portfolio",
            "lifecycle_at_creation": "active",
            "category": "thesis",
        },
        "research_context": {"capability_id": "research.thesis.edit"},
    }


def test_routes_emit_durable_ref_live_and_replay_and_expose_exact_detail(
    authority: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, db_path, _holdings = authority
    client: FlaskClient = comments_server.create_app(repo_root, db_path=db_path).test_client()

    def _respond(*_args: object, **_kwargs: object):
        yield {"type": "final", "text": "I prepared the governed change."}
        yield {"type": "diff_proposal", "diff": _thesis_diff()}

    monkeypatch.setattr(comments_server, "build_portfolio_pack", lambda *_a: SimpleNamespace())
    monkeypatch.setattr(comments_server, "respond_turn", _respond)
    monkeypatch.setattr(comments_server, "ask_retrieval_mode", lambda: "legacy")

    live = client.post("/api/ask/stream", json=_durable_payload(request_id="ask-1"))
    live_events = _sse_events(live.get_data(as_text=True))
    ref = next(event["proposal_ref"] for event in live_events if event["type"] == "proposal_ref")
    assert ref["schema_version"] == "ask_proposal_ref.v1"
    assert isinstance(ref["proposal_id"], int)
    assert ref["allowed_actions"] == ["approve", "reject"]
    assert [event["type"] for event in live_events][-2:] == ["proposal_ref", "final"]

    detail = client.get(ref["detail_url"])
    assert detail.status_code == 200
    assert detail.get_json()["new_value"] == "New thesis"
    session_id = live_events[0]["session_id"]
    hydrated = client.get(f"/api/ask/sessions/{session_id}").get_json()
    assert hydrated["exchange_artifacts"] == [
        {
            "schema_version": "session_exchange_artifact.v1",
            "exchange_id": "ask-1",
            "request_id": "ask-1",
            "assistant_turn_id": hydrated["exchange_artifacts"][0]["assistant_turn_id"],
            "session_revision": 2,
            "completed_at": hydrated["exchange_artifacts"][0]["completed_at"],
            "artifacts": {
                "schema_version": "exchange_artifacts.v1",
                "route": None,
                "view_spec": None,
                "proposal_ref": ref,
                "proposal_error": None,
                "source_links": [],
                "fact_links": [],
            },
        }
    ]
    assistant_turn = next(turn for turn in hydrated["turns"] if turn["role"] == "assistant")
    assert assistant_turn["id"] == hydrated["exchange_artifacts"][0]["assistant_turn_id"]

    replay_payload = _durable_payload(request_id="ask-1", revision=2)
    replay_payload["session_id"] = live_events[0]["session_id"]
    replay_payload["session_context"] = live_events[0]["session_context"]
    replay = client.post("/api/ask/stream", json=replay_payload)
    replay_refs = [
        event["proposal_ref"]
        for event in _sse_events(replay.get_data(as_text=True))
        if event["type"] == "proposal_ref"
    ]
    assert replay_refs == [ref]


def test_proposal_error_is_identical_live_replay_and_session_reload(
    authority: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, db_path, _holdings = authority
    client = comments_server.create_app(repo_root, db_path=db_path).test_client()

    def _respond(*_args: object, **_kwargs: object):
        yield {"type": "final", "text": "The answer remains available."}
        yield {
            "type": "diff_proposal",
            "diff": {**_thesis_diff(), "target_file": "directives/forbidden.md"},
        }

    monkeypatch.setattr(comments_server, "build_portfolio_pack", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(comments_server, "respond_turn", _respond)
    monkeypatch.setattr(comments_server, "ask_retrieval_mode", lambda: "legacy")
    live = client.post("/api/ask/stream", json=_durable_payload(request_id="proposal-error"))
    live_events = _sse_events(live.get_data(as_text=True))
    live_error = next(event for event in live_events if event["type"] == "proposal_error")
    assert live_error["code"] == "registration_failed"

    session_id = live_events[0]["session_id"]
    replay_payload = _durable_payload(request_id="proposal-error", revision=2)
    replay_payload["session_id"] = session_id
    replay_payload["session_context"] = live_events[0]["session_context"]
    replay = client.post("/api/ask/stream", json=replay_payload)
    replay_error = next(
        event
        for event in _sse_events(replay.get_data(as_text=True))
        if event["type"] == "proposal_error"
    )
    assert replay_error["code"] == live_error["code"]
    assert replay_error["message"] == live_error["message"]
    hydrated = client.get(f"/api/ask/sessions/{session_id}").get_json()
    stored = hydrated["exchange_artifacts"][0]["artifacts"]["proposal_error"]
    assert stored == {
        "schema_version": "proposal_error.v1",
        "code": live_error["code"],
        "message": live_error["message"],
    }


def test_decision_route_maps_revision_conflict_and_target_drift(
    authority: tuple[Path, Path, Path],
) -> None:
    repo_root, db_path, holdings = authority
    client = comments_server.create_app(repo_root, db_path=db_path).test_client()
    ref = _create_active(_thesis_diff(), repo_root=repo_root, db_path=db_path)
    body = _decision(ref.proposal_id).model_dump(mode="json")

    stale = dict(body)
    stale["expected_proposal_revision"] = 9
    response = client.post(ref.decision_url, json=stale)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "revision_conflict"

    holdings.write_text(json.dumps(_holdings_payload(thesis="Drifted")), encoding="utf-8")
    response = client.post(ref.decision_url, json=body)
    assert response.status_code == 412
    assert response.get_json()["error"]["code"] == "target_drift"
    assert len(response.get_json()["error"]["actual_target_sha256"]) == 64
