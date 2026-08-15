"""risk_factors — C3 business-factor taxonomy (2026-07-19 program plan,
Workstream C keystone).

Covers: the migration (0196, via a real alembic tmp-db upgrade), the
grounding gate (out-of-taxonomy factors dropped, loadings clamped, dedup
keeps the higher loading), propose (raises) vs generate (degrades) vs the
artifact-cache-aware ``refresh_ticker_exposures`` (skips the LLM call when
the input hash is unchanged), ``persist_exposures``'s owner-edit supremacy
and is_latest/superseded chain, thesis-derived provenance for a
segment-mix-less name, the book-vector aggregation + top-3 contributors, and
that every entry point degrades rather than raises against an empty/missing
DB. Every LLM call is an injected stub — zero live LLM.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import risk_factors as rf
from llm.structured import StructuredParseError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_holdings(
    repo_root: Path,
    ticker: str,
    *,
    name: str = "Example Co",
    thesis: str = "A thesis about the business.",
    key_driver: str = "the driver",
) -> None:
    d = repo_root / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "name": name,
        "thesis": thesis,
        "key_driver": key_driver,
        "tier_1_kpis": [{"name": "Metric A", "break_condition": "falls below X"}],
    }
    (d / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


_FACTOR_A = rf.TAXONOMY[0]
_FACTOR_B = rf.TAXONOMY[1]


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
        CREATE TABLE business_factor_exposures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            factor TEXT NOT NULL,
            loading REAL NOT NULL,
            rationale TEXT,
            provenance TEXT NOT NULL,
            input_sha TEXT,
            owner_edited INTEGER NOT NULL DEFAULT 0,
            is_latest INTEGER NOT NULL DEFAULT 1,
            superseded_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TEXT
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


def _fixed(loadings: list[dict[str, object]]) -> rf.FactorCall:
    def call(_prompt: str) -> list[object]:
        return loadings

    return call


def _raising() -> rf.FactorCall:
    def call(_prompt: str) -> list[object]:
        raise StructuredParseError("bad json")

    return call


def _write_weights(repo_root: Path, weights: dict[str, float]) -> None:
    path = repo_root / "data" / "portfolio_weights.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"computed_at": "2026-07-24T00:00:00", "weights": weights}))


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_table(tmp_path: Path) -> None:
    from alembic.config import Config

    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    db = tmp_path / "mig.db"
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0194_research_triage")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(business_factor_exposures)")}
    finally:
        conn.close()
    assert {
        "id",
        "ticker",
        "factor",
        "loading",
        "rationale",
        "provenance",
        "input_sha",
        "owner_edited",
        "is_latest",
        "superseded_by",
        "created_at",
        "updated_at",
    } <= cols

    # Symmetric downgrade drops the table cleanly.
    command.downgrade(cfg, "0194_research_triage")
    conn = sqlite3.connect(str(db))
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='business_factor_exposures'"
            )
        }
    finally:
        conn.close()
    assert not names


# ---------------------------------------------------------------------------
# Grounding gate: _validate_loadings
# ---------------------------------------------------------------------------


def test_validate_loadings_drops_out_of_taxonomy_factor() -> None:
    raw = [
        {"factor": _FACTOR_A, "loading": 0.5, "rationale": "grounded"},
        {"factor": "a made-up factor", "loading": 0.9, "rationale": "hallucinated"},
    ]
    out = rf._validate_loadings(raw)
    assert [fl.factor for fl in out] == [_FACTOR_A]


def test_validate_loadings_clamps_range() -> None:
    raw = [
        {"factor": _FACTOR_A, "loading": 5.0, "rationale": "too high"},
        {"factor": _FACTOR_B, "loading": -2.0, "rationale": "too low"},
    ]
    out = {fl.factor: fl.loading for fl in rf._validate_loadings(raw)}
    assert out[_FACTOR_A] == 1.0
    assert out[_FACTOR_B] == 0.0


def test_validate_loadings_dedups_keeping_higher() -> None:
    raw = [
        {"factor": _FACTOR_A, "loading": 0.2, "rationale": "first"},
        {"factor": _FACTOR_A, "loading": 0.8, "rationale": "second, higher"},
    ]
    out = rf._validate_loadings(raw)
    assert len(out) == 1
    assert out[0].loading == 0.8
    assert out[0].rationale == "second, higher"


def test_validate_loadings_caps_at_max_and_orders_desc() -> None:
    raw = [
        {"factor": f, "loading": i / 10.0, "rationale": "r"}
        for i, f in enumerate(rf.TAXONOMY[: rf.MAX_LOADINGS_PER_TICKER + 3], start=1)
    ]
    out = rf._validate_loadings(raw)
    assert len(out) == rf.MAX_LOADINGS_PER_TICKER
    assert [fl.loading for fl in out] == sorted((fl.loading for fl in out), reverse=True)


def test_validate_loadings_rejects_malformed_entries() -> None:
    raw = [
        "not a dict",
        {"factor": _FACTOR_A},  # missing loading/rationale
        {"factor": _FACTOR_A, "loading": "not a number", "rationale": "x"},
        {"factor": _FACTOR_A, "loading": True, "rationale": "bool is not a number"},
    ]
    assert rf._validate_loadings(raw) == ()
    assert rf._validate_loadings("not a list") == ()


# ---------------------------------------------------------------------------
# propose (raises) vs generate (degrades)
# ---------------------------------------------------------------------------


def test_propose_raises_on_bad_json() -> None:
    with pytest.raises(StructuredParseError):
        rf.propose_factor_loadings(
            "AAA", name="A", geo_mix=None, product_mix=None, snapshot=None, call=_raising()
        )


def test_generate_degrades_to_empty_on_bad_json() -> None:
    out = rf.generate_factor_loadings(
        "AAA", name="A", geo_mix=None, product_mix=None, snapshot=None, call=_raising()
    )
    assert out == ()


def test_generate_returns_validated_loadings() -> None:
    out = rf.generate_factor_loadings(
        "AAA",
        name="A",
        geo_mix=None,
        product_mix=None,
        snapshot=None,
        call=_fixed([{"factor": _FACTOR_A, "loading": 0.6, "rationale": "r"}]),
    )
    assert out == (rf.FactorLoading(factor=_FACTOR_A, loading=0.6, rationale="r"),)


# ---------------------------------------------------------------------------
# derive_factor_exposures — thesis-derived provenance for a mix-less name
# ---------------------------------------------------------------------------


def test_derive_returns_none_with_no_thesis_and_no_mix(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    # No holdings file, no segment tables populated -> nothing to ground on.
    assert rf.derive_factor_exposures("ZZZ", db_path=db_path, repo_root=tmp_path) is None


def test_derive_is_thesis_derived_when_no_segment_mix(tmp_path: Path) -> None:
    """No segment_dimensions/segment_periods tables exist in this fixture DB,
    so latest_revenue_mix always degrades to None (sqlite3.OperationalError
    caught internally) — every derivation in this test module is therefore
    thesis-derived, exercising exactly the segment-less-name path the C3 plan
    calls out (ETFs, or names whose crosstab extraction hasn't landed)."""
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", name="Alpha Corp", thesis="Alpha thesis")
    loadings = rf.derive_factor_exposures(
        "AAA",
        db_path=db_path,
        repo_root=tmp_path,
        call=_fixed([{"factor": _FACTOR_A, "loading": 0.7, "rationale": "r"}]),
    )
    assert loadings == [rf.FactorLoading(factor=_FACTOR_A, loading=0.7, rationale="r")]


def test_build_prompt_carries_ticker_and_taxonomy(tmp_path: Path) -> None:
    _write_holdings(tmp_path, "AAA", name="Alpha Corp", thesis="Alpha thesis text")
    snapshot = rf.load_thesis_snapshot(tmp_path, "AAA")
    prompt = rf.build_prompt(
        "AAA", name="Alpha Corp", geo_mix=None, product_mix=None, snapshot=snapshot
    )
    assert "AAA" in prompt
    assert "Alpha Corp" in prompt
    assert "Alpha thesis text" in prompt
    assert _FACTOR_A in prompt
    assert str(rf.MAX_LOADINGS_PER_TICKER) in prompt


def test_build_prompt_degrades_without_thesis_or_mix() -> None:
    prompt = rf.build_prompt("ZZZ", name=None, geo_mix=None, product_mix=None, snapshot=None)
    assert "ZZZ" in prompt
    assert "no thesis on file" in prompt
    assert "no disclosed geography mix" in prompt


# ---------------------------------------------------------------------------
# persist_exposures — idempotency, owner-edit supremacy, supersede chain
# ---------------------------------------------------------------------------


def test_persist_writes_new_rows(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    loadings = [rf.FactorLoading(factor=_FACTOR_A, loading=0.5, rationale="r")]
    written = rf.persist_exposures(
        "AAA", loadings, provenance="thesis_derived", input_sha="sha1", db_path=db_path
    )
    assert written == 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT ticker, factor, loading, is_latest, provenance, input_sha "
            "FROM business_factor_exposures"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("AAA", _FACTOR_A, 0.5, 1, "thesis_derived", "sha1")]


def test_persist_is_idempotent_on_unchanged_input_sha(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    loadings = [rf.FactorLoading(factor=_FACTOR_A, loading=0.5, rationale="r")]
    first = rf.persist_exposures(
        "AAA", loadings, provenance="thesis_derived", input_sha="sha1", db_path=db_path
    )
    second = rf.persist_exposures(
        "AAA", loadings, provenance="thesis_derived", input_sha="sha1", db_path=db_path
    )
    assert first == 1
    assert second == 0  # no-op: same input_sha, same factor set
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT COUNT(*) FROM business_factor_exposures").fetchone()[0]
    finally:
        conn.close()
    assert n == 1  # no duplicate/superseding row written


def test_persist_supersedes_on_changed_input_sha(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.4, rationale="v1")],
        provenance="thesis_derived",
        input_sha="sha1",
        db_path=db_path,
    )
    written = rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.9, rationale="v2")],
        provenance="thesis_derived",
        input_sha="sha2",
        db_path=db_path,
    )
    assert written == 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT loading, is_latest, superseded_by FROM business_factor_exposures ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    old, new = rows
    assert old[1] == 0  # old row no longer latest
    assert old[2] == 2  # points at the new row's id
    assert new == (0.9, 1, None)


