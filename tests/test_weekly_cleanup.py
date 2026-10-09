"""Contract tests for the allowlist-only weekly filesystem cleanup CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest
from _pytest.capture import CaptureFixture

from execution import run_weekly_cleanup as cleanup
from src.operations import temp_run_retention

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def test_managed_help_without_pythonpath(tmp_path: Path) -> None:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    before = sorted(tmp_path.rglob("*"))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(cleanup.PROJECT_ROOT / "execution/sqlite_bootstrap.py"),
            str(Path(cleanup.__file__).resolve()),
            "--help",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--apply" in result.stdout
    assert "--code-root" in result.stdout
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("metadata_kind", ["file", "directory"])
def test_modes_hold_whole_temporary_source_checkout(
    tmp_path: Path, apply: bool, owned: bool, metadata_kind: str
) -> None:
    scope = tmp_path / ".tmp" / ("cron_runs/checkout" if owned else "checkout")
    scope.mkdir(parents=True)
    marker = scope / ".git"
    if metadata_kind == "file":
        marker.write_text("gitdir: outside-state-must-not-be-followed")
    else:
        marker.mkdir()
    held = scope / "nested/ordinary.json"
    held.parent.mkdir()
    held.write_text("source-owned output")
    empty = scope / "nested/empty"
    empty.mkdir()
    _age(held, 90)
    if marker.is_file():
        _age(marker, 90)
    result = _run(tmp_path, *(("--apply",) if apply else ()))
    assert result.deleted == result.would_delete == 0
    assert held.read_text() == "source-owned output"
    assert marker.exists()
    assert empty.is_dir()


@pytest.mark.parametrize("apply", [False, True])
def test_modes_hold_observed_loose_source_and_recovery_files(tmp_path: Path, apply: bool) -> None:
    root = tmp_path / ".tmp/ordinary"
    root.mkdir(parents=True)
    held = [
        root / f"source{suffix}"
        for suffix in (
            ".mjs",
            ".reg",
            ".sql",
            ".dll",
            ".patch",
            ".diff",
            ".py.wave2",
            ".csv",
            ".tsv",
            ".b64",
            ".html",
            ".htm",
            ".jsonl",
            ".css",
            ".md",
        )
    ]
    free = root / "ordinary.txt"
    cache_sentinel = tmp_path / ".ruff_cache/.gitignore"
    cache_sentinel.parent.mkdir()
    for path in (*held, free, cache_sentinel):
        path.write_text("fixture")
        _age(path, 90)
    result = _run(tmp_path, *(("--apply",) if apply else ()))
    assert result.deleted == (2 if apply else 0)
    assert result.would_delete == (0 if apply else 2)
    assert all(path.read_text() == "fixture" for path in held)
    assert free.exists() is not apply
    assert cache_sentinel.exists() is not apply


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    "suffix", [".db.old", ".json.bak.local_path_migration", ".tar.gz.uploading"]
)
def test_modes_hold_recovery_tree_with_renamed_archive(
    tmp_path: Path, apply: bool, suffix: str
) -> None:
    root = tmp_path / ".tmp/unknown"
    root.mkdir(parents=True)
    archive, sibling = root / f"original{suffix}", root / "ordinary.txt"
    for path in (archive, sibling):
        path.write_text("recovery fixture")
        _age(path, 90)
    result = _run(tmp_path, *(("--apply",) if apply else ()))
    assert result.deleted == result.would_delete == 0
    assert archive.read_text() == sibling.read_text() == "recovery fixture"


@pytest.fixture(autouse=True)
def isolated_user_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inspect the controller's or another session's real temporary root."""
    root = tmp_path / "user-system-temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    search_roots: Callable[[Path, Path | None], list[tuple[Path, bool]]] = getattr(
        temp_run_retention, "_search_roots"
    )

    def isolated_roots(repo_root: Path, code_root: Path | None) -> list[tuple[Path, bool]]:
        return [item for item in search_roots(repo_root, code_root) if item[0] != Path("C:/tmp")]

    monkeypatch.setattr(
        temp_run_retention,
        "_search_roots",
        isolated_roots,
    )


def _age(path: Path, days: int) -> None:
    timestamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _run(root: Path, *args: str) -> cleanup.CleanupSummary:
    return cleanup.run(["--repo-root", str(root), "--now", NOW.isoformat(), *args])


@pytest.mark.parametrize("runtime_scope", [False, True])
@pytest.mark.parametrize("policy_root", ["loose", "cron_runs", "pdf_pages", "news_cache"])
def test_ordinary_tmp_preserves_source_secret_and_archive_material(
    tmp_path: Path, runtime_scope: bool, policy_root: str
) -> None:
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    owner = runtime if runtime_scope else state
    protected = [
        owner / ".tmp" / policy_root / relative
        for relative in (
            "ir_documents/source.pdf",
            "transcripts/original.txt",
            "src/experiment.py",
            "candidate/credentials.json",
            "candidate/token.json",
            "candidate/.env.production",
            "candidate/api_key.txt",
            "candidate/service.pem",
            "candidate/private.key",
            "candidate/script.py",
            "archives/closed.tar",
        )
    ]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cached_at": (NOW - timedelta(days=90)).isoformat()}))
        _age(path, 90)
    disposable = owner / ".tmp" / "disposable" / "output.txt"
    source_cache = owner / "src" / "__pycache__" / "module.pyc"
    for path in (disposable, source_cache):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"disposable")
        _age(path, 90)

    _run(state, "--code-root", str(runtime), "--apply")

    assert all(path.exists() for path in protected)
    assert not disposable.exists()
    assert not source_cache.exists()


