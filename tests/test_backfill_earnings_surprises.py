"""Tests for execution/backfill_earnings_surprises.py — ticker resolution,
lookback trimming, per-ticker cache write, and end-to-end backfill flow.

The dispatcher and source functions are exercised by test_surprise_sources.py;
here we focus on the orchestration layer: DB-driven ticker resolution, file
output shape, dry-run semantics, and per-ticker error isolation.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from surprise_sources import SurpriseHit, SurpriseSource

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    """Import the backfill script as a module without executing main().

    Uses the same pattern as test_onboard_pending_tickers — execution/ scripts
    aren't on the package path, so we load by file path.
    """
    src = PROJECT_ROOT / "execution" / "backfill_earnings_surprises.py"
    spec = importlib.util.spec_from_file_location("backfill_earnings_surprises", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_earnings_surprises"] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_tracked_companies(db_path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    """Insert minimal tracked_companies rows: (ticker, list_type, archived_at)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        """
    )
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, name, list_type, archived_at) VALUES (?, ?, ?, ?)",
        [(t, t, lt, arch) for t, lt, arch in rows],
    )
    conn.commit()
    conn.close()


def _hit(release: date, source: str = "fmp_calendar") -> SurpriseHit:
    return SurpriseHit(
        ticker="X",
        release_date=release,
        eps_estimate=Decimal("0.9"),
        eps_actual=Decimal("1.0"),
        revenue_estimate=None,
        revenue_actual=None,
        eps_surprise_pct=Decimal("11.11"),
        revenue_surprise_pct=None,
        num_analysts_eps=None,
        num_analysts_revenue=None,
        source_name=source,
        source_url=None,
    )


# --- _trim_to_lookback -------------------------------------------------------


def test_trim_to_lookback_keeps_most_recent() -> None:
    mod = _load_module()
    hits = [_hit(date(2024, q, 1)) for q in (3, 6, 9, 12)]
    out = mod._trim_to_lookback(hits, 2)
    assert [h.release_date for h in out] == [date(2024, 9, 1), date(2024, 12, 1)]


def test_trim_to_lookback_zero_keeps_all() -> None:
    """Lookback=0 is the documented "keep all" sentinel."""
    mod = _load_module()
    hits = [_hit(date(2024, q, 1)) for q in (3, 6, 9, 12)]
    assert mod._trim_to_lookback(hits, 0) == hits


def test_trim_to_lookback_shorter_than_lookback_unchanged() -> None:
    mod = _load_module()
    hits = [_hit(date(2024, 3, 1)), _hit(date(2024, 6, 1))]
    assert mod._trim_to_lookback(hits, 8) == hits


# --- _resolve_tickers --------------------------------------------------------


def test_resolve_tickers_active_universe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver returns portfolio + watchlist + evaluation tickers; excludes
    archived rows."""
    db_path = tmp_path / "portfolio.db"
    _seed_tracked_companies(
        db_path,
        [
            ("AAPL", "portfolio", None),
            ("MSFT", "watchlist", None),
            ("NVDA", "evaluation", None),
            ("ZZZZ", "pending", None),  # not in ACTIVE list types
            ("XXXX", "portfolio", "2024-01-01"),  # archived
        ],
    )
    # Re-point db module to the fixture DB
    import db

    monkeypatch.setattr(db, "DB_PATH", str(db_path))

    mod = _load_module()
    tickers = mod._resolve_tickers(None)
    assert set(tickers) == {"AAPL", "MSFT", "NVDA"}


def test_resolve_tickers_single_ticker_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_tracked_companies(
        db_path,
        [
            ("AAPL", "portfolio", None),
            ("MSFT", "watchlist", None),
        ],
    )
    import db

    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    mod = _load_module()
    assert mod._resolve_tickers("AAPL") == ["AAPL"]
    # Case-insensitive
    assert mod._resolve_tickers("aapl") == ["AAPL"]
    # Unknown ticker -> empty
    assert mod._resolve_tickers("NOSUCH") == []


# --- _write_ticker_cache -----------------------------------------------------


def test_write_ticker_cache_writes_valid_json(tmp_path: Path) -> None:
    mod = _load_module()
    surprise_dir = tmp_path / "surprise"
    hits = [_hit(date(2024, 3, 1)), _hit(date(2024, 6, 1))]
    out = mod._write_ticker_cache("WIX", hits, surprise_dir, dry_run=False)
    assert out == surprise_dir / "WIX_surprises.json"
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ticker"] == "WIX"
    assert payload["record_count"] == 2
    assert len(payload["records"]) == 2
    assert payload["records"][0]["release_date"] == "2024-03-01"
    # Decimals serialize as strings (precision preserved)
    assert payload["records"][0]["eps_actual"] == "1.0"