def test_persist_never_overwrites_owner_edited_row(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    now = "2026-07-24T00:00:00"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO business_factor_exposures "
            "(ticker, factor, loading, rationale, provenance, input_sha, owner_edited, "
            "is_latest, created_at, updated_at) "
            "VALUES ('AAA', ?, 0.15, 'owner correction', 'owner', NULL, 1, 1, ?, ?)",
            (_FACTOR_A, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    written = rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.95, rationale="auto re-derive")],
        provenance="thesis_derived",
        input_sha="sha-new",
        db_path=db_path,
    )
    assert written == 0  # the only factor in this call is owner-protected

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT loading, owner_edited, is_latest FROM business_factor_exposures"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(0.15, 1, 1)]  # untouched


def test_persist_protects_owner_factor_but_writes_others(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    now = "2026-07-24T00:00:00"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO business_factor_exposures "
            "(ticker, factor, loading, rationale, provenance, input_sha, owner_edited, "
            "is_latest, created_at, updated_at) "
            "VALUES ('AAA', ?, 0.15, 'owner correction', 'owner', NULL, 1, 1, ?, ?)",
            (_FACTOR_A, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    written = rf.persist_exposures(
        "AAA",
        [
            rf.FactorLoading(factor=_FACTOR_A, loading=0.95, rationale="auto re-derive"),
            rf.FactorLoading(factor=_FACTOR_B, loading=0.4, rationale="new factor"),
        ],
        provenance="thesis_derived",
        input_sha="sha-new",
        db_path=db_path,
    )
    assert written == 1  # only _FACTOR_B written; _FACTOR_A protected

    conn = sqlite3.connect(str(db_path))
    try:
        by_factor = dict(
            conn.execute(
                "SELECT factor, loading FROM business_factor_exposures WHERE is_latest = 1"
            ).fetchall()
        )
    finally:
        conn.close()
    assert by_factor[_FACTOR_A] == 0.15  # unchanged
    assert by_factor[_FACTOR_B] == 0.4


def test_persist_no_op_on_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    written = rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.5, rationale="r")],
        provenance="thesis_derived",
        input_sha="sha1",
        db_path=missing,
    )
    assert written == 0


