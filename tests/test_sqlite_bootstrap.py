"""Startup and provenance contracts for the managed Windows SQLite runtime."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from execution import sqlite_bootstrap

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DLL_PATH = PROJECT_ROOT / "vendor" / "sqlite" / "windows-x64" / "sqlite3.dll"
EXPECTED_DLL_SHA256 = (
    "ab57d0437795ecc757cb693f32ea224173fa9856594d95cfa6b5033e645cd1ec"  # pragma: allowlist secret
)


def test_vendored_sqlite_dll_matches_pinned_binary_hash() -> None:
    assert hashlib.sha256(DLL_PATH.read_bytes()).hexdigest() == EXPECTED_DLL_SHA256


def test_official_download_provenance_is_pinned() -> None:
    provenance = (DLL_PATH.parent / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "https://www.sqlite.org/2026/sqlite-dll-win-x64-3530400.zip" in provenance
    assert (
        "deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a" in provenance
    )  # pragma: allowlist secret
    assert EXPECTED_DLL_SHA256 in provenance


def test_ci_builds_and_preloads_hash_verified_sqlite_3534() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "sqlite-amalgamation-3530400.zip" in workflow
    assert "628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e" in workflow
    assert 'echo "LD_PRELOAD=$sqlite_dir/libsqlite3.so.0" >> "$GITHUB_ENV"' in workflow
    assert 'assert sqlite3.sqlite_version == "3.53.4"' in workflow


def test_application_failure_is_not_relabelled_as_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite_bootstrap, "preload_sqlite", lambda: "3.53.4")

    def fail_target(_arguments: list[str]) -> int:
        raise RuntimeError("application sentinel")

    monkeypatch.setattr(sqlite_bootstrap, "_run_target", fail_target)
    with pytest.raises(RuntimeError, match="application sentinel"):
        sqlite_bootstrap.main(["target.py"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL preload contract")
def test_bootstrap_preloads_sqlite_3534_before_application_import() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            os.fspath(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py"),
            "--check",
        ],
        cwd=PROJECT_ROOT,
        env=os.environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "3.53.4"


def test_managed_launchers_enable_preimport_bootstrap() -> None:
    cron_launcher = (PROJECT_ROOT / "cron" / "run_python.bat").read_text(encoding="utf-8")
    comments_launcher = (PROJECT_ROOT / "start_comments_server.bat").read_text(encoding="utf-8")

    assert "sqlite_bootstrap.py" in cron_launcher
    assert "sqlite_bootstrap.py" in comments_launcher


def test_root_convenience_wrappers_supply_job_and_write_set() -> None:
    wrappers = (
        "build_report.bat",
        "process_comments.bat",
        "refresh_fmp.bat",
        "refresh_news.bat",
        "refresh_transcripts.bat",
    )
    for wrapper in wrappers:
        invocations = [
            line
            for line in (PROJECT_ROOT / wrapper).read_text(encoding="utf-8").splitlines()
            if "run_python.bat" in line and not line.lstrip().lower().startswith("rem ")
        ]
        assert invocations, wrapper
        assert all('"portfolio-db"' in invocation for invocation in invocations), wrapper


def test_project_backup_uses_managed_python_launcher() -> None:
    backup = (PROJECT_ROOT / "cron" / "backup_project.ps1").read_text(encoding="utf-8")
    assert "run_python.bat" in backup
    assert "& $py" not in backup


def test_noncentral_sqlite_writer_entrypoints_require_runtime_gate() -> None:
    sources = {
        "alembic/env.py": ("run_migrations_online", "require_safe_sqlite_writer_runtime()"),
        "execution/upgrade_database.py": (
            "def upgrade_database",
            "require_safe_sqlite_writer_runtime()",
        ),
        "execution/fix_kpi_series.py": (
            "if args.apply:",
            "require_safe_sqlite_writer_runtime()",
        ),
    }
    for relative_path, required_fragments in sources.items():
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for fragment in required_fragments:
            assert fragment in source, (relative_path, fragment)
