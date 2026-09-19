"""Owner falsifiers → the break-condition engine (task 9, 2026-07-02 grill).

Three behaviors:
- attach_conditions extracts an OWNER row's conditions from its ``falsifier``
  column (not the rationale), skips unratified '(inferred)' falsifiers with
  zero LLM spend, and stamps portfolio-scope rows '[]'
- load_open_decisions keeps a graded owner decision's falsifier evaluable
  while the ticker is still a portfolio holding (an advisor row retires on
  grading as before)
- close_intent stamps closure provenance (the claude_session channel's write)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

import decision_conditions as dc
from decision_conditions import attach_conditions, load_open_decisions
from research.decision_feed import persist_owner_decision
from synthesis.reconcile import close_intent

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    path = migrated_db(tmp_path / "portfolio.db")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('NU', 'Nu', 'portfolio')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('MU', 'Micron', 'index_member')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _condition_obj() -> dict[str, object]:
    return {
        "metric": "NPL 15-90d",
        "metric_source": "kpi",
        "op": "gt",
        "threshold": 5.0,
        "unit": "percent",
        "for_periods": 2,
        "note": "15-90d NPL above 5% for two straight quarters",
    }


def test_owner_falsifier_feeds_extraction_not_rationale(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_owner_decision(
        ticker="NU",
        direction="add",
        conviction="high",
        falsifier="15-90d NPL >5% for 2Q",
        rationale="Credit book compounding; the rationale is NOT a tripwire.",
        db_path=db,
    )
    prompts: list[str] = []

    def fake_structured(prompt: str, **kwargs: object) -> object:
        prompts.append(prompt)
        return [_condition_obj()]

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    tally = attach_conditions(db_path=db)
    assert tally["extracted"] == 1
    assert "15-90d NPL >5% for 2Q" in prompts[0]
    assert "NOT a tripwire" not in prompts[0]  # rationale never reaches the extractor

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT decision_conditions FROM decisions WHERE decided_by='owner'"
        ).fetchone()
        assert json.loads(row[0])[0]["metric"] == "NPL 15-90d"
    finally:
        conn.close()


def test_inferred_falsifier_waits_for_ratification(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_owner_decision(
        ticker="MU",
        direction="sell",
        falsifier="Memory cycle rolls over. (inferred)",
        db_path=db,
    )

    def exploding(prompt: str, **kwargs: object) -> object:
        raise AssertionError("LLM called on an unratified falsifier")

    monkeypatch.setattr(dc, "call_llm_structured", exploding)
    tally = attach_conditions(db_path=db)
    assert tally["awaiting_ratification"] == 1
    assert tally["extracted"] == 0
    conn = sqlite3.connect(str(db))
    try:
        # unstamped — retried after the reconcile pass strips the marker
        assert (
            conn.execute(
                "SELECT conditions_extracted_at FROM decisions WHERE ticker='MU'"
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_portfolio_scope_row_stamps_empty(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, scope, "
            "falsifier, made_at, created_at) VALUES "
            "(NULL,'trim','owner','portfolio','LatAm credit exposure > $150k',"
            "'2026-07-02','2026-07-02')"
        )
        conn.commit()
    finally:
        conn.close()

    def exploding(prompt: str, **kwargs: object) -> object:
        raise AssertionError("LLM called on a portfolio-scope row")

    monkeypatch.setattr(dc, "call_llm_structured", exploding)
    tally = attach_conditions(db_path=db)
    assert tally["no_section"] == 1


def test_owner_falsifier_outlives_grading_while_held(db: Path) -> None:
    conditions = json.dumps([_condition_obj()])
    conn = sqlite3.connect(str(db))
    try:
        # Graded OWNER decision on a held name (NU = portfolio) → still open
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "decision_conditions, outcome_at, outcome_label, made_at, created_at) VALUES "
            "('NU','add','owner',?, '2026-07-01','wrong','2026-03-15','2026-07-02')",
            (conditions,),
        )
        # Graded ADVISOR decision → retired on grading, as before
        conn.execute(
            "INSERT INTO llm_artifacts (ticker, purpose, content_md, generated_at, input_sha256) "
            "VALUES ('NU','lens:five_min_reread','x','2026-05-01','sha_nu_lens')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
            "decision_conditions, outcome_at, outcome_label, made_at, created_at) VALUES "
            "('NU','hold',1,?, '2026-07-01','correct','2026-05-01','2026-07-02')",
            (conditions,),
        )
        # Graded OWNER decision on an UNHELD name (MU = index_member) → retired
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "decision_conditions, outcome_at, outcome_label, made_at, created_at) VALUES "
            "('MU','sell','owner',?, '2026-07-01','wrong','2025-12-15','2026-07-02')",
            (conditions,),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        nu_open = load_open_decisions(conn, "NU")
        mu_open = load_open_decisions(conn, "MU")
    finally:
        conn.close()
    assert [d.recommendation_kind for d in nu_open] == ["add"]  # owner survives, advisor retired
    assert mu_open == []


def test_close_intent_stamps_provenance(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, source_ref, created_at, "
            "updated_at) VALUES ('intent','LEAP sleeve','capture','seed:intent:leap-sleeve',"
            "'2026-07-01','2026-07-01')"
        )
        conn.commit()
    finally:
        conn.close()

    ok = close_intent(
        "seed:intent:leap-sleeve",
        "resolved-rejected",
        reason="deletes the NVO hedge",
        closed_by="claude_session:test",
        db_path=db,
    )
    assert ok
    conn = sqlite3.connect(str(db))
    try:
        status, ctx = conn.execute(
            "SELECT status, context_json FROM analyst_notes WHERE source_ref=?",
            ("seed:intent:leap-sleeve",),
        ).fetchone()
        assert status == "resolved"
        parsed = json.loads(ctx)
        assert parsed["closed_by"] == "claude_session:test"
        assert parsed["status"] == "resolved-rejected"
    finally:
        conn.close()
    # Idempotent: already-resolved intents don't re-close
    assert not close_intent(
        "seed:intent:leap-sleeve",
        "done",
        reason="x",
        closed_by="y",
        db_path=db,
    )
    with pytest.raises(ValueError):
        close_intent("seed:intent:leap-sleeve", "live", reason="x", closed_by="y", db_path=db)
