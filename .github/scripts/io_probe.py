"""Diagnostic only: time SQLite commits, imports and a migration build on this runner."""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(dir=os.environ.get("RUNNER_TEMP")))


def timed(label: str, action: Callable[[], object]) -> None:
    start = time.perf_counter()
    action()
    print(f"PROBE {sys.platform} {label}: {time.perf_counter() - start:.2f}s", flush=True)


def commits(name: str, pragmas: tuple[str, ...], count: int = 500) -> None:
    connection = sqlite3.connect(ROOT / f"{name}.db")
    for pragma in pragmas:
        connection.execute(pragma)
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.commit()
    for value in range(count):
        connection.execute("INSERT INTO t VALUES (?)", (value,))
        connection.commit()
    connection.close()


def build_template() -> None:
    from alembic.config import Config

    from alembic import command

    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{(ROOT / 'template.db').as_posix()}")
    command.upgrade(config, "head")


timed("500 commits default", lambda: commits("default", ()))
timed("500 commits synchronous=OFF", lambda: commits("off", ("PRAGMA synchronous=OFF",)))
timed(
    "500 commits WAL synchronous=NORMAL",
    lambda: commits("wal", ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL")),
)
timed("500 commits journal_mode=MEMORY", lambda: commits("memory", ("PRAGMA journal_mode=MEMORY",)))
timed(
    "1000 small file create+delete",
    lambda: [(ROOT / f"f{i}").write_bytes(b"x") or (ROOT / f"f{i}").unlink() for i in range(1000)],
)
timed(
    "import sqlalchemy+alembic+pydantic",
    lambda: [importlib.import_module(name) for name in ("sqlalchemy", "alembic", "pydantic")],
)
timed("alembic head template build", build_template)
