from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ir_pipeline import download


class _Response:
    def __init__(self, body: bytes, *, content_length: str | None = None) -> None:
        self._body = body
        self._offset = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        start = self._offset
        self._offset = min(len(self._body), self._offset + size)
        return self._body[start : self._offset]


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, *_args: object, **_kwargs: object) -> _Response:
        return self.response


def _ignore_url(_url: str) -> None:
    return None


@pytest.fixture(autouse=True)
def _safe_network_seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(download, "ensure_safe_public_url", _ignore_url)
    yield


def test_download_streams_to_atomic_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"spreadsheet-bytes"
    monkeypatch.setattr(download, "build_public_opener", lambda: _Opener(_Response(body)))

    result = download.download_spreadsheet("https://ir.example/file.xlsx", tmp_path, "abc")

    assert result.read_bytes() == body
    assert not list(result.parent.glob("*.tmp"))


def test_download_rejects_declared_oversize_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _Response(b"ignored", content_length=str(download.MAX_IR_DOWNLOAD_BYTES + 1))
    monkeypatch.setattr(download, "build_public_opener", lambda: _Opener(response))

    with pytest.raises(ValueError, match="ir_download_too_large"):
        download.download_spreadsheet("https://ir.example/file.xlsx", tmp_path, "abc")

    assert not list((tmp_path / "data" / "ir_spreadsheets" / "ABC").glob("*"))


def test_download_rejects_stream_overflow_without_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(download, "MAX_IR_DOWNLOAD_BYTES", 8)
    monkeypatch.setattr(
        download,
        "build_public_opener",
        lambda: _Opener(_Response(b"123456789")),
    )

    with pytest.raises(ValueError, match="ir_download_too_large"):
        download.download_spreadsheet("https://ir.example/file.xlsx", tmp_path, "abc")

    assert not list((tmp_path / "data" / "ir_spreadsheets" / "ABC").glob("*"))
