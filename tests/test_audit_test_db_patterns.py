from __future__ import annotations

import subprocess
from pathlib import Path

from quality.test_db_patterns import audit_test_db_patterns


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    _git(tmp_path, "init", "-q")
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", ".")
    return tmp_path


def test_classifies_only_tracked_builders_with_frozen_precedence(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "tests/test_down.py": (
                "def test_db(tmp_path):\n"
                "    command.downgrade(cfg, 'base')\n"
                "    conn.execute('CREATE TABLE x (id INTEGER)')\n"
            ),
            "tests/test_hand.py": (
                "def test_db(tmp_path):\n"
                "    conn.execute('CREATE TABLE x (id INTEGER)')\n"
                "    db = tmp_path / 'data' / 'portfolio.db'\n"
            ),
            "tests/test_read.py": "def test_read():\n    conn.execute('SELECT 1')\n",
        },
    )
    untracked = root / "tests/test_untracked.py"
    untracked.write_text("command.upgrade(cfg, 'head')\n", encoding="utf-8")
    report = audit_test_db_patterns(root)
    classes = {item.path: item.taxonomy for item in report.database_builders}
    assert classes == {
        "tests/test_down.py": "direct-downgrade",
        "tests/test_hand.py": "hand-DDL-unit-schema",
    }
    assert all("untracked" not in path for path in report.tracked_test_files)
    assert any(finding.kind == "explicit_fixture" for finding in report.findings)
    assert report.status == "PASS"


def test_checkout_default_holds_but_guard_assertion_does_not(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "tests/test_bad.py": (
                "import sqlite3\n"
                "def test_db():\n"
                "    sqlite3.connect('data/portfolio.db')\n"
                "    conn.execute('CREATE TABLE x (id INTEGER)')\n"
            ),
            "tests/test_guard.py": (
                "def test_guard():\n"
                "    forbidden = PROJECT_ROOT / 'data' / 'portfolio.db'\n"
                "    with pytest.raises(RuntimeError):\n"
                "        connect_sqlite(forbidden)\n"
            ),
        },
    )
    report = audit_test_db_patterns(root)
    assert report.status == "HOLD"
    assert any("forbidden_checkout_default" in violation for violation in report.violations)
    assert not any("test_guard.py" in violation for violation in report.violations)
