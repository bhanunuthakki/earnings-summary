"""Artifact resolution + text extraction for the capture_intent engage pipeline (Phase 2).

When the intent tap flags a musing ``brief_artifact``/``stress_artifact`` (it writes
``context['engage_intent']`` — see ``research.proposals``), this module finds the
artifact the musing points at and pulls its readable text. It is **LLM-FREE and
deterministic** by design, so Phase 3's brief runs over already-extracted text
(mockable in tests, cached so re-briefing never refetches).

Resolution order:
  1. a URL inline in the musing body (the owner pasted it with their ask), else
  2. the most-recent captured READING (``kind='observation'``, ``item_type`` in
     link/doc) that predates the musing — "stress-test that deck I just sent".

Extraction: URL → ``requests`` (browser UA, SSRF-guarded, size/time-capped) + a
BeautifulSoup readable-text pass; local doc → ``parser.extract_text_from_pdf``.
Both normalized + capped, cached to disk keyed on the source.

SECURITY: the extracted text is UNTRUSTED web/document content — an indirect
prompt-injection surface. ``ArtifactText.untrusted`` carries that provenance so the
Phase-3 brief step spotlights it (BEGIN/END UNTRUSTED-DATA markers), exactly the
reason the intent tap's trust-zone gate never lets fetched text classify itself.
The SSRF guard (``is_safe_url``) blocks non-http(s) schemes and private / loopback
/ link-local / metadata hosts so a pasted link can't reach the local network.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from user_state.notes import AnalystNoteRow, get_note, list_capture_feed

log = logging.getLogger(__name__)

# (url) -> readable text. Injected in tests so the extractor never hits the network.
FetchFn = Callable[[str], str]

_MAX_CHARS = 24_000  # matches ir_narrative's per-doc cap — enough for a brief, bounds the tail
_FETCH_TIMEOUT = 15  # seconds
_MAX_BYTES = 5_000_000  # 5 MB — don't stream a giant asset into memory
_UA = "Mozilla/5.0 (compatible; earnings-summary-ledger/1.0; +artifact-brief)"

# A URL sitting inside free text (the musing body). Stops at whitespace / common
# wrapping punctuation; trailing sentence punctuation is trimmed after the match.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
_PAGE_NUM_RE = re.compile(r"^\d{1,4}$")

# Hostnames that must never be fetched even though they aren't IP literals.
_BLOCKED_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal"})


class ArtifactFetchError(RuntimeError):
    """A URL could not be safely fetched (unsafe target, network error, bad status)."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Where an artifact lives + how we found it."""

    kind: str  # "url" | "doc"
    origin: str  # "inline" | "recent_reading"
    url: str | None = None
    local_path: str | None = None
    source_note_id: int | None = None  # the reading note it came from (recent_reading)

    @property
    def source(self) -> str:
        return self.url or self.local_path or ""


@dataclass(frozen=True, slots=True)
class ArtifactText:
    """Extracted, normalized artifact text + its provenance."""

    text: str
    char_count: int
    truncated: bool
    source: str
    kind: str  # "url" | "doc"
    untrusted: bool = True  # web/document content — the brief step MUST spotlight it


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def extract_url_from_text(text: str) -> str | None:
    m = _URL_IN_TEXT_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)'\"")


def _recent_reading(
    note: AnalystNoteRow, *, db_path: Path | str | None, window: int
) -> AnalystNoteRow | None:
    """The most-recent captured link/doc reading that predates ``note``. Best-effort
    ([] on a missing DB)."""
    rows = list_capture_feed(
        user_id=note.user_id, kinds=("observation",), limit=max(1, window), db_path=db_path
    )
    for r in rows:  # newest-first — the first predating link/doc is the freshest
        if r.id == note.id or r.created_at > note.created_at:
            continue
        if str((r.context or {}).get("item_type") or "") in ("link", "doc"):
            return r
    return None