@pytest.mark.parametrize("runtime_scope", [False, True])
def test_tmp_empty_directory_pruning_preserves_protected_names(
    tmp_path: Path, runtime_scope: bool
) -> None:
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    owner = runtime if runtime_scope else state
    protected = [
        owner / ".tmp/cron_runs" / name / "empty"
        for name in (
            "src",
            "ir_documents",
            "transcripts",
            "secrets",
            "credentials",
            "keys",
            "certificates",
        )
    ]
    for path in protected:
        path.mkdir(parents=True)

    _run(state, "--code-root", str(runtime), "--apply")

    assert all(path.is_dir() for path in protected)


def test_runtime_owned_policies_use_payload_age_and_keep_recovery(tmp_path: Path) -> None:
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    paths = {
        "old_log": ".tmp/cron_logs/fetch_20260401T010000Z.log",
        "latest_log": ".tmp/cron_logs/fetch_20260402T010000Z.log",
        "failed_log": ".tmp/cron_logs/fetch_20260331T010000Z.log",
        "old_run": ".tmp/cron_runs/completed/output.txt",
        "active_run": ".tmp/cron_runs/active/output.txt",
        "page": ".tmp/pdf_pages/processed.png",
        "old_cache": ".tmp/news_cache/old.json",
        "fresh_cache": ".tmp/news_cache/fresh.json",
        "invalid_cache": ".tmp/news_cache/invalid.json",
        "source_cache": "src/__pycache__/module.pyc",
    }
    fixtures = {key: runtime / value for key, value in paths.items()}
    for key, path in fixtures.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("failed: upload error" if key == "failed_log" else "completed")
        _age(path, 90)
    (fixtures["active_run"].parent / "state.json").write_text('{"status":"active"}')
    fixtures["old_cache"].write_text(
        json.dumps({"cached_at": (NOW - timedelta(days=8)).isoformat()})
    )
    fixtures["fresh_cache"].write_text(json.dumps({"cached_at": NOW.isoformat()}))

    summary = _run(state, "--code-root", str(runtime), "--apply")

    for key in ("old_log", "old_run", "page", "old_cache", "source_cache"):
        assert not fixtures[key].exists(), key
    for key in ("latest_log", "failed_log", "active_run", "fresh_cache", "invalid_cache"):
        assert fixtures[key].exists(), key
    assert summary.policies["runtime_cron_logs_30d"].deleted == 1
    assert summary.policies["runtime_cron_runs_30d"].deleted == 1
    assert summary.policies["runtime_news_cache_7d"].deleted == 1
    assert summary.policies["runtime_news_cache_7d"].skipped_invalid == 1
    assert summary.policies["runtime_pdf_pages_30d"].deleted == 1
    assert summary.policies["runtime_main_python_caches_7d"].deleted == 1


@pytest.mark.parametrize("runtime_scope", [False, True])
def test_pre_unlink_protection_does_not_depend_on_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_scope: bool
) -> None:
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    owner = runtime if runtime_scope else state
    paths = [
        owner / ".tmp/cron_runs" / relative
        for relative in (
            "candidate/credentials.json",
            "source.py",
            "ir_documents/source.pdf",
            "secrets/opaque.txt",
        )
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("protected")
        _age(path, 90)

    def unsafe_collector(root: Path, cutoff: datetime, counts: object) -> list[cleanup.Candidate]:
        del cutoff, counts
        if root != owner / ".tmp/cron_runs":
            return []
        return [
            cleanup.Candidate(
                path=path,
                size=(metadata := path.stat()).st_size,
                inode=metadata.st_ino,
                device=metadata.st_dev,
                mtime_ns=metadata.st_mtime_ns,
            )
            for path in paths
        ]

    monkeypatch.setattr(cleanup, "_collect_tmp_owned_by_age", unsafe_collector)
    summary = _run(state, "--code-root", str(runtime), "--apply")

    assert all(path.exists() for path in paths)
    assert summary.deleted == 0
    policy = "runtime_cron_runs_30d" if runtime_scope else "cron_runs_30d"
    assert summary.policies[policy].skipped_unsafe == len(paths)


def test_runtime_catalog_protects_registered_scope_siblings(tmp_path: Path) -> None:
    import hashlib

    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    protected = runtime / ".tmp/cron_runs/held/fixture.json"
    sibling = protected.parent / "metadata.json"
    disposable = runtime / ".tmp/cron_runs/completed/output.txt"
    for path in (protected, sibling, disposable):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        _age(path, 90)
    catalog = runtime / "data/operations/artifact-retention.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "path": str(protected),
                        "allowed_root": str(protected.parent),
                        "family": "fixture",
                        "created_at": NOW.isoformat(),
                        "sha256": hashlib.sha256(protected.read_bytes()).hexdigest(),
                        "size": protected.stat().st_size,
                        "kind": "disposable_test",
                        "status": "failed",
                        "verified": True,
                        "pins": [],
                    }
                ],
            }
        )
    )

    summary = _run(state, "--code-root", str(runtime), "--apply")

    assert protected.exists() and sibling.exists()
    assert not disposable.exists()
    assert summary.policies["runtime_cron_runs_30d"].deleted == 1


