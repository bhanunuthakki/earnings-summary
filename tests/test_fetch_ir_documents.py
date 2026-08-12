"""Tests for execution/fetch_ir_documents.py — the IR document downloader.

The load-bearing fix guarded here: the downloader must send a real BROWSER
User-Agent. Issuer file CDNs (e.g. Brookfield's bam.brookfield.com) return 403 to
a self-identifying bot UA on the document fetch even when the (browser-UA) crawler
already harvested the link — which silently zeroed BN (51 links discovered, 0
downloaded, all 403). A blocked URL must still degrade to a skip, never a crash.
"""

from __future__ import annotations

import email.message
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import fetch_ir_documents as fid  # noqa: E402


class _FakeResp:
    """Minimal urlopen() stand-in: context manager + headers.get + read."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.headers: dict[str, str] = {"Content-Type": "application/pdf"}

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


class _FakeOpener:
    """Minimal build_opener() result for URL-guarded downloader tests."""

    def __init__(
        self, open_fn: Callable[[urllib.request.Request, float | None], _FakeResp]
    ) -> None:
        self._open_fn = open_fn

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        return self._open_fn(req, timeout)


def _no_registered_urls(*_a: object, **_k: object) -> set[str]:
    """Typed stand-in for _registered_source_urls (a bare lambda trips pyright)."""
    return set()


def _no_sleep(_seconds: float) -> None:
    return None


def _write_manifest(root: Path, ticker: str, url: str) -> None:
    mdir = fid.manifest_dir(root)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{ticker}_urls.json").write_text(
        json.dumps([{"url": url, "doc_type": "press_release", "year": 2026, "quarter": "Q1"}]),
        encoding="utf-8",
    )


def _make_policy_db(db: Path, rows: list[tuple[str, str]]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL)",
            rows,
        )


def test_downloader_sends_browser_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download Request must carry a browser UA + Accept — the BN 403 fix.

    A regression here (reverting to a self-identifying bot UA) silently 403s every
    file on bot-mitigating issuer CDNs, so guard the UA explicitly.
    """
    root = tmp_path
    db = tmp_path / "p.db"
    _make_policy_db(db, [("BN", "portfolio")])
    _write_manifest(root, "BN", "https://bam.brookfield.com/x/Q1-26-BAM-Press-Release.pdf")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)
    captured: dict[str, str | None] = {}

    def _fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = timeout
        captured["ua"] = req.get_header("User-agent")
        captured["accept"] = req.get_header("Accept")
        return _FakeResp(b"%PDF-1.4 fake document bytes")

    def _fake_opener(*_args: object) -> _FakeOpener:
        return _FakeOpener(_fake_urlopen)

    monkeypatch.setattr(
        "execution.fetch_ir_documents.urllib.request.build_opener",
        _fake_opener,
    )
    summary = fid.process_ticker("BN", root=root, db_path=db, categorize=False)

    assert summary["downloaded"] == 1
    ua = captured["ua"] or ""
    assert "Mozilla" in ua and "Chrome" in ua  # a real browser UA
    assert "InvestorResearchBot" not in ua  # NOT the old self-identifying bot UA
    assert "pdf" in (captured["accept"] or "")


