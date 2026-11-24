from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from quality import static_quality_gate
from quality import static_quality_incremental as inc


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root: Path, files: dict[str, str]) -> str:
    _git(root, "init", "-q")
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def test_imports_find_every_statement_form() -> None:
    source = (
        "import a.b as c, d\n"
        "from e import (f,\n    g as h)\n"
        "if TYPE_CHECKING:\n    from ..i import j\n"
        "x = 1; import k\n"
        "from l import \\\n    m\n"
    )
    assert inc.imports(source) == {
        (0, "a.b", None),
        (0, "d", None),
        (0, "e", None),
        (0, "e", "f"),
        (0, "e", "g"),
        (2, "i", None),
        (2, "i", "j"),
        (0, "k", None),
        (0, "l", None),
        (0, "l", "m"),
    }


def test_imports_resolve_under_every_search_root_and_the_local_folder() -> None:
    sources = {
        "src/report/__init__.py": "",
        "src/report/models.py": "X = 1\n",
        "execution/upgrade_database.py": "X = 1\n",
        "tests/_helpers.py": "X = 1\n",
        "tests/test_x.py": "import report.models\nimport upgrade_database\nimport _helpers\n",
        "src/pkg/__init__.py": "",
        "src/pkg/base.py": "X = 1\n",
        "src/pkg/sub/peer.py": "X = 1\n",
        "src/pkg/sub/mod.py": "from ..base import X\nfrom . import peer\n",
    }
    for seed in (
        "src/report/models.py",
        "src/report/__init__.py",
        "execution/upgrade_database.py",
        "tests/_helpers.py",
    ):
        assert "tests/test_x.py" in inc.reverse_closure(sources, {seed}), seed
    for seed in ("src/pkg/base.py", "src/pkg/__init__.py", "src/pkg/sub/peer.py"):
        assert "src/pkg/sub/mod.py" in inc.reverse_closure(sources, {seed}), seed


def test_closure_is_transitive_and_follows_removed_modules() -> None:
    sources = {
        "src/a.py": "VALUE = 1\n",
        "src/b.py": "from a import VALUE\n",
        "tests/test_c.py": "import b\n",
        "tests/test_d.py": "import gone\n",
        "tests/test_e.py": "import json\n",
    }
    assert inc.reverse_closure(sources, {"src/a.py"}) == {"src/a.py", "src/b.py", "tests/test_c.py"}
    # A deleted seed has no source, but its old path still names its importers.
    assert inc.reverse_closure(sources, {"src/gone.py"}) == {"src/gone.py", "tests/test_d.py"}


def test_receipt_must_describe_the_exact_base_commit(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    inc.write_receipt(receipt, "a" * 40, {"src/a.py": 2})
    assert inc.load_receipt(receipt, "a" * 40) == {"src/a.py": 2}
    with pytest.raises(inc.IncrementalUnavailableError, match="another commit"):
        inc.load_receipt(receipt, "b" * 40)
    receipt.write_text(json.dumps({"schema": "old", "commit": "a" * 40, "files": {}}))
    with pytest.raises(inc.IncrementalUnavailableError, match="schema"):
        inc.load_receipt(receipt, "a" * 40)
    receipt.write_text(
        json.dumps({"schema": inc.RECEIPT_SCHEMA, "commit": "a" * 40, "files": {"x.py": -1}})
    )
    with pytest.raises(inc.IncrementalUnavailableError, match="malformed"):
        inc.load_receipt(receipt, "a" * 40)


def _population(count: int) -> dict[str, str]:
    files = {"src/core.py": "VALUE = 1\n", "src/user.py": "from core import VALUE\n"}
    files.update({f"src/leaf_{index}.py": "X = 1\n" for index in range(count)})
    return files


def test_plan_rechecks_the_closure_and_carries_the_rest(tmp_path: Path) -> None:
    files = _population(6)
    base = _repo(tmp_path, files)
    (tmp_path / "src/core.py").write_text("VALUE = '1'\n", encoding="utf-8")
    retained = sorted(files)
    counts = dict.fromkeys(retained, 0) | {"src/leaf_0.py": 3}
    plan = inc.plan(tmp_path, retained, counts, inc.changed_paths(tmp_path, base))
    assert plan.recheck == ("src/core.py", "src/user.py")
    assert plan.carried["src/leaf_0.py"] == 3
    assert set(plan.carried) | set(plan.recheck) == set(retained)


@pytest.mark.parametrize("trigger", ["pyproject.toml", "src/stub.pyi", ".github/workflows/ci.yml"])
def test_plan_requires_a_full_scan_for_global_inputs(tmp_path: Path, trigger: str) -> None:
    files = _population(6)
    base = _repo(tmp_path, files)
    (tmp_path / trigger).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / trigger).write_text("x = 1\n", encoding="utf-8")
    retained = sorted(files)
    with pytest.raises(inc.IncrementalUnavailableError, match="every file"):
        inc.plan(tmp_path, retained, dict.fromkeys(retained, 0), inc.changed_paths(tmp_path, base))


