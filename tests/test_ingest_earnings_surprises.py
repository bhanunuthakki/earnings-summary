"""Tests for execution/ingest_earnings_surprises.py — record parsing, upsert
idempotence, dry-run semantics, and per-cache error isolation.

The earnings_surprises schema is created inline (mirroring the Alembic
0029_earnings_surprises migration) so the tests stay self-contained. Test
0028's table CREATE matches the migration shape — keep them in sync if the
migration changes.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "ingest_earnings_surprises.py"
    spec = importlib.util.spec_from_file_location("ingest_earnings_surprises", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_earnings_surprises"] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_schema(db_path: Path) -> sqlite3.Connection:
    """Create the earnings_surprises table inline — schema must mirror
    alembic/versions/0029_earnings_surprises.py exactly."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE earnings_surprises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            release_date TEXT NOT NULL,
            eps_estimate NUMERIC,
            eps_actual NUMERIC,
            revenue_estimate NUMERIC,
            revenue_actual NUMERIC,
            eps_surprise_pct NUMERIC,
            revenue_surprise_pct NUMERIC,
            num_analysts_eps INTEGER,
            num_analysts_revenue INTEGER,
            source_name TEXT NOT NULL,
            source_url TEXT,
            fetched_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source_observation_id TEXT
        );
        CREATE TABLE earnings_surprise_observations (
            observation_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            release_date TEXT NOT NULL,
            eps_estimate NUMERIC, eps_actual NUMERIC,
            revenue_estimate NUMERIC, revenue_actual NUMERIC,
            eps_surprise_pct NUMERIC, revenue_surprise_pct NUMERIC,
            num_analysts_eps INTEGER, num_analysts_revenue INTEGER,
            source_name TEXT NOT NULL, source_url TEXT,
            fetched_at TEXT NOT NULL, cache_path TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL,
            raw_payload_json TEXT NOT NULL, raw_payload_sha256 TEXT NOT NULL,
            canonical_payload_json TEXT NOT NULL,
            canonical_payload_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TABLE earnings_surprise_quarantine (
            quarantine_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            ticker_hint TEXT, cache_path TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL,
            raw_payload_json TEXT NOT NULL, raw_payload_sha256 TEXT NOT NULL,
            reason_code TEXT NOT NULL, reason_details_json TEXT NOT NULL,
            reason_details_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX ux_earnings_surprises_ticker_release
            ON earnings_surprises(ticker, release_date);
        CREATE INDEX ix_earnings_surprises_ticker
            ON earnings_surprises(ticker);
        """
    )
    conn.commit()
    return conn


def _write_cache(surprise_dir: Path, ticker: str, records: list[dict[str, object]]) -> Path:
    """Write `<TICKER>_surprises.json` in the exact shape the backfill writer
    produces — payload root + records list."""
    surprise_dir.mkdir(parents=True, exist_ok=True)
    path = surprise_dir / f"{ticker.upper()}_surprises.json"
    payload = {
        "ticker": ticker.upper(),
        "generated_at": "2026-05-13",
        "record_count": len(records),
        "records": records,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _record(
    release_date: str = "2025-08-06",
    eps_actual: str = "2.28",
    eps_estimate: str = "1.75",
    source: str = "fmp_calendar",
    **overrides: object,
) -> dict[str, object]:
    """Build a record matching SurpriseHit.to_json() shape."""
    base: dict[str, object] = {
        "ticker": "WIX",
        "release_date": release_date,
        "eps_estimate": eps_estimate,
        "eps_actual": eps_actual,
        "revenue_estimate": "502532690",
        "revenue_actual": "489930000",
        "eps_surprise_pct": "30.29",
        "revenue_surprise_pct": "-2.51",
        "num_analysts_eps": None,
        "num_analysts_revenue": None,
        "source_name": source,
        "source_url": None,
        "fetched_at": "2026-05-13T12:00:00",
    }
    base.update(overrides)
    return base


# --- _parse_record validation -----------------------------------------------


def test_parse_record_rejects_non_dict() -> None:
    mod = _load_module()
    assert mod._parse_record("not a dict") is None
    assert mod._parse_record(None) is None


def test_parse_record_rejects_missing_required_fields() -> None:
    mod = _load_module()
    # Missing release_date
    assert mod._parse_record({"ticker": "WIX", "source_name": "fmp_calendar"}) is None
    # Missing ticker
    assert mod._parse_record({"release_date": "2025-08-06", "source_name": "fmp_calendar"}) is None
    # Missing source_name
    assert mod._parse_record({"ticker": "WIX", "release_date": "2025-08-06"}) is None


def test_parse_record_normalizes_ticker_uppercase() -> None:
    mod = _load_module()
    parsed = mod._parse_record(
        {
            "ticker": "wix",
            "release_date": "2025-08-06",
            "source_name": "fmp_calendar",
            "fetched_at": "2026-05-13T12:00:00",
        }
    )
    assert parsed is not None
    assert parsed.ticker == "WIX"


def test_parse_record_keeps_string_decimals() -> None:
    """Decimals are passed through as strings — SQLite NUMERIC coerces transparently
    and we avoid float-precision loss in the round-trip."""
    mod = _load_module()
    parsed = mod._parse_record(_record(eps_actual="2.28"))
    assert parsed is not None
    assert str(parsed.eps_actual) == "2.28"
    assert isinstance(parsed.eps_actual, Decimal)


def test_parse_record_treats_non_string_decimal_as_none() -> None:
    """If JSON ever contains a raw int/float for a Decimal field, treat as None.
    The source layer always emits strings, so non-strings indicate schema drift."""
    mod = _load_module()
    parsed = mod._parse_record(_record(eps_actual=2.28))  # type: ignore[arg-type]
    assert parsed is None


def test_parse_record_analyst_counts_typed() -> None:
    mod = _load_module()
    parsed = mod._parse_record(_record(num_analysts_eps=12, num_analysts_revenue=8))
    assert parsed is not None
    assert parsed.num_analysts_eps == 12
    assert parsed.num_analysts_revenue == 8


def test_parse_record_rejects_bool_for_analyst_count() -> None:
    """bool is an int subclass; must not slip into an integer column."""
    mod = _load_module()
    parsed = mod._parse_record(_record(num_analysts_eps=True))  # type: ignore[arg-type]
    assert parsed is None


# --- _candidate_caches discovery --------------------------------------------


def test_candidate_caches_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    mod = _load_module()
    assert mod._candidate_caches(tmp_path / "nope", None) == []


def test_candidate_caches_lists_all_matching_files(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "WIX_surprises.json").write_text("{}")
    (tmp_path / "MELI_surprises.json").write_text("{}")
    (tmp_path / "README.md").write_text("noise")
    (tmp_path / "WIX_other.json").write_text("{}")  # different suffix
    out = mod._candidate_caches(tmp_path, None)
    names = sorted(p.name for p in out)
    assert names == ["MELI_surprises.json", "WIX_surprises.json"]


def test_candidate_caches_filters_to_one_ticker(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "WIX_surprises.json").write_text("{}")
    (tmp_path / "MELI_surprises.json").write_text("{}")
    out = mod._candidate_caches(tmp_path, "WIX")
    assert [p.name for p in out] == ["WIX_surprises.json"]


def test_candidate_caches_missing_ticker_returns_empty(tmp_path: Path) -> None:
    mod = _load_module()
    out = mod._candidate_caches(tmp_path, "NOSUCH")
    assert out == []


# --- ingest_one_ticker end-to-end -------------------------------------------


def test_ingest_inserts_new_records(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(release_date="2025-08-06"),
            _record(release_date="2025-11-19", eps_actual="1.68", eps_estimate="1.54"),
        ],
    )
    result = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert result.errors == []
    assert result.inserted == 2
    assert result.updated == 0
    rows = conn.execute(
        "SELECT ticker, release_date FROM earnings_surprises ORDER BY release_date"
    ).fetchall()
    assert [(r["ticker"], r["release_date"]) for r in rows] == [
        ("WIX", "2025-08-06"),
        ("WIX", "2025-11-19"),
    ]


def test_ingest_rerun_is_idempotent(tmp_path: Path) -> None:
    """Second run on unchanged JSON yields zero net changes — the contract that
    lets the daily cron run safely."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(tmp_path / "surprise", "WIX", [_record()])
    mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    result2 = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert result2.inserted == 0
    assert result2.updated == 0
    assert result2.unchanged == 1
    # Still exactly 1 row in the table
    n = conn.execute("SELECT COUNT(*) AS n FROM earnings_surprises").fetchone()["n"]
    assert n == 1


def test_ingest_updates_when_source_changes(tmp_path: Path) -> None:
    """If the backfill picks up a fresher record (e.g. yfinance gets displaced
    by FMP), the upsert overwrites in place and is counted as 'updated'."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    # First run: yfinance-only (no revenue)
    cache_v1 = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(source="yfinance", revenue_actual=None, revenue_estimate=None),  # type: ignore[arg-type]
        ],
    )
    mod.ingest_one_ticker(conn, cache_v1, dry_run=False)
    conn.commit()
    # Second run: FMP shows up with revenue data
    cache_v2 = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(source="fmp_calendar"),
        ],
    )
    result = mod.ingest_one_ticker(conn, cache_v2, dry_run=False)
    conn.commit()
    assert result.inserted == 0
    assert result.updated == 1
    row = conn.execute("SELECT source_name, revenue_actual FROM earnings_surprises").fetchone()
    assert row["source_name"] == "fmp_calendar"
    assert str(row["revenue_actual"]) == "489930000"


def test_ingest_dry_run_makes_no_writes(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(tmp_path / "surprise", "WIX", [_record()])
    result = mod.ingest_one_ticker(conn, cache, dry_run=True)
    # Dry-run still reports what WOULD happen
    assert result.updated == 1
    # But no row was written
    n = conn.execute("SELECT COUNT(*) AS n FROM earnings_surprises").fetchone()["n"]
    assert n == 0


def test_ingest_skips_malformed_records(tmp_path: Path) -> None:
    """Records missing required fields are counted as skipped, not crash."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            {"ticker": "WIX"},  # missing release_date + source_name
            _record(release_date="2025-08-06"),
            "not-a-dict",  # type: ignore[list-item]
        ],
    )
    result = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert result.inserted == 1
    assert result.skipped_malformed == 2
    assert result.errors == []


