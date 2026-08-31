from pathlib import Path

from execution.verify_public_tree import verify


def test_public_tree_has_no_private_or_generated_material() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert verify(repo_root) == []
