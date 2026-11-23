"""Pytest plugin that switches the test-impact recorder between buckets.

Load it with ``-p src.quality.test_impact_plugin``; it does nothing unless
``TEST_IMPACT_RECORD`` names an output folder. Under pytest-xdist only workers
record; the controller runs no tests.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

from src.quality import test_impact_recorder as recorder_module

_recorder: recorder_module.Recorder | None = None
_root = Path.cwd()


def _relative(path: Path) -> str:
    return Path(os.path.relpath(path, _root)).as_posix()


def pytest_configure(config: pytest.Config) -> None:
    global _recorder, _root
    out_dir = os.environ.get(recorder_module.ENV_DIR)
    # An xdist controller distributes tests but runs none; its workers record.
    controller = not hasattr(config, "workerinput") and bool(
        config.getoption("numprocesses", default=None)
    )
    if not out_dir or controller:
        return
    _root = config.rootpath
    _recorder = recorder_module.start_parent(_root, Path(out_dir))


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Generator[None, object, object]:
    if _recorder is None or not isinstance(collector, pytest.Module):
        return (yield)
    _recorder.push(_relative(collector.path))
    try:
        return (yield)
    finally:
        _recorder.pop()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    if _recorder is not None:
        test_file = _relative(item.path)
        _recorder.switch(test_file)
        for name in getattr(item, "fixturenames", ()):
            _recorder.fixture_users[str(name)].add(test_file)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[object], request: pytest.FixtureRequest
) -> Generator[None, object, object]:
    del request
    if _recorder is None or fixturedef.scope == "function":
        return (yield)
    _recorder.push(recorder_module.FIXTURE + fixturedef.argname)
    try:
        return (yield)
    finally:
        _recorder.pop()


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    global _recorder
    if _recorder is None:
        return
    _recorder.switch(recorder_module.SESSION)
    _recorder.stop()
    _recorder.dump()
    _recorder = None
