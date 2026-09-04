# pyright: reportPrivateUsage=false
"""Cache hit must read/decode once, bypass LLM, return exact payload."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from compute.valuation_basis import (
    _CACHE_VERSION,
    ValuationHistPoint,
    _available_estimates_md,
    _financial_profile_md,
    extract_for_ticker,
)


def test_cache_hit_decodes_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_metrics: list[dict[str, object]] = [
        {"date": "2026-03-31", "enterpriseValue": 100.0, "marketCap": 90.0}
    ]
    thesis = "thesis-text"

    def _fake_quarterly(repo: Path, ticker: str, filename: str) -> list[dict[str, object]]:
        _ = (repo, ticker)
        return [dict(r) for r in key_metrics] if filename == "key_metrics_quarterly.json" else []

    def _fake_list(repo: Path, ticker: str, filename: str) -> list[dict[str, object]]:
        _ = (repo, ticker, filename)
        return []

    def _fake_thesis(repo: Path, ticker: str) -> str:
        _ = (repo, ticker)
        return thesis

    def _fake_sector(repo: Path, ticker: str) -> tuple[str | None, str | None]:
        _ = (repo, ticker)
        return (None, None)

    def _fake_override(repo: Path, ticker: str) -> str | None:
        _ = (repo, ticker)
        return None

    def _fail_llm(*args: Any, **kwargs: Any) -> str:
        _ = (args, kwargs)
        raise AssertionError("LLM must not be called on cache hit")

    monkeypatch.setattr("compute.valuation_basis._load_quarterly", _fake_quarterly)
    monkeypatch.setattr("compute.valuation_basis._load_list", _fake_list)
    monkeypatch.setattr("compute.valuation_basis._load_thesis", _fake_thesis)
    monkeypatch.setattr("compute.valuation_basis._load_sector_industry", _fake_sector)
    monkeypatch.setattr("compute.valuation_basis._load_multiple_override", _fake_override)
    monkeypatch.setattr("compute.valuation_basis.generate_valuation_basis", _fail_llm)

    profile = _financial_profile_md(key_metrics, [])
    estimates = _available_estimates_md([])
    inputs_sha = hashlib.sha256(
        (_CACHE_VERSION + "\x00" + thesis + "\x00" + profile + "\x00" + estimates + "\x00").encode(
            "utf-8"
        )
    ).hexdigest()

    cache_path = tmp_path / "data" / "valuation_basis" / "TEST.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ticker": "TEST",
        "multiple_name": "P/E (LTM)",
        "rationale": "r",
        "target_band": "tb",
        "notes": "n",
        "current_value": 12.5,
        "current_value_display": "12.5x",
        "current_period_end": "2026-03-31",
        "history": [
            {"period_end": "2025-12-31", "value": 11.0},
            {"period_end": "2026-03-31", "value": None},
        ],
        "historical_min": 11.0,
        "historical_max": 11.0,
        "historical_median": 11.0,
        "rich_cheap_verdict": "v",
        "peg_ratio": None,
        "peg_growth_pct": None,
        "cache_sha256": inputs_sha,
        "extracted_at": "2026-01-01T00:00:00Z",
        "skipped_reason": None,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    orig_read = Path.read_text
    calls: list[Path] = []

    def _counting(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == cache_path:
            calls.append(self)
        return orig_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting)

    conn = sqlite3.connect(":memory:")
    try:
        result = extract_for_ticker("TEST", tmp_path, conn)
    finally:
        conn.close()

    assert len(calls) == 1
    assert result.ticker == "TEST"
    assert result.multiple_name == "P/E (LTM)"
    assert result.rationale == "r"
    assert result.current_value == 12.5
    assert result.current_value_display == "12.5x"
    assert result.current_period_end == "2026-03-31"
    assert result.cache_sha256 == inputs_sha
    assert result.history == [
        ValuationHistPoint(period_end="2025-12-31", value=11.0),
        ValuationHistPoint(period_end="2026-03-31", value=None),
    ]