def _make_data_alias(runtime: Path, target: Path, *, name: str = "data") -> Path:
    alias = runtime / name
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            check=True,
            capture_output=True,
        )
        assert getattr(alias.lstat(), "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
    else:
        alias.symlink_to(target, target_is_directory=True)
    return alias


@pytest.mark.parametrize("pin_during_collection", [False, True])
@pytest.mark.parametrize("metadata_prefix", ["", "\\\\?\\", "\\??\\"])
def test_approved_runtime_data_alias_uses_state_catalog_without_descendant_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pin_during_collection: bool,
    metadata_prefix: str,
) -> None:
    import hashlib

    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    (state / "data").mkdir()
    alias = _make_data_alias(runtime, state / "data")
    if metadata_prefix:
        original_readlink = os.readlink

        def prefixed_alias_target(
            path: str | os.PathLike[str], *, dir_fd: int | None = None
        ) -> str:
            if Path(path) == alias:
                return metadata_prefix + str(state / "data")
            return original_readlink(path, dir_fd=dir_fd)

        monkeypatch.setattr(os, "readlink", prefixed_alias_target)
    fixture = state / ".tmp/explicit-fixture/fixture.bin"
    ordinary = runtime / ".tmp/cron_runs/completed/output.txt"
    producer_root = runtime / ".tmp/producer-active"
    producer_root.mkdir(parents=True)
    temp_run_retention.begin_temp_run(producer_root, repo_root=state, code_root=runtime, now=NOW)
    producer_file = producer_root / "fixture.bin"
    producer_file.write_bytes(b"active synthetic fixture")
    for file in (fixture, ordinary, producer_file):
        file.parent.mkdir(parents=True, exist_ok=True)
        if not file.exists():
            file.write_bytes(b"synthetic fixture")
        _age(file, 90)
    catalog = state / "data/operations/artifact-retention.json"
    catalog.parent.mkdir(parents=True)

    def entry(file: Path, pins: list[str]) -> dict[str, object]:
        return {
            "path": str(file),
            "allowed_root": str(file.parent),
            "family": str(file.parent.name),
            "created_at": (NOW - timedelta(days=8)).isoformat(),
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            "size": file.stat().st_size,
            "kind": "disposable_test",
            "status": "completed",
            "verified": True,
            "pins": pins,
        }

    explicit = entry(fixture, [])
    catalog.write_text(json.dumps({"schema_version": 1, "artifacts": [explicit]}))
    original_stat = Path.stat
    original_exists = Path.exists

    def deny_alias_child_probe(file: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if alias in file.parents:
            raise OSError(448, "synthetic Windows untrusted descendant probe")
        return original_stat(file, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", deny_alias_child_probe)

    def deny_alias_child_exists(file: Path, *, follow_symlinks: bool = True) -> bool:
        if alias in file.parents:
            raise OSError(448, "synthetic Windows untrusted descendant probe")
        return original_exists(file)

    monkeypatch.setattr(Path, "exists", deny_alias_child_exists)
    original_collector: Callable[[Path, datetime, object], list[cleanup.Candidate]] = getattr(
        cleanup, "_collect_tmp_owned_by_age"
    )

    def collect_then_pin(root: Path, cutoff: datetime, counts: object) -> list[cleanup.Candidate]:
        candidates = original_collector(root, cutoff, counts)
        if pin_during_collection and root == runtime / ".tmp/cron_runs":
            pinned = entry(ordinary, ["new operator hold"])
            catalog.write_text(json.dumps({"schema_version": 1, "artifacts": [explicit, pinned]}))
        return candidates

    monkeypatch.setattr(cleanup, "_collect_tmp_owned_by_age", collect_then_pin)
    try:
        result = _run(state, "--code-root", str(runtime), "--apply")
        assert not fixture.exists()
        assert ordinary.exists() is pin_during_collection
        assert producer_file.exists()
        assert result.policies["registered_artifact_retention"].deleted == 1
        assert result.policies["runtime_cron_runs_30d"].deleted == (
            0 if pin_during_collection else 1
        )
        assert all(policy.skipped_error == 0 for policy in result.policies.values())
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


@pytest.mark.parametrize("unsafe", ["foreign", "broken", "unreadable", "linked_state"])
def test_unsafe_runtime_data_alias_fails_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    state, runtime, foreign = tmp_path / "state", tmp_path / "runtime", tmp_path / "foreign"
    for root in (state, runtime, foreign):
        root.mkdir()
    if unsafe == "linked_state":
        _make_data_alias(state, foreign)
    else:
        (state / "data").mkdir()
    target = state / "data" if unsafe in {"unreadable", "linked_state"} else foreign
    if unsafe == "broken":
        target = tmp_path / "missing"
    alias = _make_data_alias(runtime, target)
    disposable = runtime / ".tmp/loose/output.txt"
    disposable.parent.mkdir(parents=True)
    disposable.write_text("keep until authority is valid")
    _age(disposable, 90)
    if unsafe == "unreadable":
        original_readlink = os.readlink

        def unreadable_alias(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> str:
            if Path(path) == alias:
                raise OSError(448, "synthetic unreadable junction metadata")
            return original_readlink(path, dir_fd=dir_fd)

        monkeypatch.setattr(os, "readlink", unreadable_alias)
    try:
        with pytest.raises(ValueError, match="data alias"):
            _run(state, "--code-root", str(runtime), "--apply")
        assert disposable.exists()
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
        if unsafe == "linked_state":
            state_alias = state / "data"
            if os.name == "nt":
                state_alias.rmdir()
            else:
                state_alias.unlink()


@pytest.mark.parametrize("boundary", ["data", "operations"])
@pytest.mark.parametrize("catalog_exists", [False, True])
def test_canonical_catalog_junction_fails_before_child_probe_or_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    catalog_exists: bool,
) -> None:
    state, foreign = tmp_path / "state", tmp_path / "foreign"
    state.mkdir()
    foreign.mkdir()
    parent = state
    if boundary == "operations":
        parent = state / "data"
        parent.mkdir()
    alias = _make_data_alias(parent, foreign, name=boundary)
    child = foreign / "artifact-retention.json"
    if boundary == "data":
        child = foreign / "operations/artifact-retention.json"
    if catalog_exists:
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text('{"schema_version":1,"artifacts":[]}')
    disposable = state / ".tmp/loose/output.txt"
    disposable.parent.mkdir(parents=True)
    disposable.write_text("keep while catalog authority is invalid")
    _age(disposable, 90)
    original_exists = Path.exists

    def deny_child_probe(file: Path, *, follow_symlinks: bool = True) -> bool:
        if alias in file.parents:
            raise OSError(448, "synthetic Windows untrusted descendant probe")
        return original_exists(file)

    monkeypatch.setattr(Path, "exists", deny_child_probe)
    try:
        with pytest.raises(ValueError, match=r"catalog.*links"):
            _run(state, "--apply")
        assert disposable.exists()
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


@pytest.mark.parametrize("apply", [False, True])
def test_runtime_readonly_review_manifest_stays_while_disposable_output_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool, capsys: CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    manifest = (
        runtime
        / ".tmp/bha78-activation-20260821T105637PT/meli_q2_2026_doc12645_kpi_manifest.v2.reviewed.json"
    )
    disposable = runtime / ".tmp/disposable/output.txt"
    for path in (manifest, disposable):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("retained evidence" if path == manifest else "completed")
        _age(path, 90)
    original_stat = Path.stat

    def windows_stat(
        path: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result | SimpleNamespace:
        metadata = original_stat(path, follow_symlinks=follow_symlinks)
        if path == manifest:
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_size=metadata.st_size,
                st_ino=metadata.st_ino,
                st_dev=metadata.st_dev,
                st_mtime=metadata.st_mtime,
                st_mtime_ns=metadata.st_mtime_ns,
                st_file_attributes=33,
            )
        return metadata

    monkeypatch.setattr(Path, "stat", windows_stat)
    summary = _run(state, "--code-root", str(runtime), *(("--apply",) if apply else ()))

    assert manifest.read_text() == "retained evidence"
    assert disposable.exists() is not apply
    assert summary.policies["runtime_tmp_unclassified_30d"].would_delete == (0 if apply else 1)
    assert summary.policies["runtime_tmp_unclassified_30d"].deleted == (1 if apply else 0)
    assert summary.policies["runtime_tmp_unclassified_30d"].skipped_unsafe == 1
    assert all(policy.skipped_error == 0 for policy in summary.policies.values())
    if not apply:
        events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert [event["path"] for event in events if event["event"] == "cleanup_candidate"] == [
            str(disposable)
        ]


def test_coverage_counts_excluded_environments_and_unknown_database_siblings(
    tmp_path: Path,
) -> None:
    run = tmp_path / ".tmp" / "old-run"
    database = run / "fixture.db"
    package = run / ".venv" / "package.bin"
    for path in (database, package):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained")
        _age(path, 45)

    summary = _run(tmp_path, "--apply")

    assert database.exists() and package.exists()
    assert summary.coverage.status == "incomplete"
    assert summary.coverage.bytes == 16
    assert summary.coverage.groups[0].age_bucket == "31-60d"


def test_runtime_temp_is_covered_without_deleting_runtime_recovery(tmp_path: Path) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    old = runtime / ".tmp" / "loose" / "output.txt"
    recovery = runtime / ".tmp" / "release" / "snapshot.db"
    for path in (old, recovery):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"temp")
        _age(path, 45)

    summary = _run(state, "--code-root", str(runtime), "--apply")

    assert not old.exists()
    assert recovery.exists()
    assert summary.policies["runtime_tmp_unclassified_30d"].deleted == 1
    assert summary.coverage.bytes == 4
    assert summary.coverage.status == "incomplete"


def test_manifest_run_expires_automatically_and_new_sibling_stays_visible(tmp_path: Path) -> None:
    from src.operations.temp_run_retention import begin_temp_run, finish_temp_run

    root = tmp_path / ".tmp" / "run"
    root.mkdir(parents=True)
    begin_temp_run(root, repo_root=tmp_path, now=NOW - timedelta(days=20))
    fixture = root / "fixture.db"
    fixture.write_bytes(b"synthetic fixture")
    finish_temp_run(
        root,
        repo_root=tmp_path,
        success=True,
        disposable_paths=[fixture],
        now=NOW - timedelta(days=8),
    )
    unknown = root / "added-later.txt"
    unknown.write_bytes(b"new output")
    _age(unknown, 45)

    summary = _run(tmp_path, "--apply")

    assert not fixture.exists()
    assert unknown.exists()
    assert (root / ".earnings-temp-run.json").exists()
    assert summary.policies["registered_artifact_retention"].deleted == 1
    assert summary.coverage.status == "incomplete"
    assert any(group.disposition == "unclassified" for group in summary.coverage.groups)


def test_dry_run_is_allowlist_only_and_reports_jsonl(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    old_log = tmp_path / ".tmp" / "cron_logs" / "old.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("old", encoding="utf-8")
    _age(old_log, 31)
    protected = tmp_path / "data" / "portfolio.db"
    protected.parent.mkdir()
    protected.write_text("never touch", encoding="utf-8")
    _age(protected, 90)
    checkpoint = tmp_path / ".tmp" / "cron_logs" / "active" / "state.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text("{}", encoding="utf-8")
    _age(checkpoint, 90)
    lock = tmp_path / ".tmp" / "cron_runs" / "job_locks" / "weekly.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("locked", encoding="utf-8")
    _age(lock, 90)

    summary = _run(tmp_path)

    assert old_log.exists()
    assert protected.exists()
    assert checkpoint.exists()
    assert lock.exists()
    assert summary.mode == "dry_run"
    assert summary.idempotency_key == "weekly_cleanup:2026-W31:weekly-cleanup-v4"
    assert summary.would_delete == 1
    assert summary.deleted == 0
    assert summary.bytes == len("old")
    event = json.loads(capsys.readouterr().err.splitlines()[0])
    assert event["event"] == "cleanup_candidate"
    assert event["policy"] == "cron_logs_30d"


def test_apply_removes_old_allowlisted_files_and_is_idempotent(tmp_path: Path) -> None:
    old_run = tmp_path / ".tmp" / "cron_runs" / "nested" / "old.json"
    old_pdf = tmp_path / ".tmp" / "pdf_pages" / "old.png"
    fresh_log = tmp_path / ".tmp" / "cron_logs" / "fresh.log"
    for path in (old_run, old_pdf, fresh_log):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"abc")
    _age(old_run, 31)
    _age(old_pdf, 31)
    _age(fresh_log, 30)

    summary = _run(tmp_path, "--apply")

    assert not old_run.exists()
    assert not old_pdf.exists()
    assert fresh_log.exists()
    assert summary.deleted == 2
    assert summary.would_delete == 0
    assert summary.bytes == 6
    assert not (tmp_path / ".tmp" / "cron_runs" / "nested").exists()
    again = _run(tmp_path, "--apply")
    assert again.deleted == 0
    assert again.would_delete == 0


def test_news_cache_uses_payload_timestamp_and_preserves_bad_payloads(tmp_path: Path) -> None:
    cache = tmp_path / ".tmp" / "news_cache"
    cache.mkdir(parents=True)
    old = cache / "old.json"
    fresh = cache / "fresh.json"
    invalid = cache / "invalid.json"
    missing = cache / "missing.json"
    old.write_text(
        json.dumps({"cached_at": (NOW - timedelta(days=8)).isoformat()}), encoding="utf-8"
    )
    fresh.write_text(
        json.dumps({"cached_at": (NOW - timedelta(days=6)).isoformat()}), encoding="utf-8"
    )
    invalid.write_text("not json", encoding="utf-8")
    missing.write_text("{}", encoding="utf-8")
    # Deliberately old mtimes: bad/missing timestamps must not fall back to mtime.
    for path in (old, fresh, invalid, missing):
        _age(path, 90)

    summary = _run(tmp_path, "--apply")

    assert not old.exists()
    assert fresh.exists()
    assert invalid.exists()
    assert missing.exists()
    policy = summary.policies["news_cache_7d"]
    assert policy.deleted == 1
    assert policy.skipped_invalid == 2


def test_main_cache_policy_removes_only_old_cache_entries_and_excludes_claude(
    tmp_path: Path,
) -> None:
    old_pyc = tmp_path / "src" / "__pycache__" / "module.cpython-311.pyc"
    old_pytest = tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids"
    old_ruff = tmp_path / ".ruff_cache" / "0" / "blob"
    protected_worktree = tmp_path / ".claude" / "worktrees" / "other" / "__pycache__" / "keep.pyc"
    protected_venv = tmp_path / "venv" / "lib" / "__pycache__" / "keep.pyc"
    source = tmp_path / "src" / "keep.py"
    for path in (old_pyc, old_pytest, old_ruff, protected_worktree, protected_venv, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cache")
        _age(path, 8)

    summary = _run(tmp_path, "--apply")

    assert not old_pyc.exists()
    assert not old_pytest.exists()
    assert not old_ruff.exists()
    assert protected_worktree.exists()
    assert protected_venv.exists()
    assert source.exists()
    assert summary.policies["main_python_caches_7d"].deleted == 3


def test_symlink_is_never_followed_or_deleted(tmp_path: Path) -> None:
    target = tmp_path / "outside.log"
    target.write_text("do not follow", encoding="utf-8")
    _age(target, 90)
    link = tmp_path / ".tmp" / "cron_logs" / "linked.log"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except OSError:
        # Symlink creation can be unavailable on locked-down Windows hosts.
        return

    summary = _run(tmp_path, "--apply")

    assert link.is_symlink()
    assert target.exists()
    assert summary.deleted == 0
    assert summary.policies["cron_logs_30d"].skipped_unsafe == 1


def test_temp_audio_is_explicitly_skipped_without_qa_database_access(tmp_path: Path) -> None:
    audio = tmp_path / ".tmp" / "temp_audio_MSFT_Q1_2026.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    _age(audio, 90)

    summary = _run(tmp_path, "--apply")

    assert audio.exists()
    assert summary.policies["temp_audio_qa_guard"].skipped_qa_unverified == 1


def test_generic_tmp_removes_old_completed_artifacts_but_preserves_active_state(
    tmp_path: Path,
) -> None:
    completed = tmp_path / ".tmp" / "llm_runs" / "completed" / "response.json"
    loose = tmp_path / ".tmp" / "old-response.txt"
    active = tmp_path / ".tmp" / "morning_pipeline" / "response.json"
    state = active.parent / "state.json"
    for path in (completed, loose, active, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        _age(path, 31)

    summary = _run(tmp_path, "--apply")

    assert not completed.exists()
    assert not loose.exists()
    assert active.exists()
    assert state.exists()
    assert summary.policies["tmp_unclassified_30d"].deleted == 2


def test_completed_checkpoint_with_state_expires_after_30_days(tmp_path: Path) -> None:
    checkpoint_root = tmp_path / ".tmp" / "llm_runs" / "completed"
    state = checkpoint_root / "state.json"
    response = checkpoint_root / "response.json"
    checkpoint_root.mkdir(parents=True)
    state.write_text('{"status": "completed"}', encoding="utf-8")
    response.write_text("{}", encoding="utf-8")
    _age(state, 31)
    _age(response, 31)

    summary = _run(tmp_path, "--apply")

    assert not state.exists()
    assert not response.exists()
    assert summary.policies["tmp_unclassified_30d"].deleted == 2


def test_owned_tmp_policy_preserves_active_checkpoint_tree(tmp_path: Path) -> None:
    checkpoint_root = tmp_path / ".tmp" / "cron_runs" / "active"
    state = checkpoint_root / "state.json"
    response = checkpoint_root / "response.json"
    checkpoint_root.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    response.write_text("{}", encoding="utf-8")
    _age(state, 31)
    _age(response, 31)

    summary = _run(tmp_path, "--apply")

    assert state.exists()
    assert response.exists()
    assert summary.policies["cron_runs_30d"].deleted == 0


def test_generic_tmp_preserves_recovery_material_and_owned_policy_roots(tmp_path: Path) -> None:
    backup = tmp_path / ".tmp" / "recovery" / "portfolio-precutover.db.gz"
    owned = tmp_path / ".tmp" / "cron_logs" / "old.log"
    for path in (backup, owned):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")
        _age(path, 31)

    summary = _run(tmp_path, "--apply")

    assert backup.exists()
    assert not owned.exists()
    assert summary.policies["tmp_unclassified_30d"].deleted == 0
    assert summary.policies["cron_logs_30d"].deleted == 1


def test_main_fails_loudly_when_an_eligible_file_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    old_log = tmp_path / ".tmp" / "cron_logs" / "undeletable.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("locked", encoding="utf-8")
    _age(old_log, 31)

    original_unlink = Path.unlink

    def fail_target(path: Path, missing_ok: bool = False) -> None:
        if path == old_log:
            raise PermissionError("in use")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_target)
    exit_code = cleanup.main(["--repo-root", str(tmp_path), "--now", NOW.isoformat(), "--apply"])

    assert exit_code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["policies"]["cron_logs_30d"]["skipped_error"] == 1
    assert old_log.exists()


@pytest.mark.parametrize("ancestor", [".pytest_cache", "__pycache__", ".ruff_cache"])
def test_checkout_beneath_cache_ancestor_preserves_ordinary_source(
    tmp_path: Path, ancestor: str
) -> None:
    root = tmp_path / ancestor / "checkout"
    source = root / "src" / "module.py"
    cache = root / "src" / "__pycache__" / "module.pyc"
    for path in (source, cache):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        _age(path, 8)
    summary = _run(root, "--apply")
    assert source.exists()
    assert not cache.exists()
    assert summary.policies["main_python_caches_7d"].deleted == 1


@pytest.mark.parametrize("folder", [".git", ".claude", "venv", ".venv", "node_modules"])
def test_generic_tmp_does_not_traverse_nested_workspaces(tmp_path: Path, folder: str) -> None:
    file = tmp_path / ".tmp" / "test-run" / folder / "old.txt"
    file.parent.mkdir(parents=True)
    file.write_text("keep")
    _age(file, 90)
    assert _run(tmp_path, "--apply").deleted == 0
    assert file.exists()


def test_inner_completed_checkpoint_cannot_hide_outer_failed_checkpoint(tmp_path: Path) -> None:
    outer = tmp_path / ".tmp" / "test-run"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "state.json").write_text('{"status":"failed"}')
    (inner / "state.json").write_text('{"status":"completed"}')
    file = inner / "response.json"
    file.write_text("{}")
    for path in (outer / "state.json", inner / "state.json", file):
        _age(path, 90)
    assert _run(tmp_path, "--apply").deleted == 0
    assert file.exists()


def test_recovery_ancestor_and_database_siblings_survive(tmp_path: Path) -> None:
    backup_manifest = tmp_path / ".tmp" / "backup-20260601" / "manifest.json"
    fixture = tmp_path / ".tmp" / "closed-test" / "data" / "fixture.db"
    metadata = fixture.parent.parent / "metadata.json"
    for path in (backup_manifest, fixture, metadata):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
        _age(path, 90)
    assert _run(tmp_path, "--apply").deleted == 0
    assert all(path.exists() for path in (backup_manifest, fixture, metadata))


def test_failed_cron_log_survives_age_cleanup(tmp_path: Path) -> None:
    log = tmp_path / ".tmp" / "cron_logs" / "daily_20260601.log"
    log.parent.mkdir(parents=True)
    log.write_text("Traceback: backup upload failed")
    _age(log, 90)
    assert _run(tmp_path, "--apply").deleted == 0
    assert log.exists()


@pytest.mark.parametrize("skipped_error", [1, 3, "unknown"])
def test_cleanup_log_preserves_failure_beyond_tail_window(
    tmp_path: Path, skipped_error: int | str
) -> None:
    root = tmp_path / ".tmp/cron_logs"
    root.mkdir(parents=True)
    failed = root / "weekly_cleanup_20260601T010000Z.log"
    latest = root / "weekly_cleanup_20260602T010000Z.log"
    failed.write_text(
        '{"event":"cleanup_skipped","reason":"unlink_error"}\n'
        + ('{"event":"cleanup_deleted"}\n' * 26000)
        + json.dumps({"policies": {"cron_logs_30d": {"skipped_error": skipped_error}}})
        + "\n"
    )
    latest.write_text('{"status":"ok"}\n')
    for path in (failed, latest):
        _age(path, 90)
    assert failed.stat().st_size > 633692
    assert _run(tmp_path, "--apply").deleted == 0
    assert failed.exists()


@pytest.mark.parametrize(
    "result",
    [
        '{"policies":{"cron_logs_30d":{"skipped_error":2}}}',
        '{"policies":{"cron_logs_30d":{"skipped_error":"invalid"}}}',
        '{"policies":{"cron_logs_30d":{"skipped_error":',
        "exit_code=75",
        '{"exit_code":124}',
        '{"exit_code":"unknown"}',
        '{"status":"unexpected"}',
        '{"status":"suppressed"}',
        '{"status":',
    ],
)
def test_operational_failure_fields_cannot_expire_as_success(tmp_path: Path, result: str) -> None:
    root = tmp_path / ".tmp/cron_logs"
    root.mkdir(parents=True)
    prior = root / "weekly_cleanup_20260601T010000Z.log"
    latest = root / "weekly_cleanup_20260602T010000Z.log"
    prior.write_text(result + "\n")
    latest.write_text('{"status":"ok"}\n')
    for path in (prior, latest):
        _age(path, 90)
    assert _run(tmp_path, "--apply").deleted == 0
    assert prior.exists()


def test_successful_old_operational_log_can_expire(tmp_path: Path) -> None:
    root = tmp_path / ".tmp/cron_logs"
    root.mkdir(parents=True)
    prior = root / "weekly_cleanup_20260601T010000Z.log"
    latest = root / "weekly_cleanup_20260602T010000Z.log"
    prior.write_text(
        '{"status":"ok","exit_code":0,"policies":{"cron_logs_30d":{"skipped_error":0}}}\n'
    )
    latest.write_text('{"status":"ok"}\n')
    for path in (prior, latest):
        _age(path, 90)
    assert _run(tmp_path, "--apply").deleted == 1
    assert not prior.exists()
    assert latest.exists()


def test_registered_scope_cannot_be_partially_deleted_by_age_policy(tmp_path: Path) -> None:
    import hashlib

    scope = tmp_path / ".tmp" / "catalogued-run"
    file = scope / "fixture.db"
    manifest = scope / "manifest.json"
    scope.mkdir(parents=True)
    file.write_bytes(b"keep")
    manifest.write_text("keep")
    for path in (file, manifest):
        _age(path, 90)
    catalog = tmp_path / "data/operations/artifact-retention.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "path": str(file),
                        "allowed_root": str(scope),
                        "family": "fixture",
                        "created_at": NOW.isoformat(),
                        "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                        "size": 4,
                        "kind": "disposable_test",
                        "status": "failed",
                        "verified": True,
                        "pins": [],
                    }
                ],
            }
        )
    )
    assert _run(tmp_path, "--apply").deleted == 0
    assert file.exists()
    assert manifest.exists()


