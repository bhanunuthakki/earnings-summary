"""thesis_collision — the whole-book cross-name audit.

Covers: snapshot loading off holdings JSON, propose (raises) vs generate
(degrades) vs the cache-aware refresh (skips the LLM call when the thesis
set is unchanged), in-code validation of findings against the real ticker
set, and the render-side read + section markup.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import thesis_collision as tc
from llm.structured import StructuredParseError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_holdings(
    repo_root: Path,
    ticker: str,
    *,
    thesis: str = "A thesis.",
    key_driver: str = "the driver",
    tier1: list[dict[str, str]] | None = None,
) -> None:
    d = repo_root / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "thesis": thesis,
        "key_driver": key_driver,
        "tier_1_kpis": tier1 or [{"name": "Metric A", "break_condition": "falls below X"}],
    }
    (d / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def _snap(ticker: str, thesis: str = "T", driver: str = "D") -> tc.ThesisSnapshot:
    return tc.ThesisSnapshot(
        ticker=ticker, key_driver=driver, thesis=thesis, tier1_breaks=(("M", "cond"),)
    )


def _payload(
    clusters: list[dict[str, object]] | None = None,
    contradictions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {"clusters": clusters or [], "contradictions": contradictions or []}


def _fixed(payload: dict[str, object]) -> tc.CollisionCall:
    def call(_prompt: str) -> dict[str, object]:
        return payload

    return call


def _raising() -> tc.CollisionCall:
    def call(_prompt: str) -> dict[str, object]:
        raise StructuredParseError("bad json")

    return call


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------


def test_load_thesis_snapshot_reads_holdings_json(tmp_path: Path) -> None:
    _write_holdings(tmp_path, "AAA", thesis="Compounder bet", key_driver="pricing power")
    snap = tc.load_thesis_snapshot(tmp_path, "aaa")
    assert snap is not None
    assert snap.ticker == "AAA"
    assert snap.thesis == "Compounder bet"
    assert snap.key_driver == "pricing power"
    assert snap.tier1_breaks == (("Metric A", "falls below X"),)


def test_load_thesis_snapshot_missing_file_or_empty_thesis(tmp_path: Path) -> None:
    assert tc.load_thesis_snapshot(tmp_path, "ZZZ") is None
    _write_holdings(tmp_path, "BBB", thesis="   ")
    assert tc.load_thesis_snapshot(tmp_path, "BBB") is None


def test_load_thesis_snapshot_truncates_and_caps_tier1(tmp_path: Path) -> None:
    long_thesis = "x" * 2000
    tier1 = [{"name": f"K{i}", "break_condition": "y" * 500} for i in range(10)]
    _write_holdings(tmp_path, "CCC", thesis=long_thesis, tier1=tier1)
    snap = tc.load_thesis_snapshot(tmp_path, "CCC")
    assert snap is not None
    assert len(snap.thesis) == tc.THESIS_CHAR_CAP
    assert len(snap.tier1_breaks) == tc.MAX_TIER1_PER_TICKER
    assert all(len(cond) <= tc.DRIVER_CHAR_CAP for _name, cond in snap.tier1_breaks)


def test_load_thesis_snapshots_sorted_and_skips_missing(tmp_path: Path) -> None:
    _write_holdings(tmp_path, "BBB", thesis="B thesis")
    _write_holdings(tmp_path, "AAA", thesis="A thesis")
    snaps = tc.load_thesis_snapshots(tmp_path, ["BBB", "AAA", "ZZZ"])
    assert [s.ticker for s in snaps] == ["AAA", "BBB"]


def test_cache_inputs_deterministic_and_content_sensitive() -> None:
    a = [_snap("AAA", "T1"), _snap("BBB", "T2")]
    b = [_snap("AAA", "T1"), _snap("BBB", "T2")]
    assert tc.cache_inputs(a) == tc.cache_inputs(b)
    c = [_snap("AAA", "T1-changed"), _snap("BBB", "T2")]
    assert tc.cache_inputs(a) != tc.cache_inputs(c)


# ---------------------------------------------------------------------------
# propose vs generate
# ---------------------------------------------------------------------------


def test_propose_returns_validated_findings() -> None:
    snaps = [_snap("AAA"), _snap("BBB"), _snap("CCC")]
    payload = _payload(
        clusters=[
            {"tickers": ["AAA", "BBB"], "driver": "shared macro bet", "rationale": "because"}
        ],
        contradictions=[
            {"tickers": ["BBB", "CCC"], "contradiction": "opposite view", "rationale": "why"}
        ],
    )
    report = tc.propose_thesis_collisions(snaps, call=_fixed(payload))
    assert len(report.clusters) == 1
    assert report.clusters[0].tickers == ("AAA", "BBB")
    assert report.clusters[0].driver == "shared macro bet"
    assert len(report.contradictions) == 1
    assert report.contradictions[0].tickers == ("BBB", "CCC")
    assert report.tickers_analyzed == ("AAA", "BBB", "CCC")


def test_propose_drops_findings_with_hallucinated_or_single_ticker() -> None:
    snaps = [_snap("AAA"), _snap("BBB")]
    payload = _payload(
        clusters=[
            {"tickers": ["AAA", "ZZZ"], "driver": "x", "rationale": "y"},  # ZZZ unknown -> <2 left
            {"tickers": ["AAA"], "driver": "x", "rationale": "y"},  # single ticker
            {"tickers": ["AAA", "BBB"], "driver": "", "rationale": "y"},  # empty driver
        ]
    )
    report = tc.propose_thesis_collisions(snaps, call=_fixed(payload))
    assert report.clusters == ()


def test_propose_dedupes_repeated_ticker_in_one_finding() -> None:
    snaps = [_snap("AAA"), _snap("BBB")]
    payload = _payload(
        clusters=[{"tickers": ["AAA", "aaa", "BBB"], "driver": "x", "rationale": "y"}]
    )
    report = tc.propose_thesis_collisions(snaps, call=_fixed(payload))
    assert len(report.clusters) == 1
    assert report.clusters[0].tickers == ("AAA", "BBB")


def test_propose_raises_on_bad_json() -> None:
    snaps = [_snap("AAA"), _snap("BBB")]
    with pytest.raises(StructuredParseError):
        tc.propose_thesis_collisions(snaps, call=_raising())


def test_generate_degrades_parse_error_to_empty() -> None:
    snaps = [_snap("AAA"), _snap("BBB")]
    report = tc.generate_thesis_collisions(snaps, call=_raising())
    assert report.clusters == () and report.contradictions == ()
    assert report.tickers_analyzed == ("AAA", "BBB")


def test_generate_skips_call_below_two_snapshots() -> None:
    def boom(_prompt: str) -> dict[str, object]:
        raise AssertionError("must not call the LLM with <2 snapshots")

    report = tc.generate_thesis_collisions([_snap("AAA")], call=boom)
    assert report.clusters == () and report.tickers_analyzed == ("AAA",)
    report0 = tc.generate_thesis_collisions([], call=boom)
    assert report0.tickers_analyzed == ()


# ---------------------------------------------------------------------------
# CollisionReport round-trip
# ---------------------------------------------------------------------------


def test_collision_report_json_round_trip() -> None:
    report = tc.CollisionReport(
        clusters=(tc.SharedDriverCluster(("AAA", "BBB"), "driver", "rationale"),),
        contradictions=(tc.ThesisContradiction(("CCC", "DDD"), "opposite", "why"),),
        tickers_analyzed=("AAA", "BBB", "CCC", "DDD"),
    )
    restored = tc.CollisionReport.from_json(report.to_json())
    assert restored == report


def test_collision_report_from_json_rejects_bad_shape() -> None:
    assert tc.CollisionReport.from_json("not a dict") is None
    assert tc.CollisionReport.from_json(None) is None
    empty = tc.CollisionReport.from_json({})
    assert empty is not None
    assert empty.clusters == () and empty.contradictions == ()


# ---------------------------------------------------------------------------
# Cache-aware refresh + render-side read (over a real llm_artifacts DB)
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER REFERENCES llm_artifacts(id),
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        );
        """
    )
    conn.commit()


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
    finally:
        conn.close()
    return path


