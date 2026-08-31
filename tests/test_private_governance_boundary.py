"""Public checkouts must not contain owner-specific governance state."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATHS = (
    ".harden/state.json",
    "docs/design/deletion_catalog_2026_08.json",
    "docs/hardening/v2/evidence/windows-live-gap-2026-08-29.json",
)


def test_private_governance_state_is_absent_and_ignored() -> None:
    for relative in PRIVATE_PATHS:
        assert not (PROJECT_ROOT / relative).exists()
        ignored = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "check-ignore", "-q", relative],
            check=False,
        )
        assert ignored.returncode == 0, relative
