"""Owner diff registration shares the governed Ask decision and apply authorities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from execution.register_owner_proposal import main
from research.proposal_approval import (
    AskProposalDecisionV1,
    ProposalConflictError,
    ProposalValidationError,
    TargetDriftError,
    create_owner_proposal,
    decide_ask_proposal,
    get_ask_proposal_detail,
    preview_owner_proposal,
)
from research.proposals import create_proposal, get_proposal

PRIOR = "0059_scoped_research_universe_commitments"
CURRENT = "0060_owner_governed_proposals"


@pytest.fixture
def authority(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path, Path]:
    root = tmp_path / "state"
    holdings = root / "micro_thesis/holdings/NU.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text(
        json.dumps(
            {
                "ticker": "NU",
                "name": "Nu Holdings",
                "thesis": "Old thesis",
                "tier_1_kpis": [],
                "tier_2_kpis": [],
                "tier_3_kpis": [],
                "break_rules": [],
                "business_model_rules": [],
                "break_rules_soft": [],
            }
        )
    )
    return root, migrated_db(tmp_path / "authority.db"), holdings


def _diff(kpi: bool = False) -> dict[str, object]:
    return {
        "target_file": "micro_thesis/holdings/NU.json",
        "target_path": "/tier_1_kpis" if kpi else "/thesis",
        "old_value": [] if kpi else "Old thesis",
        # Exercise whole-tier inputs beyond Ask's query limit.
        "new_value": [{"name": f"Metric {i}", "note": "x" * 1900} for i in range(6)]
        if kpi
        else "New thesis",
        "summary": "Owner reviewed diff",
    }


def _authorization() -> dict[str, object]:
    return {
        "authorization_id": "owner-review:1",
        "ticker": "NU",
        "user_id": "bhanu",
        "statement": "I reviewed this diff; register it for a separate decision.",
        "source_ref": "owner conversation 2026-10-21",
        "evidence": [{"path": "reviewed-diff.json", "sha256": "a" * 64}],
    }


def _register(root: Path, db: Path, *, kpi: bool = False):
    diff = _diff(kpi)
    preview = preview_owner_proposal(diff, repo_root=root, authorization=_authorization())
    return create_owner_proposal(
        diff, repo_root=root, db_path=db, authorization=preview.authorization
    )


def _decision(proposal_id: int, decision: str = "approve") -> AskProposalDecisionV1:
    return AskProposalDecisionV1.model_validate(
        {
            "proposal_id": proposal_id,
            "decision": decision,
            "expected_proposal_revision": 0,
            "decision_request_id": "decision:1",
        }
    )


@pytest.mark.parametrize("kpi", [False, True])
def test_register_and_approve_through_existing_routes(
    authority: tuple[Path, Path, Path],
    kpi: bool,
) -> None:
    root, db, holdings = authority
    ref = _register(root, db, kpi=kpi)
    assert ref.allowed_actions == ["approve", "reject"]
    prop = get_proposal(ref.proposal_id, db_path=db)
    assert prop is not None and prop.kind == ("owner_kpi_edit" if kpi else "owner_thesis_edit")
    assert prop.ask_exchange_request_id is None
    assert prop.owner_authorization_id == "owner-review:1"
    assert json.loads(prop.evidence_json) == _authorization()["evidence"]
    client = comments_server.create_app(root, db_path=db).test_client()
    detail = client.get(ref.detail_url)
    assert detail.status_code == 200
    request = _decision(ref.proposal_id).model_dump(mode="json")
    response = client.post(ref.decision_url, json=request)
    assert response.status_code == 200
    assert response.get_json()["applied"] is True
    assert client.post(ref.decision_url, json=request).get_json()["replayed"] is True
    payload = json.loads(holdings.read_text())
    key = "tier_1_kpis" if kpi else "thesis"
    assert payload[key] == _diff(kpi)["new_value"]
    with sqlite3.connect(db) as conn:
        mirror = conn.execute("SELECT raw_json FROM thesis_state WHERE ticker='NU'").fetchone()
        assert mirror is not None and json.loads(mirror[0])[key] == payload[key]
        assert conn.execute(
            "SELECT COUNT(*) FROM research_proposal_decision_receipts"
        ).fetchone() == (1,)
        ledger = conn.execute(
            "SELECT body FROM thesis_ledger_entries WHERE ticker='NU' AND entry_kind='thesis_update'"
        ).fetchall()
        assert ledger == ([] if kpi else [("New thesis",)])
        if kpi:
            assert conn.execute(
                "SELECT COUNT(*) FROM kpi_definitions WHERE ticker='NU' AND name LIKE 'Metric %'"
            ).fetchone() == (6,)


def test_drift_and_rejection(authority: tuple[Path, Path, Path]) -> None:
    root, db, holdings = authority
    ref = _register(root, db)
    holdings.write_text(holdings.read_text() + "\n")
    before = holdings.read_bytes()
    with pytest.raises(TargetDriftError):
        decide_ask_proposal(_decision(ref.proposal_id), repo_root=root, db_path=db)
    detail = get_ask_proposal_detail(ref.proposal_id, db_path=db)
    assert detail is not None and detail.status == "pending"
    receipt = decide_ask_proposal(_decision(ref.proposal_id, "reject"), repo_root=root, db_path=db)
    assert receipt.status == "rejected" and not receipt.applied
    assert holdings.read_bytes() == before


def test_hash_mismatch_and_atomic_registration(authority: tuple[Path, Path, Path]) -> None:
    root, db, _ = authority
    preview = preview_owner_proposal(_diff(), repo_root=root, authorization=_authorization())
    authorization = preview.authorization.model_copy(update={"canonical_content_sha256": "b" * 64})
    with pytest.raises(ProposalValidationError, match="hash"):
        create_owner_proposal(_diff(), repo_root=root, db_path=db, authorization=authorization)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_proposal_authorizations").fetchone() == (0,)
        # Force the second insert to fail; the authorization must roll back too.
        conn.execute(
            "CREATE TRIGGER fail_proposal BEFORE INSERT ON research_proposals BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        create_owner_proposal(
            _diff(), repo_root=root, db_path=db, authorization=preview.authorization
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_proposal_authorizations").fetchone() == (0,)


@pytest.mark.parametrize("kind", ["owner_thesis_edit", "ask_thesis_edit"])
def test_trigger_requires_registration_authority(
    authority: tuple[Path, Path, Path], kind: str
) -> None:
    _, db, _ = authority
    with pytest.raises(sqlite3.IntegrityError, match="invalid governed"):
        create_proposal(
            task_id=None,
            kind=kind,
            ticker="NU",
            title="test",
            body_md="test",
            canonical_content_json="{}",
            canonical_content_sha256="a" * 64,
            target_precondition_sha256="b" * 64,
            db_path=db,
        )


def test_authorization_and_link_are_immutable(authority: tuple[Path, Path, Path]) -> None:
    root, db, _ = authority
    ref = _register(root, db)
    with sqlite3.connect(db) as conn:
        for sql in [
            "UPDATE owner_proposal_authorizations SET statement='changed'",
            "DELETE FROM owner_proposal_authorizations",
            "UPDATE research_proposals SET owner_authorization_id=NULL",
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(sql)
        # The unchanged status CAS still forbids bypassing the decision revision.
        with pytest.raises(sqlite3.IntegrityError, match="CAS"):
            conn.execute(
                "UPDATE research_proposals SET status='approved' WHERE id=?", (ref.proposal_id,)
            )


@pytest.mark.parametrize(
    "change",
    [
        {"authorization_id": "-invalid"},
        {"evidence": []},
        {"evidence": [{"path": "a"}]},
        {"statement": ""},
        {"evidence_sha256": "b" * 64},
        {"ticker": "OTHER"},
    ],
)
def test_authorization_validation(
    authority: tuple[Path, Path, Path], change: dict[str, object]
) -> None:
    root, _, _ = authority
    with pytest.raises((ProposalValidationError, ValidationError)):
        preview_owner_proposal(
            _diff(), repo_root=root, authorization={**_authorization(), **change}
        )


def _config(db: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


def test_migration_round_trip(authority: tuple[Path, Path, Path]) -> None:
    root, db, _ = authority
    cfg = _config(db)
    command.downgrade(cfg, PRIOR)
    with sqlite3.connect(db) as conn:
        old = conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_research_proposal_%' ORDER BY name"
        ).fetchall()
        assert "owner_authorization_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(research_proposals)")
        }
    command.upgrade(cfg, CURRENT)
    command.downgrade(cfg, PRIOR)
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_research_proposal_%' ORDER BY name"
            ).fetchall()
            == old
        )
    command.upgrade(cfg, CURRENT)
    _register(root, db)
    with pytest.raises(RuntimeError, match="owner"):
        command.downgrade(cfg, PRIOR)


def test_cli_dry_run_and_registration(
    authority: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, db, holdings = authority
    diff_file, auth_file = root / "diff.json", root / "authorization.json"
    diff_file.write_text(json.dumps(_diff(True)))
    auth_file.write_text(json.dumps(_authorization()))
    argv = [
        "--db",
        str(db),
        "--repo-root",
        str(root),
        "--diff",
        str(diff_file),
        "--authorization",
        str(auth_file),
    ]
    before = db.read_bytes(), holdings.read_bytes()
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert len(preview["canonical_content_sha256"]) == 64
    assert (
        preview["target_precondition_sha256"] == hashlib.sha256(holdings.read_bytes()).hexdigest()
    )
    assert before == (db.read_bytes(), holdings.read_bytes())
    assert main([*argv, "--apply"]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["status"] == "pending" and registered["proposal_id"] > 0
    assert holdings.read_bytes() == before[1]


def test_decision_rechecks_authorization_match(authority: tuple[Path, Path, Path]) -> None:
    root, db, holdings = authority
    ref = _register(root, db)
    before = holdings.read_bytes()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE research_proposals SET ticker='OTHER' WHERE id=?", (ref.proposal_id,))
    with pytest.raises(ProposalConflictError, match="authorization"):
        decide_ask_proposal(_decision(ref.proposal_id), repo_root=root, db_path=db)
    assert holdings.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"ticker": "OTHER"},
        {"canonical_content_sha256": "b" * 64},
        {"target_precondition_sha256": None},
        {"actionable_at": None},
        {"ask_exchange_request_id": "some-exchange"},
        {"invalidated_at": "invalid"},
    ],
)
def test_owner_insert_checks_authorization_and_actionability(
    authority: tuple[Path, Path, Path],
    change: dict[str, object],
) -> None:
    root, db, _ = authority
    ref = _register(root, db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM research_proposals WHERE id=?", (ref.proposal_id,)
        ).fetchone()
        assert row is not None
        values = dict(row)
        values.pop("id")
        values.update(change)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with pytest.raises(sqlite3.IntegrityError, match="invalid governed"):
            conn.execute(
                f"INSERT INTO research_proposals ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )


@pytest.mark.parametrize(
    "change",
    [
        {"authorization_id": "-invalid"},
        {"statement": ""},
        {"source_ref": "x" * 513},
        {"evidence_json": "[]"},
        {"evidence_json": '[{"path":"a"}]'},
        {"evidence_json": '["not-an-object"]'},
    ],
)
def test_authorization_sql_boundary(
    authority: tuple[Path, Path, Path],
    change: dict[str, object],
) -> None:
    root, db, _ = authority
    _register(root, db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM owner_proposal_authorizations").fetchone()
        assert row is not None
        values = dict(row)
        values["authorization_id"] = "another-authorization"
        values.update(change)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO owner_proposal_authorizations ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