def resolve_artifact(
    note: AnalystNoteRow, *, db_path: Path | str | None = None, window: int = 15
) -> ArtifactRef | None:
    """Resolve the artifact a ``*_artifact`` musing points at: an inline URL first,
    else the most-recent captured reading. None when nothing is bound."""
    url = extract_url_from_text(note.body)
    if url:
        return ArtifactRef(kind="url", origin="inline", url=url)

    reading = _recent_reading(note, db_path=db_path, window=window)
    if reading is not None:
        ctx = reading.context or {}
        item_type = str(ctx.get("item_type") or "")
        if item_type == "link" and ctx.get("url"):
            return ArtifactRef(
                kind="url", origin="recent_reading", url=str(ctx["url"]), source_note_id=reading.id
            )
        if item_type == "doc" and ctx.get("local_path"):
            return ArtifactRef(
                kind="doc",
                origin="recent_reading",
                local_path=str(ctx["local_path"]),
                source_note_id=reading.id,
            )
    return None


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def is_safe_url(url: str) -> bool:
    """True only for an http(s) URL whose host is not local / private / metadata —
    the SSRF guard for owner-pasted links."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host in _BLOCKED_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    return not (
        ip is not None
        and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    )


def _normalize(text: str) -> str:
    """Collapse whitespace to one line per source line and drop pure page-number
    noise. No length cap here — the caller caps (so the cache keeps the full text)."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line or _PAGE_NUM_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def html_to_text(html: str) -> str:
    """Readable-text pass over fetched HTML: drop chrome, prefer the main article,
    normalize. Pure (no network) so it is unit-testable."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return _normalize(main.get_text(separator="\n"))


def fetch_url_text(url: str, *, timeout: int = _FETCH_TIMEOUT, max_bytes: int = _MAX_BYTES) -> str:
    """Fetch ``url`` (SSRF-guarded, byte-capped) and return its readable text.
    Raises ``ArtifactFetchError`` on an unsafe target or a network/status failure."""
    if not is_safe_url(url):
        raise ArtifactFetchError(f"refusing to fetch unsafe or non-http(s) url: {url!r}")
    import requests

    resp = None
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout, stream=True)
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=16_384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                break
        encoding = resp.encoding or "utf-8"
        html = b"".join(chunks).decode(encoding, "replace")
    except requests.RequestException as exc:
        raise ArtifactFetchError(f"fetch failed for {url!r}: {exc}") from exc
    finally:
        if resp is not None:
            with contextlib.suppress(Exception):
                resp.close()
    return html_to_text(html)


def _extract_pdf(path: str) -> str:
    """Local-PDF text (normalized). Split out so tests can monkeypatch without a
    real PDF fixture."""
    from typing import cast

    import parser  # src/parser.py — its signature is untyped; pin it at the boundary

    extract = cast("Callable[[str], str]", parser.extract_text_from_pdf)
    return _normalize(extract(path))


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def _cache_key(ref: ArtifactRef) -> str:
    return hashlib.sha256(f"{ref.kind}:{ref.source}".encode()).hexdigest()[:32]


def _read_cache(cache_dir: Path | str, key: str) -> str | None:
    path = Path(cache_dir) / f"{key}.txt"
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


def _write_cache(cache_dir: Path | str, key: str, text: str) -> None:
    try:
        directory = Path(cache_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{key}.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


def extract_artifact_text(
    ref: ArtifactRef,
    *,
    cache_dir: Path | str | None = None,
    fetch: FetchFn | None = None,
    max_chars: int = _MAX_CHARS,
) -> ArtifactText | None:
    """Extract + normalize + cap the artifact's text, using the disk cache when
    ``cache_dir`` is given. ``fetch`` injects the URL fetcher for tests. Returns None
    (never raises) when the artifact can't be read — the pipeline degrades, the
    capture never breaks."""
    do_fetch = fetch or fetch_url_text
    key = _cache_key(ref)

    if cache_dir is not None:
        cached = _read_cache(cache_dir, key)
        if cached is not None:
            text, truncated = _cap(cached, max_chars)
            return ArtifactText(
                text=text,
                char_count=len(text),
                truncated=truncated,
                source=ref.source,
                kind=ref.kind,
            )

    raw: str | None = None
    if ref.kind == "url" and ref.url:
        try:
            raw = do_fetch(ref.url)
        except ArtifactFetchError as exc:
            log.warning({"event": "artifact_fetch_failed", "url": ref.url, "error": str(exc)})
            return None
    elif ref.kind == "doc" and ref.local_path:
        path = Path(ref.local_path)
        if not path.exists() or path.suffix.lower() != ".pdf":
            log.warning({"event": "artifact_doc_unreadable", "path": str(path)})
            return None
        try:
            raw = _extract_pdf(str(path))
        except Exception as exc:  # pypdf on an untrusted file — degrade, don't crash
            log.warning(
                {"event": "artifact_pdf_extract_failed", "path": str(path), "error": str(exc)}
            )
            return None

    if not raw or not raw.strip():
        return None
    if cache_dir is not None:
        _write_cache(cache_dir, key, raw)
    text, truncated = _cap(raw, max_chars)
    return ArtifactText(
        text=text, char_count=len(text), truncated=truncated, source=ref.source, kind=ref.kind
    )


def resolve_and_extract(
    note_id: int,
    *,
    db_path: Path | str | None = None,
    cache_dir: Path | str | None = None,
    fetch: FetchFn | None = None,
) -> ArtifactText | None:
    """Convenience: note id → resolved artifact → extracted text (or None). The
    Phase-3 brief entry point calls this."""
    note = get_note(note_id, db_path=db_path)
    if note is None:
        return None
    ref = resolve_artifact(note, db_path=db_path)
    if ref is None:
        return None
    return extract_artifact_text(ref, cache_dir=cache_dir, fetch=fetch)
