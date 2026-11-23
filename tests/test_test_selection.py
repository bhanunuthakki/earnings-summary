from __future__ import annotations

import ast
import gzip
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import cast

import pytest

from quality import change_regions, test_impact_recorder, test_selection


def _regions(before: str, after: str) -> change_regions.Regions:
    import difflib

    old, new = before.splitlines(), after.splitlines()
    base_lines: set[int] = set()
    head_lines: set[int] = set()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            base_lines.update(range(i1 + 1, i2 + 1))
            head_lines.update(range(j1 + 1, j2 + 1))
    return change_regions.changed_regions(
        ast.parse(before),
        ast.parse(after),
        base_lines & change_regions.code_lines(before),
        head_lines & change_regions.code_lines(after),
    )


BASE = textwrap.dedent(
    '''\
    """Module docstring."""
    import json

    LIMIT = 3


    def helper(value):
        return value + LIMIT


    @register("x")
    def handler():
        return 1


    class Box:
        size = 1

        def grow(self):
        # comment
            return self.size + 1
    '''
)


def test_function_body_change_alters_only_that_function() -> None:
    regions = _regions(BASE, BASE.replace("value + LIMIT", "value - LIMIT"))
    assert regions.functions == {"helper"}
    assert not (regions.module or regions.names or regions.classes)


def test_comments_and_docstrings_alter_nothing() -> None:
    changed = BASE.replace('"""Module docstring."""', '"""Other text."""').replace(
        "# comment", "# other"
    )
    regions = _regions(BASE, changed)
    assert regions == change_regions.Regions()


def test_module_constant_and_import_rebind_names() -> None:
    regions = _regions(
        BASE, BASE.replace("LIMIT = 3", "LIMIT = 4").replace("import json", "import csv")
    )
    assert regions.names == {"LIMIT", "json", "csv"}
    assert not regions.module


def test_registration_decorator_and_bare_call_alter_the_module() -> None:
    assert _regions(BASE, BASE.replace('@register("x")', '@register("y")')).module
    assert _regions(BASE, BASE + "\nregister(helper)\n").module


def test_class_level_statement_alters_the_class_and_methods_alter_themselves() -> None:
    assert _regions(BASE, BASE.replace("size = 1", "size = 2")).classes == {"Box"}
    assert _regions(BASE, BASE.replace("self.size + 1", "self.size + 2")).functions == {"Box.grow"}


def test_new_definitions_alter_only_what_they_shadow_or_override() -> None:
    fresh = _regions(BASE, BASE + "\n\ndef brand_new():\n    return 2\n")
    assert fresh == change_regions.Regions()
    shadow = _regions(BASE, BASE + "\n\ndef json():\n    return 2\n")
    assert shadow.names == {"json"}
    override = _regions(BASE, BASE + "\n    def __eq__(self, other):\n        return True\n")
    assert override.classes == {"Box"}


def test_cached_functions_are_marked() -> None:
    before = "import functools\n\n@functools.cache\ndef load():\n    return 1\n"
    regions = _regions(before, before.replace("return 1", "return 2"))
    assert regions.functions == {"load"} and regions.cached == {"load"}


def _process(
    keys: list[list[str]], buckets: dict[str, list[int]], **extra: object
) -> dict[str, object]:
    return {
        "schema": test_impact_recorder.PROCESS_SCHEMA,
        "child": False,
        "keys": keys,
        "buckets": buckets,
        "import_time": extra.get("import_time", []),
        "fixture_users": extra.get("fixture_users", {}),
    }


def _decoded(
    receipt: dict[str, object],
) -> tuple[dict[tuple[str, str], set[str]], list[tuple[str, str]]]:
    tests = [str(item) for item in cast(list[object], receipt["tests"])]
    keys = [(str(item[0]), str(item[1])) for item in cast(list[list[object]], receipt["keys"])]
    runs = cast(dict[str, list[int]], receipt["runs"])
    return {
        keys[int(key)]: {tests[index] for index in owners} for key, owners in runs.items()
    }, keys


def test_merge_expands_fixture_buckets_and_keeps_session_code_global() -> None:
    first = _process(
        [["src/a.py", "f"], ["src/a.py", "g"], ["src/s.py", "boot"]],
        {"tests/test_one.py": [0], "<fixture>:db": [1], "<session>": [2]},
        import_time=[1],
        fixture_users={"db": ["tests/test_one.py", "tests/test_two.py"]},
    )
    child = _process([["src/a.py", "f"]], {"tests/test_two.py": [0]})
    receipt = test_impact_recorder.merge([first, child], "c" * 40)
    runs, keys = _decoded(receipt)
    assert runs[("src/a.py", "f")] == {"tests/test_one.py", "tests/test_two.py"}
    assert runs[("src/a.py", "g")] == {"tests/test_one.py", "tests/test_two.py"}
    assert [keys[index] for index in cast(list[int], receipt["everywhere"])] == [
        ("src/s.py", "boot")
    ]
    assert [keys[index] for index in cast(list[int], receipt["import_time"])] == [("src/a.py", "g")]


