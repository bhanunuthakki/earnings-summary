"""Native-cache to ``llm_artifacts`` projection contract."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import native_artifact_projection as native_projection
from llm_artifact_store import UpsertRequest, upsert
from native_artifact_projection import NativeArtifactProjectionError, project_native_artifact


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            scope TEXT NOT NULL DEFAULT 'ticker',
            purpose TEXT NOT NULL,
            fiscal_period TEXT,
            content_md TEXT,
            content_json TEXT,
            input_sha256 TEXT NOT NULL,
            output_sha256 TEXT,
            model TEXT,
            prompt_version TEXT NOT NULL DEFAULT 'v1',
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            superseded_by_id INTEGER,
            dirty INTEGER NOT NULL DEFAULT 0,
            dirty_reason TEXT,
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        )
        """
    )
    return conn


def _seed_dirty(conn: sqlite3.Connection, *, purpose: str) -> int:
    row = conn.execute(
        """
        INSERT INTO llm_artifacts (
            ticker, scope, purpose, input_sha256, prompt_version, dirty
        ) VALUES ('NU', 'ticker', ?, 'old', 'v1', 1)
        RETURNING id
        """,
        (purpose,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _write_bear_cache(repo_root: Path, payload: object) -> Path:
    path = repo_root / "data" / "bear_case" / "NU.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_bear_payload() -> dict[str, object]:
    return {
        "failure_modes": [
            {
                "hypothesis": "Growth stalls",
                "evidence_in_data": "Bookings decelerate",
                "leading_indicator": "Net adds",
                "quantitative_impact": "Revenue below plan",
                "refutation_criteria": "Net adds reaccelerate",
            }
        ],
        "most_underweighted": "Pricing pressure",
        "out_of_scope_flags": [],
    }


def test_projection_supersedes_only_the_exact_dirty_purpose(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        old_bear_id = _seed_dirty(conn, purpose="bear_case")
        qa_id = _seed_dirty(conn, purpose="qa_topics")
    finally:
        conn.close()
    queued_at = datetime.now(UTC) - timedelta(seconds=1)
    _write_bear_cache(tmp_path, _valid_bear_payload())

    new_id = project_native_artifact(
        ticker="NU",
        purpose="bear_case",
        repo_root=tmp_path,
        db_path=db_path,
        queued_at=queued_at,
        obligation_ids=(old_bear_id,),
    )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT superseded_by_id FROM llm_artifacts WHERE id = ?", (old_bear_id,)
        ).fetchone() == (new_id,)
        assert conn.execute(
            "SELECT dirty, superseded_by_id FROM llm_artifacts WHERE id = ?", (qa_id,)
        ).fetchone() == (1, None)
        current = conn.execute(
            """
            SELECT ticker, purpose, dirty, superseded_by_id, content_json
            FROM llm_artifacts WHERE id = ?
            """,
            (new_id,),
        ).fetchone()
    finally:
        conn.close()
    assert current is not None
    assert current[:4] == ("NU", "bear_case", 0, None)
    assert json.loads(str(current[4]))["failure_modes"][0]["hypothesis"] == "Growth stalls"


def test_projection_rejects_a_cache_not_written_after_queue_time(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        old_id = _seed_dirty(conn, purpose="bear_case")
    finally:
        conn.close()
    _write_bear_cache(tmp_path, _valid_bear_payload())

    with pytest.raises(NativeArtifactProjectionError, match="not refreshed after queue time"):
        project_native_artifact(
            ticker="NU",
            purpose="bear_case",
            repo_root=tmp_path,
            db_path=db_path,
            queued_at=datetime.now(UTC) + timedelta(seconds=1),
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT dirty, superseded_by_id FROM llm_artifacts WHERE id = ?", (old_id,)
        ).fetchone() == (1, None)
    finally:
        conn.close()


def test_projection_rejects_cache_timestamp_equal_to_queue_time(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        _seed_dirty(conn, purpose="bear_case")
    finally:
        conn.close()
    path = _write_bear_cache(tmp_path, _valid_bear_payload())
    fixed_timestamp = 1_786_700_000.0
    os.utime(path, (fixed_timestamp, fixed_timestamp))
    queued_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    with pytest.raises(NativeArtifactProjectionError, match="not refreshed after queue time"):
        project_native_artifact(
            ticker="NU",
            purpose="bear_case",
            repo_root=tmp_path,
            db_path=db_path,
            queued_at=queued_at,
        )


def test_projection_rejects_payload_ticker_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        _seed_dirty(conn, purpose="valuation_basis")
    finally:
        conn.close()
    path = tmp_path / "data" / "valuation_basis" / "NU.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "ticker": "META",
                "multiple_name": "P/E (NTM)",
                "history": [],
                "cache_sha256": "abc",
                "extracted_at": "2026-08-14T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(NativeArtifactProjectionError, match="does not match"):
        project_native_artifact(
            ticker="NU",
            purpose="valuation_basis",
            repo_root=tmp_path,
            db_path=db_path,
            queued_at=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_projection_persistence_failure_leaves_obligation_dirty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        old_id = _seed_dirty(conn, purpose="bear_case")
    finally:
        conn.close()
    _write_bear_cache(tmp_path, _valid_bear_payload())

    def _failed_upsert(*args: object, **kwargs: object) -> tuple[None, bool]:
        del args, kwargs
        return (None, False)

    monkeypatch.setattr(native_projection, "upsert", _failed_upsert)

    with pytest.raises(NativeArtifactProjectionError, match="could not be persisted"):
        project_native_artifact(
            ticker="NU",
            purpose="bear_case",
            repo_root=tmp_path,
            db_path=db_path,
            queued_at=datetime.now(UTC) - timedelta(seconds=1),
            obligation_ids=(old_id,),
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT dirty, superseded_by_id FROM llm_artifacts WHERE id = ?", (old_id,)
        ).fetchone() == (1, None)
    finally:
        conn.close()


def test_projection_forces_fresh_successor_when_identical_clean_artifact_predates_queue(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    payload = _valid_bear_payload()
    raw = json.dumps(payload)
    try:
        stale_clean_id, _ = upsert(
            UpsertRequest(
                ticker="NU",
                purpose="bear_case",
                content_json=payload,
                prompt_version="native-cache-projection-v1",
                cache_inputs=[raw],
            ),
            db_path=db_path,
        )
        assert stale_clean_id is not None
        queued_id = _seed_dirty(conn, purpose="bear_case")
    finally:
        conn.close()
    queued_at = datetime.now(UTC)
    path = _write_bear_cache(tmp_path, payload)
    fresh_timestamp = queued_at.timestamp() + 1
    os.utime(path, (fresh_timestamp, fresh_timestamp))

    successor_id = project_native_artifact(
        ticker="NU",
        purpose="bear_case",
        repo_root=tmp_path,
        db_path=db_path,
        queued_at=queued_at,
        obligation_ids=(queued_id,),
    )

    assert successor_id not in {stale_clean_id, queued_id}
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT superseded_by_id FROM llm_artifacts WHERE id = ?", (queued_id,)
        ).fetchone() == (successor_id,)
        generated_at = conn.execute(
            "SELECT generated_at FROM llm_artifacts WHERE id = ?", (successor_id,)
        ).fetchone()
    finally:
        conn.close()
    assert generated_at is not None
    assert datetime.fromisoformat(str(generated_at[0])) > queued_at


def test_projection_rolls_back_when_any_exact_obligation_has_wrong_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        bear_id = _seed_dirty(conn, purpose="bear_case")
        wrong_id = _seed_dirty(conn, purpose="qa_topics")
        before_count = int(conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0])
    finally:
        conn.close()
    _write_bear_cache(tmp_path, _valid_bear_payload())

    with pytest.raises(NativeArtifactProjectionError, match="could not be persisted"):
        project_native_artifact(
            ticker="NU",
            purpose="bear_case",
            repo_root=tmp_path,
            db_path=db_path,
            queued_at=datetime.now(UTC) - timedelta(seconds=1),
            obligation_ids=(bear_id, wrong_id),
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone() == (before_count,)
        assert conn.execute(
            "SELECT id, dirty, superseded_by_id FROM llm_artifacts ORDER BY id"
        ).fetchall() == [(bear_id, 1, None), (wrong_id, 1, None)]
    finally:
        conn.close()


def test_projection_rejects_invalid_purpose_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        _seed_dirty(conn, purpose="bear_case")
    finally:
        conn.close()
    queued_at = datetime.now(UTC) - timedelta(seconds=1)
    _write_bear_cache(tmp_path, {"failure_modes": "not-a-list"})

    with pytest.raises(NativeArtifactProjectionError, match="schema validation failed"):
        project_native_artifact(
            ticker="NU",
            purpose="bear_case",
            repo_root=tmp_path,
            db_path=db_path,
            queued_at=queued_at,
        )


@pytest.mark.parametrize(
    ("purpose", "payload"),
    [
        (
            "valuation_basis",
            {
                "ticker": "NU",
                "multiple_name": "P/E (NTM)",
                "history": [{"period_end": "2026-06-30", "value": 22.0}],
                "cache_sha256": "abc",
                "extracted_at": "2026-08-14T20:00:00Z",
            },
        ),
        (
            "qa_topics",
            {"by_key": {"sha": [{"id": "0", "topic": "Growth", "tag": "GROWTH"}]}},
        ),
        ("saydo_filter", {"by_key": {"sha": ["0"]}}),
        (
            "company_description",
            {
                "ticker": "NU",
                "source_sha256": "abc",
                "extracted_at_end": "2026-08-14T20:00:00Z",
                "model": "claude-sonnet-4-6",
                "elevator_pitch": "A scaled financial platform.",
                "segments": [],
                "geographies": [],
            },
        ),
        (
            "filing_intelligence",
            {
                "ticker": "NU",
                "source_sha256": "abc",
                "analyzed_at": "2026-08-14T20:00:00Z",
                "model": "claude-sonnet-4-6",
                "summary": {
                    "ticker": "NU",
                    "fiscal_year": 2025,
                    "analyzed_at": "2026-08-14T20:00:00Z",
                    "segment_changes": {"has_changes": False},
                    "metric_redefinitions": {"has_changes": False},
                    "executive_comp": {"metrics_used": []},
                    "investment_signals": [],
                    "raw_synthesis_md": "No material change.",
                },
            },
        ),
    ],
)
def test_each_current_native_purpose_has_a_typed_projection(
    tmp_path: Path,
    purpose: str,
    payload: dict[str, object],
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = _make_db(db_path)
    try:
        _seed_dirty(conn, purpose=purpose)
    finally:
        conn.close()
    directory = {
        "valuation_basis": "valuation_basis",
        "qa_topics": "qa_topics",
        "saydo_filter": "saydo_filter",
        "company_description": "company_description",
        "filing_intelligence": "filing_intelligence",
    }[purpose]
    cache_path = tmp_path / "data" / directory / "NU.json"
    cache_path.parent.mkdir(parents=True)
    queued_at = datetime.now(UTC) - timedelta(seconds=1)
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    artifact_id = project_native_artifact(
        ticker="NU",
        purpose=purpose,
        repo_root=tmp_path,
        db_path=db_path,
        queued_at=queued_at,
    )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT purpose, dirty FROM llm_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone() == (purpose, 0)
    finally:
        conn.close()