def test_persist_empty_loadings_is_noop(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    assert (
        rf.persist_exposures(
            "AAA", [], provenance="thesis_derived", input_sha="sha1", db_path=db_path
        )
        == 0
    )


# ---------------------------------------------------------------------------
# refresh_ticker_exposures — artifact-cache-aware, per-ticker
# ---------------------------------------------------------------------------


def test_refresh_ticker_none_with_nothing_to_ground(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    assert rf.refresh_ticker_exposures("ZZZ", db_path=db_path, repo_root=tmp_path) is None


def test_refresh_ticker_calls_llm_and_persists(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis")
    calls = {"n": 0}

    def call(_prompt: str) -> list[object]:
        calls["n"] += 1
        return [{"factor": _FACTOR_A, "loading": 0.6, "rationale": "r"}]

    result = rf.refresh_ticker_exposures("AAA", db_path=db_path, repo_root=tmp_path, call=call)
    assert result is not None
    assert result.cache_hit is False
    assert result.provenance == "thesis_derived"
    assert calls["n"] == 1
    assert result.rows_written == 1

    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM business_factor_exposures WHERE ticker='AAA' AND is_latest=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_refresh_ticker_skips_llm_when_input_unchanged(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis")
    calls = {"n": 0}

    def call(_prompt: str) -> list[object]:
        calls["n"] += 1
        return [{"factor": _FACTOR_A, "loading": 0.6, "rationale": "r"}]

    first = rf.refresh_ticker_exposures("AAA", db_path=db_path, repo_root=tmp_path, call=call)
    second = rf.refresh_ticker_exposures("AAA", db_path=db_path, repo_root=tmp_path, call=call)
    assert first is not None and first.cache_hit is False
    assert second is not None and second.cache_hit is True
    assert calls["n"] == 1  # no new spend on the second run
    assert second.loadings == first.loadings


def test_refresh_ticker_recalls_when_thesis_changes(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis v1")
    calls = {"n": 0}

    def call(_prompt: str) -> list[object]:
        calls["n"] += 1
        return [{"factor": _FACTOR_A, "loading": 0.6, "rationale": "r"}]

    rf.refresh_ticker_exposures("AAA", db_path=db_path, repo_root=tmp_path, call=call)
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis v2 — materially different")
    rf.refresh_ticker_exposures("AAA", db_path=db_path, repo_root=tmp_path, call=call)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# refresh_all — per-item degrade sweep
# ---------------------------------------------------------------------------


def test_refresh_all_sweeps_portfolio_tickers(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES ('AAA','portfolio',NULL)"
        )
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES ('BBB','portfolio',NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    _write_holdings(tmp_path, "AAA", thesis="Alpha thesis")
    # BBB has no holdings file and no segment tables -> nothing to ground on.

    def call(_prompt: str) -> list[object]:
        return [{"factor": _FACTOR_A, "loading": 0.5, "rationale": "r"}]

    counts = rf.refresh_all(db_path, tmp_path, call=call)
    assert counts["tickers"] == 2
    assert counts["regenerated"] == 1
    assert counts["skipped_no_input"] == 1
    assert counts["deferred_transient"] == 0


def test_refresh_all_on_empty_db_never_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    counts = rf.refresh_all(missing, tmp_path, call=_fixed([]))
    assert counts["tickers"] == 0


# ---------------------------------------------------------------------------
# book_factor_vector — pure read, book-level aggregation + top-3
# ---------------------------------------------------------------------------


def test_book_factor_vector_aggregates_weight_times_loading(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.8, rationale="r")],
        provenance="thesis_derived",
        input_sha="s1",
        db_path=db_path,
    )
    rf.persist_exposures(
        "BBB",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.4, rationale="r")],
        provenance="thesis_derived",
        input_sha="s2",
        db_path=db_path,
    )
    _write_weights(tmp_path, {"AAA": 0.6, "BBB": 0.4})

    result = rf.book_factor_vector(db_path, tmp_path)
    # 0.6*0.8 + 0.4*0.4 = 0.48 + 0.16 = 0.64
    assert result.vector[_FACTOR_A] == pytest.approx(0.64)
    top = result.top_contributors[_FACTOR_A]
    assert top[0][0] == "AAA"  # AAA's contribution (0.48) ranks above BBB's (0.16)
    assert len(top) == 2


def test_book_factor_vector_top3_caps_and_orders(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    weights: dict[str, float] = {}
    for i, ticker in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        weight = 0.1 * (i + 1)
        weights[ticker] = weight
        rf.persist_exposures(
            ticker,
            [rf.FactorLoading(factor=_FACTOR_A, loading=1.0, rationale="r")],
            provenance="thesis_derived",
            input_sha=f"s{i}",
            db_path=db_path,
        )
    _write_weights(tmp_path, weights)

    result = rf.book_factor_vector(db_path, tmp_path)
    top = result.top_contributors[_FACTOR_A]
    assert len(top) == 3
    assert [t for t, _ in top] == ["DDD", "CCC", "BBB"]  # highest weight first


def test_book_factor_vector_ignores_unweighted_names(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.5, rationale="r")],
        provenance="thesis_derived",
        input_sha="s1",
        db_path=db_path,
    )
    _write_weights(tmp_path, {})  # no weighted holdings

    result = rf.book_factor_vector(db_path, tmp_path)
    assert result.vector == {}


def test_book_factor_vector_never_raises_on_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    _write_weights(tmp_path, {"AAA": 1.0})
    result = rf.book_factor_vector(missing, tmp_path)
    assert result.vector == {}
    assert result.top_contributors == {}
    assert result.availability == "missing_table"
    assert result.excluded_tickers == ("AAA",)


def test_book_factor_vector_never_raises_on_missing_weights(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    result = rf.book_factor_vector(db_path, tmp_path)
    assert result.vector == {}
    assert result.availability == "unavailable"


def test_book_factor_vector_coverage_and_provenance_full(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.8, rationale="r")],
        provenance="thesis_derived",
        input_sha="s1",
        db_path=db_path,
    )
    rf.persist_exposures(
        "BBB",
        [rf.FactorLoading(factor=_FACTOR_B, loading=0.5, rationale="r")],
        provenance="thesis_derived",
        input_sha="s2",
        db_path=db_path,
    )
    _write_weights(tmp_path, {"AAA": 0.5, "BBB": 0.5, "USD": 0.2})

    result = rf.book_factor_vector(db_path, tmp_path)
    assert result.availability == "full"
    assert result.coverage_pct == 100.0
    assert result.covered_weight_pct == 1.0
    assert result.total_weight_pct == 1.0
    assert result.evaluated_tickers == ("AAA", "BBB")
    assert result.excluded_tickers == ()
    assert result.source_as_of is not None
    assert result.registry_version == rf.TAXONOMY_VERSION


