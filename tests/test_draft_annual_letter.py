"""``execution/draft_annual_letter.py`` — evidence-pack assembly, the LLM-stubbed
draft, and idempotency (monthly_red_team.md Phase 3, PR7). NO live LLM calls —
``call_llm_structured`` is monkeypatched throughout."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import draft_annual_letter as dal  # noqa: E402

from llm.structured import StructuredParseError  # noqa: E402

_SCHEMA = """
CREATE TABLE thesis_ledger_entries (
    id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, entry_kind TEXT, body TEXT,
    source_alert_id INTEGER, created_at TEXT, accepted_at TEXT
);
CREATE TABLE position_entries (
    id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, entry_date TEXT, entry_price REAL,
    entry_conviction TEXT, entry_thesis_excerpt TEXT, entry_conditions TEXT,
    exit_date TEXT, exit_price REAL, exit_reason TEXT, lessons TEXT, outcome_vs_thesis TEXT,
    source TEXT DEFAULT 'sync', created_at TEXT, updated_at TEXT
);
CREATE TABLE red_team_items (
    id INTEGER PRIMARY KEY, run_key TEXT NOT NULL, ticker TEXT, lens TEXT NOT NULL,
    kind TEXT NOT NULL, attack_md TEXT NOT NULL, question_md TEXT NOT NULL,
    proposed_change_md TEXT NOT NULL, severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', defer_count INTEGER NOT NULL DEFAULT 0,
    response_md TEXT, responded_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY, ticker TEXT, live_price REAL, created_at TEXT,
    is_latest INTEGER DEFAULT 1, segment_name TEXT DEFAULT ''
);
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO thesis_ledger_entries (id,user_id,ticker,entry_kind,body,source_alert_id,created_at,accepted_at) "
        "VALUES (1,'bhanu','NVO','update','GLP-1 thesis update',NULL,'2026-03-01T00:00:00','2026-03-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO position_entries (id,user_id,ticker,entry_date,entry_price,entry_conviction,exit_date,exit_price,"
        "exit_reason,lessons,outcome_vs_thesis,source,created_at,updated_at) "
        "VALUES (1,'bhanu','NVO','2025-06-01',100,'high','2026-02-01',80,'thesis broke on US pricing',"
        "'should have sized smaller','broke','sync','2026-02-01T00:00:00','2026-02-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO red_team_items (run_key,ticker,lens,kind,attack_md,question_md,proposed_change_md,"
        "severity,status,response_md,responded_at,created_at) "
        "VALUES ('red_team_2026_04','NVO','model_vs_market','per_name','a','q','p','high','refuted',"
        "'refuted, thesis still holds','2026-04-05T00:00:00','2026-04-05T00:00:00')"
    )
    conn.commit()
    conn.close()
    return tmp_path


# ---------------------------------------------------------------------------
# evidence pack assembly
# ---------------------------------------------------------------------------


