"""
execution/fetch_ir_documents.py
--------------------------------
Download IR documents from the per-ticker URL manifest
(``.tmp/ir_url_manifest/<T>_urls.json``, written by ``discover_ir_documents.py``)
into a ticker staging folder, then — with ``--categorize`` — hand them to
``categorize_ir_uploads.py`` for canonical registration.

Each URL is downloaded to ``ir_documents/<T>/<descriptive>__<url_sha8>.<ext>`` (a
bare filename directly under the ticker folder, NOT a period subdir). That layout
gives the categorizer a strong parent-folder ticker hint and the correct
canonical destination root, so the categorizer can content-classify the file and
move it to ``ir_documents/<T>/<period_end>/<doc_type>__<sha8>.<ext>`` + insert the
``documents`` row (and the legacy JSON-index mirror the LLM step reads). The real
source URL is recorded in ``.tmp/ir_incoming_urls.json`` so the categorizer stamps
it as ``documents.source_url`` (provenance) instead of the ``manual_upload:`` placeholder.

Idempotent: a URL already present in the ``documents`` table (source_url) is
skipped. Best-effort: a failed/forbidden download is logged and skipped (the rest
of the ticker still runs); never bypasses auth.

Usage:
    python execution/fetch_ir_documents.py --ticker GOOG
    python execution/fetch_ir_documents.py --ticker NU --categorize --calendar calendar
    python execution/fetch_ir_documents.py --all --categorize
    python execution/fetch_ir_documents.py --ticker GOOG --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from ir_pipeline._net import (  # noqa: E402
    UnsafeURLError,
    build_public_opener,
    curl_resolve_entries,
    ensure_safe_public_url,
    safe_redirect_url,
)
from log_redact import redact  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)

# A real browser User-Agent. Many issuer file CDNs (e.g. Brookfield's bam.brookfield.com)
# return 403 to a self-identifying bot UA on the DOCUMENT fetch even when robots.txt allows
# it and the (browser-UA) Playwright crawler already harvested the link — so a link we can
# SEE, we couldn't DOWNLOAD (BN: 51 links discovered, 0 downloaded, all 403). Matching the
# crawler's browser UA fixes it. robots.txt is still honored upstream in the crawler; this
# is the same accepted pattern as the robots-UA fix, not auth-wall bypass.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CONNECT_READ_TIMEOUT = 60
_RATE_LIMIT_S = 0.5
_CATEGORIZER = SCRIPT_DIR / "categorize_ir_uploads.py"
_MAX_REDIRECTS = 5


def resolve_root(arg_repo_root: str | None) -> Path:
    """Root holding ``ir_documents/``, ``data/portfolio.db`` and ``.tmp/``.

    ``--repo-root`` wins; else ``IR_PROJECT_ROOT`` (a worktree points this at the
    main checkout where the gitignored data lives); else this repo.
    """
    if arg_repo_root:
        return Path(arg_repo_root).resolve()
    env = os.environ.get("IR_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return PROJECT_ROOT


def manifest_dir(root: Path) -> Path:
    return root / ".tmp" / "ir_url_manifest"


def incoming_urls_path(root: Path) -> Path:
    return root / ".tmp" / "ir_incoming_urls.json"


def load_url_manifest(root: Path, ticker: str) -> list[dict[str, object]]:
    """Load ``.tmp/ir_url_manifest/<T>_urls.json`` (a list of entry dicts)."""
    path = manifest_dir(root) / f"{ticker.upper()}_urls.json"
    if not path.exists():
        log.warning({"event": "manifest_not_found", "ticker": ticker, "path": str(path)})
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [cast("dict[str, object]", e) for e in cast("list[object]", raw) if isinstance(e, dict)]


def _url_sha8(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]


def _safe_token(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-") or "x"


def _staging_base_name(ticker: str, doc_type: str, year: object, quarter: object, url: str) -> str:
    """``<T>_<doctype>_<period>__<url_sha8>`` (no extension; period 'undated' if unknown)."""
    period = f"{year}{quarter}" if (year and quarter) else "undated"
    return f"{ticker.upper()}_{_safe_token(doc_type or 'document')}_{_safe_token(period)}__{_url_sha8(url)}"


def _ext_from_headers(url: str, content_disposition: str, content_type: str) -> str:
    """Decide the on-disk extension. The content classifier dispatches on suffix,
    so this must match the bytes (an xlsx saved as .pdf would fail to fingerprint)."""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', content_disposition)
    if m:
        name = m.group(1).lower()
        for e in (".xlsx", ".xls", ".pdf"):
            if name.endswith(e):
                return e
    ct = content_type.lower()
    if "spreadsheet" in ct or "excel" in ct:
        return ".xlsx"
    if "pdf" in ct:
        return ".pdf"
    low = url.lower()
    for e in (".xlsx", ".xls", ".pdf"):
        if e in low:
            return e
    return ".pdf"


def _registered_source_urls(db_path: Path, ticker: str) -> set[str]:
    """source_urls already in the ``documents`` table for ``ticker`` (idempotency key)."""
    if not db_path.exists():
        return set()
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            rows = conn.execute(
                "SELECT source_url FROM documents WHERE ticker = ? AND source_url IS NOT NULL",
                (ticker.upper(),),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows if r[0]}


# curl_cffi impersonation target — the rolling "latest Chrome" alias replays a
# real Chrome JA3/HTTP2 fingerprint, which is what the tarpit hosts check for.
_IMPERSONATE = "chrome"


def _fetch_curl_cffi(url: str) -> tuple[bytes, str, str] | None:
    """Fetch ``url`` impersonating Chrome's full TLS/HTTP2 fingerprint.

    Some issuer hosts (e.g. ``investor.lilly.com``) tarpit any client whose TLS
    fingerprint isn't a real browser's — they accept the connection but never
    respond, so a plain urllib GET stalls until timeout, while a real Chrome is
    served instantly. ``curl_cffi`` replays Chrome's JA3/HTTP2 fingerprint, which
    clears the tarpit. Optional dep (the ``ir`` extra); a missing dep or any fetch
    failure degrades to a logged skip, never a crash.

    Returns ``(data, content_disposition, content_type)`` or None.
    """
    try:
        from curl_cffi import CurlOpt
        from curl_cffi import requests as cc
    except ImportError:
        log.error({"event": "curl_cffi_unavailable", "url": redact(url)})
        return None
    current = url
    cc_any = cast(Any, cc)
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            with cc_any.Session(
                trust_env=False,
                curl_options={CurlOpt.RESOLVE: curl_resolve_entries(current)},
            ) as session:
                ensure_safe_public_url(current)
                resp = session.get(
                    current,
                    impersonate=_IMPERSONATE,
                    headers={
                        "Accept": (
                            "application/pdf,application/vnd.ms-excel,application/octet-stream,*/*"
                        )
                    },
                    timeout=_CONNECT_READ_TIMEOUT,
                    allow_redirects=False,
                )
        except (UnsafeURLError, ValueError) as e:
            log.warning({"event": "unsafe_url_blocked", "url": redact(current), "error": redact(e)})
            return None
        except Exception as e:  # curl_cffi exposes a broad error tree; degrade safely
            log.error(
                {"event": "curl_cffi_failed", "url": redact(current), "error": redact(e)[:120]}
            )
            return None
        if 300 <= resp.status_code < 400:
            try:
                current = safe_redirect_url(current, resp.headers.get("Location", "") or "")
            except UnsafeURLError as e:
                log.warning(
                    {
                        "event": "unsafe_redirect_blocked",
                        "url": redact(current),
                        "error": redact(e),
                    }
                )
                return None
            continue
        if resp.status_code != 200:
            log.error(
                {"event": "curl_cffi_http", "url": redact(current), "status": resp.status_code}
            )
            return None
        cd = resp.headers.get("Content-Disposition", "") or ""
        ct = resp.headers.get("Content-Type", "") or ""
        return resp.content, cd, ct
    log.warning({"event": "too_many_redirects", "url": redact(url)})
    return None


def _fetch_bytes(url: str) -> tuple[bytes, str, str] | None:
    """``(data, content_disposition, content_type)`` for ``url``, or None.

    A plain browser-UA urllib GET first (fast, no extra dep for the ~95% of CDNs
    that serve cleanly); on a connection/timeout failure — the signature of a
    TLS-fingerprint tarpit — fall back to a Chrome-impersonating ``curl_cffi`` GET.
    An explicit HTTP error (403/404) is a real refusal, not a tarpit, so it is NOT
    retried.
    """
    try:
        ensure_safe_public_url(url)
    except UnsafeURLError as e:
        log.warning({"event": "unsafe_url_blocked", "url": redact(url), "error": redact(e)})
        return None
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            # Browser-like Accept so strict CDNs serve the file rather than 406/403.
            "Accept": "application/pdf,application/vnd.ms-excel,application/octet-stream,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        opener = build_public_opener()
        ensure_safe_public_url(url)
        with opener.open(req, timeout=_CONNECT_READ_TIMEOUT) as resp:
            return (
                resp.read(),
                resp.headers.get("Content-Disposition", "") or "",
                resp.headers.get("Content-Type", "") or "",
            )
    except urllib.error.HTTPError as e:
        log.error({"event": "http_error", "url": redact(url), "status": e.code})
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Connection refused / DNS / read-timeout (the tarpit signature) → try the
        # TLS-impersonating client before giving up.
        log.warning(
            {"event": "urllib_failed_trying_curl", "url": redact(url), "error": redact(e)[:120]}
        )
        return _fetch_curl_cffi(url)


def _download(url: str, dest_dir: Path, base_name: str) -> Path | None:
    """GET ``url`` into ``dest_dir/<base_name><ext>``; ext from response headers.

    Returns the written Path, or None on any network/HTTP error (logged, skipped).
    Falls back to a Chrome-TLS-impersonating client for hosts that tarpit urllib.
    """
    fetched = _fetch_bytes(url)
    if fetched is None:
        return None
    data, cd, ct = fetched
    ext = _ext_from_headers(url, cd, ct)
    dest = dest_dir / f"{base_name}{ext}"
    if dest.exists():
        log.info({"event": "already_staged", "path": str(dest)})
        return dest

    # Some IR doc links serve an HTML error/redirect page (with a .pdf
    # Content-Disposition). Don't stage HTML as a .pdf — it just fails pypdf
    # downstream and clutters the quarantine.
    if dest.suffix in {".pdf", ".xlsx", ".xls"} and data[:512].lstrip()[:15].lower().startswith(
        (b"<!doctype", b"<html", b"<?xml")
    ):
        log.warning({"event": "html_not_document", "url": redact(url)})
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    log.info({"event": "downloaded", "dest": str(dest), "size_kb": len(data) // 1024})
    return dest


def _write_incoming_sidecar(root: Path, mapping: dict[str, str]) -> None:
    """Merge ``{staging_filename: source_url}`` into ``.tmp/ir_incoming_urls.json``."""
    if not mapping:
        return
    path = incoming_urls_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = {str(k): str(v) for k, v in cast("dict[str, object]", loaded).items()}
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(mapping)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _run_categorize(ticker: str, root: Path, db_path: Path, calendar: str | None) -> int:
    """Invoke categorize_ir_uploads.py for ``ticker`` (content-classify + register)."""
    argv = [
        *managed_python_prefix(PROJECT_ROOT),
        str(_CATEGORIZER),
        "--ticker",
        ticker.upper(),
        "--source-dir",
        str(root / "ir_documents"),
        "--db-path",
        str(db_path),
        "--rel-root",
        str(root),
    ]
    if calendar:
        argv += ["--calendar", calendar]
    env = dict(os.environ, IR_PROJECT_ROOT=str(root))
    proc = subprocess.run(argv, env=env, check=False)
    return proc.returncode


def process_ticker(
    ticker: str,
    *,
    root: Path,
    db_path: Path,
    dry_run: bool = False,
    categorize: bool = False,
    calendar: str | None = None,
) -> dict[str, object]:
    """Download every manifest URL for ``ticker`` into staging; optionally categorize."""
    ticker = ticker.upper()
    entries = load_url_manifest(root, ticker)
    if not entries:
        empty: dict[str, object] = {
            "ticker": ticker,
            "status": "no_manifest",
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
        }
        print(json.dumps(empty))
        return empty

    already = _registered_source_urls(db_path, ticker)
    dest_dir = root / "ir_documents" / ticker
    sidecar: dict[str, str] = {}
    downloaded = skipped = failed = 0

    for entry in entries:
        url = str(entry.get("url") or "")
        if not url:
            continue
        if url in already:
            skipped += 1
            continue
        base = _staging_base_name(
            ticker, str(entry.get("doc_type") or ""), entry.get("year"), entry.get("quarter"), url
        )
        if dry_run:
            print(f"[DRY RUN] would download {url} -> {dest_dir / base}.<ext>")
            skipped += 1
            continue
        dest = _download(url, dest_dir, base)
        time.sleep(_RATE_LIMIT_S)
        if dest is None:
            failed += 1
            continue
        sidecar[dest.name] = url
        downloaded += 1

    if not dry_run:
        _write_incoming_sidecar(root, sidecar)

    cat_rc: int | None = None
    if categorize and not dry_run and downloaded:
        cat_rc = _run_categorize(ticker, root, db_path, calendar)

    summary: dict[str, object] = {
        "ticker": ticker,
        "status": "done",
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "categorize_rc": cat_rc,
    }
    print(json.dumps(summary))
    return summary


def verify_ticker(root: Path, db_path: Path, ticker: str) -> None:
    """Report which manifest URLs are registered in the ``documents`` table."""
    ticker = ticker.upper()
    entries = load_url_manifest(root, ticker)
    registered = _registered_source_urls(db_path, ticker)
    if not entries:
        print(f"[{ticker}] no manifest.")
        return
    for entry in entries:
        url = str(entry.get("url") or "")
        mark = "✓" if url in registered else "✗ not registered"
        print(f"  [{mark}] {ticker} {entry.get('doc_type')} {url[:80]}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = resolve_root(args.repo_root)
    db_path = Path(args.db) if args.db else root / "data" / "portfolio.db"

    if args.verify:
        if args.all:
            for p in sorted(manifest_dir(root).glob("*_urls.json")):
                verify_ticker(root, db_path, p.stem.replace("_urls", ""))
        else:
            verify_ticker(root, db_path, args.ticker)
        return 0

    if args.all:
        manifests = sorted(manifest_dir(root).glob("*_urls.json"))
        if not manifests:
            print(f"No URL manifests in {manifest_dir(root)}", file=sys.stderr)
            return 1
        for p in manifests:
            process_ticker(
                p.stem.replace("_urls", ""),
                root=root,
                db_path=db_path,
                dry_run=args.dry_run,
                categorize=args.categorize,
                calendar=args.calendar,
            )
        return 0

    process_ticker(
        args.ticker,
        root=root,
        db_path=db_path,
        dry_run=args.dry_run,
        categorize=args.categorize,
        calendar=args.calendar,
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download IR documents from discovered URL manifests.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Company ticker (e.g. GOOG)")
    group.add_argument(
        "--all", action="store_true", help="Process every manifest in .tmp/ir_url_manifest/"
    )
    p.add_argument(
        "--categorize", action="store_true", help="Run categorize_ir_uploads.py after downloading"
    )
    p.add_argument(
        "--calendar",
        help="Fiscal-calendar id passed to the categorizer (auto-fetch attribution for "
        "tickers not in ISSUER_REGISTRY; see ir_uploads.calendar_id_from_fye)",
    )
    p.add_argument(
        "--verify", action="store_true", help="Report manifest-vs-documents coverage; no download"
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would download; no writes")
    p.add_argument("--db", help="portfolio.db path (default: <root>/data/portfolio.db)")
    p.add_argument(
        "--repo-root", dest="repo_root", help="Root holding ir_documents/ + data/ + .tmp/"
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
