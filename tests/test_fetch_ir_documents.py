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
import sys
import urllib.error
import urllib.request
from pathlib import Path

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


def _no_registered_urls(*_a: object, **_k: object) -> set[str]:
    """Typed stand-in for _registered_source_urls (a bare lambda trips pyright)."""
    return set()


def _write_manifest(root: Path, ticker: str, url: str) -> None:
    mdir = fid.manifest_dir(root)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{ticker}_urls.json").write_text(
        json.dumps([{"url": url, "doc_type": "press_release", "year": 2026, "quarter": "Q1"}]),
        encoding="utf-8",
    )


def test_downloader_sends_browser_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download Request must carry a browser UA + Accept — the BN 403 fix.

    A regression here (reverting to a self-identifying bot UA) silently 403s every
    file on bot-mitigating issuer CDNs, so guard the UA explicitly.
    """
    root = tmp_path
    _write_manifest(root, "BN", "https://bam.brookfield.com/x/Q1-26-BAM-Press-Release.pdf")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)
    captured: dict[str, str | None] = {}

    def _fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = timeout
        captured["ua"] = req.get_header("User-agent")
        captured["accept"] = req.get_header("Accept")
        return _FakeResp(b"%PDF-1.4 fake document bytes")

    monkeypatch.setattr("execution.fetch_ir_documents.urllib.request.urlopen", _fake_urlopen)
    summary = fid.process_ticker("BN", root=root, db_path=tmp_path / "p.db", categorize=False)

    assert summary["downloaded"] == 1
    ua = captured["ua"] or ""
    assert "Mozilla" in ua and "Chrome" in ua  # a real browser UA
    assert "InvestorResearchBot" not in ua  # NOT the old self-identifying bot UA
    assert "pdf" in (captured["accept"] or "")


def test_downloader_skips_on_http_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A still-blocked URL degrades to a skip (failed++), never a crash."""
    root = tmp_path
    _write_manifest(root, "ZZ", "https://blocked.example/Q1.pdf")
    monkeypatch.setattr("execution.fetch_ir_documents._registered_source_urls", _no_registered_urls)

    def _raise_403(req: urllib.request.Request, timeout: float | None = None) -> _FakeResp:
        _ = timeout
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", email.message.Message(), None)

    monkeypatch.setattr("execution.fetch_ir_documents.urllib.request.urlopen", _raise_403)
    summary = fid.process_ticker("ZZ", root=root, db_path=tmp_path / "p.db", categorize=False)
    assert summary["downloaded"] == 0
    assert summary["failed"] == 1
