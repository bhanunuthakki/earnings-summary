"""Small, dependency-free Alembic compatibility guard for write paths.

Readers intentionally remain tolerant of an older local snapshot.  Writers do
not: applying code that expects a different migration head can silently omit
provenance columns or constraints.  The guard is deliberately stdlib-only so
it is safe to call from low-level SQLite stores.
"""

from __future__ import annotations

import ast
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import cast


class SchemaRevisionMismatch(sqlite3.OperationalError):
    """The target database is not at this checkout's single Alembic head."""


def expected_head(project_root: Path | None = None) -> str:
    """Return the one Alembic leaf in this checkout, or fail loudly on forks."""
    root = project_root or Path(__file__).resolve().parents[1]
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (root / "alembic" / "versions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign | ast.Assign):
                continue
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    value_node = node.value
                    if value_node is None:
                        continue
                    with suppress(ValueError):
                        values[target.id] = ast.literal_eval(value_node)
        revision = values.get("revision")
        if not isinstance(revision, str):
            continue
        revisions.add(revision)
        down = values.get("down_revision")
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, tuple):
            tuple_down = cast(tuple[object, ...], down)
            parents.update(parent for parent in tuple_down if isinstance(parent, str))
    heads = revisions - parents
    if len(heads) != 1:
        raise SchemaRevisionMismatch(
            f"checkout has {len(heads)} Alembic heads ({sorted(heads)}); merge revisions before writes"
        )
    return heads.pop()


def require_current_for_write(conn: sqlite3.Connection) -> None:
    """Refuse a versioned DB whose revision differs from the code checkout.

    Minimal in-memory fixtures without ``alembic_version`` are intentionally
    left to their local table contracts; production DBs are versioned and must
    be upgraded before any mutation through a guarded store.
    """
    has_version = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if has_version is None:
        return
    rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    actual = {str(row[0]) for row in rows}
    expected = expected_head()
    if actual != {expected}:
        raise SchemaRevisionMismatch(
            "database schema revision does not match this checkout "
            f"(db={sorted(actual) or ['<none>']}, code={expected}); run `alembic upgrade head`"
        )