def test_evidence_pack_pulls_ledger_positions_and_redteam(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    ev = dal.build_evidence_pack(db_path=db, repo_root=repo, year=2026)
    assert any("NVO" in line and "GLP-1 thesis update" in line for line in ev.ledger_lines)
    assert any("CLOSED NVO" in line and "broke" in line for line in ev.position_lines)
    assert any("NVO" in line and "REFUTED" in line for line in ev.redteam_lines)


def test_evidence_pack_filters_to_year(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    # 2025: the position OPENED in 2025-06 shows; the ledger entry (2026-03) and
    # the CLOSED position + red-team response (both 2026) do not.
    ev_2025 = dal.build_evidence_pack(db_path=db, repo_root=repo, year=2025)
    assert ev_2025.ledger_lines == []
    assert any("OPENED NVO" in line for line in ev_2025.position_lines)
    assert not any("CLOSED" in line for line in ev_2025.position_lines)
    assert ev_2025.redteam_lines == []

    # 2024: nothing on file at all.
    ev_2024 = dal.build_evidence_pack(db_path=db, repo_root=repo, year=2024)
    assert ev_2024.ledger_lines == []
    assert ev_2024.position_lines == []
    assert ev_2024.redteam_lines == []


def test_render_evidence_md_shows_honest_empty_sections(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    ev = dal.build_evidence_pack(db_path=db, repo_root=repo, year=2025)
    md = dal.render_evidence_md(ev)
    assert md.count("(none on file)") >= 3


# ---------------------------------------------------------------------------
# draft_letter — the ONE LLM call, stubbed
# ---------------------------------------------------------------------------


def test_draft_letter_calls_structured_llm_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = repo / "data" / "portfolio.db"
    ev = dal.build_evidence_pack(db_path=db, repo_root=repo, year=2026)
    calls: list[dict[str, object]] = []

    def fake_call(prompt: str, **kwargs: object) -> dict[str, object]:
        calls.append({"prompt": prompt, **kwargs})
        return {"letter_md": "Dear self, this is the drafted letter."}

    monkeypatch.setattr(dal, "call_llm_structured", fake_call)
    letter = dal.draft_letter(evidence=ev, db_path=db)
    assert letter == "Dear self, this is the drafted letter."
    assert len(calls) == 1
    assert calls[0]["purpose"] == "annual_letter"
    assert "2026" in str(calls[0]["prompt"])


def test_draft_letter_raises_on_empty_letter(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = repo / "data" / "portfolio.db"
    ev = dal.build_evidence_pack(db_path=db, repo_root=repo, year=2026)
    monkeypatch.setattr(dal, "call_llm_structured", lambda *a, **k: {"letter_md": "   "})
    with pytest.raises(StructuredParseError):
        dal.draft_letter(evidence=ev, db_path=db)


# ---------------------------------------------------------------------------
# CLI: dry-run (zero LLM calls) + idempotency + write path
# ---------------------------------------------------------------------------


def test_cli_dry_run_makes_no_llm_call_and_writes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _blocked(*a: object, **k: object) -> object:
        raise AssertionError("dry-run must not call the LLM")

    monkeypatch.setattr(dal, "call_llm_structured", _blocked)
    db = repo / "data" / "portfolio.db"
    rc = dal.main(["--dry-run", "--year", "2026", "--repo-root", str(repo), "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "evidence pack" in out
    assert not (repo / "data" / "annual_letters" / "2026.md").exists()


def test_cli_writes_letter_and_is_idempotent(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        dal, "call_llm_structured", lambda *a, **k: {"letter_md": "First draft body."}
    )
    db = repo / "data" / "portfolio.db"
    rc = dal.main(["--year", "2026", "--repo-root", str(repo), "--db", str(db)])
    assert rc == 0
    out_path = repo / "data" / "annual_letters" / "2026.md"
    assert out_path.exists()
    body = out_path.read_text(encoding="utf-8")
    assert "First draft body." in body
    assert "Letter to self — 2026" in body

    # Second run without --force is a no-op (idempotency_key annual_letter_2026).
    monkeypatch.setattr(
        dal,
        "call_llm_structured",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call LLM again")),
    )
    capsys.readouterr()
    rc2 = dal.main(["--year", "2026", "--repo-root", str(repo), "--db", str(db)])
    assert rc2 == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_done"] is True
    assert "First draft body." in out_path.read_text(encoding="utf-8")


def test_cli_force_overwrites_existing_letter(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dal, "call_llm_structured", lambda *a, **k: {"letter_md": "v1"})
    db = repo / "data" / "portfolio.db"
    dal.main(["--year", "2026", "--repo-root", str(repo), "--db", str(db)])
    monkeypatch.setattr(dal, "call_llm_structured", lambda *a, **k: {"letter_md": "v2"})
    rc = dal.main(["--year", "2026", "--repo-root", str(repo), "--db", str(db), "--force"])
    assert rc == 0
    body = (repo / "data" / "annual_letters" / "2026.md").read_text(encoding="utf-8")
    assert "v2" in body
    assert "v1" not in body
