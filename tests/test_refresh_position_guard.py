"""execution/refresh_position_guard.py — the naked-position gate CLI: builds
+ materializes the cache, exit codes, and the last-good-on-failure contract."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import execution.refresh_position_guard as refresh_position_guard
from position_guard_cache import cache_path, read_position_guard_cache


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, is_latest INTEGER, "
        "segment_name TEXT, live_price REAL, assumption_snapshot_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE position_sizing_intent ("
        "id INTEGER PRIMARY KEY, ticker TEXT, intent_kind TEXT, intent_value REAL, "
        "narrative TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE v_thesis_status (ticker TEXT, thesis_updated_at TEXT)")
    conn.commit()
    conn.close()
    return db_path


def _weights_cache(repo_root: Path, weights: dict[str, float]) -> None:
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    (repo_root / "data" / "portfolio_weights.json").write_text(
        json.dumps({"weights": weights}), encoding="utf-8"
    )


def test_main_writes_cache_and_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {})  # empty book — still a valid run

    rc = refresh_position_guard.main(["--db-path", str(db_path)])
    assert rc == 0
    assert cache_path(tmp_path).exists()

    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["names_checked"] == 0
    assert payload["violations"] == 0


def test_main_reports_violations_for_uncovered_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"FLKR": 0.06})  # no bear, no thesis, no downside rule

    rc = refresh_position_guard.main(["--db-path", str(db_path)])
    assert rc == 0
    cache = read_position_guard_cache(tmp_path)
    assert cache is not None
    assert len(cache.violations) == 1

    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["violations"] == 1


def _no_db_path(_override: Path | str | None) -> Path | None:
    return None


def test_main_halts_loud_when_db_path_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("execution.refresh_position_guard.resolve_db_path", _no_db_path)
    rc = refresh_position_guard.main([])
    assert rc == 1


def test_rerun_with_same_inputs_produces_the_same_cache(tmp_path: Path) -> None:
    """Idempotency: re-running the stage against unchanged inputs writes an
    equivalent cache (same rows/violations), not a divergent one."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"FLKR": 0.06})

    assert refresh_position_guard.main(["--db-path", str(db_path)]) == 0
    first = read_position_guard_cache(tmp_path)
    assert refresh_position_guard.main(["--db-path", str(db_path)]) == 0
    second = read_position_guard_cache(tmp_path)

    assert first is not None and second is not None
    assert [r.model_dump() for r in first.rows] == [r.model_dump() for r in second.rows]
