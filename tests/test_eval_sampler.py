"""Tests for the stratified eval-case sampler (src/evals/sampler.py — PR2 of
meta_eval_governance.md). All LLM calls are dependency-injected fakes; the suite
never spends."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from evals.sampler import (
    UNCLASSIFIED,
    CensusRow,
    FrameRecord,
    build_classify_prompt,
    ensure_difficulty_features,
    load_cached_features,
    load_census,
    load_frame,
    recent_sample_shas,
    sample_cases,
)
from llm.structured import StructuredParseError

_RECENT = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None).isoformat()


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
        CREATE TABLE eval_case_features (
            purpose TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
            classifier_version TEXT NOT NULL, ticker TEXT, scope TEXT,
            prompt_chars INTEGER NOT NULL DEFAULT 0, difficulty TEXT NOT NULL,
            case_type TEXT NOT NULL DEFAULT '', hard_signals_json TEXT NOT NULL DEFAULT '[]',
            classified_at TEXT NOT NULL,
            PRIMARY KEY (purpose, prompt_sha256, classifier_version)
        );
        CREATE TABLE model_eval_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            candidate TEXT NOT NULL, incumbent TEXT NOT NULL, verdict TEXT NOT NULL,
            run_id TEXT NOT NULL, parity_rate REAL, judge_agreement REAL,
            n_cases INTEGER, n_parity INTEGER, summary_json TEXT, recorded_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _ledger_row(
    conn: sqlite3.Connection,
    *,
    purpose: str = "bear_case",
    sha: str,
    ticker: str = "META",
    scope: str | None = None,
    chars: int = 1000,
    calls: int = 1,
) -> None:
    for _ in range(calls):
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, ticker, scope, prompt_sha256, prompt_chars)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_RECENT, purpose, ticker, scope, sha, chars),
        )


def _census(
    n: int, *, tickers: tuple[str, ...] = ("META", "GOOG", "NU", "MELI"), chars_step: int = 500
) -> dict[str, CensusRow]:
    out: dict[str, CensusRow] = {}
    for i in range(n):
        sha = f"sha{i:03d}"
        out[sha] = CensusRow(
            prompt_sha256=sha,
            calls=1 + (i % 3),
            ticker=tickers[i % len(tickers)],
            scope=None,
            prompt_chars=200 + i * chars_step,
        )
    return out


def _frame_for(
    census: dict[str, CensusRow], shas: list[str] | None = None
) -> dict[str, FrameRecord]:
    keys = shas if shas is not None else list(census)
    return {
        sha: FrameRecord(
            prompt_sha256=sha,
            prompt=f"prompt body {sha} " + "x" * census[sha].prompt_chars,
            response=f"incumbent answer {sha}",
            ticker=census[sha].ticker,
            scope=census[sha].scope,
            model="claude-sonnet-4-6",
        )
        for sha in keys
    }


# ---------------------------------------------------------------------------
# Census + frame loading
# ---------------------------------------------------------------------------


def test_load_census_excludes_eval_scopes_and_counts(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    _ledger_row(conn, sha="a", calls=3)
    _ledger_row(conn, sha="b", scope="model_eval")  # eval traffic: excluded
    _ledger_row(conn, sha="c", scope="web")  # production, web-scoped: kept
    conn.commit()
    conn.close()
    census = load_census(db_path, "bear_case")
    assert set(census) == {"a", "c"}
    assert census["a"].calls == 3  # recurrence weight
    assert census["c"].scope == "web"


def test_load_frame_parses_and_dedups(tmp_path: Path) -> None:
    cdir = tmp_path / "cap"
    cdir.mkdir()
    old = cdir / "capture_2026-06-01.jsonl"
    new = cdir / "capture_2026-06-02.jsonl"
    rec_old = {"purpose": "bear_case", "prompt": "OLD", "response": "r1", "prompt_sha256": "s1"}
    rec_new = {"purpose": "bear_case", "prompt": "NEW", "response": "r2", "prompt_sha256": "s1"}
    empty = {"purpose": "bear_case", "prompt": "p", "response": "", "prompt_sha256": "s2"}
    old.write_text(json.dumps(rec_old) + "\n", encoding="utf-8")
    new.write_text(json.dumps(rec_new) + "\n" + json.dumps(empty) + "\n", encoding="utf-8")
    frame = load_frame([old, new], "bear_case")
    assert set(frame) == {"s1"}  # empty-response record skipped
    assert frame["s1"].prompt == "NEW"  # newest occurrence wins


# ---------------------------------------------------------------------------
# The honesty gate
# ---------------------------------------------------------------------------


def test_thin_frame_is_insufficient() -> None:
    census = _census(20)
    frame = _frame_for(census, ["sha000", "sha001"])  # 10% < 30%
    result = sample_cases(
        purpose="bear_case",
        n=8,
        min_n=4,
        census=census,
        frame=frame,
        features={},
        rng_seed="seed",
    )
    assert result.insufficient_frame is True
    assert "frame_share" in result.reason
    assert result.manifest["frame_share"] == pytest.approx(0.1)
    assert result.cases == []


def test_pool_below_min_n_is_insufficient() -> None:
    census = _census(10)
    frame = _frame_for(census, ["sha000", "sha001", "sha002", "sha003"])  # share 0.4 ok
    result = sample_cases(
        purpose="bear_case",
        n=8,
        min_n=6,  # pool of 4 < 6
        census=census,
        frame=frame,
        features={},
        rng_seed="seed",
    )
    assert result.insufficient_frame is True
    assert "min_n" in result.reason


def test_web_scoped_rows_excluded_from_pool() -> None:
    census = _census(10)
    web_census = {
        sha: CensusRow(sha, row.calls, row.ticker, "web", row.prompt_chars)
        for sha, row in census.items()
    }
    frame = _frame_for(census)
    result = sample_cases(
        purpose="bear_case",
        n=4,
        min_n=2,
        census=web_census,
        frame=frame,
        features={},
        rng_seed="seed",
    )
    # Everything is web-scoped -> eligible pool empty -> insufficient.
    assert result.insufficient_frame is True
    assert result.manifest["web_excluded"] == 10


# ---------------------------------------------------------------------------
# The draw
# ---------------------------------------------------------------------------


def test_quota_oversamples_hard() -> None:
    census = _census(30)
    frame = _frame_for(census)
    features = {
        sha: ("hard" if i % 3 == 0 else ("moderate" if i % 3 == 1 else "easy"))
        for i, sha in enumerate(census)
    }
    result = sample_cases(
        purpose="bear_case",
        n=12,
        min_n=8,
        census=census,
        frame=frame,
        features=features,
        rng_seed="seed",
    )
    assert result.insufficient_frame is False
    drawn = cast("list[dict[str, object]]", result.manifest["cases"])
    by_diff: dict[str, int] = {}
    for row in drawn:
        by_diff[str(row["difficulty"])] = by_diff.get(str(row["difficulty"]), 0) + 1
    # 25/33/42 of 12 -> easy 3 / moderate 4 / hard 5.
    assert by_diff == {"easy": 3, "moderate": 4, "hard": 5}
    assert result.manifest["quota"] == {"easy": 3, "moderate": 4, "hard": 5}


def test_underfilled_hard_spills_and_logs() -> None:
    census = _census(12)
    frame = _frame_for(census)
    features = {sha: "easy" for sha in census}  # no hard cases exist
    result = sample_cases(
        purpose="bear_case",
        n=8,
        min_n=4,
        census=census,
        frame=frame,
        features=features,
        rng_seed="seed",
    )
    assert result.insufficient_frame is False
    assert len(result.cases) == 8
    spills = result.manifest["spills"]
    assert isinstance(spills, list) and spills  # the spill is visible, not silent


def test_ticker_cap_holds_when_alternatives_exist() -> None:
    census = _census(30)  # 4 tickers round-robin
    frame = _frame_for(census)
    result = sample_cases(
        purpose="bear_case",
        n=9,
        min_n=4,
        census=census,
        frame=frame,
        features={},
        rng_seed="seed",
    )
    drawn = cast("list[dict[str, object]]", result.manifest["cases"])
    per_ticker: dict[str, int] = {}
    for row in drawn:
        per_ticker[str(row["ticker"])] = per_ticker.get(str(row["ticker"]), 0) + 1
    assert max(per_ticker.values()) <= 3  # ceil(9/3)


def test_seeded_draw_is_reproducible() -> None:
    census = _census(30)
    frame = _frame_for(census)
    a = sample_cases(
        purpose="bear_case",
        n=8,
        min_n=4,
        census=census,
        frame=frame,
        features={},
        rng_seed="run42",
    )
    b = sample_cases(
        purpose="bear_case",
        n=8,
        min_n=4,
        census=census,
        frame=frame,
        features={},
        rng_seed="run42",
    )
    assert [c.prompt_sha256 for c in a.cases] == [c.prompt_sha256 for c in b.cases]


def test_dedup_excludes_prior_sample_shas(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    manifest = {"sample_manifest": {"cases": [{"sha": "sha000"}, {"sha": "sha001"}]}}
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO model_eval_verdicts (purpose, candidate, incumbent, verdict, run_id,"
        " summary_json, recorded_at) VALUES ('bear_case', 'cand', 'inc', 'HOLD', 'r1', ?, ?)",
        (json.dumps(manifest), _RECENT),
    )
    conn.commit()
    conn.close()
    shas = recent_sample_shas(db_path, "bear_case", "cand")
    assert shas == {"sha000", "sha001"}
    # A different candidate is unaffected (re-use across candidates is fine).
    assert recent_sample_shas(db_path, "bear_case", "other") == set()

    census = _census(10)
    frame = _frame_for(census)
    result = sample_cases(
        purpose="bear_case",
        n=8,
        min_n=4,
        census=census,
        frame=frame,
        features={},
        rng_seed="seed",
        exclude_shas=shas,
    )
    picked = {c.prompt_sha256 for c in result.cases}
    assert picked.isdisjoint(shas)


def test_no_census_degrades_to_legacy_mode() -> None:
    census: dict[str, CensusRow] = {}
    frame = _frame_for(_census(5))
    result = sample_cases(
        purpose="bear_case",
        n=3,
        min_n=2,
        census=census,
        frame=frame,
        features={},
        rng_seed="seed",
    )
    assert result.insufficient_frame is False
    assert result.manifest["mode"] == "legacy_no_census"
    assert len(result.cases) == 3


def test_cases_carry_sha_and_incumbent_response() -> None:
    census = _census(10)
    frame = _frame_for(census)
    result = sample_cases(
        purpose="bear_case",
        n=4,
        min_n=2,
        census=census,
        frame=frame,
        features={},
        rng_seed="seed",
    )
    for case in result.cases:
        assert case.prompt_sha256 in census
        assert case.incumbent_response.startswith("incumbent answer")
        assert case.label.startswith("bear_case:")


# ---------------------------------------------------------------------------
# Difficulty classification (cached; DI'd fake — never spends)
# ---------------------------------------------------------------------------


def test_classifier_caches_and_skips_cached(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    census = _census(3)
    frame = _frame_for(census)
    calls: list[str] = []

    def fake_struct(prompt: str, **kwargs: object) -> object:
        calls.append(prompt)
        return {"difficulty": "hard", "case_type": "unit trap", "hard_signals": ["units"]}

    features, deferred = ensure_difficulty_features(
        db_path, "bear_case", frame, census, classifier_version="v1", struct=fake_struct
    )
    assert deferred == 0
    assert set(features.values()) == {"hard"}
    assert len(calls) == 3

    # Second run: everything cached -> zero calls.
    calls.clear()
    features2, _ = ensure_difficulty_features(
        db_path, "bear_case", frame, census, classifier_version="v1", struct=fake_struct
    )
    assert calls == []
    assert features2 == features
    assert load_cached_features(db_path, "bear_case", classifier_version="v1") == features


def test_classifier_version_forks_cache(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    census = _census(1)
    frame = _frame_for(census)

    def fake_struct(prompt: str, **kwargs: object) -> object:
        return {"difficulty": "easy"}

    ensure_difficulty_features(
        db_path, "bear_case", frame, census, classifier_version="v1", struct=fake_struct
    )
    assert load_cached_features(db_path, "bear_case", classifier_version="v2") == {}


def test_classifier_failure_degrades_without_stalling(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    census = _census(5)
    frame = _frame_for(census)
    calls: list[str] = []

    def broken_struct(prompt: str, **kwargs: object) -> object:
        calls.append(prompt)
        raise StructuredParseError("unusable JSON", raw_head="???")

    features, _ = ensure_difficulty_features(
        db_path, "bear_case", frame, census, classifier_version="v1", struct=broken_struct
    )
    # One attempt, then the dead classifier is not hammered; all degrade.
    assert len(calls) == 1
    assert set(features.values()) == {UNCLASSIFIED}
    # Unclassified rides the moderate bucket — the draw still works.
    result = sample_cases(
        purpose="bear_case",
        n=4,
        min_n=2,
        census=census,
        frame=frame,
        features=features,
        rng_seed="seed",
    )
    assert result.insufficient_frame is False
    assert len(result.cases) == 4


def test_classifier_invalid_enum_degrades_to_unclassified(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    census = _census(1)
    frame = _frame_for(census)

    def weird_struct(prompt: str, **kwargs: object) -> object:
        return {"difficulty": "impossible"}

    features, _ = ensure_difficulty_features(
        db_path, "bear_case", frame, census, classifier_version="v1", struct=weird_struct
    )
    assert set(features.values()) == {UNCLASSIFIED}
    # Invalid classifications are never cached (a later sweep retries).
    assert load_cached_features(db_path, "bear_case", classifier_version="v1") == {}


def test_classify_cap_defers_and_reports(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    census = _census(6)
    frame = _frame_for(census)

    def fake_struct(prompt: str, **kwargs: object) -> object:
        return {"difficulty": "moderate"}

    features, deferred = ensure_difficulty_features(
        db_path,
        "bear_case",
        frame,
        census,
        classifier_version="v1",
        struct=fake_struct,
        max_new=4,
    )
    assert deferred == 2
    assert sum(1 for d in features.values() if d == "moderate") == 4
    assert sum(1 for d in features.values() if d == UNCLASSIFIED) == 2


def test_classify_prompt_spotlights_the_captured_prompt() -> None:
    prompt = build_classify_prompt("bear_case", "SECRET TASK BODY")
    assert "SECRET TASK BODY" in prompt
    # The untrusted wrapper's data markers are present (artifact, not instructions).
    assert "UNTRUSTED" in prompt.upper()
    assert '"difficulty"' in prompt
