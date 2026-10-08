"""Producer completion receipts control disposable test-run retirement."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from src.operations import artifact_retention
from src.operations import temp_run_retention as temp_run
from src.operations.artifact_retention import CATALOG_RELATIVE_PATH, run_retention
from src.operations.temp_run_retention import begin_temp_run, discover_temp_runs, finish_temp_run

NOW = datetime(2026, 10, 8, tzinfo=UTC)
MANIFEST_NAME = ".earnings-temp-run.json"


@pytest.fixture
def production_search_roots() -> Callable[[Path, Path | None], list[tuple[Path, bool]]]:
    candidate = getattr(temp_run, "_search_roots")
    assert isinstance(candidate, Callable)
    return cast(Callable[[Path, Path | None], list[tuple[Path, bool]]], candidate)


@pytest.fixture(autouse=True)
def isolated_user_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    production_search_roots: Callable[[Path, Path | None], list[tuple[Path, bool]]],
) -> None:
    root = tmp_path / "isolated-user-temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))

    def isolated_roots(repo_root: Path, code_root: Path | None) -> list[tuple[Path, bool]]:
        return [
            item
            for item in production_search_roots(repo_root, code_root)
            if item[0] != Path("C:/tmp")
        ]

    monkeypatch.setattr(temp_run, "_search_roots", isolated_roots)


def test_production_search_root_mapping_does_not_inspect_unrelated_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    production_search_roots: Callable[[Path, Path | None], list[tuple[Path, bool]]],
) -> None:
    state = tmp_path / "state"
    code = tmp_path / "runtime"
    user_temp = Path(tempfile.gettempdir())

    def no_inspection(path: Path) -> Iterator[Path]:
        raise AssertionError(f"search-root mapping inspected {path}")

    monkeypatch.setattr(Path, "iterdir", no_inspection)
    roots = production_search_roots(state, code)
    expected = [(state / ".tmp", False), (code / ".tmp", False), (user_temp, True)]
    if os.name == "nt":
        expected.append((Path("C:/tmp"), True))
    assert roots == expected
    assert user_temp / "unrelated-application" not in {root for root, _ in roots}


def test_completed_run_expires_without_manual_catalog(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "closed-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"disposable synthetic fixture")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )

    assert not (tmp_path / CATALOG_RELATIVE_PATH).exists()
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {fixture}
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog)
    assert result.deleted == 1
    assert not fixture.exists()
    assert (root / MANIFEST_NAME).exists()


def test_retention_clock_starts_after_long_active_run_finishes(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "long-test"
    begin_temp_run(root, repo_root=tmp_path, now=NOW - timedelta(days=14))
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"old file, newly completed run")
    old_timestamp = (NOW - timedelta(days=14)).timestamp()
    os.utime(fixture, (old_timestamp, old_timestamp))
    finish_temp_run(root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=NOW)

    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert discovery.catalog.artifacts[0].created_at == NOW
    for elapsed in (timedelta(0), timedelta(days=7)):
        result = run_retention(tmp_path, now=NOW + elapsed, apply=True, catalog=discovery.catalog)
        assert result.deleted == 0
        assert fixture.exists()
    result = run_retention(
        tmp_path, now=NOW + timedelta(days=7, seconds=1), apply=True, catalog=discovery.catalog
    )
    assert result.deleted == 1
    assert not fixture.exists()


@pytest.mark.parametrize("status", ["active", "failed"])
def test_old_unfinished_runs_remain_visible_and_preserved(tmp_path: Path, status: str) -> None:
    root = tmp_path / ".tmp" / f"{status}-test"
    started = NOW - timedelta(days=60)
    begin_temp_run(root, repo_root=tmp_path, now=started)
    fixture = root / "fixture.db"
    fixture.write_bytes(b"failure recovery fixture")
    if status == "failed":
        finish_temp_run(
            root, repo_root=tmp_path, success=False, disposable_paths=[fixture], now=started
        )
    discovery = discover_temp_runs(tmp_path, now=NOW)
    reports = [item for item in discovery.reports if item.root == root]
    assert len(reports) == 1
    assert reports[0].status == status
    assert reports[0].held_files >= 1
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 0
    assert fixture.exists()


def test_completed_manifest_does_not_classify_unlisted_files(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "partial-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    disposable = root / "fixture.bin"
    unclassified = root / "original-financial-snapshot.db"
    disposable.write_bytes(b"synthetic")
    unclassified.write_bytes(b"unclassified durable data")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[disposable], now=completed
    )
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {disposable}
    assert next(item for item in discovery.reports if item.root == root).held_files >= 1
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 1
    assert unclassified.exists()


def test_changed_bytes_after_sealing_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "changed-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"before")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )
    fixture.write_bytes(b"change")
    discovery = discover_temp_runs(tmp_path, now=NOW)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog)
    assert result.deleted == 0
    assert fixture.read_bytes() == b"change"


def test_files_written_after_sealing_are_not_added_to_retirement(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "late-file-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"declared")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )
    late_file = root / "recovery.db"
    late_file.write_bytes(b"post-completion evidence")
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {fixture}
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 1
    assert late_file.read_bytes() == b"post-completion evidence"


@pytest.mark.parametrize("protection", ["link", "hardlink", "live", "outside"])
def test_finish_rejects_files_outside_disposable_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    root = tmp_path / ".tmp" / f"{protection}-test"
    begin_temp_run(root, repo_root=tmp_path, now=NOW - timedelta(days=8))
    original = tmp_path / "original.db"
    original.write_bytes(b"must survive")
    fixture = root / "fixture.db"
    if protection == "link":
        try:
            fixture.symlink_to(original)
        except OSError:
            pytest.skip("symlink creation unavailable")
    elif protection == "hardlink":
        try:
            os.link(original, fixture)
        except OSError:
            pytest.skip("hardlink creation unavailable")
    elif protection == "outside":
        fixture = original
    else:
        fixture.write_bytes(b"configured live database")
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(fixture))
    with pytest.raises(ValueError):
        finish_temp_run(
            root,
            repo_root=tmp_path,
            success=True,
            disposable_paths=[fixture],
            now=NOW - timedelta(days=8),
        )
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 0
    assert fixture.exists()
    assert original.read_bytes() == b"must survive"


def test_distinct_concurrent_runs_do_not_overwrite_registration(tmp_path: Path) -> None:
    completed = NOW - timedelta(days=8)

    def complete(index: int) -> Path:
        root = tmp_path / ".tmp" / f"test-{index}"
        begin_temp_run(root, repo_root=tmp_path, now=completed)
        fixture = root / "fixture.bin"
        fixture.write_bytes(str(index).encode())
        finish_temp_run(
            root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
        )
        return fixture

    with ThreadPoolExecutor(max_workers=2) as executor:
        fixtures = set(executor.map(complete, range(4)))
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == fixtures
    assert {item.root for item in discovery.reports if item.status == "completed"} == {
        fixture.parent for fixture in fixtures
    }
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 4


@pytest.mark.parametrize("location", ["state", "runtime", "user_temp"])
def test_discovery_covers_approved_roots_without_touching_unrelated_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    state = tmp_path / "state"
    code = tmp_path / "runtime"
    user_temp = tmp_path / "user-temp"
    for directory in (state, code, user_temp):
        directory.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(user_temp))
    parent = {"state": state / ".tmp", "runtime": code / ".tmp", "user_temp": user_temp}[location]
    root = parent / "earnings-summary-successful-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=state, code_root=code, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"owned synthetic fixture")
    finish_temp_run(
        root,
        repo_root=state,
        code_root=code,
        success=True,
        disposable_paths=[fixture],
        now=completed,
    )
    unrelated = parent / "other-application" / "fixture.bin"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated")
    discovery = discover_temp_runs(state, code_root=code, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {fixture}
    result = run_retention(state, now=NOW, apply=True, catalog=discovery.catalog)
    inside_windows_temp_scope = os.name == "nt" and Path("C:/tmp") in root.parents
    if location == "runtime" and not inside_windows_temp_scope:
        # A supplied code root enables inventory, but cannot grant retirement authority.
        assert result.deleted == 0
        assert result.decisions[0].reason == "unapproved_scope"
        assert fixture.exists()
    else:
        assert result.deleted == 1
    assert unrelated.read_bytes() == b"unrelated"


def test_unapproved_retirement_scope_cannot_gain_authority_from_code_root(tmp_path: Path) -> None:
    candidate = getattr(artifact_retention, "_approved_root")
    assert isinstance(candidate, Callable)
    approved_root = cast(Callable[[Path, Path], bool], candidate)
    unrelated = Path("Z:/unapproved") if os.name == "nt" else Path("/unapproved")
    assert not approved_root(unrelated, tmp_path)
    if os.name == "nt":
        assert approved_root(Path("C:/tmp/earnings-summary-disposable"), tmp_path)
        assert not approved_root(Path("C:/tmp"), tmp_path)


def test_unapproved_external_run_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_temp = tmp_path / "user-temp"
    user_temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(user_temp))
    for root in (tmp_path / "elsewhere" / "earnings-summary-test", user_temp / "other-app"):
        with pytest.raises(ValueError):
            begin_temp_run(root, repo_root=tmp_path, now=NOW)


def test_existing_nonempty_run_cannot_acquire_disposable_ownership(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "unknown-run"
    root.mkdir(parents=True)
    source = root / "original.db"
    source.write_bytes(b"pre-existing data")
    with pytest.raises(ValueError):
        begin_temp_run(root, repo_root=tmp_path, now=NOW)
    assert source.read_bytes() == b"pre-existing data"
    assert not (root / MANIFEST_NAME).exists()


def test_unregistered_prefixed_external_run_is_reported_and_preserved(tmp_path: Path) -> None:
    root = tmp_path / "isolated-user-temp" / "earnings-summary-unregistered-test"
    root.mkdir()
    database = root / "unknown.db"
    log = root / "run.log"
    database.write_bytes(b"unclassified snapshot")
    log.write_bytes(b"unclassified diagnostic")
    discovery = discover_temp_runs(tmp_path, now=NOW)
    report = next(item for item in discovery.reports if item.root == root)
    assert report.status == "unknown"
    assert report.files == 2
    assert report.bytes == database.stat().st_size + log.stat().st_size
    assert report.registered_files == 0
    assert report.held_files == 2
    assert not discovery.catalog.artifacts
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 0
    assert database.exists()
    assert log.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows system temporary root")
def test_windows_system_temp_run_is_discovered_and_retired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    production_search_roots: Callable[[Path, Path | None], list[tuple[Path, bool]]],
) -> None:
    system_temp = Path("C:/tmp")
    if not system_temp.is_dir():
        pytest.skip("C:/tmp is unavailable")
    with tempfile.TemporaryDirectory(prefix="earnings-summary-retention-", dir=system_temp) as name:
        root = Path(name)
        original_iterdir = Path.iterdir

        def only_owned_run(path: Path) -> Iterator[Path]:
            return iter([root]) if path == system_temp else original_iterdir(path)

        monkeypatch.setattr(temp_run, "_search_roots", production_search_roots)
        monkeypatch.setattr(Path, "iterdir", only_owned_run)
        completed = NOW - timedelta(days=8)
        begin_temp_run(root, repo_root=tmp_path, now=completed)
        fixture = root / "fixture.bin"
        fixture.write_bytes(b"synthetic Windows system-temp fixture")
        finish_temp_run(
            root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
        )
        discovery = discover_temp_runs(tmp_path, now=NOW)
        assert fixture in {item.path for item in discovery.catalog.artifacts}
        assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 1
        assert not fixture.exists()


def test_malformed_completion_manifest_cannot_authorize_retirement(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "bad-test"
    begin_temp_run(root, repo_root=tmp_path, now=NOW - timedelta(days=8))
    fixture = root / "fixture.db"
    fixture.write_bytes(b"recovery")
    (root / MANIFEST_NAME).write_text('{"status":"completed"}', encoding="utf-8")
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert not discovery.catalog.artifacts
    assert next(item for item in discovery.reports if item.root == root).problems
    assert fixture.exists()


def test_successful_run_pin_preserves_disposable_files(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "pinned-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"needed for review")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )
    manifest_path = root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pins"] = ["unresolved review"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 0
    assert fixture.exists()


@pytest.mark.parametrize("change", ["pin", "failure"])
def test_lifecycle_change_after_discovery_invalidates_retirement(
    tmp_path: Path, change: str
) -> None:
    root = tmp_path / ".tmp" / "changed-lifecycle-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"recovery evidence")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert run_retention(tmp_path, now=NOW, catalog=discovery.catalog).would_delete == 1
    manifest_path = root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if change == "pin":
        manifest["pins"] = ["new unresolved recovery"]
    else:
        manifest["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog)
    assert result.deleted == 0
    assert fixture.read_bytes() == b"recovery evidence"


def test_partial_completion_retires_safe_files_and_preserves_unsafe_siblings(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".tmp" / "mixed-fixture-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.db"
    virtual_environment_file = root / ".venv" / "synthetic-package.bin"
    virtual_environment_file.parent.mkdir()
    for path in (fixture, virtual_environment_file):
        path.write_bytes(b"explicit synthetic disposable fixture")
    source = root / "ir_documents" / "source.pdf"
    source.parent.mkdir()
    source.write_bytes(b"protected source bytes")
    original = tmp_path / "original.db"
    original.write_bytes(b"original bytes")
    protected = [source]
    for kind in ("link", "hardlink"):
        path = root / f"{kind}.db"
        try:
            if kind == "link":
                path.symlink_to(original)
            else:
                os.link(original, path)
        except OSError:
            continue
        protected.append(path)
    declared = [fixture, virtual_environment_file, *protected]
    with pytest.raises(ValueError):
        finish_temp_run(
            root, repo_root=tmp_path, success=True, disposable_paths=declared, now=completed
        )
    finished = finish_temp_run(
        root,
        repo_root=tmp_path,
        success=True,
        disposable_paths=declared,
        allow_partial=True,
        now=completed,
    )
    assert finished.status == "completed"
    assert {item.path for item in finished.held_paths} == set(protected)
    assert all(item.reason for item in finished.held_paths)
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {
        fixture,
        virtual_environment_file,
    }
    report = next(item for item in discovery.reports if item.root == root)
    assert {item.path for item in report.held_paths} == set(protected)
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 2
    assert all(path.exists() for path in protected)
    assert original.read_bytes() == b"original bytes"
    assert (root / MANIFEST_NAME).exists()


def test_partial_completion_pin_preserves_safe_and_unsafe_files(tmp_path: Path) -> None:
    root = tmp_path / ".tmp" / "pinned-partial-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.db"
    fixture.write_bytes(b"safe file needed for review")
    protected = root / "transcripts" / "source.txt"
    protected.parent.mkdir()
    protected.write_bytes(b"protected source needed for review")
    finish_temp_run(
        root,
        repo_root=tmp_path,
        success=True,
        disposable_paths=[fixture, protected],
        allow_partial=True,
        now=completed,
    )
    manifest_path = root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pins"] = ["review is incomplete"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    discovery = discover_temp_runs(tmp_path, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {fixture}
    assert discovery.catalog.artifacts[0].pins == ["review is incomplete"]
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog).deleted == 0
    assert fixture.exists()
    assert protected.exists()


@pytest.mark.parametrize(
    ("outer_status", "expected_reason"),
    [
        ("active", "unfinished_lifecycle_manifest"),
        ("failed", "unfinished_lifecycle_manifest"),
        ("pinned", "pinned_lifecycle_manifest"),
        ("invalid", "unverified_lifecycle_manifest"),
        ("completed", "completed_test"),
    ],
)
def test_outer_lifecycle_guards_completed_inner_run(
    tmp_path: Path, outer_status: str, expected_reason: str
) -> None:
    outer = tmp_path / ".tmp" / "outer-run"
    inner = outer / ".tmp" / "inner-run"
    completed = NOW - timedelta(days=8)
    begin_temp_run(outer, repo_root=tmp_path, now=completed - timedelta(days=1))
    begin_temp_run(inner, repo_root=tmp_path, now=completed)
    fixture = inner / "fixture.db"
    fixture.write_bytes(b"inner recovery evidence")
    finish_temp_run(
        inner, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )
    if outer_status in {"failed", "pinned", "completed"}:
        finish_temp_run(outer, repo_root=tmp_path, success=outer_status != "failed", now=completed)
    if outer_status == "pinned":
        manifest_path = outer / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pins"] = ["outer recovery is incomplete"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif outer_status == "invalid":
        (outer / MANIFEST_NAME).write_text('{"status":"completed"}', encoding="utf-8")

    # The inner producer can publish its receipt before its enclosing owner is closed.
    discovery = discover_temp_runs(outer, now=NOW)
    assert {item.path for item in discovery.catalog.artifacts} == {fixture}
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=discovery.catalog)
    assert result.decisions[0].reason == expected_reason
    if outer_status == "completed":
        assert result.deleted == 1
        assert not fixture.exists()
    else:
        assert result.deleted == 0
        assert fixture.read_bytes() == b"inner recovery evidence"
    assert (outer / MANIFEST_NAME).exists()
    assert (inner / MANIFEST_NAME).exists()


def test_repeat_discovery_after_retirement_does_not_report_missing_file_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".tmp" / "repeat-test"
    completed = NOW - timedelta(days=8)
    begin_temp_run(root, repo_root=tmp_path, now=completed)
    fixture = root / "fixture.bin"
    fixture.write_bytes(b"disposable fixture")
    finish_temp_run(
        root, repo_root=tmp_path, success=True, disposable_paths=[fixture], now=completed
    )
    first = discover_temp_runs(tmp_path, now=NOW)
    assert run_retention(tmp_path, now=NOW, apply=True, catalog=first.catalog).deleted == 1

    second = discover_temp_runs(tmp_path, now=NOW + timedelta(seconds=1))
    report = next(item for item in second.reports if item.root == root)
    assert report.status == "completed"
    assert not report.problems
    assert report.registered_files == 0
    assert report.registered_bytes == 0
    assert report.files == 0
    repeated = run_retention(
        tmp_path, now=NOW + timedelta(seconds=1), apply=True, catalog=second.catalog
    )
    assert repeated.deleted == 0
    assert repeated.errors == 0
    assert repeated.decisions[0].action == "missing"
