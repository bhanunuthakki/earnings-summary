"""Tests for the Opus nominator (src/llm/nominator.py — PR3 of
meta_eval_governance.md, §1.2 + the §10 Q2 owner decisions). All LLM calls are
DI'd fakes; the suite never spends."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm.nominator import (
    KIND_EXCLUDE,
    KIND_MODEL_DOWNGRADE,
    excluded_purposes,
    mark_nomination,
    newest_run_info,
    nomination_run_due,
    pending_nominations,
    run_nominator,
)
from llm.structured import StructuredParseError

_NOW = datetime.now(UTC).replace(tzinfo=None)
_RECENT = (_NOW - timedelta(days=1)).isoformat()


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY, called_at TEXT NOT NULL, purpose TEXT, ticker TEXT,
            scope TEXT, model TEXT NOT NULL DEFAULT 'm', prompt_sha256 TEXT NOT NULL,
            prompt_chars INTEGER NOT NULL DEFAULT 0, cost_estimate_usd REAL
        );
        CREATE TABLE model_eval_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            candidate TEXT NOT NULL, incumbent TEXT NOT NULL, verdict TEXT NOT NULL,
            run_id TEXT NOT NULL, parity_rate REAL, judge_agreement REAL,
            n_cases INTEGER, n_parity INTEGER, summary_json TEXT, recorded_at TEXT NOT NULL
        );
        CREATE TABLE optimizer_nominations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nomination_run_id TEXT NOT NULL,
            purpose TEXT NOT NULL, kind TEXT NOT NULL, priority INTEGER NOT NULL,
            headroom_usd_30d REAL, cost_usd_30d REAL, calls_30d INTEGER,
            incumbent_model TEXT NOT NULL, candidates_json TEXT NOT NULL DEFAULT '[]',
            rationale TEXT NOT NULL, risk_tier TEXT NOT NULL, suggested_min_n INTEGER,
            source TEXT NOT NULL, ladder_sha TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', expires_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE candidate_models (
            model_id TEXT PRIMARY KEY, family TEXT NOT NULL,
            input_usd_per_mtok REAL NOT NULL, output_usd_per_mtok REAL NOT NULL,
            promise REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'frontier_research',
            status TEXT NOT NULL DEFAULT 'active',
            source_url TEXT, notes TEXT, research_run_id TEXT,
            first_seen_at TEXT NOT NULL, verified_at TEXT NOT NULL
        );
        """
    )
    # Two production purposes: an expensive Sonnet one and a cheap Haiku one.
    for i in range(4):
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, ticker, scope, prompt_sha256,"
            " prompt_chars, cost_estimate_usd) VALUES (?, 'bear_case', 'META', NULL, ?, 1000, 25.0)",
            (_RECENT, f"bc{i}"),
        )
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, ticker, scope, prompt_sha256,"
        " prompt_chars, cost_estimate_usd) VALUES (?, 'qa_topics', 'NU', NULL, 'qt1', 500, 2.0)",
        (_RECENT,),
    )
    conn.commit()
    conn.close()
    return db_path


def _pending_rows(db_path: Path) -> list[tuple[str, str, str, str | None]]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT purpose, kind, status, expires_at FROM optimizer_nominations ORDER BY id"
    ).fetchall()
    conn.close()
    return [(str(a), str(b), str(c), d) for a, b, c, d in rows]


# ---------------------------------------------------------------------------
# Validation (fail-closed, closed vocabulary)
# ---------------------------------------------------------------------------


def _payload(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"nominations": rows}


