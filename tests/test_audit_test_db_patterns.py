"""Hermetic tests for the source-only test-database-pattern audit."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import execution.audit_test_db_patterns as cli
import quality.test_db_patterns as scanner
from quality.git_env import clean_local_git_env
from quality.test_db_patterns import audit_test_db_patterns

_RunGit = Callable[[Path, tuple[str, ...]], bytes]


_ALEMBIC_UPGRADE_CANONICAL = "alembic.command." + "upgrade"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        shell=False,
        env=clean_local_git_env(),
        timeout=30,
    )


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "t@example.com").returncode == 0
    assert _git(root, "config", "user.name", "t").returncode == 0
    assert _git(root, "config", "commit.gpgsign", "false").returncode == 0


def _write(root: Path, name: str, content: bytes) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _closure(root: Path) -> None:
    for name in (
        "src/quality/test_db_patterns.py",
        "src/quality/git_env.py",
        "execution/audit_test_db_patterns.py",
        "src/quality/test_db_invocations.py",
        "src/quality/test_db_models.py",
    ):
        src = Path(__file__).resolve().parents[1] / name
        dst = root / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _repo(root: Path, files: dict[str, str]) -> Path:
    _init(root)
    _closure(root)
    for name, content in files.items():
        _write(root, name, content.encode("utf-8"))
    assert _git(root, "add", ".").returncode == 0
    assert _git(root, "commit", "-qm", "init").returncode == 0
    return root


def _route_git(monkeypatch: pytest.MonkeyPatch, overrides: dict[tuple[str, ...], bytes]) -> None:
    original = cast(_RunGit, getattr(scanner, "_run_git"))

    def fake(root: Path, args: tuple[str, ...]) -> bytes:
        for key, val in overrides.items():
            if args == key:
                return val
        return original(root, args)

    monkeypatch.setattr(scanner, "_run_git", fake)


def test_git_env_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/bogus-git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/bogus-work")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/bogus-index")
    root = _repo(tmp_path / "iso", {"tests/test_a.py": "x = 1\n"})
    rep = audit_test_db_patterns(root)
    assert rep.collection_status == "COMPLETE"


def test_expires_naive_rejected_and_aware_preserved() -> None:
    from quality.test_db_invocations import normalize_conversions

    template: dict[str, object] = {
        "invocation_id": "abc",
        "path": "tests/test_c.py",
        "locator": {"start_line": 1, "start_col": 0, "end_line": 1, "end_col": 5},
        "source_sha256": "a" * 64,
        "parity_receipt": ".tmp/quality/parity-001.json",
        "owner_issue": "BHA-147",
        "reason": "retain-with-parity",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    items, malformed = normalize_conversions([dict(template)])
    assert not malformed
    assert items[0].expires_at.tzinfo is not None
    zulu = dict(template)
    zulu["expires_at"] = "2099-01-01T00:00:00Z"
    zulu_items, zulu_malformed = normalize_conversions([zulu])
    assert not zulu_malformed
    assert zulu_items[0].expires_at.tzinfo is not None
    naive = dict(template)
    naive["expires_at"] = "2099-01-01T00:00:00"
    naive_items, naive_malformed = normalize_conversions([naive])
    assert naive_malformed
    assert naive_items == ()


def test_tracked_nested_untracked_ignored(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "a",
        {
            "tests/nested/test_down.py": "def t():\n    command.downgrade(cfg, 'base')\n",
            "instruction_tests/test_hand.py": "def t():\n    c.execute('CREATE TABLE x (id INTEGER)')\n",
        },
    )
    upgrade_call = "command." + "upgrade"
    (root / "tests/test_untracked.py").write_text(f"{upgrade_call}(cfg,'head')\n", encoding="utf-8")
    rep = audit_test_db_patterns(root)
    assert rep.collection_status == "COMPLETE"
    assert {b.path: b.taxonomy for b in rep.database_builders}[
        "tests/nested/test_down.py"
    ] == "direct-downgrade"
    assert {b.path: b.taxonomy for b in rep.database_builders}[
        "instruction_tests/test_hand.py"
    ] == "hand-DDL-unit-schema"
    assert all("untracked" not in p for p in rep.tracked_test_files)


def test_empty_and_untracked_only_hold(tmp_path: Path) -> None:
    r = tmp_path / "b"
    _init(r)
    _closure(r)
    assert _git(r, "add", ".").returncode == 0
    assert _git(r, "commit", "-qm", "init").returncode == 0
    rep = audit_test_db_patterns(r)
    assert rep.collection_status == "HOLD"
    assert rep.collection_note == "empty-scope"
    (r / "tests").mkdir(exist_ok=True)
    (r / "tests/test_x.py").write_text("x=1\n", encoding="utf-8")
    rep2 = audit_test_db_patterns(r)
    assert rep2.collection_note == "empty-scope"


def test_git_oserror_timeout_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "c", {"tests/test_a.py": "x = 1\n"})

    def boom(
        args: list[str],
        *,
        capture_output: bool,
        check: bool,
        shell: bool,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        raise OSError("down")

    def timeout_fake(
        args: list[str],
        *,
        capture_output: bool,
        check: bool,
        shell: bool,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    def nonzero(
        args: list[str],
        *,
        capture_output: bool,
        check: bool,
        shell: bool,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", boom)
    assert audit_test_db_patterns(root).collection_note == "git-unavailable"
    monkeypatch.setattr(subprocess, "run", timeout_fake)
    assert audit_test_db_patterns(root).collection_note == "git-unavailable"
    monkeypatch.setattr(subprocess, "run", nonzero)
    assert audit_test_db_patterns(root).collection_note == "git-nonzero"


def test_bad_git_bytes_and_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "d", {"tests/test_a.py": "x = 1\n"})
    _route_git(monkeypatch, {("ls-files", "-z", "--", "tests", "instruction_tests"): b"\xff\xfe"})
    assert audit_test_db_patterns(root).collection_note == "invalid-git-utf8"
    monkeypatch.undo()
    _route_git(
        monkeypatch, {("ls-files", "-z", "--", "tests", "instruction_tests"): b"tests/test_a.py"}
    )
    assert audit_test_db_patterns(root).collection_note == "invalid-git-framing"
    monkeypatch.undo()
    _route_git(monkeypatch, {("rev-parse", "HEAD"): b"ZZZ\n"})
    assert audit_test_db_patterns(root).collection_note == "invalid-head"
    monkeypatch.undo()
    _route_git(monkeypatch, {("rev-parse", "HEAD"): b"  abc123\n"})
    assert audit_test_db_patterns(root).collection_note == "invalid-head"
    monkeypatch.undo()
    _route_git(
        monkeypatch, {("rev-parse", "HEAD"): b"abcdef0123456789abcdef0123456789abcdef01  \n"}
    )
    assert audit_test_db_patterns(root).collection_note == "invalid-head"


def test_bad_paths_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = {"tests/test_a.py": "x = 1\n"}
    for tag, bad in [
        ("trav", b"tests/../evil.py\x00tests/test_a.py\x00"),
        ("abs", b"/etc/passwd\x00"),
        ("bs", b"tests\\win.py\x00"),
        ("noncan", b"tests/./test_a.py\x00"),
        ("dbl", b"tests//test_a.py\x00"),
    ]:
        root = _repo(tmp_path / f"e-{tag}", base)
        _route_git(monkeypatch, {("ls-files", "-z", "--", "tests", "instruction_tests"): bad})
        assert audit_test_db_patterns(root).collection_note == "invalid-path", tag
        monkeypatch.undo()
    root = _repo(tmp_path / "e-dup", base)
    _route_git(
        monkeypatch,
        {
            (
                "ls-files",
                "-z",
                "--",
                "tests",
                "instruction_tests",
            ): b"tests/test_a.py\x00tests/test_a.py\x00"
        },
    )
    assert audit_test_db_patterns(root).collection_note == "duplicate-path"
    monkeypatch.undo()
    root2 = _repo(tmp_path / "e-npy", base)
    _write(root2, "tests/evil", b"x")
    _route_git(
        monkeypatch,
        {
            (
                "ls-files",
                "-z",
                "--",
                "tests",
                "instruction_tests",
            ): b"tests/../evil\x00tests/test_a.py\x00"
        },
    )
    assert audit_test_db_patterns(root2).collection_note == "invalid-path"
    monkeypatch.undo()


def test_closure_git_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = (
        "ls-files",
        "-z",
        "--",
        "execution/audit_test_db_patterns.py",
        "src/quality/git_env.py",
        "src/quality/test_db_invocations.py",
        "src/quality/test_db_models.py",
        "src/quality/test_db_patterns.py",
    )
    cases = (
        (
            "duplicate",
            b"execution/audit_test_db_patterns.py\x00"
            b"execution/audit_test_db_patterns.py\x00"
            b"src/quality/git_env.py\x00"
            b"src/quality/test_db_invocations.py\x00"
            b"src/quality/test_db_models.py\x00src/quality/test_db_patterns.py\x00",
            "duplicate-path",
        ),
        (
            "missing",
            b"src/quality/git_env.py\x00"
            b"src/quality/test_db_invocations.py\x00"
            b"src/quality/test_db_models.py\x00src/quality/test_db_patterns.py\x00",
            "closure-untracked",
        ),
        (
            "noncanonical",
            b"./src/quality/git_env.py\x00"
            b"execution/audit_test_db_patterns.py\x00"
            b"src/quality/test_db_invocations.py\x00"
            b"src/quality/test_db_models.py\x00src/quality/test_db_patterns.py\x00",
            "invalid-path",
        ),
    )
    for tag, output, expected in cases:
        root = _repo(tmp_path / f"closure-{tag}", {"tests/test_a.py": "x = 1\n"})
        with monkeypatch.context() as scoped:
            _route_git(scoped, {key: output})
            assert audit_test_db_patterns(root).collection_note == expected


def test_dirty_states(tmp_path: Path) -> None:
    root = _repo(tmp_path / "f", {"tests/test_a.py": "x = 1\n"})
    (root / "tests/test_a.py").write_text("y=2\n", encoding="utf-8")
    assert audit_test_db_patterns(root).collection_note == "dirty-tree"
    root2 = _repo(tmp_path / "f2", {"tests/test_a.py": "x = 1\n"})
    (root2 / "src/quality/git_env.py").write_text("# dirty\n", encoding="utf-8")
    rep = audit_test_db_patterns(root2)
    assert rep.collection_note == "dirty-tree"
    assert rep.violations == ("dirty-tree",)
    root3 = _repo(tmp_path / "f3", {"tests/test_a.py": "x = 1\n"})
    (root3 / "src/quality/test_db_invocations.py").write_text("# dirty\n", encoding="utf-8")
    assert audit_test_db_patterns(root3).collection_note == "dirty-tree"
    root4 = _repo(tmp_path / "f4", {"tests/test_a.py": "x = 1\n"})
    (root4 / "src/quality/test_db_models.py").write_text("# dirty\n", encoding="utf-8")
    assert audit_test_db_patterns(root4).collection_note == "dirty-tree"


def test_scanner_identity_changes_only_after_committed_helper_change(tmp_path: Path) -> None:
    root = _repo(tmp_path / "identity", {"tests/test_a.py": "x = 1\n"})
    first = audit_test_db_patterns(root)
    second = audit_test_db_patterns(root)
    assert first.collection_status == "COMPLETE"
    assert first.scanner_sha256 == second.scanner_sha256
    assert first.source_sha256 == second.source_sha256
    helper = root / "src/quality/git_env.py"
    helper.write_text(helper.read_text(encoding="utf-8") + "# committed change\n", encoding="utf-8")
    dirty = audit_test_db_patterns(root)
    assert dirty.collection_note in ("dirty-tree", "scanner-closure-mismatch")
    assert _git(root, "add", "src/quality/git_env.py").returncode == 0
    assert _git(root, "commit", "-qm", "change helper").returncode == 0
    cross = audit_test_db_patterns(root)
    assert cross.collection_status == "HOLD"
    assert cross.collection_note == "scanner-closure-mismatch"
    assert cross.violations == ("scanner-closure-mismatch",)
    for helper_rel in (
        "src/quality/test_db_invocations.py",
        "src/quality/test_db_models.py",
    ):
        helper_root = _repo(tmp_path / helper_rel.replace("/", "-"), {"tests/test_a.py": "x = 1\n"})
        helper_target = helper_root / helper_rel
        helper_target.write_text(
            helper_target.read_text(encoding="utf-8") + "# committed change\n", encoding="utf-8"
        )
        assert audit_test_db_patterns(helper_root).collection_note in (
            "dirty-tree",
            "scanner-closure-mismatch",
        )
        assert _git(helper_root, "add", helper_rel).returncode == 0
        assert _git(helper_root, "commit", "-qm", "change helper").returncode == 0
        helper_cross = audit_test_db_patterns(helper_root)
        assert helper_cross.collection_status == "HOLD"
        assert helper_cross.collection_note == "scanner-closure-mismatch"
    proc = subprocess.run(
        [sys.executable, str(root / "execution/audit_test_db_patterns.py"), "--root", str(root)],
        capture_output=True,
        check=False,
        shell=False,
        env=clean_local_git_env(),
        timeout=30,
    )
    assert proc.returncode == 0
    committed = json.loads(proc.stdout.decode("utf-8"))
    assert committed["collection_status"] == "COMPLETE"
    assert committed["source_sha256"] == first.source_sha256
    assert committed["scanner_sha256"] != first.scanner_sha256
    assert "committed change" not in proc.stdout.decode("utf-8")


def test_scanner_closure_mismatch_holds(tmp_path: Path) -> None:
    root = _repo(tmp_path / "mismatch", {"tests/test_a.py": "x = 1\n"})
    helper = root / "execution/audit_test_db_patterns.py"
    helper.write_text(helper.read_text(encoding="utf-8") + "# forked scanner\n", encoding="utf-8")
    assert _git(root, "add", "execution/audit_test_db_patterns.py").returncode == 0
    assert _git(root, "commit", "-qm", "fork scanner").returncode == 0
    report = audit_test_db_patterns(root)
    assert report.collection_status == "HOLD"
    assert report.collection_note == "scanner-closure-mismatch"
    assert report.violations == ("scanner-closure-mismatch",)


def test_symlink_missing_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r1 = tmp_path / "g1"
    _init(r1)
    _closure(r1)
    _write(r1, "tests/test_a.py", b"x=1\n")
    os.symlink("test_a.py", r1 / "tests/test_link.py")
    assert _git(r1, "add", ".").returncode == 0
    assert _git(r1, "commit", "-qm", "init").returncode == 0
    assert audit_test_db_patterns(r1).collection_note == "missing-path"
    r2 = _repo(tmp_path / "g2", {"tests/test_a.py": "x = 1\n"})
    _route_git(
        monkeypatch,
        {("status", "--porcelain=v1", "-z", "--untracked-files=no", "--", "tests"): b""},
    )
    (r2 / "tests/test_a.py").unlink()
    assert audit_test_db_patterns(r2).collection_note in ("missing-path", "dirty-tree")
    monkeypatch.undo()
    r3 = _repo(tmp_path / "g3", {"tests/test_ok.py": "x = 1\n"})
    orig = Path.read_bytes

    def boom(self: Path) -> bytes:
        if str(self).endswith("test_ok.py"):
            raise OSError("denied")
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    rep = audit_test_db_patterns(r3)
    assert rep.collection_status == "COMPLETE"
    assert rep.raw_audit_status == "HOLD"
    assert rep.admission_status == "HOLD"
    assert rep.admission_reason == "disposition_and_ratchet_deferred"
    assert rep.findings == (
        scanner.PatternFinding(
            path="tests/test_ok.py", line=0, kind="parse_error", evidence="read-error"
        ),
    )
    assert rep.violations == ("parse_error:tests/test_ok.py:0",)
    assert "denied" not in rep.model_dump_json()


def test_utf8_syntax_secret(tmp_path: Path) -> None:
    r = tmp_path / "h"
    _init(r)
    _closure(r)
    _write(r, "tests/test_ok.py", b"x=1\n")
    _write(r, "tests/test_syn.py", b"def broken(:\n")
    _write(r, "tests/test_bin.py", b"x='SENTINEL_SECRET_XYZ'\n" + b"\xff\xfe")
    assert _git(r, "add", ".").returncode == 0
    assert _git(r, "commit", "-qm", "init").returncode == 0
    rep = audit_test_db_patterns(r)
    assert rep.collection_status == "COMPLETE"
    assert rep.raw_audit_status == "HOLD"
    assert rep.admission_status == "HOLD"
    assert rep.admission_reason == "disposition_and_ratchet_deferred"
    blob = rep.model_dump_json()
    assert "SENTINEL_SECRET_XYZ" not in blob
    assert "Traceback" not in blob
    assert any(f.evidence == "syntax-error" for f in rep.findings)
    assert any(f.evidence == "invalid-utf8" for f in rep.findings)
    assert "parse_error:tests/test_bin.py:0" in rep.violations
    assert "parse_error:tests/test_syn.py:0" in rep.violations


def test_taxonomy_all(tmp_path: Path) -> None:
    upgrade_call = "command." + "upgrade"
    files = {
        "tests/test_down.py": "def t():\n    command.downgrade(cfg,'b')\n",
        "tests/test_arch.py": (
            f"def t():\n    {upgrade_call}(cfg,'h')\n    x='versions_archived'\n"
        ),
        "tests/test_seed.py": (
            f"def t():\n    {upgrade_call}(cfg,'h')\n    x='seed insert into t'\n"
        ),
        "tests/test_hist.py": "def t():\n    command.stamp(cfg,'h')\n",
        "tests/test_boot.py": "def t():\n    x.create_all(e)\n",
        "tests/test_boot_perf.py": "def t():\n    x.create_all(e)\n    y='benchmark'\n",
        "tests/test_performance_volume.py": "def t():\n    foo.migrated_db(x)\n",
        "tests/test_ddl.py": "def t():\n    c.execute('CREATE INDEX i ON t(x)')\n",
        "tests/test_cached.py": "def t():\n    foo.migrated_db(x)\n",
        "tests/test_plain.py": "def t():\n    y='benchmark performance volume'\n",
    }
    root = _repo(tmp_path / "tax", files)
    rep = audit_test_db_patterns(root)
    got = {b.path: b.taxonomy for b in rep.database_builders}
    assert got["tests/test_down.py"] == "direct-downgrade"
    assert got["tests/test_arch.py"] == "archived-graph"
    assert got["tests/test_seed.py"] == "seeded-upgrade"
    assert got["tests/test_hist.py"] == "direct-historical"
    assert got["tests/test_boot.py"] == "custom-bootstrap"
    assert got["tests/test_boot_perf.py"] == "custom-bootstrap"
    assert got["tests/test_performance_volume.py"] == "performance-volume"
    assert got["tests/test_ddl.py"] == "hand-DDL-unit-schema"
    assert got["tests/test_cached.py"] == "cached-current-head"
    assert "tests/test_plain.py" not in got
    assert [builder.path for builder in rep.database_builders] == sorted(got)
    assert rep.counts_by_taxonomy == {
        "archived-graph": 1,
        "cached-current-head": 1,
        "custom-bootstrap": 2,
        "direct-downgrade": 1,
        "direct-historical": 1,
        "hand-DDL-unit-schema": 1,
        "performance-volume": 1,
        "seeded-upgrade": 1,
    }
    assert got and rep.raw_audit_status == "PASS"


def test_raises_guard(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "i",
        {
            "tests/test_bad.py": "def t():\n    connect('data/portfolio.db')\n    c.execute('CREATE TABLE x(id INTEGER)')\n",
            "tests/test_guard.py": "def t():\n    p='x'\n    with pytest.raises(RuntimeError):\n        connect_sqlite('data/portfolio.db')\n",
            "tests/test_fake.py": "def t():\n    x=obj\n    with x.raises(RuntimeError):\n        connect('data/portfolio.db')\n",
            "tests/test_tmp.py": "def t(tmp_path):\n    p=tmp_path / 'portfolio.db'\n    x='PORTFOLIO.DB'\n    c.execute('CREATE TABLE x(id INTEGER)')\n",
        },
    )
    rep = audit_test_db_patterns(root)
    assert rep.violations == (
        "forbidden_checkout_default:tests/test_bad.py:2",
        "forbidden_checkout_default:tests/test_fake.py:4",
    )
    assert rep.admission_status == "HOLD"


def test_hashes(tmp_path: Path) -> None:
    files = {"tests/test_a.py": "x = 1\n", "tests/test_b.py": "y = 2\n"}
    a = audit_test_db_patterns(_repo(tmp_path / "j1", files))
    b = audit_test_db_patterns(_repo(tmp_path / "j2", files))
    assert a.source_sha256 == b.source_sha256
    assert a.scanner_sha256 == b.scanner_sha256
    man = hashlib.sha256()
    for n in sorted(files):
        man.update(n.encode() + b"\x00" + files[n].encode() + b"\x00")
    assert a.source_sha256 == man.hexdigest()


def test_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = (Path(__file__).resolve().parents[1] / "src/quality/test_db_patterns.py").read_text(
        encoding="utf-8"
    )
    assert "sqlite3" not in src
    assert "rglob" not in src
    assert "shell=True" not in src
    root = _repo(tmp_path / "db", {"tests/test_a.py": "x = 1\n"})

    def boom(database: str) -> sqlite3.Connection:
        raise AssertionError("db touched")

    monkeypatch.setattr("sqlite3.connect", boom)
    assert audit_test_db_patterns(root).collection_status == "COMPLETE"


def test_cli_pass_hold(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path / "k", {"tests/test_a.py": "x = 1\n"})
    assert cli.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["raw_audit_status"] == "PASS"
    bad = _repo(
        tmp_path / "k2", {"tests/test_a.py": "def t():\n    connect('data/portfolio.db')\n"}
    )
    assert cli.main(["--root", str(bad)]) == 2
    out2 = capsys.readouterr().out
    assert json.loads(out2)["raw_audit_status"] == "HOLD"
    assert json.loads(out2)["admission_status"] == "HOLD"


def test_cli_receipts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "l", {"tests/test_a.py": "x = 1\n"})
    out = root / "out.json"
    assert cli.main(["--root", str(root), "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["admission_reason"] == "disposition_and_ratchet_deferred"
    monkeypatch.setattr(cli, "OUTPUT_INLINE_LIMIT", 1)
    assert cli.main(["--root", str(root)]) == 0
    summ = json.loads(capsys.readouterr().out)
    assert "receipt" in summ
    assert (root / summ["receipt"]).exists()


def test_cli_write_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "m", {"tests/test_a.py": "x = 1\n"})
    assert cli.main(["--root", str(root), "--output", "/nope-xyz/out.json"]) == 1
    initial = capsys.readouterr()
    assert len(initial.err) < 500
    assert json.loads(initial.err)["error_code"] == "delivery-error"

    def boom(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        raise OSError("denied")

    with monkeypatch.context() as scoped:
        scoped.setattr("pathlib.Path.write_text", boom)
        assert cli.main(["--root", str(root), "--output", str(root / "o.json")]) == 1
        explicit = capsys.readouterr().err
        assert len(explicit) < 500
        assert json.loads(explicit)["error_code"] == "delivery-error"
    with monkeypatch.context() as scoped:
        scoped.setattr(cli, "OUTPUT_INLINE_LIMIT", 1)
        scoped.setattr("pathlib.Path.write_text", boom)
        assert cli.main(["--root", str(root)]) == 1
        automatic = capsys.readouterr().err
        assert len(automatic) < 500
        assert json.loads(automatic)["error_code"] == "delivery-error"


def test_checkout_default_variants(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "variants",
        {
            "tests/test_mixed.py": "def t():\n    connect('DATA/PORTFOLIO.DB')\n",
            "tests/test_alias.py": (
                "def t():\n    default = 'data/portfolio.db'\n"
                "    alias = default\n    connect(alias)\n"
            ),
            "tests/test_concat.py": "def t():\n    connect('data/' + 'portfolio.db')\n",
            "tests/test_root.py": (
                "def t():\n    connect(str(PROJECT_ROOT / 'data' / 'portfolio.db'))\n"
            ),
            "tests/test_fstring.py": (
                "def t():\n    value = 'data/portfolio.db'\n    connect(f'{value}')\n"
            ),
            "tests/test_unknown.py": "def t(value):\n    connect(value)\n",
        },
    )
    report = audit_test_db_patterns(root)
    forbidden = {
        finding.path for finding in report.findings if finding.kind == "forbidden_checkout_default"
    }
    assert forbidden == {
        "tests/test_alias.py",
        "tests/test_concat.py",
        "tests/test_fstring.py",
        "tests/test_mixed.py",
        "tests/test_root.py",
    }
    assert report.raw_audit_status == "HOLD"
    assert report.admission_status == "HOLD"
    assert "DATA/PORTFOLIO.DB" not in report.model_dump_json()


def test_forbidden_exact_paths_and_call_forms(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "exact",
        {
            "tests/test_dot.py": "def t():\n    connect('./data/portfolio.db')\n",
            "tests/test_tail.py": "def t():\n    connect(PROJECT_ROOT / 'data/portfolio.db')\n",
            "tests/test_parts.py": (
                "def t():\n    connect(PROJECT_ROOT / 'data' / 'portfolio.db')\n"
            ),
            "tests/test_join.py": (
                "def t():\n    import os\n"
                "    connect(os.path.join(PROJECT_ROOT, 'data', 'portfolio.db'))\n"
            ),
            "tests/test_join_tail.py": (
                "def t():\n    import os\n"
                "    connect(os.path.join(PROJECT_ROOT, 'data/portfolio.db'))\n"
            ),
            "tests/test_kw.py": "def t():\n    connect(db_path='data/portfolio.db')\n",
            "tests/test_kw_file.py": "def t():\n    open(file='data/portfolio.db')\n",
            "tests/test_open.py": "def t():\n    open('data/portfolio.db')\n",
            "tests/test_path_open.py": "def t():\n    Path('data/portfolio.db').open()\n",
            "tests/test_other.py": "def t():\n    connect('/tmp/other.db')\n",
        },
    )
    report = audit_test_db_patterns(root)
    forbidden = {
        finding.path for finding in report.findings if finding.kind == "forbidden_checkout_default"
    }
    assert forbidden == {
        "tests/test_dot.py",
        "tests/test_join.py",
        "tests/test_join_tail.py",
        "tests/test_kw.py",
        "tests/test_kw_file.py",
        "tests/test_open.py",
        "tests/test_parts.py",
        "tests/test_path_open.py",
        "tests/test_tail.py",
    }


def test_arbitrary_keywords_and_multipart_path_are_forbidden(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "kwargs-path",
        {
            "tests/test_filename.py": "def t():\n    connect(filename='data/portfolio.db')\n",
            "tests/test_path_to_db.py": ("def t():\n    connect(path_to_db='data/portfolio.db')\n"),
            "tests/test_uri.py": "def t():\n    connect(uri='data/portfolio.db')\n",
            "tests/test_path_two.py": (
                "def t():\n    connect(Path(PROJECT_ROOT, 'data/portfolio.db'))\n"
            ),
            "tests/test_path_three.py": (
                "def t():\n    connect(Path(PROJECT_ROOT, 'data', 'portfolio.db'))\n"
            ),
            "tests/test_tmp_single.py": ("def t():\n    connect(Path('/tmp/data/portfolio.db'))\n"),
            "tests/test_tmp_parts.py": (
                "def t():\n    connect(Path('/tmp', 'data', 'portfolio.db'))\n"
            ),
            "tests/test_tmp_qualified_single.py": (
                "def t():\n    connect(pathlib.Path('/tmp/data/portfolio.db'))\n"
            ),
            "tests/test_tmp_qualified_parts.py": (
                "def t():\n    connect(pathlib.Path('/tmp', 'data', 'portfolio.db'))\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    forbidden = {
        finding.path for finding in report.findings if finding.kind == "forbidden_checkout_default"
    }
    assert forbidden == {
        "tests/test_filename.py",
        "tests/test_path_three.py",
        "tests/test_path_to_db.py",
        "tests/test_path_two.py",
        "tests/test_uri.py",
    }


def test_direct_fixture_expr_is_safe(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "fixture-direct",
        {
            "tests/test_direct.py": (
                "def t(tmp_path):\n    connect(tmp_path / 'data/portfolio.db')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert [
        finding for finding in report.findings if finding.kind == "forbidden_checkout_default"
    ] == []
    assert report.raw_audit_status == "PASS"


def test_fixture_named_alias_is_forbidden(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "fixture-alias",
        {
            "tests/test_str_alias.py": (
                "def t():\n"
                "    default_tmp_path = 'data/portfolio.db'\n"
                "    connect(str(default_tmp_path))\n"
            ),
            "tests/test_path_alias.py": (
                "def t():\n"
                "    from pathlib import Path\n"
                "    default_tmp_path = 'data/portfolio.db'\n"
                "    connect(Path(default_tmp_path))\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    forbidden = {
        finding.path for finding in report.findings if finding.kind == "forbidden_checkout_default"
    }
    assert forbidden == {"tests/test_path_alias.py", "tests/test_str_alias.py"}
    assert report.raw_audit_status == "HOLD"


def test_scope_and_program_order_are_isolated(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "scope",
        {
            "tests/test_scope.py": (
                "def first():\n"
                "    p = 'data/portfolio.db'\n"
                "    connect(p)\n"
                "    p = '/tmp/other.db'\n"
                "    connect(p)\n"
                "def second():\n"
                "    p = '/tmp/other.db'\n"
                "    connect(p)\n"
            )
        },
    )
    report = audit_test_db_patterns(root)
    assert report.violations == ("forbidden_checkout_default:tests/test_scope.py:3",)


def test_unreadable_paths_frame_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = _repo(
        tmp_path / "hash-a",
        {"tests/test_ok.py": "x = 1\n", "tests/test_bad_a.py": "y = 1\n"},
    )
    root_b = _repo(
        tmp_path / "hash-b",
        {"tests/test_ok.py": "x = 1\n", "tests/test_bad_b.py": "y = 1\n"},
    )
    original = Path.read_bytes

    def failing_read(self: Path) -> bytes:
        if self.name in {"test_bad_a.py", "test_bad_b.py"}:
            raise OSError("denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    report_a = audit_test_db_patterns(root_a)
    report_b = audit_test_db_patterns(root_b)
    assert report_a.raw_audit_status == "HOLD"
    assert report_b.raw_audit_status == "HOLD"
    assert report_a.source_sha256 != report_b.source_sha256
    assert report_a.violations == ("parse_error:tests/test_bad_a.py:0",)
    assert report_b.violations == ("parse_error:tests/test_bad_b.py:0",)


def test_toctou_source_mutation_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "toctou", {"tests/test_a.py": "x = 1\n"})
    original = Path.read_bytes

    def mutating_read(self: Path) -> bytes:
        data = original(self)
        if self == root / "tests/test_a.py":
            self.write_text("changed = True\n", encoding="utf-8")
        return data

    monkeypatch.setattr("pathlib.Path.read_bytes", mutating_read)
    report = audit_test_db_patterns(root)
    assert report.collection_status == "HOLD"
    assert report.collection_note == "dirty-tree"
    assert report.raw_audit_status == "HOLD"
    assert report.violations == ("dirty-tree",)


def test_cli_unexpected_audit_exception_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "cli-error", {"tests/test_a.py": "x = 1\n"})

    def fail_audit(_root: Path) -> scanner.TestDbAudit:
        raise RuntimeError("SENTINEL_SECRET")

    monkeypatch.setattr(cli, "audit_test_db_patterns", fail_audit)
    assert cli.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SENTINEL_SECRET" not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.err) == {
        "error_code": "delivery-error",
        "message": "audit-failed",
    }


@pytest.mark.parametrize(
    "closure_rel",
    [
        "src/quality/test_db_invocations.py",
        "src/quality/test_db_models.py",
        "src/quality/test_db_patterns.py",
    ],
)
def test_closure_unreadable_is_fixed_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closure_rel: str
) -> None:
    root = _repo(tmp_path / "closure-unreadable", {"tests/test_a.py": "x = 1\n"})
    original = Path.read_bytes

    def failing_read(self: Path) -> bytes:
        if self == root / closure_rel:
            raise OSError("SENTINEL_SECRET")
        return original(self)

    monkeypatch.setattr("pathlib.Path.read_bytes", failing_read)
    report = audit_test_db_patterns(root)
    assert report.collection_status == "HOLD"
    assert report.collection_note == "closure-unreadable"
    assert report.raw_audit_status == "HOLD"
    assert report.findings == ()
    assert report.violations == ("closure-unreadable",)
    assert "SENTINEL_SECRET" not in report.model_dump_json()


def test_dirty_status_malformed_output_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "dirty-output", {"tests/test_a.py": "x = 1\n"})
    key = (
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
        "--",
        "tests",
        "instruction_tests",
        "execution/audit_test_db_patterns.py",
        "src/quality/git_env.py",
        "src/quality/test_db_invocations.py",
        "src/quality/test_db_models.py",
        "src/quality/test_db_patterns.py",
    )
    cases = (
        (b"\xff", "invalid-git-utf8"),
        (b"M tests/test_a.py", "invalid-git-framing"),
        (b"M tests/test_a.py\x00\x00", "invalid-porcelain"),
    )
    for output, expected in cases:
        with monkeypatch.context() as scoped:
            _route_git(scoped, {key: output})
            report = audit_test_db_patterns(root)
            assert report.collection_status == "HOLD"
            assert report.collection_note == expected


def test_fixture_like_string_is_forbidden(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "fixture-spoof",
        {
            "tests/test_spoof.py": ("def t():\n    connect('tmp_path/../data/portfolio.db')\n"),
            "tests/test_spoof_path.py": (
                "def t():\n"
                "    from pathlib import Path\n"
                "    connect(Path('tmp_path/../data/portfolio.db'))\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    forbidden = {
        finding.path for finding in report.findings if finding.kind == "forbidden_checkout_default"
    }
    assert forbidden == {"tests/test_spoof.py", "tests/test_spoof_path.py"}
    assert report.raw_audit_status == "HOLD"


def test_cli_serialization_failure_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "cli-ser", {"tests/test_a.py": "x = 1\n"})

    class BadReport:
        def model_dump_json(self, *, indent: int) -> str:
            raise RuntimeError(f"SENTINEL_SER_{indent}")

    def fake_audit(_root: Path) -> scanner.TestDbAudit:
        return cast(scanner.TestDbAudit, BadReport())

    monkeypatch.setattr(cli, "audit_test_db_patterns", fake_audit)
    assert cli.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SENTINEL_SER" not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.err) == {
        "error_code": "delivery-error",
        "message": "audit-failed",
    }


@pytest.mark.parametrize(
    ("relative_path", "expected_note"),
    [
        ("src/quality/test_db_invocations.py", "closure-unreadable"),
        ("src/quality/test_db_models.py", "closure-unreadable"),
        ("src/quality/test_db_patterns.py", "closure-unreadable"),
        ("tests/test_a.py", ""),
    ],
)
def test_post_read_identity_change_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    expected_note: str,
) -> None:
    root = _repo(tmp_path / relative_path.replace("/", "-"), {"tests/test_a.py": "x = 1\n"})
    victim = root / relative_path
    original = Path.resolve
    calls = 0

    def changed_resolution(self: Path, strict: bool = False) -> Path:
        nonlocal calls
        result = original(self, strict=strict)
        if self != victim:
            return result
        calls += 1
        if calls != 2:
            return result
        return root.parent / "outside.py"

    monkeypatch.setattr(Path, "resolve", changed_resolution)
    report = audit_test_db_patterns(root)
    assert report.raw_audit_status == "HOLD"
    if expected_note:
        assert report.collection_status == "HOLD"
        assert report.collection_note == expected_note
        assert report.violations == (expected_note,)
    else:
        assert report.collection_status == "COMPLETE"
        assert report.findings == (
            scanner.PatternFinding(
                path=relative_path, line=0, kind="parse_error", evidence="read-error"
            ),
        )
        assert report.violations == (f"parse_error:{relative_path}:0",)


def _invocations_by_path(report: scanner.TestDbAudit) -> dict[str, list[scanner.BuilderInvocation]]:
    grouped: dict[str, list[scanner.BuilderInvocation]] = {}
    for invocation in report.builder_invocations:
        grouped.setdefault(invocation.path, []).append(invocation)
    return grouped


def _write_valid_receipt(
    root: Path, receipt_ref: str, invocation: scanner.BuilderInvocation
) -> None:
    target = root / receipt_ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": "test-db-parity/v1",
                "status": "PASS",
                "invocation_id": invocation.invocation_id,
                "path": invocation.path,
                "locator": {
                    "start_line": invocation.locator.start_line,
                    "start_col": invocation.locator.start_col,
                    "end_line": invocation.locator.end_line,
                    "end_col": invocation.locator.end_col,
                },
                "source_sha256": invocation.source_sha256,
            }
        ),
        encoding="utf-8",
    )


def _conversion_for(
    invocation: scanner.BuilderInvocation,
    root: Path | None = None,
    *,
    receipt: str = ".tmp/quality/parity-001.json",
    issue: str = "BHA-147",
    reason: str = "retain-with-parity",
    expires: str = "2099-01-01T00:00:00+00:00",
) -> scanner.InvocationConversion:
    from datetime import UTC, datetime

    text = expires.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if root is not None:
        _write_valid_receipt(root, receipt, invocation)
    return scanner.InvocationConversion.model_validate(
        {
            "invocation_id": invocation.invocation_id,
            "path": invocation.path,
            "locator": {
                "start_line": invocation.locator.start_line,
                "start_col": invocation.locator.start_col,
                "end_line": invocation.locator.end_line,
                "end_col": invocation.locator.end_col,
            },
            "source_sha256": invocation.source_sha256,
            "parity_receipt": receipt,
            "owner_issue": issue,
            "reason": reason,
            "expires_at": parsed,
        }
    )


def test_invocation_direct_and_alias_identities(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-direct",
        {
            "tests/test_direct_upgrade.py": (
                "from alembic.command import upgrade\ndef make_db(cfg):\n    upgrade(cfg, 'head')\n"
            ),
            "tests/test_alias_upgrade.py": (
                "import alembic.command as ac\ndef make_db(cfg):\n    ac.upgrade(cfg, 'head')\n"
            ),
            "tests/test_from_module_alias.py": (
                "from alembic import command as cmd\n"
                "def make_db(cfg):\n"
                "    cmd.stamp(cfg, 'head')\n"
            ),
            "tests/test_import_alias_downgrade.py": (
                "from alembic.command import downgrade as dg\n"
                "def make_db(cfg):\n"
                "    dg(cfg, 'base')\n"
            ),
            "tests/test_migrated.py": ("def test_x(migrated_db):\n    migrated_db('head')\n"),
            "tests/test_exec.py": (
                "def make_db(conn):\n    conn.executescript('CREATE TABLE x (id INTEGER)')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.collection_status == "COMPLETE"
    grouped = _invocations_by_path(report)
    assert grouped["tests/test_direct_upgrade.py"][0].canonical_identity == (
        _ALEMBIC_UPGRADE_CANONICAL
    )
    assert grouped["tests/test_direct_upgrade.py"][0].observed_call == "upgrade"
    assert grouped["tests/test_alias_upgrade.py"][0].canonical_identity == (
        _ALEMBIC_UPGRADE_CANONICAL
    )
    assert grouped["tests/test_alias_upgrade.py"][0].observed_call == "ac.upgrade"
    assert grouped["tests/test_from_module_alias.py"][0].canonical_identity == (
        "alembic.command.stamp"
    )
    assert grouped["tests/test_import_alias_downgrade.py"][0].canonical_identity == (
        "alembic.command.downgrade"
    )
    assert grouped["tests/test_migrated.py"][0].canonical_identity == "migrated_db"
    assert grouped["tests/test_exec.py"][0].canonical_identity == "connection.executescript"
    for invocation in report.builder_invocations:
        assert invocation.disposition == "RETAIN"
        assert invocation.factory is not None
        assert invocation.source_sha256 is not None
        assert invocation.locator.start_line >= 1
        assert invocation.locator.end_line >= invocation.locator.start_line


def test_invocation_dynamic_hold_and_unresolved(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-dynamic",
        {
            "tests/test_dyn.py": (
                "import alembic.command as ac\n"
                "def make_db(cfg):\n"
                "    getattr(ac, 'upgrade')(cfg, 'head')\n"
            ),
            "tests/test_bare.py": ("def make_db(cfg):\n    upgrade(cfg, 'head')\n"),
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    assert len(grouped["tests/test_dyn.py"]) >= 1
    assert all(item.canonical_identity is None for item in grouped["tests/test_dyn.py"])
    assert all(item.disposition == "HOLD" for item in grouped["tests/test_dyn.py"])
    assert all(item.taxonomy == "unclassified" for item in grouped["tests/test_dyn.py"])
    assert grouped["tests/test_bare.py"][0].canonical_identity is None
    assert grouped["tests/test_bare.py"][0].disposition == "HOLD"


def test_invocation_multiple_and_multiline_locators(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-multi",
        {
            "tests/test_multi.py": (
                "from alembic.command import upgrade\n"
                "def make_db(cfg):\n"
                "    upgrade(cfg, 'head')\n"
                "    upgrade(\n"
                "        cfg,\n"
                "        'head',\n"
                "    )\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    items = _invocations_by_path(report)["tests/test_multi.py"]
    assert len(items) == 2
    first, second = items
    assert (first.locator.start_line, first.locator.end_line) == (3, 3)
    assert second.locator.start_line == 4
    assert second.locator.end_line == 7
    assert second.locator.end_col > 0
    assert first.invocation_id != second.invocation_id
    assert first.locator != second.locator


def test_invocation_fingerprints_match_raw(tmp_path: Path) -> None:
    import hashlib

    files = {
        "tests/test_fp.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n",
    }
    root = _repo(tmp_path / "inv-fp", files)
    report = audit_test_db_patterns(root)
    raw = (root / "tests/test_fp.py").read_bytes()
    expected = hashlib.sha256(raw).hexdigest()
    assert len(report.builder_invocations) == 1
    assert report.builder_invocations[0].source_sha256 == expected


def test_invocation_valid_convert(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-convert",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    assert base.builder_invocations[0].disposition == "RETAIN"
    conversion = _conversion_for(base.builder_invocations[0], root)
    converted = audit_test_db_patterns(root, (conversion,))
    assert converted.builder_invocations[0].disposition == "CONVERT"
    assert converted.raw_audit_status == base.raw_audit_status
    assert "upgrade(cfg" not in converted.model_dump_json()


def test_invocation_convert_missing_expired_stale_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-hold",
        {
            "tests/test_h.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    assert _conversion_for(target, receipt="").invocation_id == target.invocation_id
    cases = [
        _conversion_for(target, receipt=""),
        _conversion_for(target, issue=""),
        _conversion_for(target, reason=""),
        _conversion_for(target, expires="2000-01-01T00:00:00+00:00"),
    ]
    for conversion in cases:
        report = audit_test_db_patterns(root, (conversion,))
        assert report.builder_invocations[0].disposition == "HOLD"
    stale_locator = _conversion_for(target)
    stale_locator = stale_locator.model_copy(
        update={
            "locator": target.locator.model_copy(update={"start_col": target.locator.start_col + 1})
        }
    )
    assert audit_test_db_patterns(root, (stale_locator,)).builder_invocations[0].disposition == (
        "HOLD"
    )
    stale_sha = _conversion_for(target).model_copy(update={"source_sha256": "0" * 64})
    assert audit_test_db_patterns(root, (stale_sha,)).builder_invocations[0].disposition == "HOLD"
    stale_path = _conversion_for(target).model_copy(update={"path": "tests/other.py"})
    assert audit_test_db_patterns(root, (stale_path,)).builder_invocations[0].disposition == "HOLD"


def test_invocation_cli_serialization(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(
        tmp_path / "inv-cli",
        {
            "tests/test_cli.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    assert cli.main(["--root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "builder_invocations" in payload
    assert len(payload["builder_invocations"]) == 1
    entry = payload["builder_invocations"][0]
    assert entry["canonical_identity"] == _ALEMBIC_UPGRADE_CANONICAL
    assert entry["disposition"] == "RETAIN"
    assert entry["locator"]["start_line"] >= 1
    assert entry["locator"]["end_line"] >= entry["locator"]["start_line"]
    assert entry["source_sha256"] is not None
    assert entry["invocation_id"] is not None
    assert "upgrade(cfg" not in json.dumps(payload)
    base = audit_test_db_patterns(root)
    conversion = _conversion_for(base.builder_invocations[0], root)
    _write_valid_receipt(root, ".tmp/quality/parity-1.json", base.builder_invocations[0])
    disp_file = root / "disps.json"
    disp_file.write_text(
        json.dumps(
            [
                {
                    "invocation_id": conversion.invocation_id,
                    "path": conversion.path,
                    "locator": {
                        "start_line": conversion.locator.start_line,
                        "start_col": conversion.locator.start_col,
                        "end_line": conversion.locator.end_line,
                        "end_col": conversion.locator.end_col,
                    },
                    "source_sha256": conversion.source_sha256,
                    "parity_receipt": ".tmp/quality/parity-1.json",
                    "owner_issue": "BHA-147",
                    "reason": "ok",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert cli.main(["--root", str(root), "--dispositions", str(disp_file)]) == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["builder_invocations"][0]["disposition"] == "CONVERT"
    naive_payload = json.loads(disp_file.read_text(encoding="utf-8"))
    naive_payload[0]["expires_at"] = "2099-01-01T00:00:00"
    disp_file.write_text(json.dumps(naive_payload), encoding="utf-8")
    assert cli.main(["--root", str(root), "--dispositions", str(disp_file)]) != 0


def test_conservative_unresolved_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-conservative",
        {
            "tests/test_bare_migrated.py": "def make_db():\n    migrated_db('head')\n",
            "tests/test_arbitrary_exec.py": "def make_db():\n    obj.executescript('x')\n",
            "tests/test_arbitrary_create.py": "def make_db():\n    obj.create_all(e)\n",
            "tests/test_shadow_migrated.py": (
                "def make_db(migrated_db):\n    migrated_db = 1\n    migrated_db('head')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    assert grouped["tests/test_bare_migrated.py"][0].canonical_identity is None
    assert grouped["tests/test_bare_migrated.py"][0].disposition == "HOLD"
    assert grouped["tests/test_arbitrary_exec.py"][0].canonical_identity is None
    assert grouped["tests/test_arbitrary_exec.py"][0].disposition == "HOLD"
    assert grouped["tests/test_arbitrary_create.py"][0].canonical_identity is None
    assert grouped["tests/test_arbitrary_create.py"][0].disposition == "HOLD"
    assert len(grouped["tests/test_shadow_migrated.py"]) == 1


def test_shadowed_import_binding_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-shadow-import",
        {
            "tests/test_bare_alembic.py": f"def m(cfg):\n    {_ALEMBIC_UPGRADE_CANONICAL}(cfg, 'h')\n",
            "tests/test_shadow_module.py": (
                "import alembic.command as ac\ndef m(cfg):\n    ac = 1\n    ac.upgrade(cfg, 'h')\n"
            ),
            "tests/test_shadow_direct.py": (
                "from alembic.command import upgrade\n"
                "def m(cfg):\n"
                "    upgrade = 1\n"
                "    upgrade(cfg, 'h')\n"
            ),
            "tests/test_module_rebind.py": (
                "import alembic.command as ac\nac = 1\ndef m(cfg):\n    ac.upgrade(cfg, 'h')\n"
            ),
            "tests/test_literal_conn.py": "def m():\n    connection.executescript('x')\n",
            "tests/test_literal_meta.py": "def m():\n    metadata.create_all(e)\n",
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    assert grouped["tests/test_bare_alembic.py"][0].canonical_identity is None
    assert grouped["tests/test_bare_alembic.py"][0].disposition == "HOLD"
    assert grouped["tests/test_shadow_module.py"][0].canonical_identity is None
    assert grouped["tests/test_shadow_direct.py"][0].canonical_identity is None
    assert grouped["tests/test_module_rebind.py"][0].canonical_identity is None
    assert grouped["tests/test_literal_conn.py"][0].canonical_identity is None
    assert grouped["tests/test_literal_meta.py"][0].canonical_identity is None


def test_dynamic_alias_and_import_chain_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-dyn-full",
        {
            "tests/test_alias.py": (
                "import alembic.command as ac\n"
                "def m(cfg):\n"
                "    fn = getattr(ac, 'upgrade')\n"
                "    fn(cfg, 'h')\n"
            ),
            "tests/test_getattr_var.py": (
                "import alembic.command as ac\ndef m(cfg, name):\n    getattr(ac, name)(cfg, 'h')\n"
            ),
            "tests/test_chain.py": (
                "def m(cfg):\n"
                "    __import__('alembic.command').upgrade(cfg, 'h')\n"
                "    import_module('alembic.command').upgrade(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    assert any(
        item.canonical_identity is None and item.disposition == "HOLD"
        for item in grouped["tests/test_alias.py"]
    )
    assert len(grouped["tests/test_alias.py"]) >= 2
    assert grouped["tests/test_getattr_var.py"][0].canonical_identity is None
    assert grouped["tests/test_getattr_var.py"][0].disposition == "HOLD"
    assert len(grouped["tests/test_chain.py"]) == 2
    assert all(item.disposition == "HOLD" for item in grouped["tests/test_chain.py"])
    assert all(item.canonical_identity is None for item in grouped["tests/test_chain.py"])
    assert "upgrade(cfg" not in report.model_dump_json()


def test_conversion_canonical_boundary_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-boundary",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    assert base.builder_invocations[0].disposition == "RETAIN"
    good = _conversion_for(target, root)
    assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "CONVERT"
    bad_cases = [
        _conversion_for(target, receipt="parity-001"),
        _conversion_for(target, receipt="evidence/parity.txt"),
        _conversion_for(target, receipt="/tmp/evil.json"),
        _conversion_for(target, receipt="../evil.json"),
        _conversion_for(target, receipt=".tmp/quality/parity idea.json"),
        _conversion_for(target, issue="bha-147"),
        _conversion_for(target, issue="BHA147"),
        _conversion_for(target, issue=" BHA-147"),
        _conversion_for(target, reason=""),
        _conversion_for(target, reason=" x "),
    ]
    for bad in bad_cases:
        assert audit_test_db_patterns(root, (bad,)).builder_invocations[0].disposition == "HOLD"
    naive = good.model_copy(update={"expires_at": good.expires_at.replace(tzinfo=None)})
    assert audit_test_db_patterns(root, (naive,)).builder_invocations[0].disposition == "HOLD"


def test_conversion_malformed_dict_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-malformed",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    valid_dict = {
        "invocation_id": target.invocation_id,
        "path": target.path,
        "locator": {
            "start_line": target.locator.start_line,
            "start_col": target.locator.start_col,
            "end_line": target.locator.end_line,
            "end_col": target.locator.end_col,
        },
        "source_sha256": target.source_sha256,
        "parity_receipt": ".tmp/quality/parity-001.json",
        "owner_issue": "BHA-147",
        "reason": "retain-with-parity",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    _write_valid_receipt(root, ".tmp/quality/parity-001.json", target)
    converted = audit_test_db_patterns(root, [valid_dict])
    assert converted.builder_invocations[0].disposition == "CONVERT"
    malformed = audit_test_db_patterns(root, [{"invocation_id": "x"}])
    assert malformed.builder_invocations[0].disposition == "HOLD"
    mixed = audit_test_db_patterns(root, ["not-a-mapping"])
    assert mixed.builder_invocations[0].disposition == "HOLD"


def test_parity_receipt_binding_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-receipt",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    good = _conversion_for(target, root)
    assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "CONVERT"
    receipt_ref = ".tmp/quality/parity-001.json"
    receipt_path = root / receipt_ref
    valid_text = receipt_path.read_text(encoding="utf-8")
    valid_obj = json.loads(valid_text)

    def check(mutated: str | None, convert: scanner.InvocationConversion | None = None) -> None:
        if mutated is not None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(mutated, encoding="utf-8")
        conv = convert if convert is not None else good
        assert audit_test_db_patterns(root, (conv,)).builder_invocations[0].disposition == "HOLD"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(valid_text, encoding="utf-8")

    receipt_path.unlink()
    assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "HOLD"
    receipt_path.write_text(valid_text, encoding="utf-8")
    check("{not-json")
    check(
        '{"schema_version": "test-db-parity/v1", "status": "PASS", "invocation_id": "x", "path": "y", "locator": {"start_line": 1, "start_col": 0, "end_line": 1, "end_col": 1}, "source_sha256": "z", "schema_version": "dup"}'
    )
    for key, bad in [
        ("schema_version", "other/v1"),
        ("status", "FAIL"),
        ("invocation_id", "0" * 16),
        ("path", "tests/other.py"),
        ("source_sha256", "0" * 64),
    ]:
        obj = dict(valid_obj)
        obj[key] = bad
        check(json.dumps(obj))
    obj = dict(valid_obj)
    loc = dict(obj["locator"])
    loc["start_col"] = loc["start_col"] + 1
    obj["locator"] = loc
    check(json.dumps(obj))


def test_parity_receipt_filesystem_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-receipt-fs",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    good = _conversion_for(target, root)
    assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "CONVERT"
    receipt_path = root / ".tmp/quality/parity-001.json"
    valid_text = receipt_path.read_text(encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text(valid_text, encoding="utf-8")
    receipt_path.unlink()
    try:
        os.symlink(str(outside), receipt_path)
        assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "HOLD"
    finally:
        receipt_path.unlink(missing_ok=True)
    receipt_path.write_text(valid_text, encoding="utf-8")
    receipt_path.unlink()
    receipt_path.mkdir(parents=True, exist_ok=True)
    assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "HOLD"
    receipt_path.rmdir()
    receipt_path.write_text(valid_text, encoding="utf-8")
    try:
        extra = root / ".tmp/quality/parity-hard.json"
        extra.write_text(valid_text, encoding="utf-8")
        try:
            os.link(extra, root / ".tmp/quality/parity-hard2.json")
            hard = _conversion_for(target, root, receipt=".tmp/quality/parity-hard2.json")
            assert (
                audit_test_db_patterns(root, (hard,)).builder_invocations[0].disposition == "HOLD"
            )
        except OSError:
            pytest.skip("hardlink unsupported")
    finally:
        receipt_path.write_text(valid_text, encoding="utf-8")


def test_conversion_duplicate_unreferenced_noncanonical_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-convert-gaps",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    valid = _conversion_for(target)
    dup = audit_test_db_patterns(root, (valid, valid))
    assert dup.builder_invocations[0].disposition == "HOLD"
    unknown = _conversion_for(target).model_copy(update={"invocation_id": "unreferenced-id"})
    mixed = audit_test_db_patterns(root, (valid, unknown))
    assert mixed.builder_invocations[0].disposition == "HOLD"
    assert mixed.builder_invocations[0].canonical_identity is not None
    for bad in (
        _conversion_for(target, receipt=" parity-001 "),
        _conversion_for(target, issue=" BHA-147 "),
        _conversion_for(target, reason=" retain "),
    ):
        assert audit_test_db_patterns(root, (bad,)).builder_invocations[0].disposition == "HOLD"


def test_lambda_comprehension_class_direct_shadow_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-scope-shadow",
        {
            "tests/test_lambda_shadow.py": (
                "import alembic.command as ac\nf = lambda ac: ac.upgrade(cfg, 'h')\n"
            ),
            "tests/test_comp_shadow.py": (
                "import alembic.command as ac\n"
                "def m(modules):\n"
                "    return [ac.upgrade(x, 'h') for ac in modules]\n"
            ),
            "tests/test_class_direct.py": (
                "import alembic.command as ac\n"
                "class C:\n"
                "    ac = object()\n"
                "    x = ac.upgrade(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.collection_status == "COMPLETE"
    grouped = _invocations_by_path(report)
    assert grouped["tests/test_lambda_shadow.py"][0].canonical_identity is None
    assert grouped["tests/test_lambda_shadow.py"][0].disposition == "HOLD"
    assert grouped["tests/test_comp_shadow.py"][0].canonical_identity is None
    assert grouped["tests/test_comp_shadow.py"][0].disposition == "HOLD"
    assert grouped["tests/test_class_direct.py"][0].canonical_identity is None
    assert grouped["tests/test_class_direct.py"][0].disposition == "HOLD"


def test_class_nested_method_and_ordinary_function_retain(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-scope-retain",
        {
            "tests/test_nested_method.py": (
                "import alembic.command as ac\n"
                "class C:\n"
                "    ac = object()\n"
                "    def m(self, cfg):\n"
                "        ac.upgrade(cfg, 'h')\n"
            ),
            "tests/test_ordinary.py": (
                "import alembic.command as ac\ndef m(cfg):\n    ac.upgrade(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.collection_status == "COMPLETE"
    grouped = _invocations_by_path(report)
    assert grouped["tests/test_nested_method.py"][0].canonical_identity == (
        _ALEMBIC_UPGRADE_CANONICAL
    )
    assert grouped["tests/test_nested_method.py"][0].disposition == "RETAIN"
    assert grouped["tests/test_ordinary.py"][0].canonical_identity == _ALEMBIC_UPGRADE_CANONICAL
    assert grouped["tests/test_ordinary.py"][0].disposition == "RETAIN"


def test_function_later_binding_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-later",
        {
            "tests/test_later_assign_module.py": (
                "import alembic.command as ac\n"
                "def m(cfg):\n"
                "    ac.upgrade(cfg, 'h')\n"
                "    ac = object()\n"
            ),
            "tests/test_later_assign_direct.py": (
                "from alembic.command import upgrade\n"
                "def m(cfg):\n"
                "    upgrade(cfg, 'h')\n"
                "    upgrade = object()\n"
            ),
            "tests/test_later_for.py": (
                "import alembic.command as ac\n"
                "def m(cfg):\n"
                "    ac.upgrade(cfg, 'h')\n"
                "    for ac in []:\n"
                "        pass\n"
            ),
            "tests/test_later_with.py": (
                "import alembic.command as ac\n"
                "def m(cfg):\n"
                "    ac.upgrade(cfg, 'h')\n"
                "    with open('x') as ac:\n"
                "        pass\n"
            ),
            "tests/test_legit_retain.py": (
                "import alembic.command as ac\ndef m(cfg):\n    ac.upgrade(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.collection_status == "COMPLETE"
    grouped = _invocations_by_path(report)
    for path in (
        "tests/test_later_assign_module.py",
        "tests/test_later_assign_direct.py",
        "tests/test_later_for.py",
        "tests/test_later_with.py",
    ):
        assert grouped[path][0].canonical_identity is None, path
        assert grouped[path][0].disposition == "HOLD", path
    assert grouped["tests/test_legit_retain.py"][0].canonical_identity == (
        _ALEMBIC_UPGRADE_CANONICAL
    )
    assert grouped["tests/test_legit_retain.py"][0].disposition == "RETAIN"


def test_factory_subscript_getattr_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-unresolved",
        {
            "tests/test_factory.py": "def m():\n    factory().executescript('x')\n",
            "tests/test_subscript.py": "def m(conns):\n    conns[0].executescript('x')\n",
            "tests/test_getattr_call.py": (
                "import alembic.command as command\n"
                "def m(cfg):\n"
                "    getattr(command, 'upgrade')(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    factory = grouped["tests/test_factory.py"]
    assert len(factory) == 1
    assert factory[0].canonical_identity is None
    assert factory[0].disposition == "HOLD"
    subscript = grouped["tests/test_subscript.py"]
    assert len(subscript) == 1
    assert subscript[0].canonical_identity is None
    assert subscript[0].disposition == "HOLD"
    dynamic = grouped["tests/test_getattr_call.py"]
    assert len(dynamic) == 1
    assert dynamic[0].canonical_identity is None
    assert dynamic[0].disposition == "HOLD"


def test_simple_transitive_alias_retain(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-alias-retain",
        {
            "tests/test_module_simple.py": (
                "from alembic.command import upgrade\n"
                "alias = upgrade\n"
                "def m(cfg):\n"
                "    alias(cfg, 'h')\n"
            ),
            "tests/test_module_chain.py": (
                "from alembic.command import upgrade\n"
                "first = upgrade\n"
                "second = first\n"
                "def m(cfg):\n"
                "    second(cfg, 'h')\n"
            ),
            "tests/test_local_simple.py": (
                "from alembic.command import upgrade\n"
                "def m(cfg):\n"
                "    alias = upgrade\n"
                "    alias(cfg, 'h')\n"
            ),
            "tests/test_local_chain.py": (
                "from alembic.command import upgrade\n"
                "def m(cfg):\n"
                "    first = upgrade\n"
                "    second = first\n"
                "    second(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    for path in (
        "tests/test_module_simple.py",
        "tests/test_module_chain.py",
        "tests/test_local_simple.py",
        "tests/test_local_chain.py",
    ):
        items = grouped[path]
        assert len(items) == 1
        assert items[0].canonical_identity == _ALEMBIC_UPGRADE_CANONICAL
        assert items[0].disposition == "RETAIN"


def test_alias_conservative_no_false_retain(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-alias-hold",
        {
            "tests/test_use_before.py": (
                "from alembic.command import upgrade\n"
                "def m(cfg):\n"
                "    alias(cfg, 'h')\n"
                "    alias = upgrade\n"
            ),
            "tests/test_later_rebind.py": (
                "from alembic.command import upgrade\n"
                "def m(cfg):\n"
                "    alias = upgrade\n"
                "    alias(cfg, 'h')\n"
                "    alias = 1\n"
            ),
            "tests/test_param_shadow.py": (
                "from alembic.command import upgrade\n"
                "alias = upgrade\n"
                "def m(alias):\n"
                "    alias(cfg, 'h')\n"
            ),
            "tests/test_ambiguous.py": (
                "from alembic.command import upgrade\n"
                "from alembic.command import stamp\n"
                "alias = upgrade\n"
                "alias = stamp\n"
                "def m(cfg):\n"
                "    alias(cfg, 'h')\n"
            ),
            "tests/test_unrelated.py": "def m(cfg):\n    ordinary(cfg, 'h')\n",
            "tests/test_nested_attr.py": (
                "from alembic.command import upgrade\n"
                "class C:\n"
                "    other = upgrade\n"
                "    def m(self, cfg):\n"
                "        other(cfg, 'h')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    grouped = _invocations_by_path(report)
    assert "tests/test_unrelated.py" not in grouped
    nested = grouped["tests/test_nested_attr.py"]
    assert len(nested) == 1
    assert nested[0].canonical_identity is None
    assert nested[0].disposition == "HOLD"
    for path in (
        "tests/test_use_before.py",
        "tests/test_later_rebind.py",
        "tests/test_param_shadow.py",
        "tests/test_ambiguous.py",
    ):
        items = grouped[path]
        assert len(items) == 1
        assert items[0].canonical_identity is None, path
        assert items[0].disposition == "HOLD", path


def test_parity_receipt_required_fields_hold(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-receipt-required",
        {
            "tests/test_c.py": "from alembic.command import upgrade\ndef m(cfg):\n    upgrade(cfg,'h')\n"
        },
    )
    base = audit_test_db_patterns(root)
    target = base.builder_invocations[0]
    receipt_ref = ".tmp/quality/parity-001.json"
    good = _conversion_for(target, root, receipt=receipt_ref)
    assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "CONVERT"
    receipt_path = root / receipt_ref
    valid_text = receipt_path.read_text(encoding="utf-8")
    valid_obj = json.loads(valid_text)
    assert valid_obj["schema_version"] == "test-db-parity/v1"
    assert valid_obj["status"] == "PASS"
    for key in ("schema_version", "status"):
        mutated = dict(valid_obj)
        del mutated[key]
        receipt_path.write_text(json.dumps(mutated), encoding="utf-8")
        assert audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "HOLD", (
            key
        )
        receipt_path.write_text(valid_text, encoding="utf-8")
        assert (
            audit_test_db_patterns(root, (good,)).builder_invocations[0].disposition == "CONVERT"
        ), key


def test_builder_attribute_alias_retain(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-build-alias",
        {
            "tests/test_exec_alias.py": (
                "def m(conn):\n    build = conn.executescript\n"
                "    build('CREATE TABLE x (id INTEGER)')\n"
            ),
            "tests/test_create_alias.py": (
                "def m(metadata):\n    build = metadata.create_all\n    build(e)\n"
            ),
            "tests/test_migrated_alias.py": (
                "def m(migrated_db):\n    build = migrated_db\n    build('head')\n"
            ),
            "tests/test_two_hop.py": (
                "def m(conn):\n    tmp = conn.executescript\n"
                "    build = tmp\n"
                "    build('CREATE TABLE x (id INTEGER)')\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.collection_status == "COMPLETE"
    grouped = _invocations_by_path(report)
    expected = {
        "tests/test_exec_alias.py": "connection.executescript",
        "tests/test_create_alias.py": "metadata.create_all",
        "tests/test_migrated_alias.py": "migrated_db",
        "tests/test_two_hop.py": "connection.executescript",
    }
    for path, canonical in expected.items():
        items = grouped[path]
        assert len(items) == 1, path
        assert items[0].canonical_identity == canonical, path
        assert items[0].disposition == "RETAIN", path


def test_builder_attribute_alias_hold_and_no_row(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "inv-build-alias-hold",
        {
            "tests/test_use_before.py": (
                "def m(conn):\n    build('x')\n    build = conn.executescript\n"
            ),
            "tests/test_rebind.py": (
                "def m(conn):\n    build = conn.executescript\n    build('x')\n    build = 1\n"
            ),
            "tests/test_competing.py": (
                "def m(conn, metadata):\n    build = conn.executescript\n"
                "    build = metadata.create_all\n    build(e)\n"
            ),
            "tests/test_unsupported.py": ("def m(client):\n    build = client.call\n    build()\n"),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.collection_status == "COMPLETE"
    grouped = _invocations_by_path(report)
    for path in (
        "tests/test_use_before.py",
        "tests/test_rebind.py",
        "tests/test_competing.py",
    ):
        items = grouped[path]
        assert len(items) == 1, path
        assert items[0].canonical_identity is None, path
        assert items[0].disposition == "HOLD", path
    assert grouped.get("tests/test_unsupported.py", []) == []
