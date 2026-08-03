"""Streaming authenticated encryption for SQLite backup archives."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import struct
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from runtime.secrets import create_secret_text, secret_write_path

MAGIC = b"ESDB\x00"
FORMAT_VERSION = 1
HEADER_LENGTH_BYTES = 4
MAX_HEADER_BYTES = 4096
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024
KEY_FILE_ENV = "ES_DB_BACKUP_KEY_FILE"


def default_key_path() -> Path:
    configured = os.environ.get(KEY_FILE_ENV, "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else secret_write_path("backup_encryption.key")
    )


def _key_path(path: Path | None) -> Path:
    key_path = (path or default_key_path()).resolve()
    if (
        path is not None
        and not os.environ.get(KEY_FILE_ENV)
        and key_path.parent != secret_write_path("backup_encryption.key").parent
    ):
        raise ValueError("custom backup key paths require ES_DB_BACKUP_KEY_FILE")
    return key_path


def load_key(path: Path | None = None) -> bytes:
    """Load an existing 256-bit key without ever creating a replacement."""
    key_path = _key_path(path)
    if not key_path.exists():
        raise RuntimeError(f"backup encryption key is missing at {key_path}")
    try:
        encoded = key_path.read_text(encoding="ascii").strip()
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (OSError, ValueError, binascii.Error) as exc:
        raise RuntimeError(f"cannot read backup encryption key at {key_path}") from exc
    if len(key) != 32:
        raise RuntimeError(f"backup encryption key at {key_path} is not 256-bit")
    return key


def load_or_create_key(path: Path | None = None) -> bytes:
    """Load a 256-bit key, creating it once for the backup writer only."""
    key_path = (path or default_key_path()).resolve()
    if not key_path.exists():
        encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        create_secret_text(key_path, encoded + "\n")
    return load_key(key_path)


def _temp_destination(destination: Path) -> tuple[int, Path]:
    """A temp file whose final ``os.replace`` to ``destination`` is atomic.

    Staging happens IN the destination directory, so same-volume atomicity
    holds by construction. The previous version staged in the system temp dir
    and merely CHECKED the volumes matched — true only while the backup
    destination lived on C:. The 2026-08-02 switch of Google Drive to Stream
    mode moved the destination to a virtual drive (G:) and every backup died
    on the check. A dot-prefixed name keeps Drive from publishing the partial
    file under the final name while it is still being written.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    return fd, Path(name)


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _require_key(key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("backup encryption requires a 256-bit key")


def _header(source: Path, key: bytes) -> bytes:
    return json.dumps(
        {
            "created_at": datetime.now(UTC).isoformat(),
            "key_id": _key_id(key),
            "source_name": source.name,
            "version": FORMAT_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encrypt_file(source: Path, destination: Path, *, key: bytes) -> None:
    """Encrypt *source* to an atomically published AES-256-GCM envelope."""
    _require_key(key)
    nonce = os.urandom(NONCE_BYTES)
    header = _header(source, key)
    if len(header) > MAX_HEADER_BYTES:
        raise RuntimeError("backup encryption header is too large")
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    fd, tmp_path = _temp_destination(destination)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            dst.write(prefix)
            dst.write(nonce)
            while chunk := src.read(CHUNK_BYTES):
                dst.write(encryptor.update(chunk))
            dst.write(encryptor.finalize())
            dst.write(encryptor.tag)
            dst.flush()
            os.fsync(dst.fileno())
        if os.name == "nt":
            os.rename(tmp_path, destination)
        else:
            os.link(tmp_path, destination)
            tmp_path.unlink()
    finally:
        tmp_path.unlink(missing_ok=True)


def decrypt_file(source: Path, destination: Path, *, key: bytes) -> None:
    """Authenticate and decrypt an envelope to an atomically published file."""
    _require_key(key)
    total = source.stat().st_size
    minimum = len(MAGIC) + HEADER_LENGTH_BYTES + 2 + NONCE_BYTES + TAG_BYTES
    if total < minimum:
        raise RuntimeError("encrypted backup is truncated")
    with source.open("rb") as src:
        magic = src.read(len(MAGIC))
        if magic != MAGIC:
            raise RuntimeError("encrypted backup has an unknown format")
        raw_length = src.read(HEADER_LENGTH_BYTES)
        if len(raw_length) != HEADER_LENGTH_BYTES:
            raise RuntimeError("encrypted backup is truncated")
        header_length = struct.unpack(">I", raw_length)[0]
        if not 2 <= header_length <= MAX_HEADER_BYTES:
            raise RuntimeError("encrypted backup header length is invalid")
        header_bytes = src.read(header_length)
        try:
            header_obj = json.loads(header_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("encrypted backup header is invalid") from exc
        if not isinstance(header_obj, dict) or header_obj.get("version") != FORMAT_VERSION:
            raise RuntimeError("encrypted backup version is unsupported")
        if header_obj.get("key_id") != _key_id(key):
            raise RuntimeError("backup encryption key does not match this snapshot")
        prefix = magic + raw_length + header_bytes
        nonce = src.read(NONCE_BYTES)
        payload_start = len(prefix) + NONCE_BYTES
        ciphertext_bytes = total - payload_start - TAG_BYTES
        if ciphertext_bytes < 0:
            raise RuntimeError("encrypted backup is truncated")
        src.seek(total - TAG_BYTES)
        tag = src.read(TAG_BYTES)
        src.seek(payload_start)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(prefix)
        fd, tmp_path = _temp_destination(destination)
        try:
            remaining = ciphertext_bytes
            with os.fdopen(fd, "wb") as dst:
                while remaining:
                    chunk = src.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise RuntimeError("encrypted backup is truncated")
                    remaining -= len(chunk)
                    dst.write(decryptor.update(chunk))
                dst.write(decryptor.finalize())
                dst.flush()
                os.fsync(dst.fileno())
            tmp_path.replace(destination)
        finally:
            tmp_path.unlink(missing_ok=True)