def test_write_ticker_cache_dry_run_no_file(tmp_path: Path) -> None:
    mod = _load_module()
    surprise_dir = tmp_path / "surprise"
    out = mod._write_ticker_cache("WIX", [_hit(date(2024, 3, 1))], surprise_dir, dry_run=True)
    # Returns the would-be path but does NOT create the file/dir
    assert out == surprise_dir / "WIX_surprises.json"
    assert not out.exists()
    assert not surprise_dir.exists()


def test_write_ticker_cache_atomic_overwrite(tmp_path: Path) -> None:
    """Re-running with new data replaces the file atomically (no .tmp leftover)."""
    mod = _load_module()
    surprise_dir = tmp_path / "surprise"
    mod._write_ticker_cache("WIX", [_hit(date(2024, 3, 1))], surprise_dir, dry_run=False)
    mod._write_ticker_cache("WIX", [_hit(date(2024, 9, 1))], surprise_dir, dry_run=False)
    payload = json.loads((surprise_dir / "WIX_surprises.json").read_text())
    assert payload["records"][0]["release_date"] == "2024-09-01"
    # No leftover temp file
    assert not (surprise_dir / "WIX_surprises.json.tmp").exists()


# --- _backfill_one (the per-ticker orchestrator) ----------------------------


def test_backfill_one_writes_trimmed_cache(tmp_path: Path) -> None:
    mod = _load_module()
    # Stub source that returns 10 fake hits across release dates
    hits_in = [_hit(date(2022 + (i // 4), ((i % 4) + 1) * 3, 1)) for i in range(10)]
    src = SurpriseSource(name="fmp_calendar", fetch_all=lambda _t: hits_in)
    result = mod._backfill_one(
        ticker="WIX",
        sources=[src],
        surprise_dir=tmp_path / "surprise",
        lookback=4,
        dry_run=False,
    )
    assert result.error is None
    assert result.hits_total == 10
    assert result.hits_written == 4
    assert result.sources_per_hit == {"fmp_calendar": 4}
    assert result.output_path is not None
    payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    assert payload["record_count"] == 4


def test_backfill_one_dry_run_writes_nothing(tmp_path: Path) -> None:
    mod = _load_module()
    src = SurpriseSource(name="fmp_calendar", fetch_all=lambda _t: [_hit(date(2024, 3, 1))])
    result = mod._backfill_one(
        ticker="WIX",
        sources=[src],
        surprise_dir=tmp_path / "surprise",
        lookback=4,
        dry_run=True,
    )
    assert result.hits_written == 1
    # Directory not created
    assert not (tmp_path / "surprise").exists()


def test_backfill_one_captures_source_exception(tmp_path: Path) -> None:
    """When a source explodes, the per-ticker result records the error and the
    pipeline keeps going (caller iterates tickers, doesn't crash the run)."""
    mod = _load_module()

    def explode(_t: str) -> list[SurpriseHit]:
        raise RuntimeError("network down")

    src = SurpriseSource(name="fmp_calendar", fetch_all=explode)
    result = mod._backfill_one(
        ticker="WIX",
        sources=[src],
        surprise_dir=tmp_path / "surprise",
        lookback=4,
        dry_run=False,
    )
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert result.hits_total == 0
    assert result.hits_written == 0


def test_backfill_one_attribution_across_sources(tmp_path: Path) -> None:
    """Telemetry should show which source contributed how many records in
    the trimmed window — that's what tells you FMP-loss impact."""
    mod = _load_module()
    src1 = SurpriseSource(
        name="fmp_calendar",
        fetch_all=lambda _t: [
            _hit(date(2024, 6, 1), source="fmp_calendar"),
            _hit(date(2024, 9, 1), source="fmp_calendar"),
        ],
    )
    src2 = SurpriseSource(
        name="yfinance",
        fetch_all=lambda _t: [
            _hit(date(2024, 3, 1), source="yfinance"),
            _hit(date(2024, 9, 1), source="yfinance"),  # duplicate — primary wins
        ],
    )
    result = mod._backfill_one(
        ticker="WIX",
        sources=[src1, src2],
        surprise_dir=tmp_path / "surprise",
        lookback=10,
        dry_run=False,
    )
    assert result.hits_written == 3
    assert result.sources_per_hit == {"fmp_calendar": 2, "yfinance": 1}
