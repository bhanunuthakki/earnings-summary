"""Download an IR document (spreadsheet/PDF) to the repo's ir cache."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from ir_pipeline._net import build_public_opener, ensure_safe_public_url

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_IR_DOWNLOAD_BYTES = 50_000_000
_DOWNLOAD_CHUNK_BYTES = 256 * 1024


def _filename_from_content_disposition(cd: str, fallback: str) -> str:
    """Pull the original filename from a Content-Disposition value, else fallback.

    Only the leaf name is kept — a server must not be able to steer the write
    out of the destination dir via a ``/`` or ``\\`` separator or a ``..`` part.
    """
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd or "")
    if not m:
        return fallback
    raw = urllib.parse.unquote(m.group(1)).strip()
    leaf = raw.replace("\\", "/").split("/")[-1]
    if not leaf or leaf in (".", ".."):
        return fallback
    return leaf


def download_spreadsheet(url: str, repo_root: Path, ticker: str) -> Path:
    """Download `url` into `<repo_root>/data/ir_spreadsheets/<TICKER>/`; return the path.

    The filename comes from the server's Content-Disposition when present (the MZ
    file manager returns the real name, e.g. "Nu Holdings Historical Data 1Q26.xlsx").
    """
    ensure_safe_public_url(url)
    dest_dir = repo_root / "data" / "ir_spreadsheets" / ticker.upper()
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    opener = build_public_opener()
    with opener.open(req, timeout=120) as resp:
        cd = resp.headers.get("Content-Disposition", "")
        name = _filename_from_content_disposition(cd, f"{ticker.upper()}_ir_spreadsheet.xlsx")
        declared_raw = resp.headers.get("Content-Length")
        if declared_raw is not None:
            try:
                declared = int(declared_raw)
            except ValueError as exc:
                raise ValueError("ir_download_invalid_content_length") from exc
            if declared < 0:
                raise ValueError("ir_download_invalid_content_length")
            if declared > MAX_IR_DOWNLOAD_BYTES:
                raise ValueError("ir_download_too_large")

        dest = dest_dir / name
        fd, temporary = tempfile.mkstemp(dir=dest_dir, prefix=f".{name}.", suffix=".tmp")
        try:
            written = 0
            with os.fdopen(fd, "wb") as handle:
                while chunk := resp.read(_DOWNLOAD_CHUNK_BYTES):
                    written += len(chunk)
                    if written > MAX_IR_DOWNLOAD_BYTES:
                        raise ValueError("ir_download_too_large")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, dest)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
    return dest