def test_plan_rejects_a_receipt_that_does_not_reconcile(tmp_path: Path) -> None:
    files = _population(6)
    base = _repo(tmp_path, files)
    retained = sorted(files)
    missing = dict.fromkeys(retained[1:], 0)
    with pytest.raises(inc.IncrementalUnavailableError, match="unchanged population"):
        inc.plan(tmp_path, retained, missing, inc.changed_paths(tmp_path, base))
    foreign = dict.fromkeys(retained, 0) | {"src/elsewhere.py": 0}
    with pytest.raises(inc.IncrementalUnavailableError, match="outside"):
        inc.plan(tmp_path, retained, foreign, inc.changed_paths(tmp_path, base))


def test_plan_prefers_a_full_scan_when_the_closure_is_most_files(tmp_path: Path) -> None:
    files = _population(0)
    base = _repo(tmp_path, files)
    (tmp_path / "src/core.py").write_text("VALUE = 2\n", encoding="utf-8")
    retained = sorted(files)
    with pytest.raises(inc.IncrementalUnavailableError, match="most of the population"):
        inc.plan(tmp_path, retained, dict.fromkeys(retained, 0), inc.changed_paths(tmp_path, base))


def _payload(diagnostics: dict[str, int], analyzed: int) -> dict[str, object]:
    rows = [{"file": path} for path, count in diagnostics.items() for _ in range(count)]
    return {"generalDiagnostics": rows, "summary": {"filesAnalyzed": analyzed}}


def _gate_repo(tmp_path: Path, ceiling: int) -> tuple[str, list[str]]:
    files = _population(6)
    base = _repo(tmp_path, files)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "schema_version": "bha-105.v1",
                "pyright_diagnostics": {"src": ceiling},
                "suppressions": {"src": 0},
            }
        ),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "config.json")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-qm", "ceilings")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src/core.py").write_text("VALUE = '1'\n", encoding="utf-8")
    return base, sorted(files)


def test_gate_passes_from_the_receipt_without_a_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base, retained = _gate_repo(tmp_path, ceiling=4)
    receipt = tmp_path / "receipt.json"
    inc.write_receipt(receipt, base, dict.fromkeys(retained, 0) | {"src/leaf_0.py": 3})
    calls: list[Sequence[str] | None] = []

    def fake_run(
        root: Path, pythonpath: str, population: Sequence[str], files: Sequence[str] | None = None
    ) -> tuple[object, dict[str, str]]:
        calls.append(files)
        assert files is not None
        return _payload({str(tmp_path / "src/user.py"): 1}, len(files)), {}

    monkeypatch.setattr(static_quality_gate, "_run_pyright", fake_run)
    argv = ["--repo-root", str(tmp_path), "--config", "config.json", "--base", base]
    code = static_quality_gate.main([*argv, "--receipt-in", str(receipt), "--receipt-commit", base])
    assert code == 0
    assert calls == [("src/core.py", "src/user.py")]
    assert "exact (incremental)" in capsys.readouterr().out


def test_gate_confirms_an_incremental_mismatch_with_a_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, retained = _gate_repo(tmp_path, ceiling=3)
    receipt = tmp_path / "receipt.json"
    inc.write_receipt(receipt, base, dict.fromkeys(retained, 0) | {"src/leaf_0.py": 3})
    calls: list[Sequence[str] | None] = []

    def fake_run(
        root: Path, pythonpath: str, population: Sequence[str], files: Sequence[str] | None = None
    ) -> tuple[object, dict[str, str]]:
        calls.append(files)
        analyzed = len(population) if files is None else len(files)
        return _payload({str(tmp_path / "src/user.py"): 1}, analyzed), {}

    monkeypatch.setattr(static_quality_gate, "_run_pyright", fake_run)
    out = tmp_path / "head-receipt.json"
    code = static_quality_gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--config",
            "config.json",
            "--base",
            base,
            "--receipt-in",
            str(receipt),
            "--receipt-commit",
            base,
            "--receipt-out",
            str(out),
        ]
    )
    # The receipt predicts 4; the ceiling says 3; the authoritative full scan decides.
    assert calls == [("src/core.py", "src/user.py"), None]
    # The tree has an uncommitted change, so no receipt may claim HEAD.
    assert code == 2
    assert not out.exists()


def test_receipt_out_records_a_clean_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, retained = _gate_repo(tmp_path, ceiling=3)
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-qam", "head")

    def fake_run(
        root: Path, pythonpath: str, population: Sequence[str], files: Sequence[str] | None = None
    ) -> tuple[object, dict[str, str]]:
        assert files is None
        return _payload({str(tmp_path / "src/leaf_0.py"): 3}, len(population)), {}

    monkeypatch.setattr(static_quality_gate, "_run_pyright", fake_run)
    out = tmp_path / "out" / "receipt.json"
    out.parent.mkdir()
    (tmp_path / ".gitignore").write_text("out/\n", encoding="utf-8")
    argv = ["--repo-root", str(tmp_path), "--config", "config.json", "--base", base]
    assert static_quality_gate.main([*argv, "--receipt-out", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert written["files"] == dict.fromkeys(retained, 0) | {"src/leaf_0.py": 3}
