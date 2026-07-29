from __future__ import annotations

from pathlib import Path

import pytest

from provenance.verifier_identity import verifier_source_artifact_sha256


def test_verifier_identity_is_path_free_order_independent_and_byte_exact(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def verify():\n    return True\n", encoding="utf-8")
    second.write_text("POLICY = 1\n", encoding="utf-8")

    expected = verifier_source_artifact_sha256(
        {"verifier/main.py": first, "verifier/policy.py": second}
    )
    assert expected == verifier_source_artifact_sha256(
        {"verifier/policy.py": second, "verifier/main.py": first}
    )
    assert str(tmp_path) not in expected

    second.write_text("POLICY = 2\n", encoding="utf-8")
    assert (
        verifier_source_artifact_sha256({"verifier/main.py": first, "verifier/policy.py": second})
        != expected
    )


@pytest.mark.parametrize(
    "logical_name",
    ("", "/absolute.py", "../escape.py", "a\\b.py", "a//b.py"),
)
def test_verifier_identity_rejects_noncanonical_logical_names(
    tmp_path: Path,
    logical_name: str,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="logical name"):
        verifier_source_artifact_sha256({logical_name: source})
