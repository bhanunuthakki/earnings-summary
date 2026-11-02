"""The open-loops ritual-debt band — queue counts, doorways, ritual-clear state,
and the never-raises guarantee on thin/pre-migration DBs."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from capture import ingest
from capture.matcher import build_roster_index
from pipeline.open_loops import render_open_loops_band

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db = tmp_path / "loops.db"
    migrated_db(db)
    return db


def test_ritual_clear_on_empty_db(db_path: Path) -> None:
    html = render_open_loops_band(db_path)
    assert "Ritual clear" in html
    assert "cc-open-loops" in html


def _insert_decision_draft(db_path: Path, *, status: str = "awaiting_confirmation") -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decision_drafts (source_channel, idempotency_key, original_text, "
            "status, created_at, updated_at) VALUES ('tracker', 'idem:1', 'buy 10 NU', "
            "?, '2026-07-24T08:00:00', '2026-07-24T08:00:00')",
            (status,),
        )
        conn.commit()
    finally:
        conn.close()


def test_pending_confirmation_line_counts_and_doors(db_path: Path) -> None:
    _insert_decision_draft(db_path)
    html = render_open_loops_band(db_path)
    assert "Pending confirmations" in html
    assert 'href="/mobile/inbox"' in html
    assert "Ritual clear" not in html


def test_pending_confirmation_line_ignores_non_awaiting_status(db_path: Path) -> None:
    _insert_decision_draft(db_path, status="dismissed")
    html = render_open_loops_band(db_path)
    assert "Pending confirmations" not in html


def test_pending_confirmation_line_is_unconditional(db_path: Path) -> None:
    _insert_decision_draft(db_path)
    html = render_open_loops_band(db_path)
    assert "Pending confirmations" in html


def test_undispositioned_card_line(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, added_at) "
            "VALUES ('NU', 'Nu Holdings', 'evaluation', '2026-06-01')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts (ticker, scope, purpose, input_sha256, "
            "generated_at) VALUES ('NU', 'ticker', 'investment_decision_card', "
            "'sha', '2026-07-01T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Cards awaiting disposition" in html
    assert 'href="/mobile/inbox"' in html


def test_undispositioned_card_line_clears_once_dispositioned(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, added_at) "
            "VALUES ('NU', 'Nu Holdings', 'evaluation', '2026-06-01')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts (id, ticker, scope, purpose, input_sha256, "
            "generated_at) VALUES (1, 'NU', 'ticker', 'investment_decision_card', "
            "'sha', '2026-07-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, advice_artifact_id, source_artifact_id, "
            "made_at, created_at) VALUES ('NU', 'watch', 1, 1, '2026-07-02', '2026-07-02')"
        )
        conn.commit()
    finally:
        conn.close()
    assert "Cards awaiting disposition" not in render_open_loops_band(db_path)


def test_reconcile_line_counts_and_doors(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU"], phrases={})
    result = ingest.ingest_capture(
        channel="tray", text="seed thought", roster=roster, db_path=db_path
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE analyst_notes SET source_ref = 'seed:decision:1' WHERE id = ?",
            (result.note_id,),
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Reconcile" in html
    assert 'href="/#musings"' in html
    assert "Ritual clear" not in html


def test_proposed_tenets_flag_gated(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from synthesis.tenets import record_tenet

    record_tenet(body_md="Let winners run.", status="proposed", db_path=db_path)
    monkeypatch.delenv("LEDGER_WORLDVIEW", raising=False)
    assert "Tenets proposed" not in render_open_loops_band(db_path)
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    assert "Tenets proposed" in render_open_loops_band(db_path)


def test_never_raises_without_schema(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()  # a DB with no tables at all
    html = render_open_loops_band(bare)
    assert "Ritual clear" in html


def test_red_team_escalation_banner_absent_with_no_deferred_items(db_path: Path) -> None:
    html = render_open_loops_band(db_path)
    assert "escalated" not in html
    assert "k-well-bad" not in html


def test_red_team_escalation_banner_renders_on_second_defer(db_path: Path) -> None:
    from redteam import response, store
    from redteam.models import RedTeamLLMItem

    item_id = store.insert_item(
        db_path=db_path,
        run_key="red_team_2026_08",
        ticker="NU",
        lens="fx_translation",
        kind="per_name",
        item=RedTeamLLMItem(
            attack_md="Attack.", question_md="Q?", proposed_change_md="Change.", severity="high"
        ),
    )
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    html = render_open_loops_band(db_path)
    assert "Red Team: 1 item escalated" in html
    assert "k-well" in html
    assert "k-well-bad" in html
    assert 'href="/#red_team"' in html


def test_red_team_escalation_banner_clears_once_answered(db_path: Path) -> None:
    from redteam import response, store
    from redteam.models import RedTeamLLMItem

    item_id = store.insert_item(
        db_path=db_path,
        run_key="red_team_2026_08",
        ticker="NU",
        lens="fx_translation",
        kind="per_name",
        item=RedTeamLLMItem(
            attack_md="Attack.", question_md="Q?", proposed_change_md="Change.", severity="high"
        ),
    )
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    assert "escalated" in render_open_loops_band(db_path)
    response.respond(db_path=db_path, item_id=item_id, action="accept")
    assert "escalated" not in render_open_loops_band(db_path)


# End of open-loop coverage.