def test_valid_nomination_accepted_and_persisted(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        assert kwargs.get("purpose") == "optimizer_nominator"
        assert kwargs.get("scope") == "meta_eval"
        return _payload(
            [
                {
                    "purpose": "bear_case",
                    "kind": "model_downgrade",
                    "priority": 1,
                    "candidates": ["claude-haiku-4-5-20251001", "gemini-3-flash-preview"],
                    "why": "high headroom",
                    "risk_tier": "candidate",
                    "suggested_min_n": 10,
                }
            ]
        )

    noms = run_nominator(db_path, struct=struct)
    assert len(noms) == 1
    nom = noms[0]
    assert nom.source == "opus"
    assert nom.purpose == "bear_case"
    # bear_case carries a static RISK note -> tier tightened to risky and the
    # risky floor raises suggested_min_n to 16 (tighten-only, never loosen).
    assert nom.risk_tier == "risky"
    assert nom.suggested_min_n == 16
    assert set(nom.candidates) == {"claude-haiku-4-5-20251001", "gemini-3-flash-preview"}
    rows = _pending_rows(db_path)
    assert rows and rows[0][2] == "pending"


def test_unknown_purpose_and_bad_candidates_dropped(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        return _payload(
            [
                {  # unknown purpose -> dropped
                    "purpose": "hallucinated_purpose",
                    "kind": "model_downgrade",
                    "priority": 1,
                    "candidates": ["claude-haiku-4-5-20251001"],
                    "why": "x",
                    "risk_tier": "safe",
                },
                {  # lateral/expensive candidate (opus > sonnet incumbent) -> row dropped
                    "purpose": "bear_case",
                    "kind": "model_downgrade",
                    "priority": 2,
                    "candidates": ["claude-opus-4-8"],
                    "why": "x",
                    "risk_tier": "safe",
                },
            ]
        )

    noms = run_nominator(db_path, struct=struct)
    # Both rows rejected -> deterministic fallback kicks in instead.
    assert noms
    assert all(n.source == "deterministic_fallback" for n in noms)


def test_exclusion_gets_ttl(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        return _payload(
            [
                {
                    "purpose": "qa_topics",
                    "kind": "exclude",
                    "priority": 1,
                    "candidates": [],
                    "why": "three KEEP streaks, no headroom",
                    "risk_tier": "safe",
                }
            ]
        )

    noms = run_nominator(db_path, struct=struct)
    assert len(noms) == 1
    assert noms[0].kind == KIND_EXCLUDE
    assert noms[0].expires_at is not None
    expires = datetime.fromisoformat(noms[0].expires_at)
    assert timedelta(days=55) < (expires - _NOW) < timedelta(days=65)


def test_parse_failure_falls_back_deterministically(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        raise StructuredParseError("unusable", raw_head="?")

    noms = run_nominator(db_path, struct=struct)
    assert noms
    assert all(n.source == "deterministic_fallback" for n in noms)
    # Fallback ranks by headroom: the $100 bear_case leads.
    assert noms[0].purpose == "bear_case"
    assert noms[0].kind == KIND_MODEL_DOWNGRADE
    assert noms[0].candidates  # merged frontier candidates attached


def test_new_run_expires_previous_pending(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        raise StructuredParseError("x", raw_head="")

    run_nominator(db_path, struct=struct)
    first = pending_nominations(db_path)
    assert first
    run_nominator(db_path, struct=struct)
    rows = _pending_rows(db_path)
    statuses = [s for _p, _k, s, _e in rows]
    assert "expired" in statuses  # the first run's rows
    assert statuses.count("pending") == len(pending_nominations(db_path))


# ---------------------------------------------------------------------------
# Reads: pending / excluded / due / mark
# ---------------------------------------------------------------------------


def _insert_nomination(
    db_path: Path,
    *,
    purpose: str,
    kind: str = KIND_MODEL_DOWNGRADE,
    status: str = "pending",
    expires_at: str | None = None,
    candidates: list[str] | None = None,
    priority: int = 1,
) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO optimizer_nominations (nomination_run_id, purpose, kind, priority,"
        " incumbent_model, candidates_json, rationale, risk_tier, source, ladder_sha,"
        " status, expires_at, created_at, updated_at)"
        " VALUES ('r1', ?, ?, ?, 'claude-sonnet-4-6', ?, 'seeded', 'candidate',"
        " 'opus', 'sha', ?, ?, ?, ?)",
        (
            purpose,
            kind,
            priority,
            json.dumps(candidates or []),
            status,
            expires_at,
            _NOW.isoformat(),
            _NOW.isoformat(),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    assert row_id is not None
    return int(row_id)


def test_excluded_purposes_ttl_and_rotation_floor(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    live = (_NOW + timedelta(days=30)).isoformat()
    stale = (_NOW - timedelta(days=1)).isoformat()
    _insert_nomination(db_path, purpose="qa_topics", kind=KIND_EXCLUDE, expires_at=live)
    _insert_nomination(db_path, purpose="bear_case", kind=KIND_EXCLUDE, expires_at=stale)
    # qa_topics was recently measured -> excludable; bear_case's TTL elapsed.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO model_eval_verdicts (purpose, candidate, incumbent, verdict, run_id,"
        " recorded_at) VALUES ('qa_topics', 'c', 'i', 'KEEP_INCUMBENT', 'r', ?)",
        (_RECENT,),
    )
    conn.commit()
    conn.close()
    assert excluded_purposes(db_path) == {"qa_topics"}

    # Rotation floor: with NO recent verdict, even a live exclusion is overridden.
    (tmp_path / "b").mkdir()
    db2 = _db(tmp_path / "b")
    _insert_nomination(db2, purpose="qa_topics", kind=KIND_EXCLUDE, expires_at=live)
    assert excluded_purposes(db2) == set()


def test_pending_and_mark_roundtrip(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    row_id = _insert_nomination(
        db_path, purpose="bear_case", candidates=["claude-haiku-4-5-20251001"]
    )
    noms = pending_nominations(db_path)
    assert [n.purpose for n in noms] == ["bear_case"]
    assert noms[0].row_id == row_id
    mark_nomination(db_path, row_id, "swept")
    assert pending_nominations(db_path) == []


def test_nomination_run_due_logic(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    assert nomination_run_due(db_path) is True  # no run yet

    def struct(prompt: str, **kwargs: object) -> object:
        raise StructuredParseError("x", raw_head="")

    run_nominator(db_path, struct=struct)
    assert newest_run_info(db_path) is not None
    assert nomination_run_due(db_path) is False  # fresh run, same frontier

    # A frontier change (new discovered candidate) makes re-nomination due.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO candidate_models (model_id, family, input_usd_per_mtok,"
        " output_usd_per_mtok, first_seen_at, verified_at)"
        " VALUES ('prov/new', 'openrouter', 0.1, 0.2, ?, ?)",
        (_NOW.isoformat(), _NOW.isoformat()),
    )
    conn.commit()
    conn.close()
    assert nomination_run_due(db_path) is True
