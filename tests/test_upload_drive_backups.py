from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

from runtime import backup_retention as retention

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))

import upload_drive_backups as uploader


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
        self, *, body: dict[str, Any], media_body: Media | None = None, fields: str, **kwargs: Any
    ) -> UploadCall | Call:
        del fields
        file_id = str(kwargs["fileId"])
        item = next(value for value in self.items if value["id"] == file_id)
        item.update(body)
        if media_body is None:
            return Call(item)
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


def _verified_snapshot(root: Path, stamp: str) -> Path:
    path = root / f"portfolio.db.{stamp}.gz.enc"
    path.write_bytes(stamp.encode())
    retention.record_snapshot(
        path, family="portfolio-db", source=root / "live.db", snapshot_sha256="a" * 64
    )
    return path


def _cli(root: Path, *flags: str) -> list[str]:
    return [
        "--source-dir",
        str(root),
        "--pattern",
        "portfolio.db.*.gz.enc",
        "--folder",
        "portfolio",
        "--backup-set",
        "portfolio-db",
        "--latest-only",
        *flags,
    ]


def test_deferred_upload_retries_unchanged_then_finalizes_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    monkeypatch.setattr(uploader, "media_file_upload", fake_media_upload)
    monkeypatch.setattr(uploader, "PROJECT_ROOT", tmp_path)

    def credentials(_root: object) -> object:
        return object()

    def client(_credentials: object) -> Drive:
        return drive

    monkeypatch.setattr(uploader, "load_credentials", credentials)
    monkeypatch.setattr(uploader, "build_drive_client", client)

    def project_env(_root: Path) -> None:
        pass

    monkeypatch.setattr(uploader, "load_project_env", project_env)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "live.db"))
    old = _verified_snapshot(tmp_path, "20261001")
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    latest = _verified_snapshot(tmp_path, "20261002")
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    assert old.exists() and latest.exists() and drive.files_api.deleted == []
    transfers = len(drive.files_api.upload_calls)
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    assert len(drive.files_api.upload_calls) == transfers
    assert uploader.main(_cli(tmp_path, "--finalize-only")) == 0
    assert not old.exists() and latest.exists()
    assert not any(item.get("name") == old.name for item in drive.files_api.items)


def test_failed_upload_preserves_previous_local_and_remote_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    monkeypatch.setattr(uploader, "media_file_upload", fake_media_upload)
    monkeypatch.setattr(uploader, "PROJECT_ROOT", tmp_path)

    def credentials(_root: object) -> object:
        return object()

    def client(_credentials: object) -> Drive:
        return drive

    monkeypatch.setattr(uploader, "load_credentials", credentials)
    monkeypatch.setattr(uploader, "build_drive_client", client)

    def project_env(_root: Path) -> None:
        pass

    monkeypatch.setattr(uploader, "load_project_env", project_env)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "live.db"))
    old = _verified_snapshot(tmp_path, "20261001")
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    pending = _verified_snapshot(tmp_path, "20261002")

    def fail_upload(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(uploader, "upload_file", fail_upload)
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 1
    assert uploader.main(_cli(tmp_path, "--finalize-only")) == 1
    assert old.exists() and pending.exists() and drive.files_api.deleted == []


def test_remote_retention_preserves_unknown_pending_and_recovery_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    monkeypatch.setattr(uploader, "media_file_upload", fake_media_upload)
    folder = uploader.ensure_folder_path(drive, ["portfolio"])
    old = _verified_snapshot(tmp_path, "20261001")
    latest = _verified_snapshot(tmp_path, "20261002")
    for path in (old, latest):
        uploader.upload_file(drive, folder, path, backup_set="portfolio-db")
        retention.mark_uploaded(path, family="portfolio-db")
    old_remote = next(item for item in drive.files_api.items if item.get("name") == old.name)
    old_remote["appProperties"]["upload_status"] = "pending"
    assert (
        uploader.prune_remote(drive, folder, backup_set="portfolio-db", retain=1, survivor=latest)
        == []
    )

    old_remote["appProperties"]["upload_status"] = "complete"
    receipt = retention.validated_receipt(old, family="portfolio-db")
    assert receipt is not None
    retention.write_receipt(old, receipt.model_copy(update={"recovery_pin": True}))
    assert (
        uploader.prune_remote(drive, folder, backup_set="portfolio-db", retain=1, survivor=latest)
        == []
    )


def test_retry_selects_verified_configured_source_and_preserves_failed_newer_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    monkeypatch.setattr(uploader, "media_file_upload", fake_media_upload)
    monkeypatch.setattr(uploader, "PROJECT_ROOT", tmp_path)

    def credentials(_root: object) -> object:
        return object()

    def client(_credentials: object) -> Drive:
        return drive

    def no_environment(_root: Path) -> None:
        pass

    monkeypatch.setattr(uploader, "load_credentials", credentials)
    monkeypatch.setattr(uploader, "build_drive_client", client)
    monkeypatch.setattr(uploader, "load_project_env", no_environment)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "live.db"))
    old = _verified_snapshot(tmp_path, "20261001")
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    good = _verified_snapshot(tmp_path, "20261002")
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    failed = tmp_path / "portfolio.db.20261003.gz.enc"
    failed.write_bytes(b"failed encryption output")
    foreign = tmp_path / "portfolio.db.20261004.gz.enc"
    foreign.write_bytes(b"other source")
    retention.record_snapshot(
        foreign, family="portfolio-db", source=tmp_path / "other.db", snapshot_sha256="b" * 64
    )
    assert uploader.main(_cli(tmp_path, "--defer-retention")) == 0
    assert uploader.main(_cli(tmp_path, "--finalize-only")) == 0
    assert all(path.exists() for path in (old, good, failed, foreign))
    assert drive.files_api.deleted == []
    assert not any(
        item.get("name") in {failed.name, foreign.name} for item in drive.files_api.items
    )