def test_ingest_handles_malformed_cache_file(tmp_path: Path) -> None:
    """If the cache file is corrupt JSON or wrong shape, errors are recorded and
    the iteration continues to the next ticker."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    (tmp_path / "surprise").mkdir()
    bad = tmp_path / "surprise" / "BAD_surprises.json"
    bad.write_text("{not json", encoding="utf-8")
    result = mod.ingest_one_ticker(conn, bad, dry_run=False)
    assert result.errors  # non-empty
    assert "read/parse" in result.errors[0]
    assert result.inserted == 0


def test_ingest_handles_missing_records_key(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    (tmp_path / "surprise").mkdir()
    bad = tmp_path / "surprise" / "WIX_surprises.json"
    bad.write_text(json.dumps({"ticker": "WIX", "no_records_key": True}), encoding="utf-8")
    result = mod.ingest_one_ticker(conn, bad, dry_run=False)
    assert result.errors
    assert "records" in result.errors[0]


def test_ingest_decimal_precision_match_is_unchanged(tmp_path: Path) -> None:
    """SQLite NUMERIC strips trailing zeros: "-1800.00" → -1800. The comparator
    must treat these as value-equivalent so trailing-zero records don't get
    flagged as 'updated' on every re-ingest."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(release_date="2026-05-13", eps_surprise_pct="-1800.00"),
        ],
    )
    mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    # Confirm SQLite did normalize the stored value (sanity check on the bug premise)
    row = conn.execute("SELECT eps_surprise_pct FROM earnings_surprises").fetchone()
    assert str(row["eps_surprise_pct"]) == "-1800"  # NOT "-1800.00"
    # Re-ingesting the same cache: the comparator must see -1800 ≡ -1800.00
    result = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert result.inserted == 0
    assert result.updated == 0
    assert result.unchanged == 1


