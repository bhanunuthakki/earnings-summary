"""Tests for ``ui.earnings_audio`` — the P7 Google Finance earnings-call
deep link. Covers: NASDAQ/NYSE resolve, OTC/unknown/missing-profile don't,
share-class dash->dot rewriting, and that a bad/missing profile file never
raises."""

from __future__ import annotations

import json
from pathlib import Path

from ui.earnings_audio import google_finance_earnings_url


def _write_profile(root: Path, ticker: str, **fields: object) -> None:
    d = root / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker.upper()}_profile.json").write_text(json.dumps(fields), encoding="utf-8")


def test_nyse_resolves(tmp_path: Path) -> None:
    _write_profile(tmp_path, "NU", exchange="NYSE", exchangeShortName=None)
    assert (
        google_finance_earnings_url(tmp_path, "NU")
        == "https://www.google.com/finance/beta/quote/NU:NYSE?tab=earnings"
    )


def test_nasdaq_resolves(tmp_path: Path) -> None:
    _write_profile(tmp_path, "WIX", exchange="NASDAQ", exchangeShortName=None)
    assert (
        google_finance_earnings_url(tmp_path, "WIX")
        == "https://www.google.com/finance/beta/quote/WIX:NASDAQ?tab=earnings"
    )


def test_exchange_short_name_preferred_when_present(tmp_path: Path) -> None:
    _write_profile(tmp_path, "AAPL", exchange="NASDAQ Global Select", exchangeShortName="NASDAQ")
    assert (
        google_finance_earnings_url(tmp_path, "AAPL")
        == "https://www.google.com/finance/beta/quote/AAPL:NASDAQ?tab=earnings"
    )


def test_otc_renders_no_link(tmp_path: Path) -> None:
    """OTC resolves to a loading Google Finance page (OTCMKTS) but that page
    carries no recorded-call audio/transcript panel (verified in-browser) —
    the whole point of the link — so it must not render one."""
    _write_profile(tmp_path, "NTDOY", exchange="OTC", exchangeShortName=None)
    assert google_finance_earnings_url(tmp_path, "NTDOY") is None


def test_unmapped_exchange_renders_no_link(tmp_path: Path) -> None:
    _write_profile(tmp_path, "XYZ", exchange="TSX", exchangeShortName=None)
    assert google_finance_earnings_url(tmp_path, "XYZ") is None


def test_missing_profile_renders_no_link(tmp_path: Path) -> None:
    assert google_finance_earnings_url(tmp_path, "GHOST") is None


def test_missing_exchange_field_renders_no_link(tmp_path: Path) -> None:
    _write_profile(tmp_path, "NOEX", exchangeShortName=None, exchange=None)
    assert google_finance_earnings_url(tmp_path, "NOEX") is None


def test_corrupt_profile_json_renders_no_link_not_raise(tmp_path: Path) -> None:
    d = tmp_path / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "BAD_profile.json").write_text("{not json", encoding="utf-8")
    assert google_finance_earnings_url(tmp_path, "BAD") is None


def test_share_class_dash_rewritten_to_dot(tmp_path: Path) -> None:
    """FMP spells Berkshire's B-share ``BRK-B``; Google Finance's own quote
    URL scheme takes ``BRK.B`` (verified in-browser: the dash spelling falls
    through to the generic Google Finance home page, not a 404 or redirect,
    which is why this is a rewrite rule and not left to render a dead link)."""
    _write_profile(tmp_path, "BRK-B", exchange="NYSE", exchangeShortName=None)
    assert (
        google_finance_earnings_url(tmp_path, "BRK-B")
        == "https://www.google.com/finance/beta/quote/BRK.B:NYSE?tab=earnings"
    )


def test_string_repo_root_accepted(tmp_path: Path) -> None:
    _write_profile(tmp_path, "NU", exchange="NYSE", exchangeShortName=None)
    assert google_finance_earnings_url(str(tmp_path), "nu") is not None
