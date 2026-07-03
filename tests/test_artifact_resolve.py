"""Phase 2: artifact resolution + LLM-free text extraction (research/artifact.py).

Pure functions + a monkeypatched note store — no DB, no network. The SSRF guard and
the readable-text pass are the security-sensitive parts, tested directly."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from research import artifact
from research.artifact import (
    ArtifactRef,
    extract_artifact_text,
    extract_url_from_text,
    html_to_text,
    is_safe_url,
    resolve_artifact,
)
from user_state.notes import AnalystNoteRow

_TS = datetime(2026, 7, 2, 12, 0, 0)


def _note(
    body: str,
    *,
    note_id: int = 1,
    kind: str = "musing",
    context: dict[str, object] | None = None,
    created_at: datetime = _TS,
) -> AnalystNoteRow:
    return AnalystNoteRow(
        id=note_id,
        user_id="owner",
        ticker=None,
        kind=kind,
        status="active",
        body=body,
        anchor_type=None,
        anchor_key=None,
        source="capture",
        source_ref=None,
        supersedes_id=None,
        resolution_note=None,
        context=context,
        created_at=created_at,
        updated_at=created_at,
        resolved_at=None,
    )


# --- URL extraction from the musing body ------------------------------------


def test_extract_url_plain_and_with_prefix() -> None:
    assert extract_url_from_text("https://example.com/x") == "https://example.com/x"
    body = (
        "Curious about takeaways here: "
        "https://newsletter.semianalysis.com/p/tokenbudgeting-our-conversations"
    )
    assert (
        extract_url_from_text(body)
        == "https://newsletter.semianalysis.com/p/tokenbudgeting-our-conversations"
    )


def test_extract_url_strips_trailing_punctuation() -> None:
    assert extract_url_from_text("see https://example.com/a).") == "https://example.com/a"
    assert extract_url_from_text("(https://example.com/b)") == "https://example.com/b"


def test_extract_url_none() -> None:
    assert extract_url_from_text("no link here, just thoughts") is None


# --- resolution -------------------------------------------------------------


def test_resolve_inline_url() -> None:
    ref = resolve_artifact(_note("gist of https://example.com/deck ?"))
    assert ref is not None
    assert ref.kind == "url" and ref.origin == "inline"
    assert ref.url == "https://example.com/deck"


def test_resolve_recent_reading_link(monkeypatch: pytest.MonkeyPatch) -> None:
    musing = _note("stress test that piece I sent", note_id=10)
    reading = _note(
        "https://example.com/article",
        note_id=9,
        kind="observation",
        context={"item_type": "link", "url": "https://example.com/article"},
        created_at=datetime(2026, 7, 2, 11, 0, 0),
    )

    def fake_feed(**_kw: object) -> Sequence[AnalystNoteRow]:
        return [reading]

    monkeypatch.setattr(artifact, "list_capture_feed", fake_feed)
    ref = resolve_artifact(musing)
    assert ref is not None
    assert ref.kind == "url" and ref.origin == "recent_reading"
    assert ref.url == "https://example.com/article" and ref.source_note_id == 9


def test_resolve_recent_reading_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    musing = _note("expand the takeaways of that deck", note_id=10)
    reading = _note(
        "deck.pdf",
        note_id=8,
        kind="observation",
        context={"item_type": "doc", "local_path": "/data/docs/deck.pdf"},
        created_at=datetime(2026, 7, 2, 10, 0, 0),
    )

    def fake_feed(**_kw: object) -> Sequence[AnalystNoteRow]:
        return [reading]

    monkeypatch.setattr(artifact, "list_capture_feed", fake_feed)
    ref = resolve_artifact(musing)
    assert ref is not None
    assert ref.kind == "doc" and ref.local_path == "/data/docs/deck.pdf"


def test_resolve_ignores_future_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reading captured AFTER the musing is not what the musing referred to.
    musing = _note("stress test this", note_id=10, created_at=datetime(2026, 7, 2, 12, 0, 0))
    later = _note(
        "x",
        note_id=11,
        kind="observation",
        context={"item_type": "link", "url": "https://later.com"},
        created_at=datetime(2026, 7, 2, 13, 0, 0),
    )

    def fake_feed(**_kw: object) -> Sequence[AnalystNoteRow]:
        return [later]

    monkeypatch.setattr(artifact, "list_capture_feed", fake_feed)
    assert resolve_artifact(musing) is None


def test_resolve_none_when_no_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    def empty_feed(**_kw: object) -> Sequence[AnalystNoteRow]:
        return []

    monkeypatch.setattr(artifact, "list_capture_feed", empty_feed)
    assert resolve_artifact(_note("just a flat thought")) is None


# --- SSRF guard -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a",
        "http://news.ycombinator.com",
        "https://sub.domain.co/path?q=1",
    ],
)
def test_is_safe_url_accepts_public(url: str) -> None:
    assert is_safe_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://[::1]/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "https://metadata.google.internal/x",
        "",
        "not a url",
    ],
)
def test_is_safe_url_rejects_unsafe(url: str) -> None:
    assert is_safe_url(url) is False


# --- readable-text pass -----------------------------------------------------


def test_html_to_text_strips_chrome_and_prefers_article() -> None:
    html = (
        "<html><head><style>.x{color:red}</style><script>evil()</script></head>"
        "<body><nav>Home About</nav>"
        "<article><h1>Title</h1><p>First para.</p><p>Second para.</p></article>"
        "<footer>copyright 2026</footer></body></html>"
    )
    text = html_to_text(html)
    assert "First para." in text and "Second para." in text
    assert "evil()" not in text
    assert "Home About" not in text
    assert "copyright" not in text


# --- extraction + cache -----------------------------------------------------


def test_extract_url_with_injected_fetch_and_cache(tmp_path: Path) -> None:
    ref = ArtifactRef(kind="url", origin="inline", url="https://example.com/a")
    calls = {"n": 0}

    def fake_fetch(_url: str) -> str:
        calls["n"] += 1
        return "clean article text"

    at = extract_artifact_text(ref, cache_dir=tmp_path, fetch=fake_fetch)
    assert at is not None
    assert at.text == "clean article text" and at.untrusted is True
    assert calls["n"] == 1
    # second call hits the disk cache — fetch is not called again
    at2 = extract_artifact_text(ref, cache_dir=tmp_path, fetch=fake_fetch)
    assert at2 is not None and at2.text == "clean article text"
    assert calls["n"] == 1


def test_extract_url_fetch_failure_returns_none(tmp_path: Path) -> None:
    ref = ArtifactRef(kind="url", origin="inline", url="https://example.com/a")

    def boom(_url: str) -> str:
        raise artifact.ArtifactFetchError("down")

    assert extract_artifact_text(ref, cache_dir=tmp_path, fetch=boom) is None


def test_extract_truncates_and_flags() -> None:
    ref = ArtifactRef(kind="url", origin="inline", url="https://example.com/a")

    def long_fetch(_url: str) -> str:
        return "x" * 50

    at = extract_artifact_text(ref, fetch=long_fetch, max_chars=10)
    assert at is not None
    assert at.truncated is True and len(at.text) == 10


def test_extract_doc_via_monkeypatched_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    def fake_pdf(_path: str) -> str:
        return "extracted deck text"

    monkeypatch.setattr(artifact, "_extract_pdf", fake_pdf)
    ref = ArtifactRef(kind="doc", origin="recent_reading", local_path=str(pdf))
    at = extract_artifact_text(ref)
    assert at is not None
    assert at.text == "extracted deck text" and at.kind == "doc"


def test_extract_doc_missing_file_returns_none() -> None:
    ref = ArtifactRef(kind="doc", origin="inline", local_path="/nope/missing.pdf")
    assert extract_artifact_text(ref) is None


def test_extract_doc_non_pdf_returns_none(tmp_path: Path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("hi", encoding="utf-8")
    ref = ArtifactRef(kind="doc", origin="inline", local_path=str(txt))
    assert extract_artifact_text(ref) is None


def test_resolve_and_extract_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    note = _note("takeaways of https://example.com/x ?", note_id=5)

    def fake_get_note(nid: int, **_kw: object) -> AnalystNoteRow | None:
        return note if nid == 5 else None

    def fake_fetch(_url: str) -> str:
        return "brief me"

    monkeypatch.setattr(artifact, "get_note", fake_get_note)
    at = artifact.resolve_and_extract(5, fetch=fake_fetch)
    assert at is not None
    assert at.text == "brief me" and at.source == "https://example.com/x"
    assert artifact.resolve_and_extract(999, fetch=fake_fetch) is None