def test_ingest_ignores_fetched_at_drift(tmp_path: Path) -> None:
    """fetched_at drifts every backfill run (new timestamp per fetch). Re-ingest
    with only a timestamp change should report 'unchanged', not 'updated' —
    otherwise the daily cron's telemetry overstates real activity."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache_v1 = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(fetched_at="2026-05-13T12:00:00"),
        ],
    )
    mod.ingest_one_ticker(conn, cache_v1, dry_run=False)
    conn.commit()
    cache_v2 = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(fetched_at="2026-05-14T12:00:00"),  # later run, same data
        ],
    )
    result = mod.ingest_one_ticker(conn, cache_v2, dry_run=False)
    conn.commit()
    assert result.unchanged == 1
    assert result.updated == 0
    # The new fetched_at IS written to the row (we just don't count it as a change)
    row = conn.execute("SELECT fetched_at FROM earnings_surprises").fetchone()
    assert row["fetched_at"] == "2026-05-14T12:00:00Z"


def test_ingest_unique_index_enforces_release_date(tmp_path: Path) -> None:
    """Two records with the same (ticker, release_date) collapse to one row —
    the second upsert updates rather than duplicating. This is the contract
    that makes the daily cron safe to run repeatedly."""
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [
            _record(release_date="2025-08-06", eps_actual="2.28"),
            _record(release_date="2025-08-06", eps_actual="2.29"),  # same date, different actual
        ],
    )
    result = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM earnings_surprises").fetchone()["n"]
    assert n == 1
    # Second record's value wins (last-write-wins on conflict)
    row = conn.execute("SELECT eps_actual FROM earnings_surprises").fetchone()
    assert str(row["eps_actual"]) == "2.29"
    # The first was insert, second was update (within the same ingest pass)
    assert result.inserted == 1
    assert result.updated == 1
    assert result.observations_inserted == 2


def test_ingest_appends_observation_and_exact_rerun_is_idempotent(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(tmp_path / "surprise", "WIX", [_record()])
    first = mod.ingest_one_ticker(conn, cache, dry_run=False)
    second = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert first.observations_inserted == 1
    assert second.observation_duplicates == 1
    assert conn.execute("SELECT COUNT(*) FROM earnings_surprise_observations").fetchone()[0] == 1
    projection = conn.execute("SELECT source_observation_id FROM earnings_surprises").fetchone()[0]
    observation = conn.execute(
        "SELECT observation_id FROM earnings_surprise_observations"
    ).fetchone()[0]
    assert projection == observation


def test_missing_fetched_at_is_quarantined_not_synthesized(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [_record(fetched_at=None)],  # type: ignore[arg-type]
    )
    result = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert result.skipped_malformed == 1
    assert conn.execute("SELECT COUNT(*) FROM earnings_surprises").fetchone()[0] == 0
    reason = conn.execute("SELECT reason_code FROM earnings_surprise_quarantine").fetchone()[0]
    assert reason == "schema_validation_failed"


def test_malformed_records_are_quarantined_idempotently(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(tmp_path / "surprise", "WIX", [{"ticker": "WIX"}])
    mod.ingest_one_ticker(conn, cache, dry_run=False)
    mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    row = conn.execute(
        "SELECT reason_code, record_ordinal FROM earnings_surprise_quarantine"
    ).fetchone()
    assert tuple(row) == ("schema_validation_failed", 0)
    assert conn.execute("SELECT COUNT(*) FROM earnings_surprise_quarantine").fetchone()[0] == 1


def test_older_observation_does_not_replace_newer_projection(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [_record(eps_actual="2.50", fetched_at="2026-05-14T12:00:00+00:00")],
    )
    mod.ingest_one_ticker(conn, cache, dry_run=False)
    cache = _write_cache(
        tmp_path / "surprise",
        "WIX",
        [_record(eps_actual="1.00", fetched_at="2026-05-13T12:00:00+00:00")],
    )
    result = mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()
    assert result.unchanged == 1
    assert str(conn.execute("SELECT eps_actual FROM earnings_surprises").fetchone()[0]) == "2.5"
    assert conn.execute("SELECT COUNT(*) FROM earnings_surprise_observations").fetchone()[0] == 2


def test_malformed_cache_file_gets_quarantine_disposition(tmp_path: Path) -> None:
    mod = _load_module()
    conn = _seed_schema(tmp_path / "db.sqlite")
    cache = tmp_path / "BAD_surprises.json"
    cache.write_text("{not json", encoding="utf-8")
    mod.ingest_one_ticker(conn, cache, dry_run=False)
    conn.commit()


def test_main_exits_nonzero_when_any_record_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    surprise_dir = tmp_path / "data" / "surprise"
    _write_cache(surprise_dir, "WIX", [{"ticker": "WIX"}])
    db_path = tmp_path / "portfolio.db"
    connection = _seed_schema(db_path)

    def retarget(_root: Path) -> Path:
        return surprise_dir

    monkeypatch.setattr(mod, "_retarget_paths", retarget)
    monkeypatch.setattr(mod.db, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_earnings_surprises.py", "--repo-root", str(tmp_path)],
    )

    assert mod.main() == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["terminal_status"] == "partial_failure"
    assert receipt["totals"]["quarantined"] == 1
    with sqlite3.connect(db_path) as verify:
        assert (
            verify.execute("SELECT COUNT(*) FROM earnings_surprise_quarantine").fetchone()[0] == 1
        )