def test_latest_timestamped_logs_survive_per_job_even_after_30_days(tmp_path: Path) -> None:
    root = tmp_path / ".tmp/cron_logs"
    root.mkdir(parents=True)
    old_failure = root / "nightly_backup_20260601T010000Z.log"
    resolved = root / "nightly_backup_20260602T010000Z.log"
    latest_failure = root / "weekly_cleanup_20260603_010000.log"
    old_failure.write_text("ERROR: backup failed")
    resolved.write_text("backup succeeded")
    latest_failure.write_text("ERROR: cleanup failed")
    for path in (old_failure, resolved, latest_failure):
        _age(path, 90)
    result = _run(tmp_path, "--apply")
    assert result.deleted == 0
    assert old_failure.exists()
    assert resolved.exists()
    assert latest_failure.exists()


def test_empty_directory_pruning_does_not_enter_nested_virtualenv(tmp_path: Path) -> None:
    empty = tmp_path / ".tmp/cron_logs/rehearsal/venv/empty-package"
    empty.mkdir(parents=True)
    _run(tmp_path, "--apply")
    assert empty.is_dir()


@pytest.mark.parametrize(
    "newer_contents", ["", '{"status":"suppressed"}', '{"status":"in_progress"}', "partial output"]
)
def test_newer_log_cannot_resolve_prior_failure_without_explicit_evidence(
    tmp_path: Path, newer_contents: str
) -> None:
    root = tmp_path / ".tmp/cron_logs"
    root.mkdir(parents=True)
    prior_failure = root / "daily_20260601T010000Z.log"
    newer = root / "daily_20260602T010000Z.log"
    prior_failure.write_text("ERROR: backup failed")
    newer.write_text(newer_contents)
    for path in (prior_failure, newer):
        _age(path, 90)
    assert _run(tmp_path, "--apply").deleted == 0
    assert prior_failure.exists()
    assert newer.exists()


