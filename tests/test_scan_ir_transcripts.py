"""Tests for execution/scan_ir_transcripts.py — the post-earnings IR-transcript scan.

The fetch path (Playwright + network) can't run in CI, so these tests pin the
pure window decision (`due_for_scan`), the per-ticker orchestration (`scan_one`,
via monkeypatched seams), and the `last_earnings_date` FMP-cache helper — never
hitting the network or a browser.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sources.earnings_calendar import last_earnings_date

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "scan_ir_transcripts.py"
    spec = importlib.util.spec_from_file_location("scan_ir_transcripts", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_ir_transcripts"] = mod
    spec.loader.exec_module(mod)
    return mod


# Shared fixtures for scan_one: today, an in-window earnings date, a Dec FYE.
_TODAY = date(2026, 5, 10)
_IN_WINDOW = date(2026, 5, 8)  # 2 days before _TODAY
_FYE_DEC = 12


def _earnings(_r: Path, _t: str) -> date:
    return _IN_WINDOW


def _no_earnings(_r: Path, _t: str) -> date | None:
    return None


def _stale_earnings(_r: Path, _t: str) -> date:
    return date(2026, 4, 1)  # 39 days before _TODAY → out of window


def _quarters(_fye: int, _today: date, _n: int) -> list[tuple[int, int]]:
    return [(2026, 1)]


def _true(*_a: object, **_k: object) -> bool:
    return True


def _false(*_a: object, **_k: object) -> bool:
    return False


def _fetch_hit(_spec: object, **_k: object) -> object:
    return SimpleNamespace(source_name="issuer_ir")


def _fetch_none(_spec: object, **_k: object) -> object | None:
    return None


def _fetch_boom(_spec: object, **_k: object) -> object:
    raise RuntimeError("playwright exploded")


# ---------------------------------------------------------------------------
# due_for_scan — pure window decision
# ---------------------------------------------------------------------------


def test_due_for_scan_in_window() -> None:
    mod = _load_module()
    today = date(2026, 5, 10)
    assert mod.due_for_scan(date(2026, 5, 10), today, 14) is True  # day 0
    assert mod.due_for_scan(date(2026, 5, 1), today, 14) is True  # day 9
    assert mod.due_for_scan(date(2026, 4, 26), today, 14) is True  # day 14 (inclusive edge)


def test_due_for_scan_out_of_window() -> None:
    mod = _load_module()
    today = date(2026, 5, 10)
    assert mod.due_for_scan(date(2026, 4, 25), today, 14) is False  # day 15
    assert mod.due_for_scan(None, today, 14) is False  # no calendar cache
    assert mod.due_for_scan(date(2026, 5, 20), today, 14) is False  # future date (negative delta)


# ---------------------------------------------------------------------------
# scan_one — per-ticker orchestration (monkeypatched seams)
# ---------------------------------------------------------------------------


def test_scan_one_out_of_window_no_earnings(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _no_earnings)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "out_of_window"


def test_scan_one_out_of_window_stale_earnings(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _stale_earnings)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "out_of_window"


def test_scan_one_already_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_processed_exists", _true)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "already_ingested"
    assert r.quarter == "Q1_2026"


def test_scan_one_dry_run_would_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_processed_exists", _false)
    # fetch_qa must not run in a dry-run.
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, True)
    assert r.status == "would_fetch"
    assert r.quarter == "Q1_2026"


def test_scan_one_pending_ingest_when_raw_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_processed_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _true)
    # A raw already on disk must NOT trigger a re-fetch — flag it for ingest.
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "pending_ingest"


def test_scan_one_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_processed_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_hit)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "fetched"
    assert r.detail == "issuer_ir"


def test_scan_one_not_published_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_processed_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_none)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "not_published_yet"


def test_scan_one_error_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "last_earnings_date", _earnings)
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _quarters)
    monkeypatch.setattr(mod, "_processed_exists", _false)
    monkeypatch.setattr(mod, "_raw_exists", _false)
    monkeypatch.setattr(mod, "fetch_qa", _fetch_boom)
    r = mod.scan_one("NU", _FYE_DEC, Path("/x"), _TODAY, 14, False)
    assert r.status == "error"
    assert "RuntimeError" in r.detail


# ---------------------------------------------------------------------------
# last_earnings_date — most-recent past date from the FMP calendar cache
# ---------------------------------------------------------------------------


def _write_cal(root: Path, ticker: str, dates: list[str]) -> None:
    fmp = root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / f"{ticker}_earnings_calendar.json").write_text(
        json.dumps([{"date": d} for d in dates]), encoding="utf-8"
    )


def test_last_earnings_date_picks_max_past(tmp_path: Path) -> None:
    # 2099 is future for any plausible run date; max past is 2021-05-08.
    _write_cal(tmp_path, "NU", ["2020-02-20", "2021-05-08", "2099-08-14"])
    assert last_earnings_date(tmp_path, "NU") == date(2021, 5, 8)


def test_last_earnings_date_none_when_cache_missing(tmp_path: Path) -> None:
    assert last_earnings_date(tmp_path, "ZZZZ") is None


def test_last_earnings_date_none_when_all_future(tmp_path: Path) -> None:
    _write_cal(tmp_path, "NU", ["2099-01-01", "2099-08-14"])
    assert last_earnings_date(tmp_path, "NU") is None


def test_last_earnings_date_none_on_garbled_json(tmp_path: Path) -> None:
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "NU_earnings_calendar.json").write_text("{not json", encoding="utf-8")
    assert last_earnings_date(tmp_path, "NU") is None
