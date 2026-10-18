"""Small synthetic ledgers exercise shared admission and exact release."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.operations import storage_reservation as storage


def test_windows_accounts_with_different_temp_roots_share_volume_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared-appdata"
    monkeypatch.setattr(storage.sys, "platform", "win32")
    monkeypatch.setattr(storage, "_windows_common_appdata", lambda: shared)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "untrusted-programdata"))
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setattr(storage.tempfile, "gettempdir", lambda: str(tmp_path / "account-one"))
    token = storage.reserve_peak(first, 80, host_limit=100, reserve_bytes=0)
    ledger = shared / "BhanuOperations" / "storage-reservations" / str(tmp_path.stat().st_dev)
    assert (ledger / f"{token}.json").exists()
    monkeypatch.setattr(storage.tempfile, "gettempdir", lambda: str(tmp_path / "account-two"))
    with pytest.raises(ValueError, match="host limit"):
        storage.reserve_peak(second, 80, host_limit=100, reserve_bytes=0)
    assert len(list(ledger.glob("*.json"))) == 1
    storage.release_peak(first, token)
    assert not (tmp_path / "untrusted-programdata").exists()


def test_concurrent_processes_cannot_both_admit_over_host_limit(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    script = """
import json
import sys
from pathlib import Path
from src.operations.storage_reservation import reserve_peak
try:
    token = reserve_peak(Path(sys.argv[1]), 80, host_limit=100, reserve_bytes=0,
                         store=Path(sys.argv[2]))
except ValueError as error:
    print(json.dumps({"rejected": str(error)}))
else:
    print(json.dumps({"token": token}))
"""

    def admit(name: str) -> dict[str, str]:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / name), str(ledger)],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(Path(__file__).resolve().parents[1]),
                        str(Path(__file__).resolve().parents[1] / "src"),
                    )
                ),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(admit, ("first", "second")))
    assert sum("token" in outcome for outcome in outcomes) == 1
    assert [outcome["rejected"] for outcome in outcomes if "rejected" in outcome] == [
        "concurrent disposable storage exceeds the host limit"
    ]
    assert len(list(ledger.glob("*.json"))) == 1


def test_release_requires_exact_root_and_retains_unresolved_lease(tmp_path: Path) -> None:
    ledger, root = tmp_path / "ledger", tmp_path / "run"
    token = storage.reserve_peak(root, 80, host_limit=100, reserve_bytes=0, store=ledger)
    lease = ledger / f"{token}.json"
    original = lease.read_bytes()
    with pytest.raises(ValueError, match="another root"):
        storage.release_peak(tmp_path / "other", token, store=ledger)
    assert lease.read_bytes() == original
    with pytest.raises(ValueError, match="host limit"):
        storage.reserve_peak(root, 80, host_limit=100, reserve_bytes=0, store=ledger)
    storage.release_peak(root, token, store=ledger)
    assert not lease.exists()
    replacement = storage.reserve_peak(root, 80, host_limit=100, reserve_bytes=0, store=ledger)
    storage.release_peak(root, replacement, store=ledger)


def test_ledger_parent_link_is_refused_without_writing_target(tmp_path: Path) -> None:
    target, alias = tmp_path / "target", tmp_path / "alias"
    target.mkdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            check=True,
            capture_output=True,
            timeout=5,
        )
    else:
        alias.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="links or reparse"):
            storage.reserve_peak(tmp_path / "run", 1, reserve_bytes=0, store=alias / "ledger")
        assert not list(target.iterdir())
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


def test_hard_linked_lock_is_refused_without_changing_other_file(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    other = tmp_path / "other-file"
    other.write_bytes(b"preserve")
    os.link(other, ledger / "lock")
    with pytest.raises(ValueError, match="hard linked"):
        storage.reserve_peak(tmp_path / "run", 1, reserve_bytes=0, store=ledger)
    assert other.read_bytes() == b"preserve"


def test_permission_failure_does_not_fall_back_to_account_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared-appdata"
    monkeypatch.setattr(storage.sys, "platform", "win32")
    monkeypatch.setattr(storage, "_windows_common_appdata", lambda: shared)
    mkdir = Path.mkdir

    def deny_ledger(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if shared in path.parents:
            raise PermissionError("synthetic shared-ledger denial")
        mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", deny_ledger)
    with pytest.raises(PermissionError, match="shared-ledger denial"):
        storage.reserve_peak(tmp_path / "run", 1, reserve_bytes=0)
    assert not shared.exists()