@pytest.mark.parametrize(
    "mutation",
    ["replacement", "resumed_checkpoint", "registered_scope", "pinned_scope", "source_checkout"],
)
@pytest.mark.parametrize("apply", [False, True])
def test_apply_rechecks_identity_and_protection_after_collection(
    tmp_path: Path, mutation: str, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    file = tmp_path / ".tmp/cron_runs/closed/response.json"
    file.parent.mkdir(parents=True)
    file.write_text("old payload")
    _age(file, 90)
    original_collector = getattr(cleanup, "_collect_tmp_owned_by_age")

    def collect_then_mutate(
        root: Path, cutoff: datetime, counts: object
    ) -> list[cleanup.Candidate]:
        candidates: list[cleanup.Candidate] = original_collector(root, cutoff, counts)
        if root.name != "cron_runs":
            return candidates
        assert len(candidates) == 1
        if mutation == "replacement":
            replacement = file.with_name("new-response.json")
            replacement.write_text("new payload")
            _age(replacement, 90)
            replacement.replace(file)
        elif mutation == "resumed_checkpoint":
            (file.parent / "state.json").write_text('{"status":"in_progress"}')
        elif mutation == "source_checkout":
            (file.parent / ".git").write_text("gitdir: outside-state-must-not-be-followed")
        else:
            import hashlib

            catalog = tmp_path / "data/operations/artifact-retention.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifacts": [
                            {
                                "path": str(file),
                                "allowed_root": str(file.parent),
                                "family": "fixture",
                                "created_at": NOW.isoformat(),
                                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                                "size": file.stat().st_size,
                                "kind": "disposable_test",
                                "status": "completed" if mutation == "pinned_scope" else "failed",
                                "verified": True,
                                "pins": ["review"] if mutation == "pinned_scope" else [],
                            }
                        ],
                    }
                )
            )
        return candidates

    monkeypatch.setattr(cleanup, "_collect_tmp_owned_by_age", collect_then_mutate)
    result = _run(tmp_path, *(("--apply",) if apply else ()))
    assert file.exists()
    assert result.deleted == 0
    assert result.would_delete == 0
    assert result.policies["cron_runs_30d"].skipped_unsafe == 1