@pytest.mark.parametrize("status", [401, 403])
def test_downloader_halts_on_explicit_auth_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    root = tmp_path
    db = tmp_path / "p.db"
    _make_policy_db(db, [("ZZ", "portfolio")])
    _write_manifest(root, "ZZ", "https://blocked.example/Q1.pdf")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)

    def _raise_auth(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = timeout
        raise urllib.error.HTTPError(
            req.full_url,
            status,
            "Authentication denied",
            email.message.Message(),
            None,
        )

    def _fake_opener(*_args: object) -> _FakeOpener:
        return _FakeOpener(_raise_auth)

    monkeypatch.setattr(fid, "ensure_safe_public_url", lambda _url: None)
    monkeypatch.setattr(fid, "build_public_opener", _fake_opener)
    with pytest.raises(fid.SourceAuthenticationDeniedError) as exc_info:
        fid.process_ticker("ZZ", root=root, db_path=db, categorize=False)
    assert exc_info.value.status_code == status


class _CurlResp:
    """Minimal curl_cffi response stand-in."""

    status_code = 200
    content = b"%PDF-1.4 lilly press release"
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/pdf"}


def test_downloader_falls_back_to_curl_cffi_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A urllib read-timeout (the TLS-tarpit signature) falls back to curl_cffi.

    investor.lilly.com tarpits any non-browser TLS fingerprint — urllib stalls to
    timeout, but a Chrome-impersonating curl_cffi GET is served. A 403 is NOT a
    tarpit, so it is not retried (covered above).
    """
    ccr = pytest.importorskip("curl_cffi.requests")
    root = tmp_path
    db = tmp_path / "p.db"
    _make_policy_db(db, [("LLY", "portfolio")])
    _write_manifest(root, "LLY", "https://investor.lilly.com/static-files/uuid-1")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)

    def _timeout_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = (req, timeout)
        raise TimeoutError("tarpit: the read operation timed out")

    class _FakeSession:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["curl_options"]

        def __enter__(self) -> _FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str, **_kw: object) -> _CurlResp:
            _ = url
            return _CurlResp()

    def _fake_opener(*_args: object) -> _FakeOpener:
        return _FakeOpener(_timeout_urlopen)

    monkeypatch.setattr(
        "execution.fetch_ir_documents.urllib.request.build_opener",
        _fake_opener,
    )
    monkeypatch.setattr(ccr, "Session", _FakeSession)
    summary = fid.process_ticker("LLY", root=root, db_path=db, categorize=False)
    assert summary["downloaded"] == 1  # recovered via curl_cffi after urllib stalled


def test_direct_ticker_and_all_cannot_bypass_stored_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "portfolio.db"
    _make_policy_db(
        db,
        [("PORT", "portfolio"), ("EVAL", "evaluation"), ("WATCH", "watchlist")],
    )
    for ticker in ("PORT", "EVAL", "WATCH", "UNKNOWN"):
        _write_manifest(tmp_path, ticker, f"https://issuer.example/{ticker}/2026Q1.pdf")
    calls: list[str] = []

    def _record(url: str, _dest: Path, _base: str) -> Path:
        calls.append(url)
        return tmp_path / "staged.pdf"

    monkeypatch.setattr(fid, "_download", _record)
    monkeypatch.setattr(fid.time, "sleep", _no_sleep)

    assert (
        fid.main(
            [
                "--ticker",
                "WATCH",
                "--repo-root",
                str(tmp_path),
                "--db",
                str(db),
            ]
        )
        == 2
    )
    assert calls == []
    assert "source_collection_policy_denied" in capsys.readouterr().err

    assert fid.main(["--all", "--repo-root", str(tmp_path), "--db", str(db)]) == 0
    assert calls == ["https://issuer.example/PORT/2026Q1.pdf"]


def test_fetch_boundary_skips_manifest_periods_outside_canonical_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "portfolio.db"
    _make_policy_db(db, [("PORT", "portfolio")])
    entries = [
        {
            "url": f"https://issuer.example/{year}Q{quarter}.pdf",
            "doc_type": "press_release",
            "year": year,
            "quarter": f"Q{quarter}",
        }
        for year, quarter in [(2026, 2), (2026, 1), (2025, 4), (2025, 3), (2025, 2), (2025, 1)]
    ]
    mdir = fid.manifest_dir(tmp_path)
    mdir.mkdir(parents=True)
    (mdir / "PORT_urls.json").write_text(json.dumps(entries), encoding="utf-8")
    calls: list[str] = []

    def _record(url: str, _dest: Path, _base: str) -> Path:
        calls.append(url)
        return tmp_path / "staged.pdf"

    monkeypatch.setattr(fid, "_download", _record)
    monkeypatch.setattr(fid.time, "sleep", _no_sleep)

    summary = fid.process_ticker("PORT", root=tmp_path, db_path=db)

    assert len(calls) == 5
    assert "https://issuer.example/2025Q1.pdf" not in calls
    assert summary["policy_skipped"] == 1
