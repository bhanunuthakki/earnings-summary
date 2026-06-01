"""Download an IR document (spreadsheet/PDF) to the repo's ir cache."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _filename_from_content_disposition(cd: str, fallback: str) -> str:
    """Pull the original filename from a Content-Disposition value, else fallback."""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd or "")
    if m:
        return urllib.parse.unquote(m.group(1)).strip().replace("/", "_")
    return fallback


def download_spreadsheet(url: str, repo_root: Path, ticker: str) -> Path:
    """Download `url` into `<repo_root>/data/ir_spreadsheets/<TICKER>/`; return the path.

    The filename comes from the server's Content-Disposition when present (the MZ
    file manager returns the real name, e.g. "Nu Holdings Historical Data 1Q26.xlsx").
    """
    dest_dir = repo_root / "data" / "ir_spreadsheets" / ticker.upper()
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
        cd = resp.headers.get("Content-Disposition", "")
        name = _filename_from_content_disposition(cd, f"{ticker.upper()}_ir_spreadsheet.xlsx")
    dest = dest_dir / name
    dest.write_bytes(data)
    return dest