def test_book_factor_vector_partial_coverage_below_70_pct(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    rf.persist_exposures(
        "AAA",
        [rf.FactorLoading(factor=_FACTOR_A, loading=0.8, rationale="r")],
        provenance="thesis_derived",
        input_sha="s1",
        db_path=db_path,
    )
    # BBB is in weights but has NO exposures in DB
    _write_weights(tmp_path, {"AAA": 0.3, "BBB": 0.7})

    result = rf.book_factor_vector(db_path, tmp_path)
    assert result.availability == "partial"
    assert result.coverage_pct == 30.0
    assert result.covered_weight_pct == 0.3
    assert result.total_weight_pct == 1.0
    assert result.evaluated_tickers == ("AAA",)
    assert result.excluded_tickers == ("BBB",)


# ---------------------------------------------------------------------------
# compute_input_sha — determinism
# ---------------------------------------------------------------------------


def test_compute_input_sha_is_order_independent_over_mix_shares() -> None:
    from allocation.exposure import NameMix

    mix_a = NameMix(
        ticker="AAA",
        dim_type="geography",
        basis="annual",
        period_end="2026-01-01",
        currency="USD",
        shares={"US": 0.6, "Brazil": 0.4},
    )
    mix_b = NameMix(
        ticker="AAA",
        dim_type="geography",
        basis="annual",
        period_end="2026-01-01",
        currency="USD",
        shares={"Brazil": 0.4, "US": 0.6},
    )
    sha_a = rf.compute_input_sha("AAA", geo_mix=mix_a, product_mix=None, thesis_sha=None)
    sha_b = rf.compute_input_sha("AAA", geo_mix=mix_b, product_mix=None, thesis_sha=None)
    assert sha_a == sha_b


def test_compute_input_sha_changes_with_thesis_sha() -> None:
    sha_1 = rf.compute_input_sha("AAA", geo_mix=None, product_mix=None, thesis_sha="abc")
    sha_2 = rf.compute_input_sha("AAA", geo_mix=None, product_mix=None, thesis_sha="def")
    assert sha_1 != sha_2


# ---------------------------------------------------------------------------
# portfolio_tickers
# ---------------------------------------------------------------------------


def test_portfolio_tickers_reads_active_portfolio_rows(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES ('BBB','portfolio',NULL)"
        )
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES ('AAA','portfolio',NULL)"
        )
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES ('CCC','watchlist',NULL)"
        )
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, archived_at) "
            "VALUES ('DDD','portfolio','2026-01-01')"
        )
        conn.commit()
    finally:
        conn.close()
    assert rf.portfolio_tickers(db_path) == ["AAA", "BBB"]


def test_portfolio_tickers_empty_on_missing_db(tmp_path: Path) -> None:
    assert rf.portfolio_tickers(tmp_path / "nope.db") == []