@pytest.mark.parametrize("apply", [False, True])
def test_modes_hold_hardlinked_file_and_accept_independent_output(
    tmp_path: Path, apply: bool
) -> None:
    held = tmp_path / ".tmp/cron_runs/closed/response.json"
    eligible = tmp_path / ".tmp/cron_runs/free/output.txt"
    for path in (held, eligible):
        path.parent.mkdir(parents=True)
        path.write_text("fixture")
        _age(path, 90)
    os.link(held, tmp_path / "retained-copy.json")
    result = _run(tmp_path, *(("--apply",) if apply else ()))
    assert held.exists()
    assert eligible.exists() is not apply
    counts = result.policies["cron_runs_30d"]
    assert counts.would_delete == (0 if apply else 1)
    assert counts.deleted == (1 if apply else 0)
    assert counts.skipped_unsafe == 1
    assert counts.skipped_error == 0


def test_protected_scope_walk_has_bounded_comparisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".tmp"
    free = root / "free/deep/output.txt"
    held = root / "held/deep/output.txt"
    for path in (free, held):
        path.parent.mkdir(parents=True)
        path.write_text("fixture")
    scopes = (root / "held", *(root / f"unrelated-{index}" for index in range(512)))
    counts = getattr(cleanup, "_Counts")(protected_roots=scopes)
    walk = getattr(cleanup, "_iter_regular_files")
    comparisons = 0
    original_equal = Path.__eq__

    def counted_equal(left: Path, right: object) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original_equal(left, right)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "__eq__", counted_equal)
        files = list(walk(root, counts))
    assert files == [free]
    assert comparisons < 300


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("C:/tmp/held", True),
        ("C:/tmp/held/deep/file.txt", True),
        ("c:/TMP/HELD/Deep/file.txt", True),
        ("C:/tmp", False),
        ("C:/tmp/held-sibling/file.txt", False),
        ("C:/tmp/other/held/file.txt", False),
        ("D:/tmp/held/file.txt", False),
    ],
)
def test_protected_scope_lookup_preserves_windows_path_semantics(
    path: str, protected: bool
) -> None:
    within = getattr(cleanup, "_within_protected_scope")
    assert within(PureWindowsPath(path), frozenset({PureWindowsPath("C:/tmp/held")})) is protected