def test_refresh_none_below_two_theses(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA")
    assert tc.refresh_thesis_collision(db_path, tmp_path, ["AAA", "ZZZ"]) is None


def test_refresh_calls_llm_and_persists(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis")
    _write_holdings(tmp_path, "BBB", thesis="Beta thesis")
    calls = {"n": 0}

    def call(_prompt: str) -> dict[str, object]:
        calls["n"] += 1
        return _payload(clusters=[{"tickers": ["AAA", "BBB"], "driver": "d", "rationale": "r"}])

    result = tc.refresh_thesis_collision(db_path, tmp_path, ["AAA", "BBB"], call=call)
    assert result is not None
    assert result.cache_hit is False
    assert calls["n"] == 1
    assert len(result.report.clusters) == 1

    cached = tc.read_cached_report(db_path)
    assert cached is not None
    assert cached.report == result.report


def test_refresh_skips_llm_when_thesis_set_unchanged(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis")
    _write_holdings(tmp_path, "BBB", thesis="Beta thesis")
    calls = {"n": 0}

    def call(_prompt: str) -> dict[str, object]:
        calls["n"] += 1
        return _payload()

    first = tc.refresh_thesis_collision(db_path, tmp_path, ["AAA", "BBB"], call=call)
    assert first is not None and first.cache_hit is False and calls["n"] == 1

    second = tc.refresh_thesis_collision(db_path, tmp_path, ["AAA", "BBB"], call=call)
    assert second is not None
    assert second.cache_hit is True
    assert calls["n"] == 1  # no new spend
    assert second.report == first.report


def test_refresh_recalls_llm_when_thesis_text_changes(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis v1")
    _write_holdings(tmp_path, "BBB", thesis="Beta thesis")
    calls = {"n": 0}

    def call(_prompt: str) -> dict[str, object]:
        calls["n"] += 1
        return _payload()

    tc.refresh_thesis_collision(db_path, tmp_path, ["AAA", "BBB"], call=call)
    assert calls["n"] == 1

    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis v2 — a materially different bet")
    result = tc.refresh_thesis_collision(db_path, tmp_path, ["AAA", "BBB"], call=call)
    assert result is not None
    assert result.cache_hit is False
    assert calls["n"] == 2


def test_read_cached_report_none_when_nothing_generated(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    assert tc.read_cached_report(db_path) is None


def test_read_cached_report_none_when_db_missing(tmp_path: Path) -> None:
    assert tc.read_cached_report(tmp_path / "nope.db") is None