FIXTURE_REPO = {
    "pytest.ini": "[pytest]\npythonpath = lib\n",
    "lib/app.py": "LIMIT = 3\n\n\ndef add(x):\n    return x + LIMIT\n\n\nCONFIG = add(1)\n",
    "lib/other.py": "def mul(x):\n    return x * 2\n",
    "lib/runner.py": "import other\n\nother.mul(1)\n",
    "tests/conftest.py": (
        "import pytest\n\n\n@pytest.fixture(scope='session')\ndef shared():\n"
        "    from other import mul\n\n    return mul(2)\n"
    ),
    "tests/test_add.py": "from app import add\n\n\ndef test_add():\n    assert add(1) == 4\n",
    "tests/test_shared.py": "def test_shared(shared):\n    assert shared == 4\n",
    "tests/test_child.py": (
        "import subprocess\nimport sys\n\n\ndef test_child():\n"
        # A script's own folder is on its path, so runner.py imports lib/other.py.
        "    subprocess.run([sys.executable, 'lib/runner.py'], check=True)\n"
    ),
    "tests/test_plain.py": "def test_plain():\n    assert True\n",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, Path]:
    """A tiny repository, its base commit, and a receipt recorded from a real run."""
    if sys.version_info < (3, 12):
        pytest.skip("sys.monitoring needs Python 3.12")
    root = tmp_path_factory.mktemp("repo")
    for path, text in FIXTURE_REPO.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    records, site = root.parent / "records", root.parent / "site"
    assert test_impact_recorder.main(["sitecustomize", str(site)]) == 0
    env = {**os.environ, test_impact_recorder.ENV_DIR: str(records), "PYTHONPATH": str(site)}
    env.pop(test_impact_recorder.ENV_BUCKET, None)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
            "-p",
            "src.quality.test_impact_plugin",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    receipt = root.parent / "receipt.json.gz"
    assert (
        test_impact_recorder.main(["merge", "--commit", base, "--out", str(receipt), str(records)])
        == 0
    )
    return root, base, receipt


def test_recording_sees_fixtures_imports_and_subprocesses(recorded: tuple[Path, str, Path]) -> None:
    _, _, receipt_path = recorded
    with gzip.open(receipt_path, "rt", encoding="utf-8") as handle:
        receipt = cast(dict[str, object], json.load(handle))
    runs, keys = _decoded(receipt)
    assert runs[("tests/test_add.py", "test_add")] == {"tests/test_add.py"}
    # The session fixture runs once, yet every test that uses it depends on it.
    assert "tests/test_shared.py" in runs[("lib/other.py", "mul")]
    # The subprocess runs `mul` too, in its test's bucket.
    assert "tests/test_child.py" in runs[("lib/other.py", "mul")]
    assert ("lib/app.py", "add") in {
        keys[index] for index in cast(list[int], receipt["import_time"])
    }


def _change(root: Path, path: str, old: str, new: str) -> list[str]:
    target = root / path
    target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    _git(root, "commit", "-qam", "change")
    return [path]


@pytest.mark.parametrize(
    ("path", "old", "new", "expected"),
    [
        ("lib/other.py", "x * 2", "x * 3", {"tests/test_shared.py", "tests/test_child.py"}),
        ("lib/app.py", "LIMIT = 3", "LIMIT = 4", {"tests/test_add.py"}),
        ("lib/app.py", "CONFIG = add(1)", "CONFIG = add(1)\nprint(CONFIG)", {"tests/test_add.py"}),
        ("lib/app.py", "LIMIT = 3", "LIMIT = 3  # tuned", set[str]()),
        # Reflowing the same tokens over more lines alters nothing.
        ("lib/app.py", "CONFIG = add(1)", "CONFIG = add(\n    1\n)", set[str]()),
        ("lib/app.py", "def add(x):", "def add(\n    x\n):", set[str]()),
    ],
)
def test_selection_runs_exactly_the_tests_that_observe_a_change(
    recorded: tuple[Path, str, Path], path: str, old: str, new: str, expected: set[str]
) -> None:
    root, base, receipt = recorded
    try:
        changed = _change(root, path, old, new)
        selection = test_selection.select(root, changed, base=base, receipt_path=receipt)
    finally:
        _git(root, "reset", "-q", "--hard", base)
    assert not selection.full, selection.reason
    assert set(selection.files) == expected


def test_selection_requires_the_full_suite_without_an_exact_receipt(
    recorded: tuple[Path, str, Path],
) -> None:
    root, base, receipt = recorded
    assert test_selection.select(root, ["lib/other.py"], base=base, receipt_path=None).full
    other = test_selection.select(root, ["lib/other.py"], base="0" * 40, receipt_path=receipt)
    assert other.full and "another commit" in other.reason
    shared = test_selection.select(root, ["tests/conftest.py"], base=base, receipt_path=receipt)
    assert shared.full