def test_protected_scope_cache_refreshes_after_scope_replacement(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    counts = getattr(cleanup, "_Counts")(protected_roots=(first,))
    assert counts.protects(first / "nested/output.txt")
    assert not counts.protects(second / "nested/output.txt")
    counts.protected_roots = (second,)
    assert not counts.protects(first / "nested/output.txt")
    assert counts.protects(second / "nested/output.txt")


def test_default_cleanup_requires_configured_state_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.operations import paths

    monkeypatch.delenv("EARNINGS_SUMMARY_REPO_ROOT", raising=False)

    def missing_authority(_root: Path) -> Path:
        raise ValueError("EARNINGS_SUMMARY_DB_PATH is required")

    monkeypatch.setattr(paths, "configured_product_state_root", missing_authority)
    with pytest.raises(ValueError, match="EARNINGS_SUMMARY_DB_PATH"):
        cleanup.run([])


def test_windows_readonly_unregistered_file_is_skipped_without_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    file = tmp_path / ".tmp/cron_logs/old.log"
    file.parent.mkdir(parents=True)
    file.write_text("old successful log")
    _age(file, 90)
    original_stat = Path.stat

    def windows_stat(
        path: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result | SimpleNamespace:
        metadata = original_stat(path, follow_symlinks=follow_symlinks)
        if path == file:
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_size=metadata.st_size,
                st_ino=metadata.st_ino,
                st_dev=metadata.st_dev,
                st_mtime=metadata.st_mtime,
                st_mtime_ns=metadata.st_mtime_ns,
                st_file_attributes=33,
            )
        return metadata

    monkeypatch.setattr(Path, "stat", windows_stat)
    result = _run(tmp_path, "--apply")
    assert result.deleted == 0
    assert result.policies["cron_logs_30d"].skipped_error == 0
    assert result.policies["cron_logs_30d"].skipped_unsafe == 1
    assert file.exists()
