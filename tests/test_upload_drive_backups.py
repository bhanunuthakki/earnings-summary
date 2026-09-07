from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import upload_drive_backups as uploader  # noqa: E402


class Call:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result: dict[str, Any] = result or {}

    def execute(self) -> dict[str, Any]:
        return self.result


class UploadCall:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.retry_counts: list[int] = []

    def next_chunk(self, *, num_retries: int = 0) -> tuple[None, dict[str, Any]]:
        self.retry_counts.append(num_retries)
        return None, self.result


class Files:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.upload_calls: list[UploadCall] = []

    def list(self, **kwargs: Any) -> Call:
        del kwargs
        return Call({"files": list(self.items)})

    def create(
        self, *, body: dict[str, Any], media_body: Media | None = None, fields: str
    ) -> Call | UploadCall:
        del fields
        item = {"id": f"id-{len(self.items)}", **body}
        if media_body is None:
            self.items.append(item)
            return Call(item)
        item.update(media_receipt(media_body.path))
        self.items.append(item)
        upload = UploadCall(item)
        self.upload_calls.append(upload)
        return upload

    def update(
        self, *, body: dict[str, Any], media_body: Media, fields: str, **kwargs: Any
    ) -> UploadCall:
        del fields
        file_id = str(kwargs["fileId"])
        item = next(value for value in self.items if value["id"] == file_id)
        item.update(body)
        item.update(media_receipt(media_body.path))
        upload = UploadCall(item)
        self.upload_calls.append(upload)
        return upload

    def delete(self, **kwargs: Any) -> Call:
        file_id = str(kwargs["fileId"])
        self.deleted.append(file_id)
        self.items = [item for item in self.items if item["id"] != file_id]
        return Call()


class Drive:
    def __init__(self) -> None:
        self.files_api = Files()

    def files(self) -> Files:
        return self.files_api


class Media:
    def __init__(self, path: str, **kwargs: Any) -> None:
        del kwargs
        self.path = path


def media_receipt(path: str) -> dict[str, str]:
    payload = Path(path).read_bytes()
    return {
        "size": str(len(payload)),
        "md5Checksum": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
    }


def fake_media_upload(path: str | Path, **kwargs: Any) -> Media:
    del kwargs
    return Media(str(path))


def test_upload_is_idempotent_and_prunes_only_owned_set(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        uploader,
        "media_file_upload",
        fake_media_upload,
    )
    drive = Drive()
    folder = uploader.ensure_folder_path(drive, ["Windows headless backups", "portfolio"])
    first = tmp_path / "portfolio.db.20260905.gz.enc"
    second = tmp_path / "portfolio.db.20260906.gz.enc"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    assert uploader.upload_file(drive, folder, first, backup_set="portfolio") == "created"
    assert uploader.upload_file(drive, folder, first, backup_set="portfolio") == "unchanged"
    next(item for item in drive.files_api.items if item.get("name") == first.name)[
        "md5Checksum"
    ] = "corrupt-remote-receipt"
    assert uploader.upload_file(drive, folder, first, backup_set="portfolio") == "updated"
    assert uploader.upload_file(drive, folder, second, backup_set="portfolio") == "created"
    assert all(
        call.retry_counts == [uploader.UPLOAD_CHUNK_RETRIES]
        for call in drive.files_api.upload_calls
    )
    drive.files_api.items.append(
        {"id": "foreign", "name": "notes.txt", "appProperties": {"backup_owner": "other"}}
    )

    removed = uploader.prune_remote(drive, folder, backup_set="portfolio", retain=1)

    assert removed == [first.name]
    assert any(item["id"] == "foreign" for item in drive.files_api.items)


def test_folder_segments_are_validated() -> None:
    drive = Drive()
    try:
        uploader.ensure_folder_path(drive, ["../escape"])
    except ValueError as exc:
        assert "invalid Drive folder segment" in str(exc)
    else:
        raise AssertionError("unsafe folder segment was accepted")
