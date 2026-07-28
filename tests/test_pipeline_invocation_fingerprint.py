from __future__ import annotations

from pathlib import Path

from pipeline.invocation_fingerprint import (
    file_fingerprint,
    files_fingerprint,
    payload_sha256,
)


def test_file_fingerprint_uses_relative_identity_and_content(tmp_path: Path) -> None:
    source = tmp_path / "inputs" / "source.json"
    source.parent.mkdir()
    source.write_text('{"value":1}', encoding="utf-8")

    first = file_fingerprint(source, root=tmp_path)
    source.write_text('{"value":2}', encoding="utf-8")
    second = file_fingerprint(source, root=tmp_path)

    assert first["path"] == "inputs/source.json"
    assert first["sha256"] != second["sha256"]


def test_files_fingerprint_is_order_independent(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    assert files_fingerprint([second, first], root=tmp_path) == files_fingerprint(
        [first, second, first], root=tmp_path
    )


def test_missing_file_and_payload_hash_are_deterministic(tmp_path: Path) -> None:
    missing = file_fingerprint(tmp_path / "missing.json", root=tmp_path)
    assert missing == {"path": "missing.json", "exists": False}
    assert payload_sha256({"files": [missing]}) == payload_sha256({"files": [missing]})

